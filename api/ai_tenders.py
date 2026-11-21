"""AI Tenders API - approved tenders moved into the AI processing queue.

Kowsalya clicks "Move to AI for processing" on an approved tender; its stored UNGM data
(including the plain-text `ai_context`) is copied into the `ai_tenders` table with
status 'queued'. An AI worker can later pick up queued rows, write `ai_result` and set
the status to 'done' (or 'error').
"""
import html
import os
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import get_current_user

router = APIRouter()

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth.db")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_ai_tenders_table():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_tenders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approved_tender_id INTEGER UNIQUE NOT NULL,
            tender_notice_id TEXT NOT NULL,
            title TEXT,
            agency TEXT,
            notice_type TEXT,
            reference TEXT,
            countries TEXT,
            deadline TEXT,
            url TEXT,
            ai_context TEXT,                 -- full notice text sent to the AI
            status TEXT DEFAULT 'queued',    -- queued | processing | done | error
            ai_result TEXT,                  -- AI output, written by the processing step
            error TEXT,
            moved_by_user_id INTEGER NOT NULL,
            moved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            FOREIGN KEY (approved_tender_id) REFERENCES approved_tenders (id),
            FOREIGN KEY (moved_by_user_id) REFERENCES users (id),
            CHECK (status IN ('queued', 'processing', 'done', 'error'))
        )
    """)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(ai_tenders)")}
    for column, ddl in (
        ("proposal_html", "TEXT"),          # proposal document (HTML), AI-generated then user-edited
        ("proposal_source", "TEXT"),        # template | ai | user
        ("proposal_updated_at", "TIMESTAMP"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE ai_tenders ADD COLUMN {column} {ddl}")
    conn.commit()
    conn.close()


def _esc(value: Optional[str]) -> str:
    return html.escape(value or "")


def proposal_template(t: dict) -> str:
    """Starter proposal shown until the AI writes one. Section bodies are placeholders."""
    title = _esc(t.get("title")) or f"UNGM Notice {_esc(t.get('tender_notice_id'))}"
    sections = [
        ("1. Executive summary",
         "Summarise our understanding of the requirement, our proposed solution and why GREEN DigitALL is the right partner."),
        ("2. Understanding of the requirement",
         "Restate the objectives and scope of work from the notice, and note any assumptions."),
        ("3. Technical approach and methodology",
         "Describe the proposed solution, equipment or services, standards followed and how they meet the minimum requirements."),
        ("4. Work plan and timeline",
         "Key phases, milestones and delivery dates."),
        ("5. Team and relevant experience",
         "Key personnel, local partners and similar projects delivered."),
        ("6. Quality, safety and after-sales support",
         "Quality assurance, health and safety, warranty, maintenance and training."),
        ("7. Financial proposal",
         "Summary of pricing (detailed in the separate price schedule)."),
        ("8. Compliance checklist",
         "Confirm each mandatory requirement and document requested in the solicitation."),
    ]
    body = "".join(f"<h2>{h}</h2><p><em>{p}</em></p>" for h, p in sections)
    return (
        f"<h1>Proposal: {title}</h1>"
        "<table><tbody>"
        f"<tr><th>Buyer</th><td>{_esc(t.get('agency'))}</td></tr>"
        f"<tr><th>Notice type</th><td>{_esc(t.get('notice_type'))}</td></tr>"
        f"<tr><th>Reference</th><td>{_esc(t.get('reference'))}</td></tr>"
        f"<tr><th>Country</th><td>{_esc(t.get('countries'))}</td></tr>"
        f"<tr><th>Submission deadline</th><td>{_esc(t.get('deadline'))}</td></tr>"
        "<tr><th>Submitted by</th><td>GREEN DigitALL</td></tr>"
        "</tbody></table>"
        + body
    )


class MoveToAiRequest(BaseModel):
    approved_tender_id: int


class AiTenderResponse(BaseModel):
    id: int
    approved_tender_id: int
    tender_notice_id: str
    title: Optional[str]
    agency: Optional[str]
    notice_type: Optional[str]
    reference: Optional[str]
    countries: Optional[str]
    deadline: Optional[str]
    url: Optional[str]
    status: str
    ai_result: Optional[str]
    error: Optional[str]
    moved_by_username: str
    moved_at: Optional[str]
    processed_at: Optional[str]


SELECT_SQL = """
    SELECT ai.*, u.username AS moved_by_username
    FROM ai_tenders ai
    JOIN users u ON ai.moved_by_user_id = u.id
"""


@router.post("", response_model=AiTenderResponse, status_code=201)
async def move_to_ai(request: MoveToAiRequest, current_user: dict = Depends(get_current_user)):
    """Copy an approved tender into the AI processing queue."""
    conn = get_db_connection()
    try:
        at = conn.execute(
            "SELECT * FROM approved_tenders WHERE id = ?", (request.approved_tender_id,)
        ).fetchone()
        if not at:
            raise HTTPException(status_code=404, detail="Approved tender not found")
        if current_user["id"] not in (at["requested_by_user_id"], at["approved_by_user_id"]):
            raise HTTPException(status_code=403, detail="You don't have access to this tender")
        if at["fetch_status"] != "ok":
            raise HTTPException(
                status_code=400,
                detail="Tender details are not loaded yet. Refresh from UNGM first.",
            )
        if conn.execute(
            "SELECT 1 FROM ai_tenders WHERE approved_tender_id = ?", (at["id"],)
        ).fetchone():
            raise HTTPException(status_code=409, detail="Tender is already in AI processing")

        cursor = conn.execute("""
            INSERT INTO ai_tenders (
                approved_tender_id, tender_notice_id, title, agency, notice_type,
                reference, countries, deadline, url, ai_context, moved_by_user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            at["id"], at["tender_notice_id"], at["title"], at["agency"], at["notice_type"],
            at["reference"], at["countries"], at["deadline"], at["url"], at["ai_context"],
            current_user["id"],
        ))
        conn.commit()
        row = conn.execute(SELECT_SQL + " WHERE ai.id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@router.get("", response_model=List[AiTenderResponse])
async def list_ai_tenders(current_user: dict = Depends(get_current_user)):
    """Tenders the current user moved to AI processing (newest first)."""
    conn = get_db_connection()
    rows = conn.execute(
        SELECT_SQL + " WHERE ai.moved_by_user_id = ? ORDER BY ai.moved_at DESC",
        (current_user["id"],),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class AiTenderDetailResponse(AiTenderResponse):
    proposal_html: str
    proposal_source: str
    proposal_updated_at: Optional[str]


class ProposalUpdate(BaseModel):
    proposal_html: str


def _get_owned(conn: sqlite3.Connection, ai_tender_id: int, user_id: int) -> sqlite3.Row:
    row = conn.execute(SELECT_SQL + " WHERE ai.id = ?", (ai_tender_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="AI tender not found")
    if row["moved_by_user_id"] != user_id:
        raise HTTPException(status_code=403, detail="You don't have access to this tender")
    return row


def _detail(row: sqlite3.Row) -> dict:
    d = dict(row)
    if not d.get("proposal_html"):
        d["proposal_html"] = proposal_template(d)
        d["proposal_source"] = "template"
    return d


@router.get("/{ai_tender_id}", response_model=AiTenderDetailResponse)
async def get_ai_tender(ai_tender_id: int, current_user: dict = Depends(get_current_user)):
    """One AI tender with its proposal document (a starter template until one is saved)."""
    conn = get_db_connection()
    try:
        return _detail(_get_owned(conn, ai_tender_id, current_user["id"]))
    finally:
        conn.close()


@router.put("/{ai_tender_id}/proposal", response_model=AiTenderDetailResponse)
async def save_proposal(
    ai_tender_id: int,
    request: ProposalUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Save the user's edited proposal."""
    if len(request.proposal_html) > 5_000_000:
        raise HTTPException(status_code=413, detail="Proposal is too large")
    conn = get_db_connection()
    try:
        _get_owned(conn, ai_tender_id, current_user["id"])
        conn.execute("""
            UPDATE ai_tenders
            SET proposal_html = ?, proposal_source = 'user', proposal_updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (request.proposal_html, ai_tender_id))
        conn.commit()
        return _detail(_get_owned(conn, ai_tender_id, current_user["id"]))
    finally:
        conn.close()


init_ai_tenders_table()
