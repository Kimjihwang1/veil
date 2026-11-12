"""route() — 무엇을 가리고 무엇을 사람에게 보낼지 정하는 유일한 곳.

정책이 여기 하나에만 있으므로, 여기가 틀리면 댓글이 조용히 사라지거나
관리자가 봐야 할 것이 통과된다. 자동 숨김은 되돌릴 수 없어서 특히 그렇다.
"""

import pytest

from app.db.models import Channel
from app.services.pipeline import (
    AUTO_HIDE_CATEGORIES,
    HIDDEN,
    PASSED,
    QUEUE_INFO,
    QUEUE_JUDGE,
    route,
)


class TestDefaultIsNothingHidden:
    """기본값은 '아무것도 자동으로 가리지 않는다'."""

    def test_module_default_is_empty(self):
        assert AUTO_HIDE_CATEGORIES == set()

    @pytest.mark.parametrize(
        "category", ["욕설", "혐오", "성희롱", "위협", "자해", "신상털기", "스팸"]
    )
    def test_harmful_goes_to_queue_when_nothing_enabled(self, category):
        assert route("harmful", category, False) == QUEUE_JUDGE

    def test_explicit_empty_set_hides_nothing(self):
        assert route("harmful", "욕설", False, set()) == QUEUE_JUDGE


class TestChannelSetting:
    """채널이 켠 분류만 가려진다."""

    def test_enabled_category_is_hidden(self):
        assert route("harmful", "스팸", False, {"스팸"}) == HIDDEN

    def test_other_categories_still_queue(self):
        assert route("harmful", "모욕", False, {"스팸"}) == QUEUE_JUDGE

    def test_multiple_categories(self):
        on = {"스팸", "신상털기"}
        assert route("harmful", "스팸", False, on) == HIDDEN
        assert route("harmful", "신상털기", False, on) == HIDDEN
        assert route("harmful", "욕설", False, on) == QUEUE_JUDGE

    def test_enabling_does_not_affect_safe(self):
        """켜둔 분류라도 harmful 이 아니면 가리지 않는다."""
        assert route("safe", "스팸", False, {"스팸"}) == PASSED

    def test_ambiguous_is_never_auto_hidden(self):
        """애매한 것은 정의상 사람이 봐야 한다. 어떤 설정으로도 가려지면 안 된다."""
        assert route("ambiguous", "스팸", False, {"스팸"}) == QUEUE_JUDGE
        assert route("ambiguous", "위협", False, {"위협", "스팸"}) == QUEUE_JUDGE


class TestUnjudged:
    def test_empty_label_goes_to_human(self):
        """판별 실패(빈 label)는 검사받지 않은 댓글이다. 통과시키면 안 된다."""
        assert route("", None, False) == QUEUE_JUDGE

    def test_empty_label_is_not_hidden_even_if_category_enabled(self):
        assert route("", "욕설", False, {"욕설"}) == QUEUE_JUDGE


class TestReviewWordSafetyNet:
    def test_safe_but_flagged_goes_to_info_queue(self):
        """AI 가 정상이라 해도 관리자 검토어에 걸렸으면 보여준다."""
        assert route("safe", "정상", True) == QUEUE_INFO

    def test_safe_and_unflagged_passes(self):
        assert route("safe", "정상", False) == PASSED


class TestAutoHideSet:
    """Channel.auto_hide_set — 저장 형식(콤마 구분)을 집합으로 읽는다."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, set()),
            ("", set()),
            ("스팸", {"스팸"}),
            ("스팸,신상털기", {"스팸", "신상털기"}),
            (" 스팸 , 신상털기 ", {"스팸", "신상털기"}),
            ("스팸,,신상털기", {"스팸", "신상털기"}),
        ],
    )
    def test_parsing(self, raw, expected):
        assert Channel(auto_hide_categories=raw).auto_hide_set == expected
