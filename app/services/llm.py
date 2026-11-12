"""3차 LLM 판별.

프롬프트 이력
  v1 (249토큰) : 숨김/검토/통과 기준만 나열. 예시 없음.
                 -> clean 500건 중 69건 오탐. 그중 최소 19건이 커뮤니티 은어
                    (게이=디시 호칭, ~노=말투)를 비하로 오독한 것이었다.
  v2           : 판단 원칙 10개 + 예시 21개.
                 원칙 6·7·8이 v1에서 놓쳤던 자기비하·인용·공인비판을 명시한다.
  v3           : 원칙 11개 + 예시 43개. 실채널에서 뚫린 걸 볼 때마다 그 댓글을
                 예시로 붙였다. 하루에 15개가 늘었고, 회귀 세트 39건 중 14건이
                 예시에 글자 그대로 들어가 있었다 — 시험이 아니라 암기였다.
                 예시가 늘수록 서로 부딪혀 예전에 되던 게 다시 틀렸다.
  v4           : 판단 순서 4단계 + 카테고리 11개 + 예시 23개. 4,700자.
                 - 원칙 11개를 '무엇이 있고 누구를 향하나 → 실제 공격인가 →
                   공격처럼 보이지만 아닌가 → 확정 못 하면 ambiguous' 로 접었다.
                   6·7·10·11은 전부 3번의 하위 항목이었다.
                 - 1번: "대상이 없으면 safe" 로 읽히던 걸 고쳤다. 유해 요소가
                   있는데 대상·의도가 불명확하면 4번으로 간다. 凸 같은 기호 욕이
                   검토 없이 공개되던 구멍이 이걸로 막혔다.
                 - 3번: 대상 없는 비속어는 흔한 탄식으로 명확하면 safe, 확정
                   어려우면 ambiguous. 그 결과 ㅋㅋㅅㅂ 같은 짧은 비속어가 큐로
                   온다 — 일관성을 택한 값이다.
                 - 4번: ambiguous 는 '모르는 표현' 이 아니라 '유해 여지가 있는데
                   확정 못 하는 것'. 그래서 山 은 safe 로 간다 (여지가 없다).
                 - 예시는 '같은 표현인데 대상·의도가 달라 갈리는 짝' 과 카테고리당
                   기준점 하나만. 기호를 외우게 하는 예시와 회귀 세트 문장은 0.
                 - "채널 맥락이 원칙보다 우선" 삭제. 맥락은 참고 정보다. 진용진
                   기준이 "출연자 평은 정상" 을 "출연자 욕도 정상" 으로 읽히게
                   하던 것이 구조적으로 막혔다 (저새끼·상또라이가 v3 에서 통과).
                 회귀 세트 30±2/39. v3 의 36 은 14건 암기 포함. 남은 9건은
                 문화 지식(밈·관용) · 사람도 갈리는 경계 · 낡은 기대값.
                 실데이터 300건 v3 대비 검토 큐 +8%p — 절반은 v3 가 놓친 욕설.
                 회귀 세트: python -m scripts.check_prompt
                 오류 기록: eval/오류기록.csv
  v5 (2026-11-09) : v4 + [위험도] 1~5 와 예시마다 그 단계. 판정(label·category)
                 원칙은 한 글자도 안 고쳤다. 카테고리 표로 정하던 위험도를 댓글을
                 읽은 LLM 이 직접 매기게 한 것 (services/severity.py 참고).
                 단계 정의는 팀 승인 전 초안이다.

앞으로의 규칙: 새 오판 하나로 프롬프트를 고치지 않는다. 기록만 한다. 같은 유형이
반복되면 그때 판단 순서의 한 줄로 덮는다. 그 댓글을 예시로 붙이지 않는다.
회귀 세트에 있는 문장은 예시에 넣지 않는다 — 넣으면 세트가 시험 구실을 못 한다.

주의 1: 예시가 원칙을 이긴다. 원칙을 고쳤으면 예시 전체를 다시 훑어야 한다.
원칙 5 를 고쳤을 때 "?" 예시가 옛 문장을 그대로 복창하고 있었고, 山 은
반대 예시 하나 때문에 원칙대로 안 갔다. 둘 다 실제로 겪었다.

주의 2: 원칙 안에서도 뒤에 오는 센 문장이 앞 문장을 이긴다. "문장부호는 safe"
라고 쓰고 바로 아래 "문장 없이 놓인 글자는 모양으로 쓰인 것" 을 넣었더니
? ??? ... 이 전부 ambiguous 로 갔다. 부딪힐 수 있는 규칙은 순서를 매기고
"앞에서 정해지면 뒤는 보지 않는다" 고 명시해야 한다 (4번 ①②③).

길이를 1,024토큰 이상으로 맞춘 이유: OpenAI 프롬프트 캐싱이 그 아래로는
안 걸린다. 넘기면 반복되는 앞부분이 1/10 값이 되어, 4배 긴 프롬프트가
짧은 것보다 오히려 싸진다 (실측 168원 -> 129원).

[이 채널의 맥락]을 맨 뒤에 둔 것도 캐싱 때문이다. 앞부분이 고정이어야
채널이 바뀌어도 공통 부분은 캐시에서 재사용된다.
"""

import asyncio
import hashlib
import json
import random
import re
from dataclasses import dataclass, field

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from app.core.config import get_settings

PRINCIPLES = """너는 유튜브 채널 관리자를 돕는 댓글 검토 AI다.
단어가 아니라 '누구에게, 무엇을 하는가'로 판단한다. 처음 보는 표현이어도
아래 순서로 본다. 특정 단어·기호를 외워서 맞추는 것이 아니다.

[판단 순서]

1. 무엇이 있고, 누구를 향하는가.
   댓글에 유해로 읽힐 요소(비속어·비하·기호 욕·위협·성적 표현 등)가 있는지,
   그리고 그것이 누구를 향하는지를 함께 본다.
   유해 요소가 전혀 없으면 safe 다. 자기 상황에 대한 감탄·탄식·잡담·질문이 그렇다.
   유해 요소는 있는데 대상이나 의도가 분명하지 않으면 safe 로 단정하지 않는다.
   그런 것은 4번으로 간다. 대상이 없다는 것만으로 safe 가 되지는 않는다.
   기호·자모·짧은 표현·은어는 그 자체의 뜻과 쓰인 맥락을 같이 본다.
   홍보·유인·도배, 신상 노출, 자해 유도는 대상이 없어도 카테고리 정의대로 harmful 이다.

2. 그 대상을 실제로 공격하는가.
   비하·조롱·위협·성적 대상화가 표현돼 있으면 harmful 이다. 욕설이 없어도
   의도가 명확하면 harmful 이고, 욕설이 있어도 공격이 아니면 harmful 이 아니다.
   화살이 행동·결과물·정책을 향하면 비판이고, 사람의 인격·지능·외모·가정·출신을
   향하면 공격이다.

3. 공격처럼 보이지만 실제 공격이 아닌 경우가 있는가.
   다음과 같은 경우는 일반적으로 safe다.
   - 자기 자신을 향한 욕설·자기비하.
   - 남의 말을 인용하거나, 상황을 전달하거나, 자기가 당한 일을 호소하는 것.
   - 공인·기관의 정책·업무·행동에 대한 비판. 거칠거나 비꼬아도 같다.
   - 결과물(영상·글·작품)에 대한 부정적 평가. 과장·비꼼이어도 같다.
   - 영상 속 인물·상황에 대한 감상·묘사·예측. 관용적 과장도 여기다.
   - 집단·국적·성별·성적 지향·질병을 언급·질문·설명만 하는 것.

   대상 없이 감정이나 상황을 표현하는 비속어는 그 자체만으로 harmful로 확정하지 않는다.
   표현이 흔한 감탄·탄식으로 명확하면 safe로 보고, 공격인지 정상적인 감정 표현인지
   확정하기 어려우면 ambiguous로 둔다.

4. 확정할 수 없는가. 그러면 ambiguous 다.
   ambiguous 는 '모르는 표현'이 아니다. 유해하게 해석할 근거가 실제로 있는데,
   문맥·대상·의도를 확인할 수 없어 harmful 로 확정하기 어려운 경우다.
   유해 요소가 전혀 없는 것은 ambiguous 가 아니라 safe 다. 문장부호·감탄·흔한
   축약만 있는 댓글이 그렇다.
   다음은 ambiguous 로 본다.
   - 뜻을 아는 기호 욕이나 비속어인데 누구를 향한 것인지 알 수 없는 것.
   - 짧아서 공격인지 감탄·관용 표현인지 가릴 수 없는 것.
   - 폭력 단어가 있는데 실제 위해 의사인지 답답함·감정 표현인지 문맥이 부족한 것.
     상대에게 위해를 가하겠다는 의사가 명확하면 harmful(위협), 관용 표현으로
     확인되면 safe 다.
   - 유해한 욕설·비하·위협 등으로 해석될 여지가 있으나, 단일 기호·자모·한자
     하나처럼 문맥이 부족해 유해 여부를 확정할 수 없는 것.
     사전적 뜻이 있다는 이유로 safe 로 단정하지 않는다.
   - 은어·우회 표기처럼 뜻을 확실히 알 수 없는 것. 아는 표현이 아니면 사람이 본다.

[카테고리]

욕설    : 특정 대상을 향해 비속어로 직접 공격한 것.
          겨냥한 대상이 없는 감탄·탄식은 여기 해당하지 않는다.
모욕    : 비속어가 없어도 인격·능력·외모를 깎아내린 것. 대상은 특정 개인이거나,
          정당·지지 성향·팬덤처럼 스스로 택한 소속으로 묶인 집단이다.
          단순한 감상이나 행동에 대한 지적은 여기 해당하지 않는다.
혐오    : 지역·성별·국적·인종·나이·장애처럼 본인이 고를 수 없는 속성을 근거로
          집단을 비하한 것.
          고를 수 있는 소속(정당·지지 성향·팬덤 등)에 대한 비난은 혐오가 아니라
          모욕이다. 둘을 가르는 것은 표현의 세기가 아니라 '그 속성을 본인이
          택할 수 있었는가' 하나뿐이다.
성희롱  : 성적 대상화, 성적 모욕, 성적 행위 요구, 성적 수치심 유발.
위협    : 작성자가 이 글을 읽을 상대에게 해를 가하겠다고 밝힌 것.
괴롭힘  : 같은 대상을 반복해서 따라다니며 시달리게 하는 것.
          한 번의 공격은 욕설이나 모욕으로 분류한다.
          반복한다는 점은 스팸(도배)과 같지만, 겨냥한 사람이 있으면 괴롭힘이다.
신상털기: 특정 개인의 실명·거주지·직장·학교·전화번호·가족관계·SNS 계정을
          노출하거나 추측해 퍼뜨린 것. 공격적 표현이 없어도 해당한다.
          이미 보도된 공인의 공적 신분을 말하거나 보도 내용을 묻는 것은 아니다.
자해    : 자해·자살에 관한 것. 남에게 죽으라고 하거나 자해를 부추기거나
          방법을 알려주면 harmful, 작성자 본인의 자해 암시는 ambiguous 로 둔다
          (관용적 과장이어도 사람이 한 번은 봐야 한다).
스팸    : 홍보·유인 목적의 게시. 링크가 없어도 자기 채널·프로필로 유도하면 해당한다.
          같은 말을 되풀이해 댓글창을 채우는 도배도 여기다. 내용이 칭찬이어도
          형태가 도배면 스팸이다. 겨냥한 사람이 있는 반복은 괴롭힘이다.
정상    : 위 어디에도 해당하지 않는 것.
기타    : 유해로 볼 요소가 분명히 있는데 위 카테고리 어디에도 맞지 않을 때만 쓴다.
          판단이 애매하다는 이유로 쓰지 않는다. 그건 label 로 표현한다.

[분류]
harmful   : 위 유해 카테고리 중 하나에 해당하는 것이 명확한 경우
ambiguous : 유해로 볼 요소가 실제로 있으나 문맥이 부족해 확정할 수 없는 경우
safe      : 정상적인 의견·질문·정보·잡담. 공격 의도가 없는 경우

[위험도]
severity 는 이 댓글이 사람에게 얼마나 해로운지다. 카테고리가 아니라 세기로 정한다.
같은 모욕이라도 가벼운 조롱과 인격을 짓밟는 공격은 다르다.
label 이 safe 면 0 이다. harmful 과 ambiguous 는 1~5 중 하나를 고른다.
ambiguous 는 유해한 쪽으로 읽었을 때의 세기를 매긴다.
1 낮음 : 특정인을 해치지 않는 유해. 홍보·유인·도배, 겨냥한 대상이 없는 비속어.
2 주의 : 특정인이나 스스로 택한 소속을 향한 가벼운 조롱·비꼼·깎아내림.
3 보통 : 특정인의 인격·외모·지능·능력을 직접 깎아내리거나 비속어로 겨냥한 것.
         고를 수 없는 속성(성별·지역·국적·장애 등)으로 집단을 낮춰 보는 것.
4 심각 : 사람이나 집단을 벌레·짐승·병균에 빗대는 비인간화, 배제·추방 주장,
         성희롱, 가족을 끌어들인 모욕, 구체적이지 않은 겁주기, 같은 대상을 거듭 공격하는 것.
5 긴급 : 실제 위해로 이어질 수 있는 것. 찾아가거나 해치겠다는 구체적 위협,
         죽으라는 말·자해 유도, 신상 노출, 성범죄 묘사, 작성자 본인의 자해 암시."""

# 예시는 회귀 세트(eval/회귀세트.csv)에 없는 문장만 쓴다. 겹치면 정답을
# 알려준 채로 채점하는 셈이라 세트가 시험 구실을 못 한다.
EXAMPLES = """
[예시]
같은 표현이라도 대상과 의도에 따라 판정이 갈린다. 아래는 그 갈림을 보여준다.
형식은 label / category / severity 다.

"ㅅㅂ 버스 놓쳤다"
-> safe / 정상 / 0. 비속어가 자기 상황에 대한 탄식이며 겨냥한 대상이 없다.

"ㅅㅂ 저 인간 진짜 꺼져라"
-> harmful / 욕설 / 3. 같은 비속어라도 특정 인물을 겨냥했다.

"몸매 좋으시네요 운동 오래 하셨나봐요"
-> safe / 정상 / 0. 외모 언급이지만 성적 대상화나 모욕이 아니다.

"그 몸으로 카메라 앞에 서는 거 부끄럽지도 않냐"
-> harmful / 모욕 / 3. 외모를 근거로 특정인을 깎아내린다.

"요즘 영상 왜 이래 노잼임 실망"
-> safe / 정상 / 0. 결과물에 대한 부정적 평가다.

"이딴 걸 만들고 밥 먹고 사냐"
-> harmful / 모욕 / 3. 평가 형식이지만 화살이 결과물이 아니라 만든 사람을 향한다.

"장관이 저따위로 일하면 안 되지 진짜"
-> safe / 정상 / 0. 공인의 공적 업무에 대한 비판이다.

"저 새끼는 얼굴만 봐도 토나온다"
-> harmful / 모욕 / 3. 특정 인물의 인격·외모를 향한 직접 공격이다.

"여자들은 원래 운전을 못해"
-> harmful / 혐오 / 3. 고를 수 없는 속성(성별)으로 집단을 비하한다.

"저 당 찍는 놈들은 다 똑같아"
-> harmful / 모욕 / 2. 지지 성향은 스스로 택한 소속이라 혐오가 아니라 모욕이다.

"쟤 게이래"
-> ambiguous / 기타 / 2. 성적 지향을 언급했으나 비하인지 단순 전달인지 알 수 없다.

"나같은 놈이 뭘 하겠냐 ㅋㅋ"
-> safe / 정상 / 0. 자기비하이며 타인을 공격하지 않는다.

"쟤가 나보고 병신이래"
-> safe / 정상 / 0. 욕설을 인용해 상황을 전달할 뿐 본인이 공격하지 않는다.

"나가면 자기만 쳐맞지 ㄹㅇㅋㅋ"
-> safe / 정상 / 0. 영상에서 벌어질 일을 예측한 감상이다.

"지혼자 처먹으려고 시키고 다 들고 나왔으면서"
-> safe / 정상 / 0. 표현이 거칠지만 행동을 지적한 것이지 인격 공격이 아니다.

"저 사람 좀 그렇지 않음?"
-> ambiguous / 기타 / 2. 대상을 지목하고 부정적으로 봤으나 공격인지 확정할 수 없다.

"뒤지고 싶냐 진짜"
-> harmful / 위협 / 4. 읽는 상대에게 신체적 위해를 암시한다.

"이 영상 보고 나도 죽고싶어졌다"
-> ambiguous / 자해 / 5. 관용적 과장일 수 있으나 자해 암시는 사람이 확인해야 한다.

"그렇게 살 바엔 그냥 죽는 게 낫지 않냐"
-> harmful / 자해 / 5. 상대에게 자살을 권하는 표현이다.

"이 영화 보면 진짜 죽여준다 ㅋㅋ"
-> safe / 정상 / 0. '죽여준다'가 감탄 표현으로 쓰였고 자해와 무관하다.

"저 사람 ○○동 살고 애 둘 있는 거 이미 다 퍼졌던데"
-> harmful / 신상털기 / 5. 공격 표현은 없으나 특정인의 거주지·가족관계를 퍼뜨린다.

"무료 이벤트 참여는 여기로 http://..."
-> harmful / 스팸 / 1. 홍보 목적의 링크 게시다.

"구독 구독 구독 구독 구독 구독 구독 구독 구독 구독"
-> harmful / 스팸 / 1. 같은 말을 되풀이해 댓글창을 채운 도배다.

[출력]
반드시 JSON 으로만 답한다. reason 은 40자 이내 한 문장."""


# 관리자가 고친 프롬프트에서 채널 판단 기준이 들어갈 자리.
CHANNEL_SLOT = "{채널 기준}"


def build_prompt(channel_context: str = "", template: str | None = None) -> str:
    """채널 맥락은 반드시 맨 뒤에 붙인다 (앞부분 캐시 재사용을 위해).

    template 은 관리자가 'AI 프롬프트' 화면에서 고쳐 저장한 것(channels.prompt_template).
    없으면 기본 프롬프트다. 출력 형식은 SCHEMA(json_schema, strict)가 강제하므로
    관리자가 [출력] 줄을 지워도 응답 모양은 깨지지 않는다.
    """
    tail = channel_context.strip() or "(없음)"
    if template and template.strip():
        return template.replace(CHANNEL_SLOT, tail)
    return (
        f"{PRINCIPLES}\n{EXAMPLES}\n\n"
        "[이 채널의 맥락]\n"
        "이 채널에 대한 참고 정보다. 대상이 누구인지, 어떤 표현이 이 채널에서\n"
        "어떻게 쓰이는지 아는 데 쓴다. 위 판단 순서 자체를 바꾸지는 않는다.\n"
        f"{tail}"
    )


def default_template() -> str:
    """기본 프롬프트를 채널 기준 자리만 비워 둔 채로. 화면의 '기본값으로' 가 이것이다."""
    return build_prompt(CHANNEL_SLOT)


def prompt_version(channel_context: str = "", template: str | None = None) -> str:
    """이 판정이 어떤 기준으로 매겨졌는지 나타내는 지문.

    사람이 "v3 으로 올려야지" 하고 기억할 필요가 없게 내용에서 뽑는다.
    프롬프트를 한 글자라도 고치면 값이 달라지고, 채널 맥락을 바꿔도 달라진다.

    이게 없던 탓에 채널 4 의 판정 4,568건 중 어느 것이 구 프롬프트 기준이고
    어느 것이 신 기준인지 구분할 수 없었다. 날짜로 짐작할 수는 있었지만
    같은 날 프롬프트를 두 번 고치면 그마저 안 된다.
    """
    h = hashlib.sha256(build_prompt(channel_context, template).encode("utf-8"))
    return h.hexdigest()[:12]


SYSTEM_PROMPT = build_prompt()

SCHEMA = {
    "name": "moderation",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": ["harmful", "ambiguous", "safe"]},
            "category": {
                "type": "string",
                "enum": ["욕설", "모욕", "혐오", "성희롱", "위협", "괴롭힘",
                         "신상털기", "자해", "스팸", "정상", "기타"],
            },
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
            # 위험도 0~5. safe 는 0. 정의는 [위험도], 쓰는 곳은 services/severity.py.
            "severity": {"type": "integer", "enum": [0, 1, 2, 3, 4, 5]},
        },
        "required": ["label", "category", "confidence", "reason", "severity"],
        "additionalProperties": False,
    },
}

# 다시 걸면 되는 오류들. 정원 초과·네트워크·서버 일시 장애.
RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)
RETRIES = 5

# label -> 파이프라인 처리 (숨김 / 관리자 검토 / 통과)
VERDICT = {"harmful": "block", "ambiguous": "review", "safe": "pass"}

# 다른 이용자 계정. 유해성 판단에 불필요하고 개인정보라 치환한다.
MENTION = re.compile(r"@[\w가-힣._-]{2,}")


def sanitize(text: str) -> str:
    return MENTION.sub("@사용자", text).strip()


@dataclass(frozen=True)
class VideoContext:
    """댓글이 달린 영상. 판별할 때 같이 보낸다.

    제목만으로도 꽤 전달된다 — "머니게임 Ep5" 한 줄이면 무슨 방송인지 안다.
    memo 는 그것만으로 부족한 영상에만 채운다(출연자 구도 같은 것).
    """

    title: str | None = None
    memo: str | None = None

    def as_prompt(self) -> str:
        # 공백만 든 값은 없는 것으로 본다. DB 에서 빈 문자열이 올라오면
        # "[이 영상] 제목:" 같은 빈 껍데기가 프롬프트에 섞인다.
        줄 = []
        if (t := (self.title or "").strip()):
            줄.append(f"제목: {t}")
        if (m := (self.memo or "").strip()):
            줄.append(m)
        return "[이 영상] " + " / ".join(줄) if 줄 else ""


@dataclass
class LlmVerdict:
    verdict: str          # block / review / pass
    label: str = ""       # harmful / ambiguous / safe
    category: str = ""
    confidence: float = 0.0
    reason: str = ""
    error: str | None = None
    raw: str = ""         # 모델이 돌려준 원문. 'AI 프롬프트' 시험 화면에서 보여준다
    severity: int | None = None   # 1~5. safe 이거나 값이 label 과 어긋나면 None


@dataclass
class LlmStats:
    calls: int = 0
    errors: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    PRICE_IN: float = field(default=0.2, repr=False)
    PRICE_OUT: float = field(default=1.2, repr=False)
    PRICE_CACHED: float = field(default=0.02, repr=False)

    @property
    def cost_usd(self) -> float:
        fresh = max(self.input_tokens - self.cached_tokens, 0)
        return (
            fresh * self.PRICE_IN / 1e6
            + self.cached_tokens * self.PRICE_CACHED / 1e6
            + self.output_tokens * self.PRICE_OUT / 1e6
        )


class LlmJudge:
    """댓글을 판정한다. 호출 상한을 넘으면 멈춘다 (사고 방지)."""

    def __init__(
        self,
        concurrency: int = 8,
        max_calls: int | None = None,
        channel_context: str = "",
        prompt_template: str | None = None,
    ):
        cfg = get_settings()
        if not cfg.openai_api_key:
            raise RuntimeError(".env에 OPENAI_API_KEY가 없다.")
        self._client = AsyncOpenAI(api_key=cfg.openai_api_key)
        self._model = cfg.openai_model
        self._sem = asyncio.Semaphore(concurrency)
        self._max_calls = max_calls or cfg.llm_max_calls_per_run
        self._prompt = build_prompt(channel_context, prompt_template)
        # 이 판정들이 어떤 기준으로 매겨졌는지. 저장할 때 같이 남긴다.
        self.prompt_version = prompt_version(channel_context, prompt_template)
        self.stats = LlmStats()

    async def judge(
        self,
        text: str,
        parent_text: str | None = None,
        video: "VideoContext | None" = None,
        reasoning_effort: str = "low",
        extra: str = "",
    ) -> LlmVerdict:
        """parent_text 는 답글일 때 부모 댓글, video 는 그 댓글이 달린 영상.

        reasoning_effort 와 extra 는 실험용(scripts/experiment.py). 운영 경로는
        기본값 그대로 쓴다. extra 는 사용자 메시지 끝에 붙는 참고 글이라
        시스템 프롬프트 캐시를 깨지 않는다.

        "그만해라 진짜" 한 줄만 보면 사람도 판단 못 한다. 무엇에 대한 답인지에
        따라 말리는 쪽일 수도 편드는 쪽일 수도 있어서, 답글은 부모를 같이 보낸다.

        영상 정보를 시스템 프롬프트가 아니라 여기(사용자 메시지)에 넣는 이유는
        캐싱 때문이다. 시스템 프롬프트는 채널이 같으면 늘 똑같아야 앞부분이
        캐시에서 재사용된다(1/10 값). 영상은 댓글마다 달라서, 시스템 쪽에
        넣으면 영상이 바뀔 때마다 캐시가 깨진다.
        """
        if self.stats.calls >= self._max_calls:
            return LlmVerdict("review", error="호출 상한 도달")

        user = sanitize(text)
        if parent_text:
            user = f"[부모 댓글] {sanitize(parent_text)}\n[판단할 댓글] {user}"
        if video and (head := video.as_prompt()):
            user = f"{head}\n{user}"
        if extra:
            user = f"{user}\n{extra}"

        async with self._sem:
            self.stats.calls += 1
            r = None
            last: Exception | None = None

            # 레이트리밋·타임아웃·5xx 는 다시 걸면 대개 된다. 재시도가 없으면
            # 1,103건을 동시 8개로 돌릴 때 194건(17.6%)이 통째로 실패했다.
            # 그렇게 실패한 건은 label 이 비어 검토 큐로 가는데, 관리자에게는
            # 근거 없는 댓글이 무더기로 쌓인 것으로 보인다.
            for attempt in range(RETRIES):
                try:
                    r = await self._client.chat.completions.create(
                        model=self._model,
                        messages=[
                            {"role": "system", "content": self._prompt},
                            {"role": "user", "content": user},
                        ],
                        response_format={"type": "json_schema", "json_schema": SCHEMA},
                        reasoning_effort=reasoning_effort,
                        # 이 한도에는 추론 토큰이 포함된다. 250 으로 뒀더니
                        # 어려운 댓글에서 추론만 하다 한도에 걸려(finish_reason=length)
                        # 본문이 빈 문자열로 왔다 — 4,567건 중 895건(19.6%)이 그랬다.
                        # 실측 추론 토큰이 160~250 이라 넉넉히 잡는다. 안 쓰면 청구도 안 된다.
                        max_completion_tokens=800,
                    )
                    break
                except RETRYABLE as e:
                    last = e
                    self.stats.retries += 1
                    if attempt == RETRIES - 1:
                        break
                    # 2, 4, 8 … 초. 같은 순간에 몰려 다시 막히지 않게 흔들어준다.
                    await asyncio.sleep(min(2 ** (attempt + 1), 30) * (1 + random.random() * 0.3))
                except Exception as e:  # 다시 걸어도 소용없는 것 (잘못된 요청 등)
                    last = e
                    break

            if r is None:
                self.stats.errors += 1
                return LlmVerdict(
                    "review", error=f"{type(last).__name__}: {last}"[:160]
                )

        u = r.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        self.stats.input_tokens += u.prompt_tokens
        self.stats.output_tokens += u.completion_tokens
        self.stats.cached_tokens += cached

        raw = r.choices[0].message.content or ""
        try:
            d = json.loads(raw)
        except Exception as e:
            self.stats.errors += 1
            return LlmVerdict("review", error=f"파싱 실패: {e}"[:160], raw=raw)

        return LlmVerdict(
            verdict=VERDICT.get(d["label"], "review"),
            label=d["label"],
            category=d.get("category", ""),
            confidence=float(d.get("confidence", 0.0)),
            reason=d.get("reason", ""),
            raw=raw,
            severity=_severity(d.get("severity"), d["label"]),
        )

    async def judge_many(self, texts: list[str]) -> list[LlmVerdict]:
        return await asyncio.gather(*(self.judge(t) for t in texts))


def _severity(value, label: str) -> int | None:
    """모델이 준 위험도를 1~5 로. safe 이거나 label 과 어긋나는 값은 버린다.

    유해라면서 0 을 주면 그 값을 믿을 근거가 없다. 버리면 카테고리 표로 대신한다
    (severity.raw_level). 관리 기준(AI 프롬프트 화면)에서 [위험도] 를 지운 프롬프트도
    스키마 때문에 값은 오는데, 그때는 정의 없이 찍은 값이라는 점을 알아 둘 것.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if label == "safe" or not 1 <= v <= 5:
        return None
    return v
