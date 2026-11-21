"""Dashboard API - one call with everything the home page needs for the current user."""
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends

from api.auth import get_current_user
import api.approved_tenders  # noqa: F401  (ensures approved_tenders exists)
import api.ai_tenders  # noqa: F401  (ensures ai_tenders exists)

router = APIRouter()

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth.db")
RECENT_LIMIT = 8

_DEADLINE_RE = re.compile(
    r"(\d{1,2}-[A-Za-z]{3}-\d{4})(?:\s+(\d{1,2}:\d{2}))?(?:\s*\(GMT\s*([+-])?(\d{1,2})(?:[.:](\d{2}))?\))?"
)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def parse_ungm_deadline(text: Optional[str]) -> Optional[str]:
    """'20-Nov-2026 13:59 (GMT -4.00)' -> '2026-11-20T17:59:00Z' (UTC). None if unparseable."""
    if not text:
        return None
    m = _DEADLINE_RE.search(text)
    if not m:
        return None
    day, hm, sign, oh, om = m.groups()
    try:
        local = datetime.strptime(f"{day} {hm or '23:59'}", "%d-%b-%Y %H:%M")
    except ValueError:
        return None
    offset = timedelta(hours=int(oh or 0), minutes=int(om or 0))
    if sign == "-":
        offset = -offset
    utc = (local - offset).replace(tzinfo=timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("")
async def get_dashboard(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    conn = get_db_connection()
    try:
        counts_rows = conn.execute("""
            SELECT
              SUM(from_user_id = :u AND bid_status = 'pending')  AS sent_pending,
              SUM(from_user_id = :u AND bid_status = 'approved') AS sent_approved,
              SUM(from_user_id = :u AND bid_status = 'rejected') AS sent_rejected,
              SUM(to_user_id = :u AND bid_status = 'pending')    AS to_review,
              SUM(to_user_id = :u AND bid_status = 'approved')   AS approved_by_me,
              SUM(to_user_id = :u AND bid_status = 'rejected')   AS rejected_by_me
            FROM bid_requests
        """, {"u": uid}).fetchone()
        counts = {k: int(counts_rows[k] or 0) for k in counts_rows.keys()}

        ai_counts = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(status = 'queued') AS queued,
                   SUM(status = 'processing') AS processing,
                   SUM(status = 'done') AS done,
                   SUM(proposal_html IS NOT NULL) AS proposals_edited
            FROM ai_tenders WHERE moved_by_user_id = ?
        """, (uid,)).fetchone()
        counts.update({f"ai_{k}": int(ai_counts[k] or 0) for k in ai_counts.keys()})

        recent_requests = [dict(r) for r in conn.execute("""
            SELECT br.id, br.tender_notice_id, br.bid_status, br.notes,
                   br.requested_at, br.updated_at,
                   COALESCE(NULLIF(br.tender_title, ''), at.title) AS title,
                   CASE WHEN br.from_user_id = :u THEN 'sent' ELSE 'received' END AS direction,
                   CASE WHEN br.from_user_id = :u THEN u2.username ELSE u1.username END AS other_user,
                   at.id AS approved_tender_id
            FROM bid_requests br
            JOIN users u1 ON br.from_user_id = u1.id
            JOIN users u2 ON br.to_user_id = u2.id
            LEFT JOIN approved_tenders at ON at.bid_request_id = br.id
            WHERE br.from_user_id = :u OR br.to_user_id = :u
            ORDER BY br.updated_at DESC, br.id DESC
            LIMIT :limit
        """, {"u": uid, "limit": RECENT_LIMIT})]

        approved = [dict(r) for r in conn.execute("""
            SELECT at.id, at.tender_notice_id, at.title, at.agency, at.deadline,
                   at.notice_status, at.approved_at, ai.id AS ai_tender_id
            FROM approved_tenders at
            LEFT JOIN ai_tenders ai ON ai.approved_tender_id = at.id
            WHERE at.requested_by_user_id = :u OR at.approved_by_user_id = :u
        """, {"u": uid})]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for t in approved:
            t["deadline_utc"] = parse_ungm_deadline(t["deadline"])
        upcoming = sorted(
            (t for t in approved if t["deadline_utc"] and t["deadline_utc"] >= now),
            key=lambda t: t["deadline_utc"],
        )[:RECENT_LIMIT]
        counts["approved_tenders"] = len(approved)
        counts["approved_open"] = sum(1 for t in approved if t["deadline_utc"] and t["deadline_utc"] >= now)

        proposals = [dict(r) for r in conn.execute("""
            SELECT id, tender_notice_id, title, agency, deadline, status,
                   COALESCE(proposal_source, 'template') AS proposal_source,
                   proposal_updated_at, moved_at
            FROM ai_tenders WHERE moved_by_user_id = ?
            ORDER BY COALESCE(proposal_updated_at, moved_at) DESC
            LIMIT ?
        """, (uid, RECENT_LIMIT))]
        for p in proposals:
            p["deadline_utc"] = parse_ungm_deadline(p["deadline"])
    finally:
        conn.close()

    return {
        "user": {
            "id": uid,
            "username": current_user["username"],
            "full_name": current_user["full_name"],
        },
        "counts": counts,
        "recent_requests": recent_requests,
        "upcoming_deadlines": upcoming,
        "proposals": proposals,
    }
