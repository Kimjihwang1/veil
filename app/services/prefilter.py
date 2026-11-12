"""GPT 앞단 — ① 규칙과 ② ML 분류기.

둘 다 '통과' 만 결정할 수 있다. 숨기거나 큐로 보내는 권한은 없다.
틀려도 'GPT 가 볼 걸 안 본 것' 으로 끝나고, 관리자 모르게 가려지는 댓글은
생기지 않는다.

전처리(normalize)는 학습(scripts/train_prefilter.py)과 판별이 같은 함수를
쓴다. 둘이 다르면 모델이 배운 글자 조각과 실제로 받는 글자 조각이 어긋난다.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings
from app.services.llm import sanitize

log = logging.getLogger("outlier.prefilter")

MODEL_PATH = Path(__file__).resolve().parent.parent.parent / "ml" / "prefilter.joblib"

URL = re.compile(r"https?://\S+|www\.\S+")
# 같은 글자 3번 넘게 → 3번. 유튜브 댓글은 ㅋㅋㅋㅋㅋㅋㅋ 가 길게 붙는데
# 학습 데이터(뉴스 댓글)엔 드물다. 길이만 다른 같은 표현을 하나로 본다.
REPEAT = re.compile(r"(.)\1{3,}")
SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFC", text)
    t = sanitize(t)              # @멘션 → @사용자. GPT 에 보내는 것과 같게
    t = URL.sub(" URL ", t)
    t = REPEAT.sub(r"\1\1\1", t)
    return SPACE.sub(" ", t).strip()


# 이모지·ㅋㅋ·숫자·문장부호·타임스탬프만 있는 댓글. 유해로 읽힐 글자가 없다.
TIMESTAMP = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")
EMOTIVE_JAMO = set("ㅋㅎㅠㅜㄷㅡ")
# 한자 (山, 凸). 범위를 ⺀~鿿 로 뭉뚱그리면 한글 자모(ㄱ~)까지
# 들어가 ㅋㅋ 만 있는 댓글이 '판단할 것 있음' 이 된다.
CJK = re.compile(r"[⺀-⿟一-鿿豈-﫿]")


def nothing_to_judge(text: str) -> bool:
    t = TIMESTAMP.sub("", unicodedata.normalize("NFC", text))
    for ch in t:
        if "가" <= ch <= "힣":
            return False
        if "ㄱ" <= ch <= "ㆎ" and ch not in EMOTIVE_JAMO:
            return False
        if ch.isascii() and ch.isalpha():
            return False
        if CJK.match(ch):
            return False
    return True


@dataclass
class PreVerdict:
    passes: bool          # GPT 없이 통과시켜도 되는가
    score: float | None   # ML 이 본 유해 확률. 판단할 내용이 없는 댓글은 0
    reason: str


class Prefilter:
    def __init__(self, mode: str, threshold: float, model=None):
        self.mode = mode
        self.threshold = threshold
        self.model = model

    @property
    def skips_gpt(self) -> bool:
        """shadow 면 기록만 하고 GPT 는 그대로 부른다."""
        return self.mode == "on"

    def check(self, text: str) -> PreVerdict | None:
        if nothing_to_judge(text):
            return PreVerdict(True, 0.0, "판단할 내용 없음 (이모지·웃음·숫자만)")
        if self.model is None:
            return None
        p = float(self.model.predict_proba([normalize(text)])[0, 1])
        return PreVerdict(p < self.threshold, p, f"ML 정상 판정 (유해 확률 {p:.1%})")


@lru_cache
def get_prefilter() -> Prefilter | None:
    """설정대로 만든 앞단. off 면 None. 모델 파일은 처음 한 번만 읽는다."""
    cfg = get_settings()
    if cfg.prefilter_mode not in ("shadow", "on"):
        return None
    model = None
    if MODEL_PATH.exists():
        import joblib  # sklearn 을 앞단을 쓸 때만 불러온다
        model = joblib.load(MODEL_PATH)
    else:
        log.warning("ML 모델 파일이 없어 ② 단계를 건너뛴다: %s", MODEL_PATH)
    return Prefilter(cfg.prefilter_mode, cfg.prefilter_threshold, model)
