"""판별 파이프라인 — 조각들을 순서대로 잇는다.

    댓글
     ↓
    관리자 등록어 확인
     ├ 차단 → 숨김 (LLM 안 부름)
     └ 검토 / 없음
         ↓
    앞단 (prefilter.py — 판단할 내용 없음 · ML 분류기)   검토어에 걸린 건 건너뜀
     ├ 확실히 정상 → 통과 (PREFILTER_MODE=on 일 때만. shadow 면 점수만 남기고 LLM 으로)
     └ 나머지 → LLM
                     ├ 유해 → 채널이 자동 숨김을 켠 분류면 숨김, 아니면 검토 큐
                     │        (기본값은 전부 꺼짐 — 그래서 대개 검토 큐로 간다)
                     ├ 애매 → 검토 큐 [판단 필요]
                     └ 정상 → 검토어였나?
                              ├ 예  → 검토 큐 [참고]
                              └ 아뇨 → 통과

핵심은 '등록어에 안 걸린 댓글도 LLM으로 보낸다'는 것이다. 등록어만 검사하면
관리자가 미리 상상한 단어만 잡히고, 새 은어나 우회 표기(Tlqkfsus 같은)는
그대로 빠져나간다.

등록어 차단은 LLM을 건너뛰므로 되돌릴 기회가 없다. 그래서 숨김 목록을
남겨 관리자가 오등록을 발견할 수 있게 한다.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.services.llm import LlmJudge, VideoContext
from app.services.moderation import judge_rules
from app.services.prefilter import Prefilter

# 최종 행선지
HIDDEN = "hidden"        # 숨김 처리
QUEUE_JUDGE = "queue_judge"  # 검토 큐 · 판단 필요
QUEUE_INFO = "queue_info"    # 검토 큐 · 참고 (AI는 정상이라 했지만 검토어가 걸림)
PASSED = "passed"        # 통과

# 자동 숨김의 기본값. 비어 있다 — 채널이 켜기 전에는 어떤 분류도 사람을
# 안 거치고 가리지 않는다. 채널이 켠 것은 Channel.auto_hide_categories 에
# 들어가고, route() 에 그 집합을 넘기면 이 기본값 대신 쓰인다.
#
# 기본값을 비워둔 이유는 아래와 같다. 관리자가 직접 켜는 것은 별개다 —
# 그건 채널이 위험을 알고 선택한 것이라 존중한다.
#
# 마지막까지 '욕설' 하나는 남겨뒀었다. 대상을 겨냥한 명백한 욕이라면
# 문맥을 볼 것도 없다고 봤기 때문이다. 실데이터에서 무너졌다.
#
#   "@jddkdjrn 보지마 꼬우면"
#     -> 욕설 "특정 사용자를 겨냥한 성적 비속어"   ('보지마'를 성기로 읽었다)
#
#   "본 프로그램은 응급 상황 및 안전 사고에 대비하여 촬영되었으며
#    음주 흡연 욕설 등 부적절한 내용이..."
#     -> 욕설 "욕설과 모욕, 위협성 발언이 반복됩니다"  (방송 고지문 인용이다)
#
#   "지혼자 처먹으려고 시키고 ... 기억이 없는 친구내"  (좋아요 455)
#     -> 욕설 "특정 친구들을 겨냥해 비속어와 비난"   (행동 지적이지 욕이 아니다)
#
# 셋 다 관리자를 안 거치고 가려졌고, 숨김 목록을 열어보지 않았으면 아무도
# 몰랐을 것이다. 판별기가 아무리 좋아져도 '되돌릴 수 없는 조치'를 기계에
# 맡기는 설계 자체가 문제다. 숨김은 사람이 누른 것만 남긴다.
#
# 관리자가 등록어를 block 으로 넣는 것은 별개다 — 그건 채널이 스스로 정한
# 것이라 존중한다 (moderation.judge_rules 가 처리한다).
AUTO_HIDE_CATEGORIES: set[str] = set()


@dataclass
class PipelineResult:
    text: str
    destination: str
    decided_by: str              # "rule" | "prefilter" | "llm"
    reason: str = ""

    # 앞단 ML 이 본 유해 확률. shadow 기간에는 LLM 판정과 나란히 남아서
    # '켰다면 무엇을 통과시켰을지' 를 나중에 잴 수 있다.
    prefilter_score: float | None = None

    # 등록어 관련
    rule_value: str | None = None
    rule_action: str | None = None   # block / review
    matched_text: str | None = None

    # LLM 관련
    llm_label: str | None = None     # harmful / ambiguous / safe
    llm_category: str | None = None
    llm_severity: int | None = None  # 1~5. safe 거나 LLM 이 안 매겼으면 None (services/severity.py)

    @property
    def is_hidden(self) -> bool:
        return self.destination == HIDDEN

    @property
    def in_queue(self) -> bool:
        return self.destination in (QUEUE_JUDGE, QUEUE_INFO)


def route(
    label: str,
    category: str | None,
    flagged: bool,
    auto_hide: set[str] | None = None,
) -> str:
    """LLM 판정을 행선지로 옮긴다.

    정책(무엇을 자동으로 숨길지)이 이 함수 하나에만 있다. 그래서 정책을
    바꿨을 때 LLM 을 다시 부르지 않고 저장된 판정만으로 재배치할 수 있다
    (scripts/reroute.py). 판정과 정책을 분리해두는 값이 여기서 나온다.

    auto_hide 는 그 채널이 켜둔 분류다. 안 넘기면 기본값(비어 있음)을 쓴다.
    """
    hide = AUTO_HIDE_CATEGORIES if auto_hide is None else auto_hide
    if label == "harmful":
        return HIDDEN if category in hide else QUEUE_JUDGE
    # 판별에 실패한 건(빈 label)은 검사받지 않은 댓글이다. 통과시키면
    # 아무도 못 본 채로 공개된다 — 사람이 봐야 한다.
    if label in ("ambiguous", ""):
        return QUEUE_JUDGE
    # safe — 검토어가 걸렸으면 그래도 보여준다 (안전망)
    return QUEUE_INFO if flagged else PASSED


async def process(
    rules: Sequence,
    judge: LlmJudge,
    text: str,
    parent_text: str | None = None,
    auto_hide: set[str] | None = None,
    video: VideoContext | None = None,
    prefilter: Prefilter | None = None,
) -> PipelineResult:
    """댓글 한 건을 파이프라인에 통과시킨다. prefilter 가 None 이면 앞단 없이 간다."""
    v = judge_rules(rules, text)

    # ① 차단어 — 여기서 끝. LLM 비용도 안 쓴다.
    if v.verdict == "block":
        return PipelineResult(
            text=text, destination=HIDDEN, decided_by="rule",
            reason=f"관리자 차단어 '{v.matched_pattern}'",
            rule_value=v.matched_pattern, rule_action="block",
            matched_text=v.matched_text,
        )

    # ② 검토어에 걸렸는지 기억해둔다. LLM이 정상이라 해도 관리자에게 보여야 한다.
    flagged = v.verdict == "review"

    # ③ 앞단. '통과' 만 정할 수 있다. 검토어에 걸린 건 관리자가 보겠다고 한
    # 것이라 앞단이 넘기지 않는다.
    pre = prefilter.check(text) if prefilter and not flagged else None
    if pre and pre.passes and prefilter.skips_gpt:
        return PipelineResult(
            text=text, destination=PASSED, decided_by="prefilter",
            reason=pre.reason, prefilter_score=pre.score,
        )

    # ④ 등록어에 안 걸린 댓글도 여기로 온다 — 이게 이 설계의 요점
    r = await judge.judge(text, parent_text=parent_text, video=video)

    base = dict(
        text=text, decided_by="llm",
        # 판별에 실패했으면 그 사유를 남긴다. 안 남기면 DB 에 reason 이 비어
        # 있어서, 나중에 왜 실패했는지 알아낼 방법이 없다 (실제로 194건을
        # 그렇게 놓쳤다). 관리자 화면에도 '판별 실패'라고 보여야 한다.
        reason=r.reason or (f"판별 실패 — {r.error}" if r.error else ""),
        llm_label=r.label, llm_category=r.category,
        # 위험도를 안 주는 판별기(실험용·테스트용 가짜)도 그대로 돈다. 그땐 카테고리 표로 대신한다.
        llm_severity=getattr(r, "severity", None),
        rule_value=v.matched_pattern if flagged else None,
        rule_action="review" if flagged else None,
        matched_text=v.matched_text if flagged else None,
        prefilter_score=pre.score if pre else None,
    )

    return PipelineResult(
        destination=route(r.label, r.category, flagged, auto_hide), **base
    )


async def process_many(
    rules: Sequence,
    judge: LlmJudge,
    items: Sequence[tuple[str, str | None]],
    auto_hide: set[str] | None = None,
    videos: Sequence[VideoContext | None] | None = None,
    prefilter: Prefilter | None = None,
) -> list[PipelineResult]:
    """(본문, 부모본문) 목록을 한꺼번에 처리한다.

    차단어에 걸린 건 LLM을 안 부르므로, 목록이 커도 실제 호출 수는 그보다 적다.
    """
    import asyncio

    vs = list(videos) if videos else [None] * len(items)
    return list(
        await asyncio.gather(
            *(
                process(rules, judge, t, p, auto_hide, v, prefilter)
                for (t, p), v in zip(items, vs)
            )
        )
    )
