"""ORM 모델의 공통 베이스.

CLAUDE.md 기준으로 스키마는 아직 초안 단계라 모델 클래스는 아직 없다.
댓글 수집기(F_R_107)부터 만들면서 이 Base를 상속해 테이블별로 추가할 것.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
