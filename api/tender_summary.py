"""AI tender summary for Bernard.

When Kowsalya sends a tender to Bernard, the full UNGM notice is fetched and Claude writes
a short bid/no-bid summary (scope, key details, eligibility, deliverables). It is stored on
the bid request (`bid_requests.ai_summary`) and shown on Bernard's approval card.

Needs ANTHROPIC_API_KEY in tea_backend/.env.
"""
import json
import os
import sqlite3
from typing import Optional

import anthropic

from api.ungm_notice import NoticeFetchError, fetch_notice

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth.db")
MODEL = "claude-opus-5-5"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short tender name, e.g. 'UNHCR RFP 3557'"},
        "location": {"type": "string", "description": "Where the work happens; empty if not stated"},
        "scope": {"type": "string", "description": "One or two sentences on what must be delivered"},
        "key_details": {
            "type": "array",
            "description": "Quantities, specs, durations, evaluation weights and deadlines",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                "required": ["label", "value"],
                "additionalProperties": False,
            },
        },
        "eligibility": {"type": "array", "items": {"type": "string"}},
        "deliverables": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "location", "scope", "key_details", "eligibility", "deliverables"],
    "additionalProperties": False,
}

SYSTEM = """You summarise UN procurement tenders for the bid manager at GREEN DigitALL, who \
decides whether the company should bid. Write a concise summary from the tender notice you \
are given.

- Use only facts stated in the notice. Never guess numbers, dates or requirements; if the \
notice doesn't give something, leave it out (use an empty list or empty string).
- key_details: concrete facts such as capacities, quantities, specifications, implementation \
period, evaluation weights, clarification deadline and submission deadline (with time and \
timezone as written). Keep each value short.
- eligibility: documents, registrations, licences, experience and financial requirements.
- deliverables: what the supplier must hand over or perform.
- Plain, short phrases; no marketing language."""


def init_summary_columns():
    conn = sqlite3.connect(DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bid_requests)")}
    for col in ("ai_summary", "summary_status", "summary_error"):
        if col not in cols:
            conn.execute(f"ALTER TABLE bid_requests ADD COLUMN {col} TEXT")
    conn.commit()
    conn.close()


def _set(bid_request_id: int, status: str, summary: Optional[dict] = None, error: Optional[str] = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE bid_requests SET summary_status = ?, ai_summary = ?, summary_error = ? WHERE id = ?",
        (status, json.dumps(summary, ensure_ascii=False) if summary else None, error, bid_request_id),
    )
    conn.commit()
    conn.close()


def mark_pending(bid_request_id: int):
    _set(bid_request_id, "pending")


def _notice_text(n: dict) -> str:
    lines = [
        f"Title: {n.get('title')}",
        f"Agency: {n.get('agency')}",
        f"Notice type: {n.get('notice_type')}",
        f"Reference: {n.get('reference')}",
        f"Beneficiary countries: {n.get('countries')}",
        f"Published: {n.get('published')}",
        f"Deadline: {n.get('deadline')}",
        f"Status: {n.get('notice_status')}",
        "",
        "Description:",
        n.get("description") or "(none)",
    ]
    if n.get("links"):
        lines += ["", "Links:"] + [f"- {l.get('description')}: {l.get('url')}" for l in n["links"]]
    if n.get("unspsc_codes"):
        lines += ["", "UNSPSC codes:"] + [f"- {c}" for c in n["unspsc_codes"]]
    return "\n".join(lines)


async def summarize_notice(notice: dict) -> dict:
    client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY
    response = await client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if Opus declines, the API retries on a fallback model
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
        messages=[{
            "role": "user",
            "content": f"<tender_notice>\n{_notice_text(notice)}\n</tender_notice>\n\nSummarise this tender.",
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarise this notice")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Summary was cut off (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


async def generate_summary(bid_request_id: int, notice_id: str):
    """Background job: fetch the notice, summarise it, store the result. Never raises."""
    mark_pending(bid_request_id)
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        _set(bid_request_id, "failed", error="AI summary is not set up: add ANTHROPIC_API_KEY to tea_backend/.env and restart the backend")
        return
    try:
        notice = await fetch_notice(notice_id)
        summary = await summarize_notice(notice)
        _set(bid_request_id, "ready", summary=summary)
    except NoticeFetchError as exc:
        _set(bid_request_id, "failed", error=f"Could not read the UNGM notice: {exc}")
    except anthropic.AuthenticationError:
        _set(bid_request_id, "failed", error="ANTHROPIC_API_KEY is missing or invalid on the server")
    except anthropic.RateLimitError:
        _set(bid_request_id, "failed", error="AI rate limit reached, try again in a minute")
    except anthropic.APIStatusError as exc:
        _set(bid_request_id, "failed", error=f"AI service error {exc.status_code}")
    except anthropic.APIConnectionError:
        _set(bid_request_id, "failed", error="Could not reach the AI service")
    except Exception as exc:  # noqa: BLE001 - store anything else so the UI can show it
        _set(bid_request_id, "failed", error=str(exc)[:300])


def summary_fields(row: dict) -> dict:
    """ai_summary (parsed), summary_status, summary_error for API responses."""
    raw = row.get("ai_summary")
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return {
        "ai_summary": parsed,
        "summary_status": row.get("summary_status"),
        "summary_error": row.get("summary_error"),
    }


init_summary_columns()
