"""사람이 채울 라벨링 시트를 만든다 (정답 세트 1단계).

왜 필요한가: AI 판정이 맞았는지 틀렸는지 잴 기준이 없다. 답지를 AI 가
만들면 자기 답을 답지로 삼는 셈이라 항상 100점이 나온다. 그래서 사람이
만든다.

설계에서 지킨 것 네 가지.

  1. **AI 판정을 넣지 않는다.** 'AI 는 혐오라고 함' 이 보이면 사람이 무의식적으로
     따라가고, 그렇게 만든 답지로 채점하면 점수가 부풀려진다.

  2. **passed 도 섞는다.** 검토 큐에는 AI 가 유해하다고 본 것만 온다. 그것만
     라벨링하면 'AI 가 그냥 통과시킨 악플'을 영원히 못 잰다. 채널 4 는
     4,568건 중 3,602건(78.9%)이 아무도 안 본 채로 통과했다.

  3. **답글은 부모 댓글을 같이 준다.** "그만해라 진짜" 한 줄로는 사람도 판단
     못 한다. AI 에게는 이미 부모를 주고 있어서, 사람에게 안 주면 불공평한
     비교가 된다. 영상 제목은 넣지 않는다 — AI 가 못 보는 정보라 역시 불공평하다.

  4. **앞에서부터 잘라도 쓸 수 있게.** 중간에 그만둬도 그때까지가 온전한
     표본이 되도록 섞어서 넣는다. 공통 구간을 맨 앞에 두는 것도 같은 이유다.

    python -m scripts.make_labeling --channel 4
    python -m scripts.make_labeling --channel 4 --per-person 500 --common 150
    python -m scripts.make_labeling --channel 4 --people 박승민 김지황 박정호 조민준
"""

import argparse
import asyncio
import csv
import random
import sys
from pathlib import Path

from sqlalchemy import text as sq

from app.db.session import AsyncSessionLocal, engine

OUT = Path("labeling")

# 시트에 쓰는 값. 프롬프트의 카테고리와 같아야 나중에 비교가 된다.
VERDICTS = ["유해", "애매", "정상"]
CATEGORIES = [
    "욕설", "모욕", "혐오", "성희롱", "위협",
    "괴롭힘", "신상털기", "자해", "스팸", "기타",
]

SQL = """
SELECT c.id, c.content, c.status, c.is_reply, p.content AS parent
FROM comments c
LEFT JOIN comments p ON p.youtube_comment_id = c.parent_comment_id
WHERE c.channel_id = :cid
ORDER BY c.id
"""

GUIDE = f"""라벨링 기준

■ 무엇을 하나
  댓글을 읽고 두 칸만 채웁니다.  판정 / 유형

■ 절대 규칙
  - 한 건에 오래 매달리지 마세요. 20~30초 안에 정하고 넘어갑니다.
    고민되면 '애매' 입니다. 그게 정답인 항목입니다.
  - 다른 사람과 상의하지 마세요. 얼마나 갈리는지 재는 것도 목적입니다.
  - 순서를 바꾸거나 행을 지우지 마세요. id 로 다시 모읍니다.

■ 판정 — 셋 중 하나
  유해 : 명백하다. 관리자가 조치해도 된다.
  애매 : 유해해 보이는 요소는 있는데, 이것만 보고 확정은 못 하겠다.
  정상 : 문제 없다.

  ※ '무슨 말인지 모르겠다' 는 애매가 아니라 정상입니다.
     유해할 만한 요소가 실제로 있을 때만 애매입니다.

■ 유형 — 판정이 유해/애매일 때만
  욕설    : 특정 대상을 향해 비속어로 직접 공격. 대상 없는 감탄·탄식은 아님.
  모욕    : 비속어가 없어도 특정 개인의 인격·능력·외모를 깎아내림.
            단순한 감상이나 행동 지적은 아님.
  혐오    : 지역·성별·국적·인종·나이·장애처럼 본인이 고를 수 없는 속성을
            근거로 집단을 비하. 정당·지지 성향은 혐오가 아니라 모욕.
  성희롱  : 성적 대상화, 성적 모욕, 성적 행위 요구, 성적 수치심 유발.
  위협    : 이 글을 읽을 상대에게 해를 가하겠다고 밝힌 것.
            영상 속 인물을 두고 '한 대 치고 싶다' 는 관용 표현이며 위협 아님.
  괴롭힘  : 같은 대상을 반복해 따라다니며 시달리게 함. 한 번이면 욕설/모욕.
  신상털기: 실명·거주지·직장·학교·전화번호·가족관계·SNS 계정 노출이나 추측.
            공격적 표현이 없어도 해당.
  자해    : 자해·자살 관련. 남에게 죽으라고 하거나 부추기면 유해,
            본인의 자해 암시는 애매.
  스팸    : 홍보·유인 목적. 링크가 없어도 '프로필 보세요' 식 유도 포함.
  기타    : 유해 요소는 분명한데 위 어디에도 안 맞을 때만.

■ 헷갈리기 쉬운 것
  - 욕설이 들어 있어도 겨냥한 대상이 없으면 정상입니다. ("ㅅㅂ 버스 놓쳤다")
  - 자기 자신을 욕하는 건 정상입니다. ("나같은 놈이 뭘 하겠냐")
  - 남의 말을 인용한 건 정상입니다. ("쟤가 나보고 병신이래")
  - 공인의 업무 비판은 정상입니다. 표현이 거칠어도요.
  - 부정적 감상('재미없다', '별로다')은 모욕이 아닙니다.

■ 부모댓글 칸
  답글인 경우 원 댓글이 들어 있습니다. 같이 보고 판단하세요.
  비어 있으면 그냥 원댓글입니다.

■ 판정에 쓸 수 있는 값
  {" / ".join(VERDICTS)}

■ 유형에 쓸 수 있는 값
  {" / ".join(CATEGORIES)}
"""


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=4)
    ap.add_argument("--people", nargs="+",
                    default=["1번", "2번", "3번", "4번"])
    ap.add_argument("--per-person", type=int, default=1000, help="1인당 총 건수")
    ap.add_argument("--common", type=int, default=200,
                    help="전원이 똑같이 보는 구간. 사람끼리 일치율을 재는 데 쓴다")
    ap.add_argument("--seed", type=int, default=20260914)
    args = ap.parse_args()

    rng = random.Random(args.seed)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sq(SQL), {"cid": args.channel})).all()
    await engine.dispose()

    if not rows:
        raise SystemExit(f"[FAIL] 채널 {args.channel} 에 댓글이 없다.")

    # AI 가 큐로 보낸 것 / 통과시킨 것을 나눠 둔다. 둘 다 섞어야
    # '잘못 잡은 것' 과 '놓친 것' 을 같이 잴 수 있다.
    queued = [r for r in rows if r[2] in ("queued", "hidden")]
    passed = [r for r in rows if r[2] == "passed"]
    rng.shuffle(queued)
    rng.shuffle(passed)

    # 공통 구간은 두 쪽을 반반 담는다. 일치율을 유형별로도 보려면
    # 유해 쪽이 충분히 들어 있어야 한다.
    half = args.common // 2
    common = queued[:half] + passed[: args.common - half]
    rng.shuffle(common)
    rest_q, rest_p = queued[half:], passed[args.common - half :]

    # 개인 구간은 실제 분포 그대로. 앞에서 잘라도 표본이 유지되도록 섞는다.
    personal_total = (args.per_person - args.common) * len(args.people)
    ratio = len(rest_q) / (len(rest_q) + len(rest_p)) if (rest_q or rest_p) else 0
    take_q = min(len(rest_q), round(personal_total * ratio))
    take_p = min(len(rest_p), personal_total - take_q)
    personal = rest_q[:take_q] + rest_p[:take_p]
    rng.shuffle(personal)

    OUT.mkdir(exist_ok=True)
    (OUT / "0_라벨링_기준.txt").write_text(GUIDE, encoding="utf-8")

    per = (len(personal) + len(args.people) - 1) // len(args.people)
    print(f"채널 {args.channel} · 전체 {len(rows)}건")
    print(f"  큐에 있던 것 {len(queued)} · 통과된 것 {len(passed)}")
    print(f"  공통 {len(common)}건 (전원 동일) + 개인 {len(personal)}건")
    print(f"  서로 다른 댓글 {len(common) + len(personal)}건\n")

    for i, name in enumerate(args.people):
        mine = personal[i * per : (i + 1) * per]
        path = OUT / f"{i + 1}_{name}.csv"
        # utf-8-sig: 엑셀이 BOM 없이는 한글을 깨뜨린다. 구글 시트는 둘 다 된다.
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "구간", "부모댓글", "댓글", "판정", "유형", "메모"])
            for section, items in (("공통", common), ("개인", mine)):
                for r in items:
                    w.writerow([r[0], section, r[4] or "", r[1], "", "", ""])
        print(f"  {path}  {len(common) + len(mine)}건  ({name})")

    print(f"\n  {OUT / '0_라벨링_기준.txt'}  <- 먼저 읽어야 할 것")


asyncio.run(main())
