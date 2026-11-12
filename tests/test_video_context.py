"""영상 맥락이 판별에 실려 가는가.

한 채널이 여러 성격의 영상을 올린다. 진용진은 머니게임도 올리고 길거리
실험도 올린다. 그래서 '남녀가 팀으로 갈려 싸운다' 같은 설명은 채널이 아니라
영상에 붙어야 한다 — 채널에 박아두면 다른 영상에 틀린 정보가 들어간다.
"""

import pytest

from app.services.llm import VideoContext
from app.services.pipeline import process



class FakeJudge:
    """LLM 대신. 실제로 어떤 문장이 전달됐는지 받아 적는다."""

    def __init__(self):
        self.받은것 = []

    async def judge(self, text, parent_text=None, video=None):
        user = text
        if parent_text:
            user = f"[부모 댓글] {parent_text}\n[판단할 댓글] {user}"
        if video and (head := video.as_prompt()):
            user = f"{head}\n{user}"
        self.받은것.append(user)

        class V:
            label, category, reason, error = "safe", "정상", "", None

        return V()


class TestVideoContext:
    def test_제목만_있으면_제목만(self):
        v = VideoContext(title="머니게임 Ep5")
        assert v.as_prompt() == "[이 영상] 제목: 머니게임 Ep5"

    def test_메모가_있으면_같이(self):
        v = VideoContext(title="머니게임 Ep5", memo="남녀가 팀으로 갈린다")
        assert "머니게임 Ep5" in v.as_prompt()
        assert "남녀가 팀으로 갈린다" in v.as_prompt()

    def test_아무것도_없으면_빈문자열(self):
        """영상 정보가 없어도 판별은 돌아야 한다."""
        assert VideoContext().as_prompt() == ""
        assert VideoContext(title=None, memo=None).as_prompt() == ""

    def test_공백만_있으면_빈문자열(self):
        assert VideoContext(title="  ").as_prompt() == ""


@pytest.mark.asyncio
class TestPipeline:
    async def test_영상정보가_LLM까지_간다(self):
        j = FakeJudge()
        await process(
            [], j, "여자들때매 박준형이 뭍히다니..",
            video=VideoContext(title="머니게임 Ep5", memo="남녀가 팀으로 갈려 대립"),
        )
        보낸것 = j.받은것[0]
        assert "머니게임 Ep5" in 보낸것
        assert "남녀가 팀으로 갈려 대립" in 보낸것
        assert "여자들때매" in 보낸것

    async def test_영상정보가_없어도_돈다(self):
        j = FakeJudge()
        r = await process([], j, "댓글 본문")
        assert r.destination is not None
        assert j.받은것[0] == "댓글 본문"

    async def test_부모댓글과_같이_간다(self):
        """답글이면 부모도, 영상도 둘 다 실려야 한다."""
        j = FakeJudge()
        await process(
            [], j, "그만해라 진짜",
            parent_text="4번이 또 저러네",
            video=VideoContext(title="머니게임 Ep5"),
        )
        보낸것 = j.받은것[0]
        assert "머니게임 Ep5" in 보낸것
        assert "4번이 또 저러네" in 보낸것
        assert "그만해라 진짜" in 보낸것

    async def test_영상마다_다른_맥락이_간다(self):
        """같은 채널이라도 영상이 다르면 다른 설명이 가야 한다."""
        j = FakeJudge()
        await process([], j, "댓글1", video=VideoContext(title="머니게임 Ep5"))
        await process([], j, "댓글2", video=VideoContext(title="지하철 실험"))

        assert "머니게임 Ep5" in j.받은것[0]
        assert "지하철 실험" not in j.받은것[0]
        assert "지하철 실험" in j.받은것[1]
        assert "머니게임 Ep5" not in j.받은것[1]
