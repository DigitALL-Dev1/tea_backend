"""Fetch UNGM tenders (https://www.ungm.org/Public/Notice) with filters."""
import asyncio
import json
import re
import threading
import time
from datetime import datetime
from typing import Literal

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.search_settings import load_search_settings

BASE = "https://www.ungm.org"
PAGE_URL = f"{BASE}/Public/Notice"
SEARCH_URL = f"{BASE}/Public/Notice/Search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
UNGM_PAGE_SIZE = 15     # max PageSize UNGM accepts (larger sizes return HTTP 400)
MAX_PAGES = 50          # safety cap when fetch_all=true

router = APIRouter()


class Tender(BaseModel):
    notice_id: str
    published: str
    deadline: str
    organization: str
    type: str
    reference: str
    country: str
    title: str
    url: str


class TenderListResponse(BaseModel):
    count: int
    filters: dict
    tenders: list[Tender]


# ---------- UNGM scraping helpers ----------

SESSION_TTL = 10 * 60   # reuse one UNGM session/token for 10 minutes
CACHE_TTL = 2 * 60      # identical searches within 2 minutes are served from memory
NOTICE_TYPE_MAX_PAGES = 10  # notice type is filtered locally, so cap how far we scan

_lock = threading.Lock()   # one UNGM conversation at a time (avoids HTTP 429)
_ctx: dict | None = None   # {"session", "token", "countries", "agencies", "created"}
_cache: dict[str, tuple[float, list[dict]]] = {}


class UngmRateLimited(Exception):
    """UNGM kept answering 429 Too Many Requests."""


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _request(s: requests.Session, method: str, url: str, **kw) -> requests.Response:
    """HTTP call with backoff on 429 (honours Retry-After)."""
    for attempt in range(3):
        r = s.request(method, url, timeout=30, **kw)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        retry_after = r.headers.get("Retry-After", "")
        wait = int(retry_after) if retry_after.isdigit() else 3 * (attempt + 1)
        time.sleep(min(wait, 15))
    raise UngmRateLimited()


def _options(soup: BeautifulSoup, select_id: str) -> dict[str, str]:
    """{option text: UNGM id} for a <select> on the notice page."""
    el = soup.find(id=select_id)
    if not el:
        return {}
    return {_clean(o.get_text()): o["value"] for o in el.find_all("option") if o.get("value")}


def _get_ctx(force: bool = False) -> dict:
    """UNGM session, anti-forgery token and country/agency id lists (cached)."""
    global _ctx
    if not force and _ctx and time.time() - _ctx["created"] < SESSION_TTL:
        return _ctx
    s = requests.Session()
    s.headers.update(HEADERS)
    soup = BeautifulSoup(_request(s, "GET", PAGE_URL).text, "html.parser")
    tok = soup.find("input", {"name": "__RequestVerificationToken"})
    if not tok:
        raise RuntimeError("Verification token not found on UNGM page (site layout may have changed).")
    _ctx = {
        "session": s,
        "token": tok["value"],
        "countries": _options(soup, "selNoticeCountry"),
        "agencies": _options(soup, "selNoticeAgency"),
        "created": time.time(),
    }
    return _ctx


def _resolve(names: list[str], options: dict[str, str]) -> list[str]:
    """Map names to UNGM ids: exact (case-insensitive) match first, else partial matches."""
    ids: list[str] = []
    for name in names:
        n = name.lower()
        exact = [v for k, v in options.items() if k.lower() == n]
        ids += exact or [v for k, v in options.items() if n in k.lower()]
    return list(dict.fromkeys(ids))


def _parse_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("div.tableRow.dataRow"):
        cells = row.find_all("div", attrs={"role": "cell"}, recursive=False)
        if len(cells) < 8:
            continue
        nid = row.get("data-noticeid", "")
        title_el = cells[1].select_one(".ungm-title")
        deadline_el = cells[2].find("span")  # first span = date; hidden span = days left
        out.append({
            "notice_id": nid,
            "published": _clean(cells[3].get_text()),
            "deadline": _clean(deadline_el.get_text() if deadline_el else cells[2].get_text()),
            "organization": _clean(cells[4].get_text()),
            "type": _clean(cells[5].get_text()),
            "reference": _clean(cells[6].get_text()),
            "country": _clean(cells[7].get_text()),
            "title": _clean(title_el.get_text() if title_el else cells[1].get_text()),
            "url": f"{BASE}/Public/Notice/{nid}",
        })
    return out


def _search_page(ctx: dict, page: int, size: int, f: dict, countries: list[str],
                 agencies: list[str]) -> list[dict]:
    if f["sort"] == "deadline":
        # Website default view: open notices, soonest deadline first
        sort_field, ascending = "Deadline", True
        deadline_from = f["deadline_from"] or (
            "" if (f["published_from"] or f["published_to"]) else datetime.now().strftime("%d-%b-%Y")
        )
    else:
        # Newest published first
        sort_field, ascending, deadline_from = "DatePublished", False, f["deadline_from"]

    payload = {
        "PageIndex": page, "PageSize": size,
        "Title": f["title"], "Description": f["description"], "Reference": f["reference"],
        "PublishedFrom": f["published_from"], "PublishedTo": f["published_to"],
        "DeadlineFrom": deadline_from, "DeadlineTo": f["deadline_to"],
        "Countries": countries, "Agencies": agencies, "UNSPSCs": [], "NoticeTypes": [],
        "SortField": sort_field, "SortAscending": ascending,
        "isPicker": False, "IsSustainable": False, "IsActive": f["active"],
        "NoticeDisplayType": None, "TypeOfCompetitions": [],
    }
    r = _request(ctx["session"], "POST", SEARCH_URL, data=json.dumps(payload), headers={
        "Content-Type": "application/json",
        "RequestVerificationToken": ctx["token"],
        "X-Requested-With": "XMLHttpRequest",
        "Referer": PAGE_URL,
    })
    return _parse_rows(r.text)


def _matches_type(row: dict, f: dict) -> bool:
    """Notice type is still matched locally by name."""
    wanted = f["notice_type"]
    return not wanted or any(w.lower() in row["type"].lower() for w in wanted)


def _fetch(f: dict, limit: int, fetch_all: bool) -> list[dict]:
    key = json.dumps([f, limit, fetch_all], sort_keys=True)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]

        ctx = _get_ctx()
        countries = _resolve(f["country"], ctx["countries"])
        agencies = _resolve(f["organization"], ctx["agencies"])
        # A country/agency name that matches nothing on UNGM can't have any tenders.
        if (f["country"] and not countries) or (f["organization"] and not agencies):
            return []

        # UNGM rejects PageSize > 15 with HTTP 400, so always page in chunks of at most 15.
        size = UNGM_PAGE_SIZE if fetch_all else min(limit, UNGM_PAGE_SIZE)
        if fetch_all:
            max_pages = MAX_PAGES
        elif f["notice_type"]:
            max_pages = NOTICE_TYPE_MAX_PAGES
        else:
            max_pages = -(-limit // size)  # ceil

        results, page = [], 0
        while page < max_pages:
            try:
                rows = _search_page(ctx, page, size, f, countries, agencies)
            except requests.HTTPError:
                if page:  # a stale token only matters on the first page
                    raise
                ctx = _get_ctx(force=True)
                rows = _search_page(ctx, page, size, f, countries, agencies)
            results.extend(r for r in rows if _matches_type(r, f))
            if len(rows) < size or (not fetch_all and len(results) >= limit):
                break
            page += 1
            time.sleep(1)  # be polite to UNGM

        results = results if fetch_all else results[:limit]
        _cache[key] = (time.time(), results)
        return results


def clear_cache() -> None:
    """Drop cached searches (called when the search settings change)."""
    with _lock:
        _cache.clear()


def _date_key(value: str) -> datetime:
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(value[:17].strip(), fmt)
        except ValueError:
            continue
    return datetime.max


def _fetch_keywords(f: dict, keywords: list[str], limit: int, fetch_all: bool) -> list[dict]:
    """Tenders whose title matches ANY keyword: one UNGM search per keyword, merged."""
    merged: dict[str, dict] = {}
    for kw in keywords:
        for row in _fetch({**f, "title": kw}, limit, fetch_all):
            merged.setdefault(row["notice_id"], row)
    rows = list(merged.values())
    if f["sort"] == "deadline":
        rows.sort(key=lambda r: _date_key(r["deadline"]))
    else:
        rows.sort(key=lambda r: _date_key(r["published"]), reverse=True)
    return rows if fetch_all else rows[:limit]


# ---------- API ----------

DATE_HELP = "Date in UNGM format, e.g. 15-Nov-2026"


def _check_date(value: str, name: str) -> str:
    if value:
        try:
            datetime.strptime(value, "%d-%b-%Y")
        except ValueError:
            raise HTTPException(422, f"'{name}' must look like 15-Nov-2026")
    return value


@router.get("", response_model=TenderListResponse)
async def get_tenders(
    title: str = Query("", description="Keyword in the tender title"),
    description: str = Query("", description="Keyword in the tender description"),
    reference: str = Query("", description="Tender reference number"),
    country: list[str] = Query([], description="Country name(s), e.g., e.g. ?country=Kenya&country=India"),
    organization: list[str] = Query([], description="Agency abbreviation(s), e.g., e.g. UNDP, UNICEF"),
    notice_type: list[str] = Query([], description="Notice type(s), partial match, e.g. Request for proposal"),
    published_from: str = Query("", description=DATE_HELP),
    published_to: str = Query("", description=DATE_HELP),
    deadline_from: str = Query("", description=DATE_HELP),
    deadline_to: str = Query("", description=DATE_HELP),
    include_closed: bool = Query(False, description="Include notices past their deadline"),
    sort: Literal["deadline", "published"] = Query(
        "deadline", description="deadline = soonest deadline first (website default); published = newest first"),
    limit: int = Query(10, ge=1, le=500, description="Max tenders to return"),
    fetch_all: bool = Query(False, description="Fetch every matching page (best with a date range)"),
    apply_settings: bool = Query(
        True,
        description="Use the saved search settings (Keywords, Countries, Agencies) for any of "
                    "title / country / organization not given in this request",
    ),
):
    """Get UNGM tenders matching the given filters."""
    filters = {
        "title": title.strip(),
        "description": description.strip(),
        "reference": reference.strip(),
        "country": [c.strip() for c in country if c.strip()],
        "organization": [o.strip() for o in organization if o.strip()],
        "notice_type": [t.strip() for t in notice_type if t.strip()],
        "published_from": _check_date(published_from.strip(), "published_from"),
        "published_to": _check_date(published_to.strip(), "published_to"),
        "deadline_from": _check_date(deadline_from.strip(), "deadline_from"),
        "deadline_to": _check_date(deadline_to.strip(), "deadline_to"),
        "active": not include_closed,
        "sort": sort,
    }
    # Saved settings fill in whatever this request didn't specify
    keywords: list[str] = []
    if apply_settings:
        saved = load_search_settings()
        if not filters["title"]:
            keywords = saved["keywords"]
        if not filters["country"]:
            filters["country"] = saved["countries"]
        if not filters["organization"]:
            filters["organization"] = saved["agencies"]
    filters["keywords"] = keywords
    filters["settings_applied"] = apply_settings

    try:
        search = {k: v for k, v in filters.items() if k not in ("keywords", "settings_applied")}
        if len(keywords) > 1:
            tenders = await asyncio.to_thread(_fetch_keywords, search, keywords, limit, fetch_all)
        else:
            if keywords:
                search["title"] = keywords[0]
            tenders = await asyncio.to_thread(_fetch, search, limit, fetch_all)
    except UngmRateLimited:
        raise HTTPException(429, "UNGM is limiting requests right now. Please wait a minute and try again.")
    except (requests.RequestException, RuntimeError) as e:
        raise HTTPException(502, f"Failed to fetch tenders from UNGM: {e}")

    return TenderListResponse(count=len(tenders), filters=filters, tenders=tenders)
