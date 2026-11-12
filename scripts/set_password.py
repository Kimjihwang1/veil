"""기존 계정에 비밀번호를 붙인다 (개발·운영자용).

    python -m scripts.set_password --email a@b.com

Google 로만 들어오던 계정(outlieraica 등)을 이메일+비밀번호로도 들어올 수 있게
한다. 첫 화면에서 Google 버튼을 뺐기 때문에(2026-09-27, 로그인과 채널 연결의
구글 화면이 같아 헷갈린다는 결정) 그 계정들은 이 스크립트로 비밀번호를 받아야 한다.

비밀번호는 화면에 안 보이게 입력받고(getpass), 해시만 저장한다. 웹 API 로는
못 하게 일부러 스크립트로만 둔다 — 남의 계정에 비밀번호를 붙이는 길이 되니까.
"""

import argparse
import asyncio
import sys
from getpass import getpass

from sqlalchemy import select

from app.db.models import User
from app.db.session import AsyncSessionLocal
from app.services.password import MIN_LENGTH, hash_password


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    args = ap.parse_args()
    email = args.email.strip().lower()

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            print(f"[FAIL] 없는 계정: {email}")
            return 1
        print(f"계정: {user.email} ({user.name}) · 구글 연결 {'있음' if user.google_id else '없음'} · "
              f"비밀번호 {'있음 → 바꿈' if user.password_hash else '없음 → 새로 만듦'}")
        pw = getpass(f"새 비밀번호 ({MIN_LENGTH}자 이상): ")
        if len(pw) < MIN_LENGTH:
            print("[FAIL] 너무 짧다")
            return 1
        if pw != getpass("한 번 더: "):
            print("[FAIL] 둘이 다르다")
            return 1
        user.password_hash = hash_password(pw)
        await db.commit()
    print("[OK] 저장했다. 첫 화면에서 이메일+비밀번호로 로그인하면 된다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
