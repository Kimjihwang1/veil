"""② 단계 ML 분류기 — 정제 · 설정값 탐색 · 학습 · 캐스케이드 평가.

    python -m scripts.train_prefilter

GPT 앞에 둘 가벼운 분류기다. 하는 일은 '확실히 정상인 댓글을 GPT 없이 통과'
하나뿐이다 (app/services/prefilter.py).

학습 데이터 (ml/data/, 레포에 안 올림 — 이 스크립트가 받는다)
  BEEP!   kocohub/korean-hate-speech   연예 뉴스 댓글   CC-BY-SA-4.0
  K-MHaS  adlnlp/K-MHaS                뉴스 댓글        CC-BY-SA-4.0
  '정상 vs 나머지' 로 묶는다. BEEP! 는 hate·offensive, K-MHaS 는 not_hate(8)
  가 아닌 모든 것이 '나머지'.

데이터 역할 — 시험 데이터는 설정값을 고르는 데 쓰지 않는다
  학습   BEEP! train 의 90% + K-MHaS train
  검증   BEEP! train 의 10% + K-MHaS valid   ← 설정값·문턱값은 여기서만 고른다
  시험   BEEP! dev, K-MHaS test              ← 마지막에 한 번
  시험   eval/라벨_김지황_200.csv             ← 개발자 1명 라벨. 같은 200건의
         GPT(v4 base) 판정이 eval/라벨_200_결과.csv 에 있어 GPT 를 다시 부르지
         않고 'GPT 만' 과 '캐스케이드' 를 비교한다. 표본은 큐 100 + 통과 100
         이라 실제 분포와 다르다.

정제 (2026-11-03 EDA 에서 나온 것)
  - normalize: NFC · @멘션 치환(GPT 와 같게) · URL 치환 · 4번 넘는 반복 압축
  - 같은 문장 중복 제거. 같은 문장에 라벨이 갈리면 둘 다 버린다
  - 시험·검증 문장과 같은 학습 문장 제거. K-MHaS test 의 1.2%(255건)가
    train 에도 있어 첫 버전의 시험 점수가 부풀려져 있었다

설정값 고르는 기준
  accuracy 가 아니다. 이 단계는 '통과' 만 하므로 중요한 건 '유해를 거의 다
  GPT 로 넘기면서 정상을 얼마나 많이 통과시키나' 다. 그래서 검증 데이터에서
  유해 recall 95% 를 지키는 문턱값을 잡고, 그때 통과되는 정상 비율로 비교한다.
"""

import csv
import random
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, average_precision_score,
                             precision_score, recall_score)
from sklearn.pipeline import Pipeline

from app.services.prefilter import MODEL_PATH, normalize, nothing_to_judge

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "ml" / "data"
REPORT = ROOT / "ml" / "report.txt"

SOURCES = {
    "beep_train.tsv": "https://raw.githubusercontent.com/kocohub/korean-hate-speech/master/labeled/train.tsv",
    "beep_dev.tsv": "https://raw.githubusercontent.com/kocohub/korean-hate-speech/master/labeled/dev.tsv",
    "kmhas_train.txt": "https://raw.githubusercontent.com/adlnlp/K-MHaS/main/data/kmhas_train.txt",
    "kmhas_valid.txt": "https://raw.githubusercontent.com/adlnlp/K-MHaS/main/data/kmhas_valid.txt",
    "kmhas_test.txt": "https://raw.githubusercontent.com/adlnlp/K-MHaS/main/data/kmhas_test.txt",
}

# 설정값 후보. 글자 몇 개짜리 조각까지 볼지 × 규제 세기(C 가 클수록 약함).
GRID = [(ng, c) for ng in ((1, 3), (1, 4), (2, 5)) for c in (1.0, 4.0, 16.0)]
# 첫 버전(2026-11-03, 정제 없이 감으로 정한 값). 전후 비교용.
FIRST = ((1, 4), 4.0)

TARGET_RECALL = 0.95
SEED = 42

Rows = list[tuple[str, int]]


def download() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        if not (DATA / name).exists():
            print(f"받는 중 {name}")
            urllib.request.urlretrieve(url, DATA / name)


def read(name: str) -> Rows:
    with open(DATA / name, encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    if name.startswith("beep"):
        return [(r["comments"], int(r["hate"] != "none")) for r in rows]
    # K-MHaS: 8 = not_hate. 여러 라벨이 '2,4' 처럼 붙어 온다.
    return [(r["document"], int(r["label"] != "8")) for r in rows]


def clean(rows: Rows, banned: set[str] = frozenset()) -> tuple[Rows, dict]:
    """정규화 → 빈 값 · 중복 · 라벨 충돌 · 시험셋과 겹치는 문장 제거."""
    labels: dict[str, set[int]] = defaultdict(set)
    order: list[str] = []
    for text, y in rows:
        t = normalize(text)
        if t and t not in labels:
            order.append(t)
        if t:
            labels[t].add(y)
    conflict = {t for t, ys in labels.items() if len(ys) > 1}
    out = [(t, next(iter(labels[t]))) for t in order
           if t not in conflict and t not in banned]
    stat = {
        "원본": len(rows),
        "중복": len(rows) - len(labels) - sum(1 for text, _ in rows if not normalize(text)),
        "라벨충돌": len(conflict),
        "시험셋과겹침": sum(1 for t in order if t in banned and t not in conflict),
        "남음": len(out),
    }
    return out, stat


def make_model(ngram: tuple[int, int], c: float) -> Pipeline:
    return Pipeline([
        ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=ngram,
                                  min_df=2, sublinear_tf=True, max_features=300_000)),
        ("clf", LogisticRegression(max_iter=2000, C=c)),
    ])


def threshold_for_recall(y: np.ndarray, p: np.ndarray, recall: float) -> float:
    """유해의 recall 비율 이상이 p >= t 가 되는 가장 큰 t."""
    toxic = np.sort(p[y == 1])
    return float(toxic[int(np.floor((1 - recall) * len(toxic)))])


def pass_rate(y: np.ndarray, p: np.ndarray, t: float) -> float:
    """문턱값 t 에서 GPT 없이 통과되는 정상 댓글 비율."""
    return float((p[y == 0] < t).mean())


def scores(y, pred) -> str:
    return (f"accuracy {accuracy_score(y, pred):6.1%}  "
            f"precision {precision_score(y, pred, zero_division=0):6.1%}  "
            f"recall {recall_score(y, pred, zero_division=0):6.1%}")


class Tee:
    def __init__(self, path: Path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, line: str = "") -> None:
        print(line)
        self.f.write(line + "\n")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    download()
    REPORT.parent.mkdir(exist_ok=True)
    out = Tee(REPORT)

    with open(ROOT / "eval" / "라벨_200_결과.csv", encoding="utf-8-sig") as f:
        labeled = list(csv.DictReader(f))

    # ── 1. 정제 ──────────────────────────────────────────────
    tests_raw = {"BEEP! dev": read("beep_dev.tsv"), "K-MHaS test": read("kmhas_test.txt")}
    tests = {k: clean(v)[0] for k, v in tests_raw.items()}
    test_texts = {t for rows in tests.values() for t, _ in rows}
    test_texts |= {normalize(r["댓글"]) for r in labeled}

    valid_k, st_vk = clean(read("kmhas_valid.txt"), test_texts)
    beep_tr, st_b = clean(read("beep_train.tsv"), test_texts | {t for t, _ in valid_k})
    random.Random(SEED).shuffle(beep_tr)
    cut = len(beep_tr) // 10
    valid = valid_k + beep_tr[:cut]
    kmhas_tr, st_k = clean(read("kmhas_train.txt"),
                           test_texts | {t for t, _ in valid} | {t for t, _ in beep_tr})
    train = beep_tr[cut:] + kmhas_tr

    out("[1] 정제")
    for name, st in (("BEEP! train", st_b), ("K-MHaS train", st_k), ("K-MHaS valid", st_vk)):
        out(f"  {name:13} " + "  ".join(f"{k} {v:,}" for k, v in st.items()))
    out(f"  → 학습 {len(train):,}건 (유해 {np.mean([y for _, y in train]):.1%})"
        f" · 검증 {len(valid):,}건 · 시험 BEEP! {len(tests['BEEP! dev'])} / K-MHaS {len(tests['K-MHaS test']):,}")

    xtr, ytr = [t for t, _ in train], np.array([y for _, y in train])
    xva, yva = [t for t, _ in valid], np.array([y for _, y in valid])

    # ── 2. 설정값 탐색 (검증 데이터만) ────────────────────────
    out(f"\n[2] 설정값 탐색 — 검증 데이터, 유해 recall {TARGET_RECALL:.0%} 를 지킬 때 통과되는 정상 비율")
    best = None
    for ng, c in GRID:
        t0 = time.time()
        m = make_model(ng, c).fit(xtr, ytr)
        p = m.predict_proba(xva)[:, 1]
        t = threshold_for_recall(yva, p, TARGET_RECALL)
        rate = pass_rate(yva, p, t)
        ap = average_precision_score(yva, p)
        out(f"  글자 {ng[0]}~{ng[1]}  C={c:<4}  정상 통과 {rate:6.1%}  (문턱값 {t:.3f}, AP {ap:.3f}, {time.time()-t0:.0f}초)")
        if best is None or rate > best[0]:
            best = (rate, ng, c, m, t)
    rate, ng, c, model, thr = best
    out(f"  → 고름: 글자 {ng[0]}~{ng[1]}, C={c}, 문턱값 {thr:.3f}")

    # 문턱값을 하나 더: recall 98% (더 보수적)
    p_va = model.predict_proba(xva)[:, 1]
    thr98 = threshold_for_recall(yva, p_va, 0.98)

    joblib.dump(model, MODEL_PATH)
    out(f"  저장 {MODEL_PATH.relative_to(ROOT)} ({MODEL_PATH.stat().st_size / 1e6:.1f} MB)")

    # ── 3. 전처리 전후 — 첫 버전을 같은 시험셋에 ──────────────
    raw_x = [t for t, _ in read("beep_train.tsv") + read("kmhas_train.txt")]
    raw_y = [y for _, y in read("beep_train.tsv") + read("kmhas_train.txt")]
    first = make_model(*FIRST).fit(raw_x, raw_y)

    out("\n[3] 공개 시험셋 — ML 혼자, 문턱값 0.5 (정제된 시험셋)")
    for name, rows in tests.items():
        tx, ty = [t for t, _ in rows], [y for _, y in rows]
        out(f"  {name:12} 첫 버전  {scores(ty, first.predict(tx))}")
        out(f"  {'':12} 정제 후  {scores(ty, model.predict(tx))}")

    # ── 4. 캐스케이드 — 개발자 라벨 200건 ─────────────────────
    texts = [r["댓글"] for r in labeled]
    human = [int(r["기대"] in ("유해", "애매")) for r in labeled]
    gpt = [int(r["base"] in ("유해", "애매")) for r in labeled]
    rule = [nothing_to_judge(t) for t in texts]

    def cascade(p: np.ndarray, t: float, label: str) -> None:
        skip = [r or v < t for r, v in zip(rule, p)]
        pred = [0 if s else g for s, g in zip(skip, gpt)]
        lost = sum(1 for s, h, g in zip(skip, human, gpt) if s and h and g)
        calls = 1 - sum(skip) / len(skip)
        out(f"  {label:<30} GPT 호출 {calls:6.1%}  {scores(human, pred)}  놓침 +{lost}")

    p_first = first.predict_proba([normalize(t) for t in texts])[:, 1]
    p_new = model.predict_proba([normalize(t) for t in texts])[:, 1]
    out(f"\n[4] 개발자 라벨 200건 (양성 {sum(human)}건) — 문턱값은 검증 데이터에서 정한 것")
    out(f"  {'GPT 만':<30} GPT 호출 100.0%  {scores(human, gpt)}")
    cascade(p_first, 0.1, "첫 버전 캐스케이드 (0.1)")
    cascade(p_new, thr, f"정제 후 캐스케이드 (recall95 {thr:.3f})")
    cascade(p_new, thr98, f"정제 후 캐스케이드 (recall98 {thr98:.3f})")
    out(f"\n  놓침 = GPT 는 큐로 보냈는데 앞단이 통과시켜 버린 '사람이 봐야 하는' 댓글")
    out(f"  보고서 {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
