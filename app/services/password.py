"""비밀번호 저장과 확인.

원문은 절대 저장하지 않는다. 소금(salt) 을 섞어 scrypt 로 늘인 값만 남긴다.
같은 비밀번호라도 소금이 달라 저장값이 다르고, 저장값에서 원문으로 되돌릴 수 없다.

scrypt 는 파이썬 표준 hashlib 에 들어 있어서 라이브러리를 더 깔지 않는다.
저장 형식: ``scrypt$<n>$<salt b64>$<hash b64>`` — 나중에 강도(n)를 올려도
옛 값을 그대로 검증할 수 있게 매개변수를 값 안에 같이 적는다.
"""

import base64
import hashlib
import hmac
import secrets

_N = 2**14          # CPU/메모리 비용. 2^14 면 로그인 한 번에 수십 ms — 사람은 못 느끼고
_R, _P = 8, 1       # 무작위 대입은 느려진다.
_SALT_BYTES = 16
_KEY_LEN = 32

MIN_LENGTH = 8


def hash_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"비밀번호는 {MIN_LENGTH}자 이상")
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_KEY_LEN)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${b64(salt)}${b64(key)}"


def verify_password(password: str, stored: str | None) -> bool:
    """틀려도, 저장값이 없어도, 형식이 깨져도 False. 예외를 밖으로 내지 않는다."""
    if not stored:
        return False
    try:
        scheme, n, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        key = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=_R, p=_P, dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    # 길이·내용을 한 번에 비교해서 걸린 시간으로 앞글자를 알아내는 공격을 막는다.
    return hmac.compare_digest(key, expected)
