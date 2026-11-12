"""위험도 1~5 실측 (2026-11-09).

새 프롬프트(v5: v4 + [위험도])가 무엇을 바꾸는지 잰다. 묻는 것 넷.

  1. 퍼지는가    — 옛 카테고리 표는 큐의 65% 를 한 칸(모욕=보통)에 몰았다. 새 단계는?
  2. 흔들리는가  — 같은 댓글을 두 번 매기면 같은 단계가 나오는가.
  3. 판정이 바뀌었나 — [위험도] 를 넣은 것만으로 harmful/ambiguous/safe 가 달라졌는가.
                     옛 프롬프트(v4, git HEAD)와 비교하고, 같은 프롬프트끼리의 흔들림을 기준선으로 둔다.
  4. 사람과 맞나 — 김지황 라벨 200건(통과/애매/유해)과 단계가 같은 방향으로 가는가.

표본 1,000건 = 사람 라벨 200건(eval/라벨_김지황_200.csv) + 라벨링 시트(labeling/)에서
무작위 800건 (seed 42). 시트의 답글은 부모 댓글을 같이 보낸다. 영상 제목은 없다.

호출: 새 프롬프트 1,000 + 다시 200 + 옛 프롬프트 300 ≈ 1,500회.
결과: eval/위험도_실험_1000.csv (댓글별), eval/위험도_실험_요약.txt

    python -m scripts.level_experiment            전부
    python -m scripts.level_experiment --n 50     시험 삼아 작게
"""

import argparse
import asyncio
import csv
import glob
import importlib.util
import io
import random
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from app.services.llm import LlmJudge
from app.services.severity import FALLBACK, NAMES, level_of

사람 = Path("eval/라벨_김지황_200.csv")
결과 = Path("eval/위험도_실험_1000.csv")
요약 = Path("eval/위험도_실험_요약.txt")
SEED = 42
KRW = 1400   # scripts/check_prompt.py 와 같은 환율


def 표본(n: int) -> list[dict]:
    시트 = {}
    for f in sorted(glob.glob("labeling/*.csv")):
        for r in csv.DictReader(io.open(f, encoding="utf-8-sig")):
            시트.setdefault(r["id"], r)
    부모 = {r["댓글"].strip(): r["부모댓글"].strip() for r in 시트.values()}

    out = []
    for r in csv.DictReader(io.open(사람, encoding="utf-8-sig")):
        t = r["댓글"].strip()
        out.append({"src": "사람200", "text": t, "parent": 부모.get(t, ""), "human": r["기대"]})
    have = {x["text"] for x in out}
    rest = [r for r in 시트.values() if r["댓글"].strip() and r["댓글"].strip() not in have]
    random.Random(SEED).shuffle(rest)
    for r in rest[: max(n - len(out), 0)]:
        out.append({"src": "시트", "text": r["댓글"].strip(), "parent": r["부모댓글"].strip(), "human": ""})
    return out[:n]


def 옛_모듈():
    """git HEAD 의 llm.py (v4). 지금 파일과 나란히 불러 비교한다."""
    src = subprocess.run(
        ["git", "show", "HEAD:app/services/llm.py"], capture_output=True, check=True
    ).stdout
    d = Path(tempfile.mkdtemp())
    p = d / "llm_v4.py"
    p.write_bytes(src)
    spec = importlib.util.spec_from_file_location("llm_v4", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


async def 돌리기(judge, items):
    return await asyncio.gather(*(judge.judge(x["text"], parent_text=x["parent"] or None) for x in items))


def 퍼센트(a, b):
    return f"{a / b * 100:.1f}%" if b else "-"


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--again", type=int, default=200, help="새 프롬프트로 한 번 더 매길 수")
    ap.add_argument("--old", type=int, default=300, help="옛 프롬프트(v4)로 매길 수")
    a = ap.parse_args()

    items = 표본(a.n)
    rng = random.Random(SEED + 1)
    idx = list(range(len(items)))
    rng.shuffle(idx)
    old_idx = sorted(idx[: min(a.old, len(items))])
    again_idx = old_idx[: min(a.again, len(old_idx))]   # 다시 매긴 것 ⊂ 옛 프롬프트로 매긴 것 → 같은 댓글로 비교

    print(f"표본 {len(items)}건 (사람 라벨 {sum(x['src'] == '사람200' for x in items)}) · "
          f"다시 {len(again_idx)} · 옛 프롬프트 {len(old_idx)}")

    new = LlmJudge(concurrency=8, max_calls=len(items) + len(again_idx) + 50)
    v1 = await 돌리기(new, items)
    print(f"새 프롬프트 1회차 끝 — {new.stats.calls}회, 오류 {new.stats.errors}")
    v2 = await 돌리기(new, [items[i] for i in again_idx])
    print(f"새 프롬프트 2회차 끝 — 누적 {new.stats.calls}회")
    old_m = 옛_모듈()
    old = old_m.LlmJudge(concurrency=8, max_calls=len(old_idx) + 20)
    v0 = await 돌리기(old, [items[i] for i in old_idx])
    print(f"옛 프롬프트 끝 — {old.stats.calls}회, 오류 {old.stats.errors}")
    won = (new.stats.cost_usd + old.stats.cost_usd) * KRW

    again = dict(zip(again_idx, v2))
    oldv = dict(zip(old_idx, v0))

    # ── 댓글별 기록 ──
    with io.open(결과, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["출처", "사람", "부모댓글", "댓글", "label", "category", "severity", "단계(표시)",
                    "옛표_단계", "근거", "다시_label", "다시_severity", "v4_label", "v4_category"])
        for i, (x, v) in enumerate(zip(items, v1)):
            lv = level_of(v.severity, v.category, v.label) if v.label in ("harmful", "ambiguous") else ""
            table = level_of(None, v.category, v.label) if v.label in ("harmful", "ambiguous") else ""
            r2, r0 = again.get(i), oldv.get(i)
            w.writerow([x["src"], x["human"], x["parent"], x["text"], v.label, v.category, v.severity or "",
                        lv, table, v.reason,
                        r2.label if r2 else "", (r2.severity or "") if r2 else "",
                        r0.label if r0 else "", r0.category if r0 else ""])

    L = []
    def say(s=""):
        L.append(s)
        print(s)

    ok = [(x, v) for x, v in zip(items, v1) if v.label]
    flagged = [(x, v) for x, v in ok if v.label in ("harmful", "ambiguous")]
    say(f"위험도 1~5 실측 — 표본 {len(items)}건, 판별 성공 {len(ok)}, 검토 대상(유해·애매) {len(flagged)}")
    say(f"호출 {new.stats.calls + old.stats.calls}회 · 비용 약 {won:.0f}원 (환율 {KRW})")
    say(f"판별 오류: 새 {new.stats.errors} · 옛 {old.stats.errors}")
    say()

    # 1. 퍼짐
    say("[1] 단계 분포 — 검토 대상만. 화면 표시 기준(애매는 한 단계 내림)")
    newd = Counter(level_of(v.severity, v.category, v.label) for _, v in flagged)
    tabd = Counter(level_of(None, v.category, v.label) for _, v in flagged)
    say("  단계     AI가 매김        옛 카테고리 표")
    for k in range(5, 0, -1):
        say(f"  {k} {NAMES[k]}   {newd[k]:>4} ({퍼센트(newd[k], len(flagged)):>6})   {tabd[k]:>4} ({퍼센트(tabd[k], len(flagged)):>6})")
    top_new = max(newd.values()) if newd else 0
    top_tab = max(tabd.values()) if tabd else 0
    say(f"  가장 큰 한 칸: AI {퍼센트(top_new, len(flagged))} · 표 {퍼센트(top_tab, len(flagged))}")
    no_sev = sum(1 for _, v in flagged if v.severity is None)
    say(f"  AI 가 단계를 안 줬거나 버린 것(표로 대신): {no_sev}건")
    say()
    say("  카테고리별 AI 단계 (유해·애매, 표시 기준)")
    bycat = defaultdict(Counter)
    for _, v in flagged:
        bycat[v.category][level_of(v.severity, v.category, v.label)] += 1
    for cat, c in sorted(bycat.items(), key=lambda kv: -sum(kv[1].values())):
        say(f"    {cat:<5} {sum(c.values()):>4}건  표={FALLBACK.get(cat, 3)}  " +
            "  ".join(f"{k}:{c[k]}" for k in range(1, 6) if c[k]))
    say()

    # 2. 흔들림
    pairs = [(v1[i], again[i]) for i in again_idx if v1[i].label and again[i].label]
    same_label = sum(a_.label == b_.label for a_, b_ in pairs)
    both = [(a_, b_) for a_, b_ in pairs if a_.label != "safe" and b_.label != "safe"]
    same_lv = sum(level_of(a_.severity, a_.category, a_.label) == level_of(b_.severity, b_.category, b_.label) for a_, b_ in both)
    near_lv = sum(abs(level_of(a_.severity, a_.category, a_.label) - level_of(b_.severity, b_.category, b_.label)) <= 1 for a_, b_ in both)
    say(f"[2] 같은 댓글 두 번 (새 프롬프트) — {len(pairs)}건")
    say(f"  label 같음 {same_label}/{len(pairs)} ({퍼센트(same_label, len(pairs))})")
    say(f"  둘 다 검토 대상인 {len(both)}건 중 단계 같음 {same_lv} ({퍼센트(same_lv, len(both))}), ±1 안 {near_lv} ({퍼센트(near_lv, len(both))})")
    say()

    # 3. 판정 변화
    po = [(v1[i], oldv[i]) for i in old_idx if v1[i].label and oldv[i].label]
    same_o = sum(a_.label == b_.label for a_, b_ in po)
    route = lambda l: "통과" if l == "safe" else "검토"
    same_route_o = sum(route(a_.label) == route(b_.label) for a_, b_ in po)
    same_route_2 = sum(route(a_.label) == route(b_.label) for a_, b_ in pairs)
    say(f"[3] 옛 프롬프트(v4)와 비교 — {len(po)}건")
    say(f"  label 같음 {same_o}/{len(po)} ({퍼센트(same_o, len(po))})   ← 기준선: 새끼리 {퍼센트(same_label, len(pairs))}")
    say(f"  통과/검토 같음 {same_route_o}/{len(po)} ({퍼센트(same_route_o, len(po))})   ← 기준선: 새끼리 {퍼센트(same_route_2, len(pairs))}")
    new_q = sum(route(a_.label) == "검토" for a_, _ in po)
    old_q = sum(route(b_.label) == "검토" for _, b_ in po)
    say(f"  검토로 간 수: 새 {new_q} · 옛 {old_q}")
    say()

    # 4. 사람
    hs = [(x, v) for x, v in ok if x["human"]]
    say(f"[4] 김지황 라벨과 비교 — {len(hs)}건 (통과/애매/유해)")
    for h in ("유해", "애매", "통과"):
        g = [v for x, v in hs if x["human"] == h]
        f_ = [v for v in g if v.label != "safe"]
        c = Counter(level_of(v.severity, v.category, v.label) for v in f_)
        avg = sum(level_of(v.severity, v.category, v.label) for v in f_) / len(f_) if f_ else 0
        say(f"  사람 {h} {len(g):>3}건 → AI 검토 대상 {len(f_):>3}  평균 단계 {avg:.2f}  " +
            "  ".join(f"{k}:{c[k]}" for k in range(1, 6) if c[k]))
    hf = [(x, v) for x, v in hs if v.label != "safe"]
    say("  단계별로 사람이 '통과' 라 한 비율 (AI 가 괜히 올린 것)")
    for k in range(5, 0, -1):
        g = [x for x, v in hf if level_of(v.severity, v.category, v.label) == k]
        p = sum(x["human"] == "통과" for x in g)
        say(f"    {k} {NAMES[k]}  {len(g):>3}건 중 사람 통과 {p} ({퍼센트(p, len(g))})")
    say()

    # 증거 자동 저장이 얼마나 생기나
    auto = [(x, v) for x, v in ok if v.label == "harmful" and v.severity and v.severity >= 4]
    say(f"[5] 증거 자동 저장 대상 (유해 · AI 단계 4 이상): {len(auto)}건 / {len(ok)} ({퍼센트(len(auto), len(ok))})")
    for x, v in auto[:12]:
        say(f"    {v.severity} {v.category:<4} {x['text'][:50]!r}")

    io.open(요약, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"\n저장: {결과} · {요약}")


if __name__ == "__main__":
    asyncio.run(main())
