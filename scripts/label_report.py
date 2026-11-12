"""사람 라벨(/label 에서 누른 답)을 AI 판정과 맞대어 본다. 돈 안 든다 — 저장된 판정만 읽는다.

    python -m scripts.label_report --labeler 김지황
    python -m scripts.label_report --labeler 김지황 --out eval/라벨_김지황_200.csv   실험용 답지로도 저장

찍는 것
  1. 사람 답 분포 (숨김 / 모름 / 유지) × 출처(큐 / 통과)
  2. AI 가 실제로 낸 판정(DB 에 저장된 것, 프롬프트 판 섞여 있음) 과의 대조표
     - 놓침  : 사람은 숨김·모름, AI 는 통과
     - 헛수고: 사람은 유지, AI 는 큐
  3. 그 대조에서 갈린 댓글 목록
답지 CSV 는 scripts.experiment 가 읽는 형식(댓글, 기대, 왜). 숨김→유해, 모름→애매, 유지→통과.
"""
import argparse
import asyncio
import csv
import io
import statistics
import sys
from collections import Counter

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine

사람말 = {"hide": "숨김", "unsure": "모름", "keep": "유지"}
기대 = {"hide": "유해", "unsure": "애매", "keep": "통과"}
행선지 = lambda v: "통과" if v in ("통과", "safe", "passed") else "검토"

Q = """
    SELECT t.comment_id, t.source, t.label, t.seconds, c.channel_id, c.content, c.status,
           ra.risk_level, ra.category, ra.reasoning, ra.prompt_version, ra.stage
    FROM label_tasks t
    JOIN comments c ON c.id = t.comment_id
    LEFT JOIN LATERAL (
        SELECT risk_level, category, reasoning, prompt_version, stage
        FROM risk_assessments r WHERE r.comment_id = c.id
        ORDER BY r.id DESC LIMIT 1
    ) ra ON true
    WHERE t.labeler = :labeler AND t.label IS NOT NULL
    ORDER BY t.position
"""


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--labeler", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(Q), {"labeler": a.labeler})).all()
    await engine.dispose()
    if not rows:
        raise SystemExit("[FAIL] 답한 게 없다.")

    print(f"  {a.labeler} · {len(rows)}건")
    초 = [r.seconds for r in rows if r.seconds and 0 < r.seconds < 300]
    if 초:
        print(f"  건당 중앙값 {statistics.median(초):.1f}초 · 합계 약 {sum(초) / 60:.0f}분\n")

    # 1. 분포
    print("  사람 답 × 출처")
    tbl = Counter((r.source, 사람말[r.label]) for r in rows)
    for src in ("큐", "통과"):
        n = sum(v for (s, _), v in tbl.items() if s == src)
        print(f"    {src:2s} {n:3d}건 : " + " · ".join(f"{k} {tbl.get((src, k), 0)}" for k in ("숨김", "모름", "유지")))

    # 2. 대조 (저장된 AI 판정 기준)
    print("\n  AI 저장 판정 vs 사람 (프롬프트 판: " + ", ".join(f"{k} {v}건" for k, v in Counter(r.prompt_version or "규칙/없음" for r in rows).most_common()) + ")")
    맞 = 놓 = 헛 = 세 = 0
    놓친것, 헛것 = [], []
    for r in rows:
        사람 = 기대[r.label]
        ai = r.risk_level or ("harmful" if r.status == "hidden" else "safe")
        if 행선지(사람) == 행선지(ai):
            if 사람 == {"harmful": "유해", "ambiguous": "애매", "safe": "통과"}.get(ai, ai):
                맞 += 1
            else:
                세 += 1
        elif 행선지(사람) == "검토":
            놓 += 1; 놓친것.append((r, ai))
        else:
            헛 += 1; 헛것.append((r, ai))
    검토수 = sum(1 for r in rows if r.label != "keep")
    통과수 = len(rows) - 검토수
    print(f"    맞춤 {맞} · 세기차 {세} · 놓침 {놓}/{검토수} (사람이 봐야 한다고 한 것 중 AI 가 통과) · 헛수고 {헛}/{통과수} (사람이 유지한 것 중 AI 가 큐)")
    if 검토수:
        print(f"    → 사람 기준 '봐야 할 댓글' 을 AI 가 잡은 비율 {100 * (검토수 - 놓) / 검토수:.0f}%")
    큐로간 = sum(1 for r in rows if 행선지(r.risk_level or "safe") == "검토")
    if 큐로간:
        print(f"    → AI 가 큐로 보낸 {큐로간}건 중 사람도 봐야 한다고 한 비율 {100 * (큐로간 - 헛) / 큐로간:.0f}%")

    def 찍기(제목, 목록):
        if not 목록:
            return
        print(f"\n  {제목} ({len(목록)}건)")
        for r, ai in 목록[:40]:
            print(f"    ch{r.channel_id} [{사람말[r.label]} / AI {ai} {r.category or ''}] {(r.content or '')[:44]:44s} | {(r.reasoning or '')[:34]}")
    찍기("놓침 — 사람은 봐야 한다, AI 는 통과", 놓친것)
    찍기("헛수고 — 사람은 유지, AI 는 큐", 헛것)

    if a.out:
        with io.open(a.out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["댓글", "기대", "왜"])
            for r in rows:
                w.writerow([r.content, 기대[r.label], f"{a.labeler} 라벨 · ch{r.channel_id} · 출처 {r.source}"])
        print(f"\n  답지 저장 → {a.out}  (실험: python -m scripts.experiment --set {a.out})")


if __name__ == "__main__":
    asyncio.run(main())
