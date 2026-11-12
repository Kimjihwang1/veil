"""증거 보관함 — 고소용으로 고정해 둔 댓글 (2026-11-09).

  GET    /channels/{id}/evidence          목록
  POST   /comments/{id}/evidence          [증거로 저장]
  DELETE /evidence/{id}                   보관함에서 빼기
  GET    /channels/{id}/evidence.csv      엑셀로 여는 CSV
  GET    /channels/{id}/evidence/report   인쇄용 화면 (브라우저에서 PDF 로 저장)

PDF 를 서버에서 만들지 않는 이유: 한글 글꼴을 담은 PDF 라이브러리를 서버 이미지에
더해야 한다. 브라우저의 'PDF 로 저장' 이 같은 일을 하고, 화면과 출력이 어긋날 일도 없다.

무엇을 왜 남기는지는 services/evidence.py, 왜 30일인지는 models.Evidence 주석 참고.
"""

import csv
import io
from datetime import UTC, datetime, timedelta
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import current_user, my_workspace_ids, require_channel, require_comment
from app.db.models import Channel, Comment, Evidence, User
from app.db.session import get_db
from app.services.evidence import author_url, capture, comment_url
from app.services.severity import NAMES

router = APIRouter(tags=["evidence"])

KST = timedelta(hours=9)


class EvidenceItem(BaseModel):
    id: int
    comment_id: int
    youtube_comment_id: str
    comment_url: str | None = Field(None, description="그 댓글로 바로 가는 유튜브 주소")
    video_id: str | None
    video_title: str | None
    author_name: str | None
    author_channel_id: str | None
    author_url: str | None = Field(None, description="작성자 채널 주소. 닉네임과 달리 안 바뀐다")
    content: str
    parent_content: str | None
    published_at: datetime | None = Field(None, description="유튜브에 달린 시각 (UTC)")
    content_updated_at: datetime | None
    label: str | None
    category: str | None
    severity: int | None
    reasoning: str | None
    source: str = Field(..., description="auto=판별 즉시 자동 / manual=관리자가 저장")
    saved_by: str | None
    note: str | None
    content_hash: str
    captured_at: datetime
    expires_at: datetime | None
    days_left: int | None = Field(None, description="파기까지 남은 날. 그 안에 내려받아야 한다")


class SaveIn(BaseModel):
    note: str | None = Field(None, max_length=500, description="왜 남기는지 메모")


def _item(e: Evidence, now: datetime) -> EvidenceItem:
    left = None
    if e.expires_at:
        left = max((e.expires_at - now).days, 0)
    return EvidenceItem(
        id=e.id, comment_id=e.comment_id, youtube_comment_id=e.youtube_comment_id,
        comment_url=comment_url(e.video_id, e.youtube_comment_id),
        video_id=e.video_id, video_title=e.video_title,
        author_name=e.author_name, author_channel_id=e.author_channel_id,
        author_url=author_url(e.author_channel_id),
        content=e.content, parent_content=e.parent_content,
        published_at=e.published_at, content_updated_at=e.content_updated_at,
        label=e.label, category=e.category, severity=e.severity, reasoning=e.reasoning,
        source=e.source, saved_by=e.saved_by, note=e.note, content_hash=e.content_hash,
        captured_at=e.captured_at, expires_at=e.expires_at, days_left=left,
    )


async def _list(db: AsyncSession, channel_id: int) -> list[Evidence]:
    # 작성자별로 모아 보이게 정렬한다. 같은 사람이 거듭 단 것은 한데 있어야 반복이 보인다.
    return list(
        (
            await db.execute(
                select(Evidence)
                .where(Evidence.channel_id == channel_id)
                .order_by(Evidence.author_channel_id, Evidence.published_at, Evidence.id)
            )
        ).scalars().all()
    )


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@router.get(
    "/channels/{channel_id}/evidence",
    response_model=list[EvidenceItem],
    summary="증거 보관함",
)
async def list_evidence(
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    now = _utcnow()
    return [_item(e, now) for e in await _list(db, channel.id)]


@router.post(
    "/comments/{comment_id}/evidence",
    response_model=EvidenceItem,
    summary="증거로 저장 (지금 모습 그대로 고정)",
)
async def save_evidence(
    payload: SaveIn | None = None,
    comment: Comment = Depends(require_comment),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """같은 원문이 이미 있으면 새로 만들지 않고 그것을 돌려준다 (자동 저장된 것 포함)."""
    note = (payload.note or "").strip() or None if payload else None
    ids = await capture(db, [comment.id], source="manual", saved_by=user.email, note=note)
    await db.commit()
    if ids:
        e = await db.get(Evidence, ids[0])
    else:
        # 이미 같은 원문이 있다. 가장 최근 것을 준다.
        e = (
            await db.execute(
                select(Evidence)
                .where(Evidence.comment_id == comment.id)
                .order_by(Evidence.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if e is None:
            # capture 가 건너뛰는 건 원문이 이미 파기된 댓글뿐이다.
            raise HTTPException(
                status.HTTP_410_GONE, "보관기한이 지나 원문이 지워진 댓글이라 증거로 남길 수 없어요."
            )
    return _item(e, _utcnow())


@router.delete(
    "/evidence/{evidence_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="보관함에서 빼기",
)
async def delete_evidence(
    evidence_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    e = await db.get(Evidence, evidence_id)
    # 남의 것은 없는 것처럼 404. require_channel 과 같은 규칙이다.
    channel = await db.get(Channel, e.channel_id) if e else None
    if (
        e is None or channel is None or channel.workspace_id is None
        or channel.workspace_id not in await my_workspace_ids(db, user)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"증거 {evidence_id} 없음")
    await db.delete(e)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _kst(t: datetime | None) -> str:
    return (t + KST).strftime("%Y-%m-%d %H:%M:%S") if t else ""


# 엑셀은 = + - @ 로 시작하는 칸을 수식으로 읽는다. 댓글이 '@닉네임' 으로 시작하는 일이
# 흔하고, 악의적인 수식이 실행될 수도 있다. 앞에 ' 를 붙여 글자로 읽게 한다.
# 원문 그대로가 필요하면 인쇄용 화면이나 SHA-256 계산에는 원문을 쓴다.
def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


COLUMNS = [
    "번호", "작성자 닉네임", "작성자 채널 주소", "댓글 원문", "작성 일시(한국)", "수정 일시(한국)",
    "댓글 주소", "영상 제목", "영상 주소", "답글 단 원댓글", "AI 분류", "AI 위험도", "AI 근거",
    "저장 방식", "저장한 사람", "메모", "저장 시각(한국)", "파기 예정(한국)", "SHA-256",
]


@router.get("/channels/{channel_id}/evidence.csv", summary="증거 내려받기 (엑셀용 CSV)")
async def export_csv(
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for i, e in enumerate(await _list(db, channel.id), 1):
        w.writerow([_cell(v) for v in [
            i, e.author_name, author_url(e.author_channel_id), e.content,
            _kst(e.published_at), _kst(e.content_updated_at),
            comment_url(e.video_id, e.youtube_comment_id), e.video_title,
            f"https://www.youtube.com/watch?v={e.video_id}" if e.video_id else "",
            e.parent_content, e.category,
            f"{e.severity} {NAMES[e.severity]}" if e.severity else "",
            e.reasoning, "자동" if e.source == "auto" else "수동", e.saved_by, e.note,
            _kst(e.captured_at), _kst(e.expires_at), e.content_hash,
        ]])
    stamp = (_utcnow() + KST).strftime("%Y%m%d_%H%M")
    # BOM 이 없으면 엑셀이 한글을 깨뜨린다.
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="evidence_{channel.id}_{stamp}.csv"'},
    )


@router.get(
    "/channels/{channel_id}/evidence/report",
    response_class=HTMLResponse,
    summary="증거 인쇄용 화면 (브라우저에서 PDF 로 저장)",
)
async def report(
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    rows = await _list(db, channel.id)
    now = _utcnow()
    e_ = escape  # 모든 값은 사용자가 쓴 글이다. 빠짐없이 escape 한다.

    def link(url: str | None) -> str:
        return f'<a href="{e_(url)}">{e_(url)}</a>' if url else "-"

    items = []
    for i, e in enumerate(rows, 1):
        meta = [
            ("작성자", f"{e_(e.author_name or '-')} · {link(author_url(e.author_channel_id))}"),
            ("작성 일시", e_(_kst(e.published_at)) + (f" (수정 {e_(_kst(e.content_updated_at))})"
                                                      if e.content_updated_at and e.content_updated_at != e.published_at else "")),
            ("댓글 주소", link(comment_url(e.video_id, e.youtube_comment_id))),
            ("영상", e_(e.video_title or e.video_id or "-")),
        ]
        if e.parent_content:
            meta.append(("답글 단 원댓글", e_(e.parent_content)))
        meta += [
            ("AI 참고 의견", e_(" · ".join(x for x in [
                e.category or "", f"위험도 {e.severity} {NAMES[e.severity]}" if e.severity else "",
                e.reasoning or ""] if x)) or "-"),
            ("저장", e_(f"{_kst(e.captured_at)} · {'판별 즉시 자동' if e.source == 'auto' else '관리자 ' + (e.saved_by or '')}")
             + (f" · 메모: {e_(e.note)}" if e.note else "")),
            ("SHA-256", f"<code>{e_(e.content_hash)}</code>"),
        ]
        items.append(
            f'<section><h2>{i}</h2><blockquote>{e_(e.content)}</blockquote><dl>'
            + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in meta)
            + "</dl></section>"
        )

    body = "".join(items) or "<p>보관한 증거가 없습니다.</p>"
    first_expiry = min((e.expires_at for e in rows if e.expires_at), default=None)
    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>증거 목록 · {e_(channel.channel_title or '')}</title>
<style>
@page {{ size: A4; margin: 15mm; }}
body {{ font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; color: #111; margin: 24px; font-size: 13px; line-height: 1.6; }}
header {{ border-bottom: 2px solid #111; padding-bottom: 8px; margin-bottom: 16px; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
.sub {{ color: #444; }}
.note {{ background: #f4f4f5; border-radius: 6px; padding: 10px 12px; margin: 12px 0 18px; color: #333; }}
section {{ break-inside: avoid; border: 1px solid #ddd; border-radius: 6px; padding: 12px 14px; margin-bottom: 12px; }}
h2 {{ font-size: 12px; color: #666; margin: 0 0 6px; }}
blockquote {{ margin: 0 0 10px; padding: 8px 12px; background: #fafafa; border-left: 3px solid #111; font-size: 15px; white-space: pre-wrap; word-break: break-all; }}
dl {{ display: grid; grid-template-columns: 110px 1fr; gap: 3px 10px; margin: 0; }}
dt {{ color: #666; }} dd {{ margin: 0; word-break: break-all; }}
code {{ font-size: 11px; }}
a {{ color: #1d4ed8; }}
.print {{ position: fixed; right: 24px; top: 20px; padding: 8px 14px; border: 0; border-radius: 6px; background: #111; color: #fff; font-size: 13px; cursor: pointer; }}
@media print {{ .print {{ display: none; }} body {{ margin: 0; }} }}
</style></head><body>
<button class="print" onclick="window.print()">인쇄 · PDF로 저장</button>
<header><h1>유튜브 댓글 증거 목록</h1>
<div class="sub">채널 {e_(channel.channel_title or '')} · {len(rows)}건 · 출력 {e_(_kst(now))} (한국 시간)</div></header>
<div class="note">Veil 이 YouTube Data API 로 수집하는 순간 고정해 둔 기록입니다. 작성 일시는 유튜브가 알려준 값입니다.
SHA-256 은 댓글 ID · 작성자 채널 ID · 작성 일시 · 원문으로 계산했습니다.
AI 의견은 관리자가 고를 때 참고한 것으로, 법적 판단이 아닙니다.
유튜브 정책상 Veil 은 이 기록을 30일까지만 보관합니다{f' (가장 이른 파기 {e_(_kst(first_expiry))})' if first_expiry else ''}.
고소에 쓰려면 이 문서를 PDF 로 저장해 두고, 가능하면 실제 화면 캡처도 함께 남겨 두세요.</div>
{body}
</body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
