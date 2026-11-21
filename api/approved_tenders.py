"""Approved Tenders API.

When Bernard approves a bid request, the full UNGM notice
(https://www.ungm.org/Public/Notice/{notice_id}) is fetched, parsed and stored in the
`approved_tenders` table. `ai_context` holds the whole notice as one plain-text document,
ready for AI processing. Kowsalya sees these tenders in the "Approved Tenders" module.
"""
import json
import os
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import get_current_user
from api.ungm_notice import NoticeFetchError, fetch_notice
import api.ai_tenders  # noqa: F401  (creates ai_tenders, used in SELECT_SQL)

router = APIRouter()

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth.db")

JSON_FIELDS = ("contacts", "links", "unspsc_codes")
DETAIL_FIELDS = (
    "title", "agency", "notice_type", "reference", "countries", "registration_level",
    "published", "deadline", "notice_status", "description",
)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_approved_tenders_table():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approved_tenders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bid_request_id INTEGER UNIQUE NOT NULL,
            tender_notice_id TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            agency TEXT,
            notice_type TEXT,
            reference TEXT,
            countries TEXT,
            registration_level TEXT,
            published TEXT,
            deadline TEXT,
            notice_status TEXT,
            description TEXT,
            contacts TEXT,          -- JSON list of {label: value}
            links TEXT,             -- JSON list of {url, description}
            unspsc_codes TEXT,      -- JSON list of strings
            ai_context TEXT,        -- full notice as plain text, for AI processing
            notes TEXT,
            requested_by_user_id INTEGER NOT NULL,
            approved_by_user_id INTEGER NOT NULL,
            approved_at TIMESTAMP,
            fetch_status TEXT DEFAULT 'pending',   -- pending | ok | error
            fetch_error TEXT,
            fetched_at TIMESTAMP,
            FOREIGN KEY (bid_request_id) REFERENCES bid_requests (id),
            FOREIGN KEY (requested_by_user_id) REFERENCES users (id),
            FOREIGN KEY (approved_by_user_id) REFERENCES users (id),
            CHECK (fetch_status IN ('pending', 'ok', 'error'))
        )
    """)
    conn.commit()
    conn.close()


def build_ai_context(d: dict) -> str:
    lines = [
        f"Title: {d.get('title', '')}",
        f"Agency: {d.get('agency', '')}",
        f"Notice type: {d.get('notice_type', '')}",
        f"Reference: {d.get('reference', '')}",
        f"UNGM notice ID: {d.get('notice_id', '')}",
        f"Beneficiary countries: {d.get('countries', '')}",
        f"Registration level: {d.get('registration_level', '')}",
        f"Published: {d.get('published', '')}",
        f"Deadline: {d.get('deadline', '')} ({d.get('notice_status', '')})",
        f"URL: {d.get('url', '')}",
        "",
        "Description:",
        d.get("description", ""),
    ]
    if d.get("contacts"):
        lines += ["", "Contacts:"]
        for c in d["contacts"]:
            lines.append("- " + "; ".join(f"{k}: {v}" for k, v in c.items()))
    if d.get("links"):
        lines += ["", "Links:"]
        for link in d["links"]:
            lines.append(f"- {link['description']}: {link['url']}")
    if d.get("unspsc_codes"):
        lines += ["", "UNSPSC codes:"] + [f"- {code}" for code in d["unspsc_codes"]]
    return "\n".join(lines).strip()


def _ensure_row(conn: sqlite3.Connection, bid_request_id: int) -> Optional[sqlite3.Row]:
    """Create the approved_tenders row for an approved bid request if missing."""
    br = conn.execute(
        "SELECT * FROM bid_requests WHERE id = ? AND bid_status = 'approved'", (bid_request_id,)
    ).fetchone()
    if not br:
        return None
    conn.execute("""
        INSERT OR IGNORE INTO approved_tenders (
            bid_request_id, tender_notice_id, url, notes,
            requested_by_user_id, approved_by_user_id, approved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        br["id"], br["tender_notice_id"],
        f"https://www.ungm.org/Public/Notice/{br['tender_notice_id']}",
        br["notes"], br["from_user_id"], br["to_user_id"], br["updated_at"],
    ))
    conn.commit()
    return br


async def store_approved_tender(bid_request_id: int, force: bool = False) -> None:
    """Fetch the UNGM notice for an approved bid request and save it. Never raises on fetch errors."""
    conn = get_db_connection()
    br = _ensure_row(conn, bid_request_id)
    if not br:
        conn.close()
        return
    row = conn.execute(
        "SELECT fetch_status FROM approved_tenders WHERE bid_request_id = ?", (bid_request_id,)
    ).fetchone()
    conn.close()
    if row["fetch_status"] == "ok" and not force:
        return

    try:
        data = await fetch_notice(br["tender_notice_id"])
    except NoticeFetchError as exc:
        conn = get_db_connection()
        conn.execute("""
            UPDATE approved_tenders
            SET fetch_status = 'error', fetch_error = ?, fetched_at = CURRENT_TIMESTAMP
            WHERE bid_request_id = ?
        """, (str(exc), bid_request_id))
        conn.commit()
        conn.close()
        return

    conn = get_db_connection()
    conn.execute(f"""
        UPDATE approved_tenders SET
            {", ".join(f"{f} = ?" for f in DETAIL_FIELDS)},
            url = ?, contacts = ?, links = ?, unspsc_codes = ?, ai_context = ?,
            fetch_status = 'ok', fetch_error = NULL, fetched_at = CURRENT_TIMESTAMP
        WHERE bid_request_id = ?
    """, (
        *[data.get(f, "") for f in DETAIL_FIELDS],
        data["url"],
        json.dumps(data["contacts"], ensure_ascii=False),
        json.dumps(data["links"], ensure_ascii=False),
        json.dumps(data["unspsc_codes"], ensure_ascii=False),
        build_ai_context(data),
        bid_request_id,
    ))
    conn.commit()
    conn.close()


async def sync_for_user(user_id: int) -> None:
    """Store any approved bid requests involving this user that are missing or failed."""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT br.id FROM bid_requests br
        LEFT JOIN approved_tenders at ON at.bid_request_id = br.id
        WHERE br.bid_status = 'approved'
          AND (br.from_user_id = ? OR br.to_user_id = ?)
          AND (at.id IS NULL OR at.fetch_status != 'ok')
    """, (user_id, user_id)).fetchall()
    conn.close()
    for row in rows:
        await store_approved_tender(row["id"])


# ==================== API ====================

class ApprovedTenderResponse(BaseModel):
    id: int
    bid_request_id: int
    tender_notice_id: str
    url: str
    title: Optional[str]
    agency: Optional[str]
    notice_type: Optional[str]
    reference: Optional[str]
    countries: Optional[str]
    registration_level: Optional[str]
    published: Optional[str]
    deadline: Optional[str]
    notice_status: Optional[str]
    description: Optional[str]
    contacts: list
    links: list
    unspsc_codes: list
    ai_context: Optional[str]
    notes: Optional[str]
    requested_by_username: str
    approved_by_username: str
    approved_by_full_name: Optional[str]
    approved_at: Optional[str]
    fetch_status: str
    fetch_error: Optional[str]
    fetched_at: Optional[str]
    ai_tender_id: Optional[int] = None
    ai_status: Optional[str] = None


SELECT_SQL = """
    SELECT at.*,
           u1.username AS requested_by_username,
           u2.username AS approved_by_username, u2.full_name AS approved_by_full_name,
           ai.id AS ai_tender_id, ai.status AS ai_status
    FROM approved_tenders at
    JOIN users u1 ON at.requested_by_user_id = u1.id
    JOIN users u2 ON at.approved_by_user_id = u2.id
    LEFT JOIN ai_tenders ai ON ai.approved_tender_id = at.id
"""


def _to_response(row: sqlite3.Row) -> dict:
    d = dict(row)
    for f in JSON_FIELDS:
        try:
            d[f] = json.loads(d[f]) if d[f] else []
        except ValueError:
            d[f] = []
    return d


@router.get("", response_model=List[ApprovedTenderResponse])
async def list_approved_tenders(current_user: dict = Depends(get_current_user)):
    """Approved tenders the current user requested or approved (newest first)."""
    await sync_for_user(current_user["id"])
    conn = get_db_connection()
    rows = conn.execute(
        SELECT_SQL + """
        WHERE at.requested_by_user_id = ? OR at.approved_by_user_id = ?
        ORDER BY at.approved_at DESC
        """,
        (current_user["id"], current_user["id"]),
    ).fetchall()
    conn.close()
    return [_to_response(r) for r in rows]


@router.post("/{approved_id}/refresh", response_model=ApprovedTenderResponse)
async def refresh_approved_tender(approved_id: int, current_user: dict = Depends(get_current_user)):
    """Re-fetch the notice from UNGM and update the stored record."""
    conn = get_db_connection()
    row = conn.execute(SELECT_SQL + " WHERE at.id = ?", (approved_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Approved tender not found")
    if current_user["id"] not in (row["requested_by_user_id"], row["approved_by_user_id"]):
        raise HTTPException(status_code=403, detail="You don't have access to this tender")

    await store_approved_tender(row["bid_request_id"], force=True)
    conn = get_db_connection()
    row = conn.execute(SELECT_SQL + " WHERE at.id = ?", (approved_id,)).fetchone()
    conn.close()
    return _to_response(row)


init_approved_tenders_table()
