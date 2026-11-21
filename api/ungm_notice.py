"""Fetch and parse a single UNGM notice page (https://www.ungm.org/Public/Notice/{id})."""
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

NOTICE_URL = "https://www.ungm.org/Public/Notice/{notice_id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BLOCK_TAGS = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}


class NoticeFetchError(Exception):
    pass


def _clean(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _block_text(el: Tag) -> str:
    """Readable text: inline fragments joined with spaces, block elements on new lines."""
    parts: list[str] = []

    def walk(node):
        for child in node.children:
            if isinstance(child, Tag):
                if child.name in ("script", "style"):
                    continue
                is_block = child.name in BLOCK_TAGS
                if is_block:
                    parts.append("\n")
                walk(child)
                if is_block:
                    parts.append("\n")
            else:
                parts.append(str(child))

    walk(el)
    text = "".join(parts).replace("\xa0", " ")
    lines = [_clean(line) for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def parse_notice_html(html: bytes | str, notice_id: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    detail = soup.find(id="noticeDetail")
    if not detail:
        raise NoticeFetchError("Notice details not found on the UNGM page")

    data: dict = {
        "notice_id": notice_id,
        "url": NOTICE_URL.format(notice_id=notice_id),
        "title": "",
        "agency": "",
        "notice_type": "",
        "reference": "",
        "countries": "",
        "registration_level": "",
        "published": "",
        "deadline": "",
        "notice_status": "",
        "description": "",
        "contacts": [],
        "links": [],
        "unspsc_codes": [],
    }

    agency = detail.select_one(".title .highlighted")
    data["agency"] = _clean(agency.get_text()) if agency else ""
    title = detail.select_one("span.title")
    data["title"] = _clean(title.get_text()) if title else ""
    notice_type = detail.select_one(".status-tag")
    data["notice_type"] = _clean(notice_type.get_text()) if notice_type else ""

    label_map = {
        "reference": "reference",
        "beneficiary countries or territories": "countries",
        "registration level": "registration_level",
        "published on": "published",
        "deadline on": "deadline",
    }
    for row in detail.select("div.row"):
        label = row.find("span", class_="label")
        value = row.find("span", class_="value")
        if not label or not value:
            continue
        key = label_map.get(_clean(label.get_text()).rstrip(":").lower())
        if key:
            data[key] = _clean(value.get_text())
            if key == "deadline":
                extra = value.find_next_sibling("span")
                data["notice_status"] = _clean(extra.get_text()) if extra else "Open"

    # Description: the list item whose title is "Description"
    for item in detail.select(".ungm-list-item"):
        head = item.find("div", class_="title", recursive=False)
        if head and _clean(head.get_text()).lower() == "description":
            head.extract()
            data["description"] = _block_text(item)
            break

    # Contacts tab: label/value rows
    contacts_tab = soup.find(id="contacts-tab")
    if contacts_tab:
        contact: dict = {}
        for row in contacts_tab.select("div.row"):
            label = row.find("span", class_="label")
            value = row.find("span", class_="value")
            if label and value:
                contact[_clean(label.get_text()).rstrip(":")] = _clean(value.get_text())
        if contact:
            data["contacts"].append(contact)

    # Links tab: table rows (url, description)
    links_table = soup.find(id="tblLinks")
    if links_table:
        for tr in links_table.select("tbody tr"):
            cells = tr.find_all("td")
            if len(cells) >= 2:
                data["links"].append({
                    "url": _clean(cells[0].get_text()),
                    "description": _clean(cells[1].get_text()),
                })

    # UNSPSC tab
    unspsc_tab = soup.find(id="unspscs-tab")
    if unspsc_tab:
        for line in _block_text(unspsc_tab).split("\n"):
            if line.strip():
                data["unspsc_codes"].append(line.strip())

    return data


async def fetch_notice(notice_id: str) -> dict:
    url = NOTICE_URL.format(notice_id=notice_id)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=HEADERS) as client:
            response = await client.get(url)
    except httpx.TimeoutException as exc:
        raise NoticeFetchError("Timed out fetching the notice from UNGM") from exc
    except httpx.HTTPError as exc:
        raise NoticeFetchError(f"Could not reach UNGM: {exc}") from exc

    if response.status_code == 404:
        raise NoticeFetchError(f"Notice {notice_id} not found on UNGM")
    if response.status_code != 200:
        raise NoticeFetchError(f"UNGM returned HTTP {response.status_code}")
    return parse_notice_html(response.content, notice_id)
