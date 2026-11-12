"""채널 하나의 최신 영상들을 훑어 댓글을 모은다.

    python -m scripts.collect_channel @채널핸들 [영상수] [영상당최대댓글]
    python -m scripts.collect_channel UCxxxxxxxxxxxxxxxxxxxxxx 20

같은 채널을 매일 돌려도 안전하다 (upsert). 새 영상·새 댓글만 늘어난다.
쿼터는 대략 '영상 수 x 3' units 정도 든다. 일일 한도 10,000 이라 여유롭다.
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Channel
from app.db.session import AsyncSessionLocal, engine
from app.services.collector import YouTubeApiError, YouTubeCollector, save_comments

# 안전장치. 사고로 무한정 도는 걸 막는다.
QUOTA_CAP = 2000


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        raise SystemExit("사용법: python -m scripts.collect_channel <@핸들|채널ID|URL> [영상수] [영상당댓글]")

    target = sys.argv[1]
    n_videos = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    per_video = int(sys.argv[3]) if len(sys.argv) > 3 else None

    key = get_settings().youtube_api_key
    if not key:
        raise SystemExit("[FAIL] .env에 YOUTUBE_API_KEY가 없다.")

    total_saved = 0
    async with YouTubeCollector(key) as yt:
        try:
            ch = await yt.resolve_channel(target)
            videos = await yt.channel_videos(ch, limit=n_videos)
        except YouTubeApiError as e:
            raise SystemExit(f"[FAIL] {e}")

        print(f"채널: {ch['snippet']['title']}  ({ch['id']})")
        print(f"영상 {len(videos)}개 수집 시작\n")

        async with AsyncSessionLocal() as db:
            channel = (
                await db.execute(
                    select(Channel).where(Channel.youtube_channel_id == ch["id"])
                )
            ).scalar_one_or_none()
            if channel is None:
                channel = Channel(
                    youtube_channel_id=ch["id"],
                    channel_title=ch["snippet"]["title"],
                )
                db.add(channel)
                await db.flush()
            channel_pk = channel.id

            for i, v in enumerate(videos, 1):
                if yt.stats.quota_units > QUOTA_CAP:
                    print(f"\n[중단] 쿼터 상한 {QUOTA_CAP} 도달")
                    break
                try:
                    comments = await yt.collect(v["video_id"], max_comments=per_video)
                except YouTubeApiError as e:
                    # 영상 하나가 실패해도 나머지는 계속 간다.
                    print(f"  [{i}/{len(videos)}] 실패 {v['title'][:28]} — {e.reason}")
                    continue

                saved = await save_comments(db, channel_pk, comments)
                total_saved += saved
                tops = sum(1 for c in comments if not c.is_reply)
                note = " (댓글 꺼짐)" if not comments else ""
                print(
                    f"  [{i}/{len(videos)}] {v['title'][:30]:<32}"
                    f" {saved:>4}건 (최상위 {tops} / 답글 {saved - tops}){note}"
                )

    st = yt.stats
    print(f"\n[OK] 총 {total_saved}건 저장 (channel_id={channel_pk})")
    print(f"     쿼터 {st.quota_units} units / 답글 보충 {st.replies_supplemented}건")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
