"""User-reported issues (flagging).

A user flags a title with a problem (missing content, wrong metadata, a broken file, …); admins
triage and resolve. Visibility: a user always sees issues THEY raised; an admin — or a user granted
the ``issues.view_all`` permission — additionally sees everyone's. Only admins change status / add a
resolution note.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..db import get_db
from ..models import Issue, User, Work
from ..permissions import has_permission
from ..schemas import IssueIn, IssueOut, IssueUpdate, _ISSUE_KINDS

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _out(i: Issue, uname: str | None, *, user: User, view_all: bool) -> IssueOut:
    mine = i.user_id == user.id
    return IssueOut(
        id=i.id, work_id=i.work_id, user_id=i.user_id,
        username=(uname if (view_all or mine) else None),   # hide the reporter from a non-privileged peer
        title=i.title, kind=i.kind, description=i.description, status=i.status,
        admin_note=i.admin_note, created_at=i.created_at, updated_at=i.updated_at,
        resolved_at=i.resolved_at, mine=mine, can_resolve=(user.role == "admin"))


@router.post("/issues", response_model=IssueOut)
def create_issue(payload: IssueIn, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> IssueOut:
    """Flag a title with a problem. ``work_id`` is optional; the title is snapshotted so the issue
    stays legible even if the Work is later removed."""
    kind = payload.kind if payload.kind in _ISSUE_KINDS else "other"
    desc = (payload.description or "").strip()[:4000]
    if not desc:
        raise HTTPException(422, "Please describe the issue.")
    title = ""
    if payload.work_id is not None:
        work = db.get(Work, payload.work_id)
        if work is None:
            raise HTTPException(404, "Work not found")
        title = work.title or ""
    issue = Issue(work_id=payload.work_id, user_id=user.id, title=title, kind=kind,
                  description=desc, status="open")
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return _out(issue, user.username, user=user, view_all=True)


@router.get("/issues", response_model=list[IssueOut])
def list_issues(
    status: str | None = Query(None, description="open | resolved"),
    scope: str = Query("all", description="all | mine (privileged viewers can narrow to their own)"),
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> list[IssueOut]:
    """Issues visible to the caller. A normal user sees only their own; an admin or an
    ``issues.view_all`` holder sees everyone's (open issues first, newest first)."""
    view_all = user.role == "admin" or has_permission(db, user, "issues.view_all")
    q = select(Issue, User.username).outerjoin(User, User.id == Issue.user_id)
    if not view_all or scope == "mine":
        q = q.where(Issue.user_id == user.id)
    if status in ("open", "resolved"):
        q = q.where(Issue.status == status)
    # open before resolved, then newest first
    q = q.order_by((Issue.status == "resolved"), Issue.created_at.desc()).limit(500)
    return [_out(i, uname, user=user, view_all=view_all) for i, uname in db.execute(q).all()]


@router.get("/issues/count")
def issues_count(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Open-issue count for a nav/tab badge — scoped to what the caller may see."""
    view_all = user.role == "admin" or has_permission(db, user, "issues.view_all")
    q = select(func.count(Issue.id)).where(Issue.status == "open")
    if not view_all:
        q = q.where(Issue.user_id == user.id)
    return {"open": int(db.scalar(q) or 0), "view_all": view_all}


@router.patch("/issues/{issue_id}", response_model=IssueOut, dependencies=[Depends(require_admin)])
def update_issue(issue_id: int, payload: IssueUpdate, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> IssueOut:
    """Admin: resolve / reopen an issue and (optionally) record a resolution note."""
    i = db.get(Issue, issue_id)
    if i is None:
        raise HTTPException(404, "Issue not found")
    if payload.status == "resolved" and i.status != "resolved":
        i.status = "resolved"
        i.resolved_at = _utcnow()
        i.resolved_by = user.id
    elif payload.status == "open" and i.status != "open":
        i.status = "open"
        i.resolved_at = None
        i.resolved_by = None
    if payload.admin_note is not None:
        i.admin_note = payload.admin_note.strip() or None
    db.commit()
    db.refresh(i)
    uname = db.scalar(select(User.username).where(User.id == i.user_id))
    return _out(i, uname, user=user, view_all=True)


@router.delete("/issues/{issue_id}")
def delete_issue(issue_id: int, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> dict:
    """Withdraw an issue. The reporter may delete their own; an admin may delete any."""
    i = db.get(Issue, issue_id)
    if i is None:
        raise HTTPException(404, "Issue not found")
    if user.role != "admin" and i.user_id != user.id:
        raise HTTPException(403, "Not your issue")
    db.delete(i)
    db.commit()
    return {"deleted": 1}
