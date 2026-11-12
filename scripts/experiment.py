"""LLM 판별을 더 낫게 만들 후보 세 가지를 같은 답지로 채점해 비교한다.

    python -m scripts.experiment                       회귀 세트 39건, 후보 전부
    python -m scripts.experiment --set eval/라벨링정답.csv   사람 라벨링 답지로
    python -m scripts.experiment --only base,vote3     일부만
    python -m scripts.experiment --channel 4           cases 후보가 이 채널의 관리자 결정을 참고

후보
  base   지금 그대로. low 로 한 번.
  vote3  base 가 ambiguous 로 낸 것만 두 번 더 물어 다수결. (흔들림 제거)
  deep   base 가 ambiguous 로 낸 것만 reasoning_effort=medium 으로 한 번 더. (경계 판단)
  cases  관리자가 이미 결정한 비슷한 댓글 3건을 참고로 붙여서 low 로 한 번. (채널 기준)
         --channel 의 댓글 중 조치 이력이 있는 것에서 임베딩 거리로 고른다.
  combo  cases 로 낸 뒤, ambiguous 만 medium 으로 세 번 물어 다수결.

답지 CSV 는 check_prompt 와 같은 형식: 댓글, 기대(통과/애매/유해), 왜.
채점도 같다 — 통과 ↔ 검토가 갈리면 틀림, 애매 ↔ 유해는 세기만 다름.
DB 는 읽기만 한다. 아무것도 저장하지 않는다.
"""
import argparse
import asyncio
import csv
import io
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from app.db.models import Action, Comment
from app.db.session import AsyncSessionLocal, engine
from app.services.embed import Embedder
from app.services.llm import LlmJudge, LlmVerdict

라벨기대 = {"safe": "통과", "ambiguous": "애매", "harmful": "유해"}
행선지 = lambda 값: "통과" if 값 in ("통과", "safe") else "검토"
조치말 = {"hide": "숨김", "keep": "유지", "ban_author": "채널 차단"}


@dataclass
class 결과:
    이름: str
    판정: list[str]        # 통과/애매/유해/판별실패
    호출: int
    비용원: float


def 읽기(path: Path, 필터: str = "") -> list[tuple[str, str]]:
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        return [(r["댓글"], r["기대"]) for r in csv.DictReader(f)
                if not 필터 or 필터 in (r.get("왜") or "")]


def 라벨(v: LlmVerdict) -> str:
    return 라벨기대.get(v.label, "판별실패")


def 다수결(labels: list[str]) -> str:
    c = Counter(labels).most_common()
    if len(c) > 1 and c[0][1] == c[1][1]:
        return "애매"  # 셋이 다 갈리면 사람에게
    return c[0][0]


async def 사례풀(channel_id: int) -> list[tuple[str, str, list[float]]]:
    """관리자가 실제로 조치한 댓글과 그 결정. (본문, 결정, 임베딩)"""
    last = (select(Action.comment_id.label("cid"), func.max(Action.id).label("aid"))
            .group_by(Action.comment_id).subquery())
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Comment.content, Action.action_type, Comment.embedding)
            .join(last, last.c.cid == Comment.id)
            .join(Action, Action.id == last.c.aid)
            .where(Comment.channel_id == channel_id, Comment.embedding.isnot(None),
                   Comment.content.isnot(None))
        )).all()
    return [(c, 조치말.get(a, a), list(e)) for c, a, e in rows]


def 코사인(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def 참고글(vec: list[float], 풀, k=3) -> str:
    top = sorted(풀, key=lambda r: -코사인(vec, r[2]))[:k]
    if not top:
        return ""
    줄 = "\n".join(f'- "{c[:80]}" → 관리자: {a}' for c, a, _ in top)
    return ("[이 채널 관리자가 결정한 비슷한 댓글] 참고 정보다. 위 판단 순서를 바꾸지 않는다.\n" + 줄)


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="eval/회귀세트.csv")
    ap.add_argument("--only", default="base,vote3,deep,cases,combo")
    ap.add_argument("--channel", type=int, default=4, help="cases 후보가 참고할 관리자 결정이 있는 채널")
    ap.add_argument("--dump", default=None, help="댓글별 후보 판정을 이 CSV 에 남긴다 (틀린 것 분석용)")
    ap.add_argument("--context", default="", help="채널 기준 글. 파일 경로면 그 내용, 아니면 그대로. 운영처럼 시스템 프롬프트 맨 뒤에 붙는다")
    ap.add_argument("--filter", default="", help="답지의 '왜' 칸에 이 글자가 있는 행만 (예: ch4)")
    a = ap.parse_args()
    후보 = [x.strip() for x in a.only.split(",")]
    ctx = a.context
    if ctx and Path(ctx).exists():
        ctx = Path(ctx).read_text(encoding="utf-8")

    행 = 읽기(Path(a.set), a.filter)
    if ctx:
        print(f"  채널 기준 글 {len(ctx)}자 붙임")
    글 = [t for t, _ in 행]
    기대 = [e for _, e in 행]
    print(f"  답지 {a.set} · {len(행)}건 · 후보 {', '.join(후보)}\n")

    결과들: list[결과] = []
    # 실행당 상한(운영용 사고 방지)은 여기서 넘긴다. 답지 300건 × 후보 5개면 600 을 넘는다.
    j = LlmJudge(concurrency=8, max_calls=20_000, channel_context=ctx)

    async def 한바퀴(extra=None, effort="low"):
        ex = extra or [""] * len(글)
        return await asyncio.gather(*(j.judge(t, reasoning_effort=effort, extra=e) for t, e in zip(글, ex)))

    def 비용():
        return j.stats.cost_usd * 1400

    # base
    전 = j.stats.calls; 전비 = 비용()
    base = await 한바퀴()
    base라벨 = [라벨(v) for v in base]
    if "base" in 후보:
        결과들.append(결과("base", base라벨, j.stats.calls - 전, 비용() - 전비))

    애매idx = [i for i, l in enumerate(base라벨) if l == "애매"]

    async def 애매만(라벨들, effort, 횟수, extra=None):
        """ambiguous 였던 것만 다시 묻는다. 횟수 ≥ 2 면 처음 답까지 넣어 다수결."""
        out = list(라벨들)
        for i in 애매idx:
            e = (extra[i] if extra else "")
            더 = await asyncio.gather(*(j.judge(글[i], reasoning_effort=effort, extra=e) for _ in range(횟수)))
            표 = [라벨들[i]] + [라벨(v) for v in 더] if 횟수 >= 2 else [라벨(v) for v in 더]
            out[i] = 다수결(표)
        return out

    if "vote3" in 후보:
        전 = j.stats.calls; 전비 = 비용()
        결과들.append(결과("vote3", await 애매만(base라벨, "low", 2), j.stats.calls - 전, 비용() - 전비))

    if "deep" in 후보:
        전 = j.stats.calls; 전비 = 비용()
        결과들.append(결과("deep", await 애매만(base라벨, "medium", 1), j.stats.calls - 전, 비용() - 전비))

    참고 = None
    if "cases" in 후보 or "combo" in 후보:
        풀 = await 사례풀(a.channel)
        if not 풀:
            print(f"  [경고] 채널 {a.channel} 에 임베딩 있는 관리자 결정이 없다. cases/combo 건너뜀")
        else:
            em = Embedder()
            vecs = await em.embed(글)
            참고 = [참고글(v, 풀) for v in vecs]
            print(f"  cases: 채널 {a.channel} 의 관리자 결정 {len(풀)}건에서 댓글마다 3건 참고\n")

    if 참고 and "cases" in 후보:
        전 = j.stats.calls; 전비 = 비용()
        cases = await 한바퀴(참고)
        cases라벨 = [라벨(v) for v in cases]
        결과들.append(결과("cases", cases라벨, j.stats.calls - 전, 비용() - 전비))
        if "combo" in 후보:
            전 = j.stats.calls; 전비 = 비용()
            애매idx_c = [i for i, l in enumerate(cases라벨) if l == "애매"]
            out = list(cases라벨)
            for i in 애매idx_c:
                더 = await asyncio.gather(*(j.judge(글[i], reasoning_effort="medium", extra=참고[i]) for _ in range(3)))
                out[i] = 다수결([라벨(v) for v in 더])
            결과들.append(결과("combo", out, j.stats.calls - 전, 비용() - 전비))

    # ── 채점표
    print(f"  {'후보':7s} {'맞춤':>6s} {'놓침':>4s} {'헛수고':>5s} {'세기차':>5s} {'실패':>4s} {'호출':>4s} {'비용':>6s}")
    for r in 결과들:
        맞 = 놓 = 헛 = 세 = 실 = 0
        for e, p in zip(기대, r.판정):
            if p == "판별실패":
                실 += 1
            elif p == e:
                맞 += 1
            elif 행선지(e) == 행선지(p):
                세 += 1
            elif 행선지(e) == "검토":
                놓 += 1
            else:
                헛 += 1
        print(f"  {r.이름:7s} {맞:3d}/{len(기대):<2d} {놓:4d} {헛:5d} {세:5d} {실:4d} {r.호출:4d} {r.비용원:5.0f}원")
    print("\n  맞춤 = 기대와 같음 · 놓침 = 검토여야 하는데 통과 · 헛수고 = 통과여야 하는데 검토 · 세기차 = 애매↔유해")
    print("  ±2 는 흔들림 범위다. 그 안의 차이는 차이가 아니다.")

    # ── base 와 달라진 댓글
    if 결과들 and 결과들[0].이름 == "base":
        for r in 결과들[1:]:
            바뀜 = [(글[i], 기대[i], base라벨[i], r.판정[i]) for i in range(len(글)) if r.판정[i] != base라벨[i]]
            if 바뀜:
                print(f"\n  {r.이름} 가 base 와 다르게 낸 것 ({len(바뀜)}건)")
                for t, e, b, n in 바뀜:
                    표시 = "○" if n == e else ("×" if 행선지(n) != 행선지(e) else "△")
                    print(f"    {표시} [{e}] {b} → {n}  {t[:40]}")

    if a.dump:
        왜 = {}
        with io.open(a.set, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                왜[r["댓글"]] = r.get("왜", "")
        with io.open(a.dump, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["댓글", "기대", "왜"] + [r.이름 for r in 결과들] + ["base_분류", "base_근거"])
            for i, t in enumerate(글):
                w.writerow([t, 기대[i], 왜.get(t, "")] + [r.판정[i] for r in 결과들]
                           + [base[i].category, (base[i].reason or base[i].error or "")[:120]])
        print(f"\n  댓글별 결과 → {a.dump}")

    print(f"\n  전체 비용 약 {비용():.0f}원 · 호출 {j.stats.calls} · 오류 {j.stats.errors}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
