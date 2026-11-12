"""사람 라벨링 (/label 화면이 쓰는 API).

구글 로그인은 없다. 팀 내부용이고, 이름을 고르는 게 곧 신원이다. 대신 서버에
올릴 때는 팀 비밀번호 하나(LABEL_KEY)를 X-Label-Key 헤더로 받는다. 주소를
아는 사람 누구나 실제 댓글을 보는 걸 막는 최소 장치다. 비어 있으면 검사 안 함.

절대 지키는 것 하나: **AI 판정을 내보내지 않는다.** 이 모듈은 risk_assessments
를 import 도 하지 않는다. 사람이 AI 답을 보면 무의식적으로 따라가고, 그렇게
만든 답지로 AI 를 채점하면 점수가 부풀려진다.
"""

from datetime import UTC, datetime
from typing import Literal

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Channel, Comment, LabelTask, Video
from app.db.session import get_db


async def require_label_key(x_label_key: str | None = Header(default=None)) -> None:
    """LABEL_KEY 가 설정돼 있으면 헤더가 그것과 같아야 한다. 아니면 401."""
    expected = get_settings().label_key
    if not expected:
        return
    if not (x_label_key and hmac.compare_digest(x_label_key, expected)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "라벨링 비밀번호가 틀립니다")


router = APIRouter(
    prefix="/label", tags=["label"], dependencies=[Depends(require_label_key)]
)

Label = Literal["hide", "keep", "unsure"]


class Labeler(BaseModel):
    name: str
    done: int
    total: int


class TaskComment(BaseModel):
    """사람이 보는 것. 관리자 화면과 같은 정보 — 영상 제목, 부모 댓글, 본문.
    AI 가 보는 것과 같아야 공평한 비교가 된다. AI 판정 필드는 없다."""

    content: str
    parent_content: str | None
    video_title: str | None
    channel_title: str | None


class NextTask(BaseModel):
    task_id: int | None = Field(None, description="없으면 다 끝난 것")
    position: int
    done: int
    total: int
    comment: TaskComment | None = None
    # 직전에 답한 것. 잘못 눌렀을 때 되돌리는 용도.
    last_task_id: int | None = None


class Submit(BaseModel):
    label: Label
    seconds: float | None = Field(None, ge=0, le=3600, description="화면에 뜬 뒤 버튼까지")


class Progress(BaseModel):
    done: int
    total: int


@router.get("/labelers", response_model=list[Labeler], summary="라벨러와 진행률")
async def labelers(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(
                LabelTask.labeler,
                func.count().label("total"),
                func.count(LabelTask.label).label("done"),
            )
            .group_by(LabelTask.labeler)
            .order_by(LabelTask.labeler)
        )
    ).all()
    return [Labeler(name=n, done=d, total=t) for n, t, d in rows]


async def _progress(db, who: str) -> tuple[int, int]:
    total, done = (
        await db.execute(
            select(func.count(), func.count(LabelTask.label)).where(
                LabelTask.labeler == who
            )
        )
    ).one()
    return done, total


@router.get("/next", response_model=NextTask, summary="다음 댓글")
async def next_task(who: str = Query(..., min_length=1), db: AsyncSession = Depends(get_db)):
    done, total = await _progress(db, who)
    if total == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"'{who}' 에게 배정된 게 없습니다")

    last = (
        await db.execute(
            select(LabelTask.id)
            .where(LabelTask.labeler == who, LabelTask.label.isnot(None))
            .order_by(LabelTask.labeled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    parent = Comment.__table__.alias("parent")
    row = (
        await db.execute(
            select(
                LabelTask,
                Comment.content,
                parent.c.content.label("parent_content"),
                Video.title,
                Channel.channel_title,
            )
            .join(Comment, Comment.id == LabelTask.comment_id)
            .outerjoin(parent, parent.c.youtube_comment_id == Comment.parent_comment_id)
            .outerjoin(Video, Video.video_id == Comment.video_id)
            .outerjoin(Channel, Channel.id == Comment.channel_id)
            .where(LabelTask.labeler == who, LabelTask.label.is_(None))
            .order_by(LabelTask.position)
            .limit(1)
        )
    ).first()

    if row is None:
        return NextTask(task_id=None, position=total, done=done, total=total, last_task_id=last)

    task, content, parent_content, video_title, channel_title = row
    return NextTask(
        task_id=task.id,
        position=task.position,
        done=done,
        total=total,
        comment=TaskComment(
            content=content,
            parent_content=parent_content,
            video_title=video_title,
            channel_title=channel_title,
        ),
        last_task_id=last,
    )


@router.post("/{task_id}", response_model=Progress, summary="답 제출")
async def submit(task_id: int, payload: Submit, db: AsyncSession = Depends(get_db)):
    task = await db.get(LabelTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "없는 항목")
    task.label = payload.label
    task.labeled_at = datetime.now(UTC).replace(tzinfo=None)
    task.seconds = payload.seconds
    await db.commit()
    done, total = await _progress(db, task.labeler)
    return Progress(done=done, total=total)


@router.post("/{task_id}/undo", response_model=Progress, summary="직전 답 취소")
async def undo(task_id: int, db: AsyncSession = Depends(get_db)):
    """잘못 눌렀을 때. 답을 지우면 그 항목이 다시 '다음' 으로 온다."""
    task = await db.get(LabelTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "없는 항목")
    task.label = None
    task.labeled_at = None
    task.seconds = None
    await db.commit()
    done, total = await _progress(db, task.labeler)
    return Progress(done=done, total=total)
