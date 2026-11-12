"""위험도 1~5단계 (2026-11-09).

전에는 카테고리마다 위험도를 하나씩 정해 둔 표(critical/high/medium/low)였다.
같은 '모욕' 이라도 "롤 개못하네 ㅋㅋ" 와 "가정교육 독학하신 티가 나네요" 는
무게가 다른데, 표는 그 차이를 버렸다. 실데이터에서 큐의 65~80% 가 '모욕=보통'
한 칸에 몰려 순서가 거의 안 매겨졌다.

지금은 댓글을 읽은 LLM 이 세기를 직접 매긴다 (llm.py [위험도], risk_assessments.severity).
단계 정의는 외부 기준을 우리 카테고리에 맞춘 초안이다.
  - Azure AI Content Safety: 카테고리마다 따로 0~7 세기를 매긴다 (안전/낮음/중간/높음).
  - Bahador 혐오표현 세기 척도: 가벼운 부정 → 비인간화·악마화 → 폭력 선동.
    5점으로 옮긴 연구는 1~3 초기 경고, 4 비인간화, 5 폭력 선동.
팀이 아직 승인하지 않았다. 바꾸려면 llm.py 의 [위험도] 와 이 파일을 같이 고친다.

LLM 이 매긴 값이 없는 판정(등록어·사전 필터·판별 실패·이 기능 전의 옛 판정)은
카테고리 표로 대신한다. 옛 4단계를 그대로 옮긴 것이다 (critical→5, high→4,
medium→3, low→1).
"""

# 화면에 보이는 이름. 숫자가 클수록 위험하다.
NAMES = {1: "낮음", 2: "주의", 3: "보통", 4: "심각", 5: "긴급"}

# LLM 이 매긴 값이 없을 때 쓰는 카테고리 → 단계.
FALLBACK = {
    "신상털기": 5, "위협": 5, "자해": 5,
    "혐오": 4, "성희롱": 4, "욕설": 4,
    "모욕": 3, "괴롭힘": 3, "기타": 3,
    "스팸": 1, "정상": 1,
}
DEFAULT = 3   # 카테고리도 없는 것(등록어에 걸린 것 등)

# 이 단계 이상이고 유해(harmful)로 확정된 댓글은 판별하는 순간 증거로 고정 저장한다.
EVIDENCE_LEVEL = 4


def raw_level(severity: int | None, category: str | None) -> int:
    """LLM 이 매긴 값, 없으면 카테고리 표. 1~5."""
    if severity is not None and 1 <= severity <= 5:
        return severity
    return FALLBACK.get(category or "", DEFAULT)


def level_of(severity: int | None, category: str | None, label: str | None) -> int:
    """화면 정렬·표시에 쓰는 단계.

    애매 판정은 한 단계 내린다. AI 가 '애매하다' 고 한 것은 유해하다고 확정한
    것과 같은 무게일 수 없다. 내리기만 하고 숨기지는 않는다 — 순서가 뒤로 갈 뿐
    사람이 반드시 본다.
    """
    lv = raw_level(severity, category)
    if label == "ambiguous":
        lv = max(lv - 1, 1)
    return lv


def needs_evidence(severity: int | None, label: str | None) -> bool:
    """판별 즉시 증거로 남길 댓글인가. LLM 이 직접 매긴 값만 본다 —
    카테고리 표로 추정한 단계로 자동 저장하면 '욕설=4' 같은 표의 거친 값이 그대로 증거가 된다."""
    return label == "harmful" and severity is not None and severity >= EVIDENCE_LEVEL
