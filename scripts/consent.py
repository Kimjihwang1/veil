"""AI 자동 판별 동의를 확인하거나 바꾼다.

YouTube API 정책상 채널을 연동할 때 AI 자동 모더레이션에 대한 관리자의
명시 동의가 필요하다. 동의가 없으면 판별 스크립트가 멈춘다.

평소에는 관리자 화면에서 누르면 되고, 이 스크립트는 개발 중에 쓴다.

    python -m scripts.consent                        지금 상태를 본다
    python -m scripts.consent --channel 4 --agree    동의 처리
    python -m scripts.consent --channel 4 --revoke   철회
"""

import argparse
import asyncio
import sys
from datetime import datetime

from sqlalchemy import select

from app.db.models import Channel
from app.db.session import AsyncSessionLocal, engine


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int)
    쌍 = ap.add_mutually_exclusive_group()
    쌍.add_argument("--agree", action="store_true", help="동의 처리")
    쌍.add_argument("--revoke", action="store_true", help="동의 철회")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        if args.channel and (args.agree or args.revoke):
            ch = await db.get(Channel, args.channel)
            if ch is None:
                raise SystemExit(f"[FAIL] 채널 {args.channel} 이 없다.")
            ch.ai_consent_agreed = args.agree
            ch.ai_consent_at = datetime.utcnow() if args.agree else None
            await db.commit()
            print(f"[OK] 채널 {ch.id} · {'동의' if args.agree else '철회'}")

        rows = (await db.execute(select(Channel).order_by(Channel.id))).scalars().all()
        print(f"\n{'id':>3}  {'채널':<22}{'동의':<8}{'동의 시각'}")
        print("─" * 60)
        for c in rows:
            표 = "예" if c.ai_consent_agreed else "아니오"
            때 = c.ai_consent_at.strftime("%Y-%m-%d %H:%M") if c.ai_consent_at else "-"
            print(f"{c.id:>3}  {str(c.channel_title)[:20]:<22}{표:<8}{때}")

        if any(not c.ai_consent_agreed for c in rows):
            print("\n동의하지 않은 채널은 판별이 돌지 않는다:")
            print("  python -m scripts.consent --channel <id> --agree")

    await engine.dispose()


asyncio.run(main())
