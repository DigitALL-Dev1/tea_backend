"""Tender search settings: Keywords, Countries and Agencies.

One shared set for the whole team, stored in the `search_settings` table. GET /api/v1/tenders
applies them automatically (see api/get_tender.py), so the Tenders page only shows matching
notices. An empty list means "no filter" for that field.
"""
import asyncio
import json
import os
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import get_current_user

router = APIRouter()

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth.db")
FIELDS = ("keywords", "countries", "agencies")
MAX_ITEMS = 50
MAX_LEN = 100


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_search_settings_table():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),   -- single shared row
            keywords TEXT NOT NULL DEFAULT '[]',     -- JSON list
            countries TEXT NOT NULL DEFAULT '[]',    -- JSON list
            agencies TEXT NOT NULL DEFAULT '[]',     -- JSON list
            updated_by_user_id INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (updated_by_user_id) REFERENCES users (id)
        )
    """)
    conn.execute("INSERT OR IGNORE INTO search_settings (id) VALUES (1)")
    conn.commit()
    conn.close()


def load_search_settings() -> dict:
    """{"keywords": [...], "countries": [...], "agencies": [...]} – used by the tender search."""
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM search_settings WHERE id = 1").fetchone()
    conn.close()
    out: dict = {}
    for f in FIELDS:
        try:
            out[f] = [str(v) for v in json.loads(row[f])] if row else []
        except (TypeError, ValueError):
            out[f] = []
    return out


def _clean_list(values: List[str], field: str) -> List[str]:
    seen: dict[str, str] = {}
    for v in values:
        v = " ".join(str(v).split())
        if not v:
            continue
        if len(v) > MAX_LEN:
            raise HTTPException(422, f"Each {field} entry must be at most {MAX_LEN} characters")
        seen.setdefault(v.lower(), v)
    if len(seen) > MAX_ITEMS:
        raise HTTPException(422, f"At most {MAX_ITEMS} {field} allowed")
    return list(seen.values())


class SearchSettings(BaseModel):
    keywords: List[str] = []
    countries: List[str] = []
    agencies: List[str] = []


class SearchSettingsResponse(SearchSettings):
    updated_by_username: Optional[str] = None
    updated_at: Optional[str] = None
    # Countries/agencies that match nothing on UNGM (a typo makes that filter return no tenders)
    unmatched_countries: List[str] = []
    unmatched_agencies: List[str] = []


def _ungm_options() -> Optional[dict]:
    """UNGM's country and agency names, or None if UNGM can't be reached."""
    from api.get_tender import _get_ctx, _lock
    try:
        with _lock:
            ctx = _get_ctx()
        return {"countries": sorted(ctx["countries"]), "agencies": sorted(ctx["agencies"])}
    except Exception:
        return None


def _unmatched(names: List[str], options: List[str]) -> List[str]:
    low = [o.lower() for o in options]
    return [n for n in names if not any(n.lower() == o or n.lower() in o for o in low)]


async def _response() -> dict:
    s = load_search_settings()
    conn = get_db_connection()
    meta = conn.execute("""
        SELECT u.username AS updated_by_username, ss.updated_at
        FROM search_settings ss LEFT JOIN users u ON u.id = ss.updated_by_user_id
        WHERE ss.id = 1
    """).fetchone()
    conn.close()
    s.update(dict(meta) if meta else {})
    opts = await asyncio.to_thread(_ungm_options) if (s["countries"] or s["agencies"]) else None
    s["unmatched_countries"] = _unmatched(s["countries"], opts["countries"]) if opts else []
    s["unmatched_agencies"] = _unmatched(s["agencies"], opts["agencies"]) if opts else []
    return s


@router.get("/search", response_model=SearchSettingsResponse)
async def get_search_settings(current_user: dict = Depends(get_current_user)):
    """The saved Keywords, Countries and Agencies."""
    return await _response()


@router.put("/search", response_model=SearchSettingsResponse)
async def save_search_settings(
    request: SearchSettings, current_user: dict = Depends(get_current_user)
):
    """Replace the Keywords, Countries and Agencies. Tender searches use them straight away."""
    cleaned = {f: _clean_list(getattr(request, f), f) for f in FIELDS}
    conn = get_db_connection()
    conn.execute("""
        UPDATE search_settings
        SET keywords = ?, countries = ?, agencies = ?,
            updated_by_user_id = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    """, (
        json.dumps(cleaned["keywords"], ensure_ascii=False),
        json.dumps(cleaned["countries"], ensure_ascii=False),
        json.dumps(cleaned["agencies"], ensure_ascii=False),
        current_user["id"],
    ))
    conn.commit()
    conn.close()

    from api.get_tender import clear_cache
    clear_cache()
    return await _response()


@router.get("/search/options")
async def get_search_options(current_user: dict = Depends(get_current_user)):
    """Country and agency names UNGM knows, for suggestions in the settings form."""
    opts = await asyncio.to_thread(_ungm_options)
    if opts is None:
        raise HTTPException(502, "Could not load the country and agency lists from UNGM")
    return opts


init_search_settings_table()
