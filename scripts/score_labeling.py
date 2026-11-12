"""채워진 라벨링 시트를 읽어 점수를 낸다.

내는 숫자는 넷이다.

  ① 사람끼리 일치율   — 이 문제의 난이도. AI 정확도의 천장이다.
                        단, 전체 평균 하나만 보면 안 된다. 댓글의 79% 가
                        "ㅋㅋㅋ" 같은 명백한 정상이라, 그것만으로 90% 가
                        넘게 나온다. 정작 판단이 갈리는 21% 가 그 평균에
                        묻히므로, 쉬운 것과 어려운 것을 갈라서 본다.
  ② AI 정확도         — 사람 답지 기준으로 몇 % 맞혔나.
  ③ 놓친 비율         — AI 가 통과시킨 것 중 사람이 유해라고 본 비율.
                        유해댓글 서비스에서 가장 아픈 지표다.
  ④ 헛다리 비율       — AI 가 잡은 것 중 사람이 정상이라고 본 비율.
                        관리자가 헛되이 보는 양이다.

②③④ 는 표본 비율이 실제 분포와 달라서 그대로 합치면 안 된다. 큐에서 뽑은
비율과 통과분에서 뽑은 비율이 다르기 때문에, 각 구간의 실제 크기로 가중해
전체 수치를 낸다.

    python -m scripts.score_labeling
    python -m scripts.score_labeling --dir labeling --channel 4
"""

import argparse
import asyncio
import collections
import csv
import itertools
import sys
from pathlib import Path

from sqlalchemy import text as sq

from app.db.session import AsyncSessionLocal, engine

# 사람이 쓴 값 -> AI 의 label
TO_LABEL = {"유해": "harmful", "애매": "ambiguous", "정상": "safe"}

AI = """
SELECT c.id, c.status, ra.risk_level, ra.category
FROM comments c
LEFT JOIN risk_assessments ra
  ON ra.id = (SELECT max(id) FROM risk_assessments WHERE comment_id = c.id)
WHERE c.channel_id = :cid
"""

SIZES = """
SELECT CASE WHEN status = 'passed' THEN 'passed' ELSE 'queued' END AS 구간, count(*)
FROM comments WHERE channel_id = :cid GROUP BY 1
"""


def read_sheets(folder: Path):
    """사람별 라벨을 읽는다. {사람: {댓글id: (판정, 유형)}}"""
    out: dict[str, dict[int, tuple[str, str]]] = {}
    for path in sorted(folder.glob("*.csv")):
        who = path.stem.split("_", 1)[-1]
        labels: dict[int, tuple[str, str]] = {}
        with path.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                v = (row.get("판정") or "").strip()
                if v not in TO_LABEL:
                    continue           # 아직 안 채운 행은 건너뛴다
                labels[int(row["id"])] = (v, (row.get("유형") or "").strip())
        if labels:
            out[who] = labels
    return out


def _pair_rate(sheets, people, ids, pick):
    """짝별 일치율 평균. pick 이 비교할 값을 고른다."""
    rates = []
    for a, b in itertools.combinations(people, 2):
        both = (set(sheets[a]) & set(sheets[b])) & ids
        if not both:
            continue
        same = sum(1 for i in both if pick(sheets[a][i]) == pick(sheets[b][i]))
        rates.append((same / len(both), len(both)))
    if not rates:
        return None, 0
    n = sum(r[1] for r in rates)
    return sum(r[0] * r[1] for r in rates) / n, len(rates)


def _kappa(sheets, people, ids):
    """우연히 맞을 확률을 빼고 본 일치도 (Cohen's kappa).

    댓글의 79% 가 명백한 정상이라, 둘 다 아무렇게나 '정상' 을 찍어도
    일치율은 높게 나온다. 그래서 원시 일치율만 보면 안 된다.
    1.0=완전일치 / 0=찍은 것과 다를 바 없음.
    """
    ks = []
    for a, b in itertools.combinations(people, 2):
        both = (set(sheets[a]) & set(sheets[b])) & ids
        if len(both) < 10:
            continue
        va = [sheets[a][i][0] for i in both]
        vb = [sheets[b][i][0] for i in both]
        po = sum(x == y for x, y in zip(va, vb)) / len(both)
        ca, cb = collections.Counter(va), collections.Counter(vb)
        pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / len(both) ** 2
        if pe < 1:
            ks.append((po - pe) / (1 - pe))
    return sum(ks) / len(ks) if ks else None


def agreement(sheets, ai, sizes) -> None:
    """공통 구간 — 두 사람씩 짝지어 얼마나 같은 답을 냈나.

    전체 평균 하나만 보면 안 된다. 데이터의 79% 가 명백한 정상이라
    그것만으로 90% 가 넘게 나오고, 정작 판단이 갈리는 구간이 묻힌다.
    """
    people = list(sheets)
    shared = set.intersection(*(set(s) for s in sheets.values())) if len(people) > 1 else set()
    print(f"\n① 사람끼리 일치율  (둘 다 채운 댓글 {len(shared)}건)")
    if not shared:
        print("   겹치는 구간이 아직 없다. 공통 구간을 채워야 나온다.")
        return

    # 공통 구간은 어려운 것(큐)을 일부러 절반쯤 담았다. 그래서 여기 나오는
    # 수치는 실제 댓글에서의 일치율보다 낮다. 아래에서 실제 비율로 환산한다.
    print("   (공통 구간 기준 — 어려운 것을 일부러 많이 담아서 낮게 나온다)")
    for a, b in itertools.combinations(people, 2):
        both = set(sheets[a]) & set(sheets[b])
        if not both:
            continue
        same_v = sum(1 for i in both if sheets[a][i][0] == sheets[b][i][0])
        print(f"   {a} vs {b}  {same_v/len(both)*100:5.1f}%   ({len(both)}건)")

    # 쉬운 것과 어려운 것을 갈라서 본다. 이게 핵심이다.
    hard = {i for i in shared if ai.get(i, ("passed",))[0] != "passed"}
    easy = shared - hard

    print("\n   [쉬운 것 / 어려운 것을 갈라 보면]")
    r_easy, _ = _pair_rate(sheets, people, easy, lambda x: x[0])
    r_hard, _ = _pair_rate(sheets, people, hard, lambda x: x[0])
    for name, ids, rate in (
        ("AI 가 통과시킨 것", easy, r_easy),
        ("AI 가 큐로 보낸 것", hard, r_hard),
    ):
        if rate is not None:
            print(f"   {name:20} {rate*100:5.1f}%   ({len(ids)}건)")

    if hard:
        rate, _ = _pair_rate(sheets, people, hard, lambda x: (x[0], x[1]))
        if rate is not None:
            print(f"   {'└ 유형까지 같은 것':20} {rate*100:5.1f}%")

    # 실제 댓글 분포(통과 79% / 큐 21%)로 환산한 값. 발표에 쓸 숫자는 이쪽이다.
    if r_easy is not None and r_hard is not None and sizes:
        wp, wq = sizes.get("passed", 0), sizes.get("queued", 0)
        if wp + wq:
            real = (r_easy * wp + r_hard * wq) / (wp + wq)
            print(f"\n   실제 댓글 분포로 환산하면 {real*100:.1f}%")
            print(f"     (통과 {wp:,}건 {wp/(wp+wq)*100:.0f}% + 큐 {wq:,}건 "
                  f"{wq/(wp+wq)*100:.0f}% 로 가중)")
            print("     이 수치가 높은 건 대부분이 명백한 정상이라서다.")
            print("     실력이 갈리는 곳은 위의 '큐로 보낸 것' 줄이다.")

    k = _kappa(sheets, people, shared)
    if k is not None:
        print(f"\n   우연 보정 일치도(kappa) {k:.2f}")
        print("     0.8↑ 매우 높음 / 0.6~0.8 높음 / 0.4~0.6 보통 / 0.4↓ 낮음")
        print("     대부분이 명백한 정상이라 원시 일치율은 원래 높게 나온다.")
        print("     찍어서 맞은 몫을 뺀 값이라 이쪽이 실제 난이도에 가깝다.")

    if len(people) > 2:
        full = [i for i in shared if len({sheets[p][i][0] for p in people}) == 1]
        print(f"\n   전원 동일 {len(full)}/{len(shared)}건 ({len(full)/len(shared)*100:.1f}%)")
        split = [i for i in shared if i not in full]
        print(f"   갈린 것 {len(split)}건 -> 팀이 같이 보면 기준이 정해진다")
        if split:
            print("   갈린 예:")
            for i in sorted(split)[:3]:
                vs = " / ".join(f"{p}:{sheets[p][i][0]}" for p in people)
                print(f"     #{i}  {vs}")


def vote(sheets, cid: int) -> str | None:
    """여러 사람이 본 댓글은 다수결. 갈리면(2:2 등) 정답에서 뺀다."""
    votes = [s[cid][0] for s in sheets.values() if cid in s]
    if not votes:
        return None
    c = collections.Counter(votes).most_common()
    if len(c) > 1 and c[0][1] == c[1][1]:
        return None           # 동수면 사람도 못 정한 것이다
    return c[0][0]


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="labeling")
    ap.add_argument("--channel", type=int, default=4)
    args = ap.parse_args()

    folder = Path(args.dir)
    sheets = read_sheets(folder)
    if not sheets:
        raise SystemExit(f"[FAIL] {folder} 에 채워진 시트가 없다.")

    total = sum(len(v) for v in sheets.values())
    print(f"[{folder}] 채워진 라벨 {total}개 / 사람 {len(sheets)}명")
    for who, v in sheets.items():
        print(f"   {who}: {len(v)}건")

    async with AsyncSessionLocal() as db:
        ai = {
            r[0]: (r[1], r[2], r[3])
            for r in (await db.execute(sq(AI), {"cid": args.channel})).all()
        }
        sizes = dict((await db.execute(sq(SIZES), {"cid": args.channel})).all())
    await engine.dispose()

    agreement(sheets, ai, sizes)

    # 사람 정답(다수결) 과 AI 판정을 구간별로 맞춰본다.
    stat = {
        "queued": {"n": 0, "hit": 0, "wrong": 0},
        "passed": {"n": 0, "hit": 0, "missed": 0},
    }
    labeled = {i for s in sheets.values() for i in s}
    unjudged = 0

    for cid in labeled:
        truth = vote(sheets, cid)
        if truth is None or cid not in ai:
            continue
        status, level, _ = ai[cid]
        if not level:
            unjudged += 1
            continue

        bucket = "passed" if status == "passed" else "queued"
        s = stat[bucket]
        s["n"] += 1

        ai_flagged = level != "safe"
        human_flagged = truth != "정상"

        if ai_flagged == human_flagged:
            s["hit"] += 1
        elif bucket == "passed":
            s["missed"] += 1        # AI 는 괜찮다는데 사람은 유해라고 함
        else:
            s["wrong"] += 1         # AI 는 잡았는데 사람은 정상이라고 함

    q, p = stat["queued"], stat["passed"]
    print(f"\n② AI 와 사람이 맞은 정도")
    if q["n"]:
        print(f"   큐에 있던 것   {q['hit']}/{q['n']} 일치 ({q['hit']/q['n']*100:.1f}%)")
    if p["n"]:
        print(f"   통과된 것      {p['hit']}/{p['n']} 일치 ({p['hit']/p['n']*100:.1f}%)")

    if p["n"]:
        rate = p["missed"] / p["n"]
        est = rate * sizes.get("passed", 0)
        print(f"\n③ 놓친 비율  {p['missed']}/{p['n']} = {rate*100:.1f}%")
        print(f"   -> 통과된 {sizes.get('passed', 0):,}건 중 약 {est:,.0f}건이 악플일 수 있다")

    if q["n"]:
        rate = q["wrong"] / q["n"]
        est = rate * sizes.get("queued", 0)
        print(f"\n④ 헛다리 비율  {q['wrong']}/{q['n']} = {rate*100:.1f}%")
        print(f"   -> 큐의 {sizes.get('queued', 0):,}건 중 약 {est:,.0f}건은 안 봐도 될 것")

    # 구간 크기로 가중한 전체 정확도. 표본 비율이 실제와 달라 그냥 합치면 틀린다.
    if q["n"] and p["n"]:
        wq, wp = sizes.get("queued", 0), sizes.get("passed", 0)
        acc = (q["hit"] / q["n"] * wq + p["hit"] / p["n"] * wp) / (wq + wp)
        print(f"\n   전체 정확도(구간 크기로 가중) {acc*100:.1f}%")

    if unjudged:
        print(f"\n   ※ AI 판정이 비어 있어 뺀 것 {unjudged}건 "
              f"(재시도 버그로 실패했던 건들. rejudge 하면 포함된다)")


asyncio.run(main())
