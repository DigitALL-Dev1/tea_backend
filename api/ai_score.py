"""AI Score: how well selected UNGM tenders fit GREEN DigitALL's interests.

POST /api/v1/ai-score/stream  {"notice_ids": ["321637", ...]}   (used by the Tenders page)
  Scores every tender in parallel and streams one NDJSON line per tender as soon as it is
  scored, so the frontend fills the AI Score column live.

POST /api/v1/ai-score  {"notice_ids": [...]}
  Same scores in one JSON response (all notices sent to Claude in one request).

Needs ANTHROPIC_API_KEY in tea_backend/.env.
"""
import asyncio
import json
import os
from typing import List, Optional

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.auth import get_current_user
from api.tender_summary import MODEL, _notice_text
from api.ungm_notice import NoticeFetchError, fetch_notice

router = APIRouter()

# What the company wants to bid on. The score is relevance to these.
INTERESTS = ["Solar", "Energy", "Renewable Energy", "Sustainable Living"]
MAX_TENDERS = 25
MAX_CONCURRENT_FETCHES = 5

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "notice_id": {"type": "string"},
                    "score": {"type": "integer", "description": "0-100"},
                    "matched_interests": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string", "description": "One short sentence"},
                },
                "required": ["notice_id", "score", "matched_interests", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}

SYSTEM = f"""You score UN procurement tenders for GREEN DigitALL, a company that bids on work \
in these areas: {", ".join(INTERESTS)}.

For each tender, give a score from 0 to 100 for how well its actual scope fits those areas:
- 80-100: the core scope is in these areas (e.g. supply/installation of solar PV, mini-grids, \
energy efficiency, renewable energy studies or programmes).
- 50-79: a substantial part of the scope is in these areas, or it is closely related \
(e.g. power supply for a facility, sustainable housing, clean cooking).
- 20-49: only a minor component or indirect link.
- 0-19: unrelated.

Judge from what the notice says the work is, not from single keywords. matched_interests lists \
which of the areas apply (exact names from the list, empty if none). reason is one short \
sentence explaining the score. Return one entry for every tender, using its notice_id exactly."""


class ScoreRequest(BaseModel):
    notice_ids: List[str] = Field(..., min_length=1)


class TenderScore(BaseModel):
    notice_id: str
    score: Optional[int] = None
    matched_interests: List[str] = []
    reason: Optional[str] = None
    error: Optional[str] = None


class ScoreResponse(BaseModel):
    interests: List[str]
    scores: List[TenderScore]


async def _fetch_all(ids: List[str]) -> dict:
    """{notice_id: notice dict or error string}"""
    sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

    async def one(nid: str):
        async with sem:
            try:
                return nid, await fetch_notice(nid)
            except NoticeFetchError as exc:
                return nid, str(exc)

    return dict(await asyncio.gather(*(one(n) for n in ids)))


async def _score_with_claude(notices: dict) -> dict:
    """{notice_id: {score, matched_interests, reason}} for the fetched notices."""
    blocks = "\n\n".join(
        f'<tender notice_id="{nid}">\n{_notice_text(n)}\n</tender>' for nid, n in notices.items()
    )
    client = anthropic.AsyncAnthropic()
    async with client.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if Opus declines, the API retries on a fallback model
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCORE_SCHEMA}},
        messages=[{"role": "user", "content": f"{blocks}\n\nScore each tender."}],
    ) as stream:
        response = await stream.get_final_message()

    if response.stop_reason == "refusal":
        raise HTTPException(502, "The AI declined to score these tenders")
    if response.stop_reason == "max_tokens":
        raise HTTPException(502, "The AI response was cut off; try fewer tenders")
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)
    allowed = {i.lower(): i for i in INTERESTS}
    out = {}
    for s in data.get("scores", []):
        out[str(s["notice_id"])] = {
            "score": max(0, min(100, int(s["score"]))),
            "matched_interests": [allowed[m.lower()] for m in s.get("matched_interests", []) if m.lower() in allowed],
            "reason": s.get("reason"),
        }
    return out


@router.post("", response_model=ScoreResponse)
async def score_tenders(request: ScoreRequest, current_user: dict = Depends(get_current_user)):
    """Score the selected tenders (by UNGM notice ID) against our interests."""
    ids = list(dict.fromkeys(n.strip() for n in request.notice_ids if n.strip()))
    if not ids:
        raise HTTPException(422, "No notice IDs given")
    if len(ids) > MAX_TENDERS:
        raise HTTPException(422, f"Select at most {MAX_TENDERS} tenders at a time")
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise HTTPException(503, "AI is not set up: add ANTHROPIC_API_KEY to tea_backend/.env and restart the backend")

    fetched = await _fetch_all(ids)
    notices = {nid: v for nid, v in fetched.items() if isinstance(v, dict)}

    results = {}
    if notices:
        try:
            results = await _score_with_claude(notices)
        except anthropic.AuthenticationError:
            raise HTTPException(503, "ANTHROPIC_API_KEY is invalid on the server")
        except anthropic.RateLimitError:
            raise HTTPException(429, "AI rate limit reached, try again in a minute")
        except anthropic.APIStatusError as exc:
            raise HTTPException(502, f"AI service error {exc.status_code}")
        except anthropic.APIConnectionError:
            raise HTTPException(502, "Could not reach the AI service")

    scores = []
    for nid in ids:
        if isinstance(fetched[nid], str):
            scores.append(TenderScore(notice_id=nid, error=f"Could not read the UNGM notice: {fetched[nid]}"))
        elif nid in results:
            scores.append(TenderScore(notice_id=nid, **results[nid]))
        else:
            scores.append(TenderScore(notice_id=nid, error="The AI returned no score for this tender"))
    return ScoreResponse(interests=INTERESTS, scores=scores)


# ==================== Streaming: one tender per Claude call ====================

MAX_CONCURRENT_SCORES = 5
SINGLE_SCORE_SCHEMA = SCORE_SCHEMA["properties"]["scores"]["items"]


def _clean_ids(notice_ids: List[str]) -> List[str]:
    ids = list(dict.fromkeys(n.strip() for n in notice_ids if n.strip()))
    if not ids:
        raise HTTPException(422, "No notice IDs given")
    if len(ids) > MAX_TENDERS:
        raise HTTPException(422, f"Select at most {MAX_TENDERS} tenders at a time")
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise HTTPException(503, "AI is not set up: add ANTHROPIC_API_KEY to tea_backend/.env and restart the backend")
    return ids


async def _score_one(client: anthropic.AsyncAnthropic, notice_id: str) -> dict:
    """Fetch one notice and score it. Always returns a result dict (errors included)."""
    result = {"type": "score", "notice_id": notice_id, "score": None,
              "matched_interests": [], "reason": None, "error": None}
    try:
        notice = await fetch_notice(notice_id)
    except NoticeFetchError as exc:
        return {**result, "error": f"Could not read the UNGM notice: {exc}"}

    try:
        async with client.beta.messages.stream(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SINGLE_SCORE_SCHEMA}},
            messages=[{
                "role": "user",
                "content": f'<tender notice_id="{notice_id}">\n{_notice_text(notice)}\n</tender>\n\nScore this tender.',
            }],
        ) as stream:
            response = await stream.get_final_message()
    except anthropic.AuthenticationError:
        return {**result, "error": "ANTHROPIC_API_KEY is invalid on the server"}
    except anthropic.RateLimitError:
        return {**result, "error": "AI rate limit reached, try again in a minute"}
    except anthropic.APIStatusError as exc:
        return {**result, "error": f"AI service error {exc.status_code}"}
    except anthropic.APIConnectionError:
        return {**result, "error": "Could not reach the AI service"}

    if response.stop_reason == "refusal":
        return {**result, "error": "The AI declined to score this tender"}
    try:
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
    except (StopIteration, ValueError):
        return {**result, "error": "The AI returned an unreadable score"}
    allowed = {i.lower(): i for i in INTERESTS}
    return {
        **result,
        "score": max(0, min(100, int(data["score"]))),
        "matched_interests": [allowed[m.lower()] for m in data.get("matched_interests", []) if m.lower() in allowed],
        "reason": data.get("reason"),
    }


@router.post("/stream")
async def score_tenders_stream(request: ScoreRequest, current_user: dict = Depends(get_current_user)):
    """Stream scores as NDJSON: a "start" line, one "score" line per tender as it finishes, then "done"."""
    ids = _clean_ids(request.notice_ids)

    async def events():
        yield json.dumps({"type": "start", "notice_ids": ids, "interests": INTERESTS}) + "\n"
        client = anthropic.AsyncAnthropic()
        sem = asyncio.Semaphore(MAX_CONCURRENT_SCORES)

        async def limited(nid: str):
            async with sem:
                return await _score_one(client, nid)

        for next_done in asyncio.as_completed([limited(n) for n in ids]):
            yield json.dumps(await next_done, ensure_ascii=False) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
