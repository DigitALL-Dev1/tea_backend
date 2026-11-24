"""Bid Request API - Kowsalya sends tender to Bernard for bid decision."""
import sqlite3
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends, status
from pydantic import BaseModel
from api.auth import get_current_user, get_db_connection as get_auth_db_connection
import os

router = APIRouter()

# Database file path
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth.db")

# ==================== Database Functions ====================

def get_db_connection():
    """Create and return a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_bid_request_table():
    """Initialize the bid_requests table."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create bid_requests table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bid_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tender_notice_id TEXT NOT NULL,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            bid_status TEXT DEFAULT 'pending',
            notes TEXT,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (from_user_id) REFERENCES users (id),
            FOREIGN KEY (to_user_id) REFERENCES users (id),
            CHECK (bid_status IN ('pending', 'approved', 'rejected'))
        )
    """)
    
    # Tender title, sent by the frontend so lists can show it without scraping UNGM
    columns = {row["name"] for row in cursor.execute("PRAGMA table_info(bid_requests)")}
    if "tender_title" not in columns:
        cursor.execute("ALTER TABLE bid_requests ADD COLUMN tender_title TEXT")

    conn.commit()
    conn.close()
    print("✓ Bid requests table initialized")


def get_user_by_username(username: str) -> Optional[dict]:
    """Get user by username."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, email, full_name FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()
    
    if user:
        return dict(user)
    return None


def create_bid_request(tender_notice_id: str, from_user_id: int, to_user_id: int, notes: Optional[str] = None, tender_title: Optional[str] = None) -> dict:
    """Create a new bid request."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO bid_requests (tender_notice_id, from_user_id, to_user_id, notes, tender_title)
        VALUES (?, ?, ?, ?, ?)
    """, (tender_notice_id, from_user_id, to_user_id, notes, tender_title))
    
    bid_request_id = cursor.lastrowid
    conn.commit()
    
    # Fetch the created request
    cursor.execute("""
        SELECT br.*, 
               u1.username as from_username, u1.full_name as from_full_name,
               u2.username as to_username, u2.full_name as to_full_name
        FROM bid_requests br
        JOIN users u1 ON br.from_user_id = u1.id
        JOIN users u2 ON br.to_user_id = u2.id
        WHERE br.id = ?
    """, (bid_request_id,))
    
    result = dict(cursor.fetchone())
    conn.close()
    
    return result


def get_bid_request_by_id(bid_request_id: int) -> Optional[dict]:
    """Get a bid request by ID."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT br.*, 
               u1.username as from_username, u1.full_name as from_full_name,
               u2.username as to_username, u2.full_name as to_full_name
        FROM bid_requests br
        JOIN users u1 ON br.from_user_id = u1.id
        JOIN users u2 ON br.to_user_id = u2.id
        WHERE br.id = ?
    """, (bid_request_id,))
    
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return dict(result)
    return None


def update_bid_status(bid_request_id: int, bid_status: str, user_id: int) -> dict:
    """Update the bid status of a request."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if the user is the recipient (to_user_id)
    cursor.execute("SELECT to_user_id FROM bid_requests WHERE id = ?", (bid_request_id,))
    result = cursor.fetchone()
    
    if not result:
        conn.close()
        raise HTTPException(status_code=404, detail="Bid request not found")
    
    if result["to_user_id"] != user_id:
        conn.close()
        raise HTTPException(
            status_code=403, 
            detail="Only the recipient can update the bid status"
        )
    
    # Update the bid status
    cursor.execute("""
        UPDATE bid_requests 
        SET bid_status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (bid_status, bid_request_id))
    
    conn.commit()
    conn.close()
    
    return get_bid_request_by_id(bid_request_id)


def get_user_bid_requests(user_id: int, request_type: str = "all") -> List[dict]:
    """Get bid requests for a user (sent, received, or all)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if request_type == "sent":
        query = "WHERE br.from_user_id = ?"
    elif request_type == "received":
        query = "WHERE br.to_user_id = ?"
    else:
        query = "WHERE br.from_user_id = ? OR br.to_user_id = ?"
    
    sql = f"""
        SELECT br.*, 
               u1.username as from_username, u1.full_name as from_full_name,
               u2.username as to_username, u2.full_name as to_full_name
        FROM bid_requests br
        JOIN users u1 ON br.from_user_id = u1.id
        JOIN users u2 ON br.to_user_id = u2.id
        {query}
        ORDER BY br.requested_at DESC
    """
    
    if request_type == "all":
        cursor.execute(sql, (user_id, user_id))
    else:
        cursor.execute(sql, (user_id,))
    
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return results


# ==================== Pydantic Models ====================

class BidRequestCreate(BaseModel):
    tender_notice_id: str
    to_username: str  # Username of the recipient (bernard)
    notes: Optional[str] = None
    tender_title: Optional[str] = None


class BidStatusUpdate(BaseModel):
    bid_status: str  # "approved" or "rejected"


class BidRequestResponse(BaseModel):
    id: int
    tender_notice_id: str
    from_user_id: int
    from_username: str
    from_full_name: Optional[str]
    to_user_id: int
    to_username: str
    to_full_name: Optional[str]
    bid_status: str
    notes: Optional[str]
    requested_at: str
    updated_at: str


# ==================== API Endpoints ====================

@router.post("/create", response_model=BidRequestResponse)
async def create_bid_request_endpoint(
    request: BidRequestCreate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """
    Create a bid request (Kowsalya sends tender to Bernard).
    Also starts the AI summary of the tender in the background (shown to Bernard).
    Authenticated user sends a tender notice to another user asking if they should bid.
    """
    # Get the recipient user
    to_user = get_user_by_username(request.to_username)
    
    if not to_user:
        raise HTTPException(
            status_code=404,
            detail=f"User '{request.to_username}' not found"
        )
    
    if to_user["id"] == current_user["id"]:
        raise HTTPException(
            status_code=400,
            detail="Cannot send bid request to yourself"
        )
    
    # Create the bid request
    bid_request = create_bid_request(
        tender_notice_id=request.tender_notice_id,
        from_user_id=current_user["id"],
        to_user_id=to_user["id"],
        notes=request.notes,
        tender_title=(request.tender_title or "").strip()[:500] or None,
    )

    # AI summary: fetch the UNGM notice and summarise it with Claude after responding
    mark_pending(bid_request["id"])
    background_tasks.add_task(generate_summary, bid_request["id"], request.tender_notice_id)

    return bid_request


@router.post("/{bid_request_id}/summary")
async def regenerate_summary_endpoint(
    bid_request_id: int,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """Re-run the AI summary for a bid request (sender or recipient only)."""
    bid_request = get_bid_request_by_id(bid_request_id)
    if not bid_request:
        raise HTTPException(status_code=404, detail="Bid request not found")
    if current_user["id"] not in (bid_request["from_user_id"], bid_request["to_user_id"]):
        raise HTTPException(status_code=403, detail="You don't have permission to view this bid request")
    mark_pending(bid_request_id)
    background_tasks.add_task(generate_summary, bid_request_id, bid_request["tender_notice_id"])
    return {"id": bid_request_id, "summary_status": "pending"}


@router.patch("/{bid_request_id}/status", response_model=BidRequestResponse)
async def update_bid_status_endpoint(
    bid_request_id: int,
    request: BidStatusUpdate,
    current_user: dict = Depends(get_current_user)
):
    """
    Update bid status (Bernard responds to Kowsalya).
    Only the recipient of the bid request can update the status.
    Status options: "approved" or "rejected"
    """
    if request.bid_status not in ["approved", "rejected"]:
        raise HTTPException(
            status_code=400,
            detail="Bid status must be 'approved' or 'rejected'"
        )
    
    updated_request = update_bid_status(
        bid_request_id=bid_request_id,
        bid_status=request.bid_status,
        user_id=current_user["id"]
    )

    # Approved: fetch the full UNGM notice and store it in approved_tenders
    if request.bid_status == "approved":
        from api.approved_tenders import store_approved_tender
        await store_approved_tender(bid_request_id)

    return updated_request


@router.get("/pending", response_model=List[dict])
async def get_pending_bid_requests(
    current_user: dict = Depends(get_current_user)
):
    """
    Get all pending bid requests for the current user (received only).
    Returns bid request data along with full tender information from UNGM.
    """
    import httpx
    from bs4 import BeautifulSoup
    import re
    
    # Get pending bid requests where current user is the recipient
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT br.*, 
               u1.username as from_username, u1.full_name as from_full_name,
               u2.username as to_username, u2.full_name as to_full_name
        FROM bid_requests br
        JOIN users u1 ON br.from_user_id = u1.id
        JOIN users u2 ON br.to_user_id = u2.id
        WHERE br.to_user_id = ? AND br.bid_status = 'pending'
        ORDER BY br.requested_at DESC
    """, (current_user["id"],))
    
    bid_requests = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    if not bid_requests:
        return []
    
    # Headers to mimic a real browser
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }
    
    results = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=headers) as client:
        for bid_request in bid_requests:
            tender_data = None
            tender_error = None
            
            try:
                notice_id = bid_request["tender_notice_id"]
                ungm_url = f"https://www.ungm.org/Public/Notice/{notice_id}"
                
                # Fetch the UNGM notice page
                response = await client.get(ungm_url)
                
                if response.status_code == 200:
                    html = response.text
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    # Extract tender information from the page
                    tender_data = {
                        "notice_id": notice_id,
                        "url": ungm_url,
                        "title": "",
                        "organization": "",
                        "reference": notice_id,
                        "country": "",
                        "type": "",
                        "deadline": "",
                        "published": "",
                    }
                    
                    # Extract title - try multiple selectors
                    title_elem = (soup.find('h2', {'class': 'notice-title'}) or 
                                 soup.find('h1', {'class': 'page-title'}) or
                                 soup.find('div', {'class': 'title'}) or
                                 soup.find('h1') or
                                 soup.find('h2'))
                    if title_elem:
                        tender_data["title"] = title_elem.get_text(strip=True)
                    
                    # Try to find a table with details
                    detail_table = soup.find('table', {'class': ['table', 'details-table']})
                    if detail_table:
                        rows = detail_table.find_all('tr')
                        for row in rows:
                            cells = row.find_all(['td', 'th'])
                            if len(cells) >= 2:
                                label = cells[0].get_text(strip=True).lower()
                                value = cells[1].get_text(strip=True)
                                
                                if 'organization' in label or 'agency' in label or 'buyer' in label:
                                    tender_data["organization"] = value
                                elif 'country' in label or 'location' in label:
                                    tender_data["country"] = value
                                elif 'deadline' in label or 'closing' in label or 'submission' in label:
                                    tender_data["deadline"] = value
                                elif 'published' in label or 'posted' in label or 'date' in label:
                                    tender_data["published"] = value
                                elif 'type' in label or 'category' in label or 'classification' in label:
                                    tender_data["type"] = value
                    
                    # Look for dl/dt/dd pairs
                    for dt in soup.find_all('dt'):
                        dd = dt.find_next_sibling('dd')
                        if dd:
                            label = dt.get_text(strip=True).lower()
                            value = dd.get_text(strip=True)
                            
                            if 'organization' in label or 'agency' in label:
                                tender_data["organization"] = value
                            elif 'country' in label or 'location' in label:
                                tender_data["country"] = value
                            elif 'deadline' in label or 'closing' in label:
                                tender_data["deadline"] = value
                            elif 'published' in label or 'posted' in label:
                                tender_data["published"] = value
                            elif 'type' in label or 'category' in label:
                                tender_data["type"] = value
                    
                    # If title is still empty, try meta tags
                    if not tender_data["title"]:
                        meta_title = (soup.find('meta', {'property': 'og:title'}) or 
                                    soup.find('meta', {'name': 'title'}))
                        if meta_title:
                            tender_data["title"] = meta_title.get('content', '').strip()
                    
                    # If still no title, use a fallback
                    if not tender_data["title"]:
                        tender_data["title"] = f"UNGM Notice {notice_id}"
                    
                elif response.status_code == 403:
                    tender_error = "Access forbidden by UNGM. The notice may require login or may not exist."
                elif response.status_code == 404:
                    tender_error = f"Notice {notice_id} not found on UNGM."
                else:
                    tender_error = f"Failed to fetch from UNGM: HTTP {response.status_code}"
                    
            except httpx.TimeoutException:
                tender_error = "Timeout while fetching from UNGM. Please try again."
            except Exception as e:
                tender_error = f"Error: {str(e)}"
            
            # Combine bid request with tender data
            result = {
                "bid_request": {
                    "id": bid_request["id"],
                    "tender_notice_id": bid_request["tender_notice_id"],
                    "from_user_id": bid_request["from_user_id"],
                    "from_username": bid_request["from_username"],
                    "from_full_name": bid_request["from_full_name"],
                    "to_user_id": bid_request["to_user_id"],
                    "to_username": bid_request["to_username"],
                    "to_full_name": bid_request["to_full_name"],
                    "bid_status": bid_request["bid_status"],
                    "notes": bid_request["notes"],
                    "requested_at": bid_request["requested_at"],
                    "updated_at": bid_request["updated_at"],
                    "tender_title": bid_request.get("tender_title"),
                    **summary_fields(bid_request),
                },
                "tender": tender_data,
                "tender_error": tender_error
            }
            
            results.append(result)
    
    return results


@router.get("/my-requests", response_model=List[BidRequestResponse])
async def get_my_bid_requests(
    request_type: str = "all",  # "all", "sent", or "received"
    current_user: dict = Depends(get_current_user)
):
    """
    Get bid requests for the current user.
    - request_type="sent": Requests sent by the user (from Kowsalya)
    - request_type="received": Requests received by the user (to Bernard)
    - request_type="all": All requests (default)
    """
    if request_type not in ["all", "sent", "received"]:
        raise HTTPException(
            status_code=400,
            detail="request_type must be 'all', 'sent', or 'received'"
        )
    
    requests = get_user_bid_requests(current_user["id"], request_type)
    return requests


@router.get("/{bid_request_id}", response_model=BidRequestResponse)
async def get_bid_request(
    bid_request_id: int,
    current_user: dict = Depends(get_current_user)
):
    """Get a specific bid request by ID."""
    bid_request = get_bid_request_by_id(bid_request_id)
    
    if not bid_request:
        raise HTTPException(status_code=404, detail="Bid request not found")
    
    # Check if user is involved in this request
    if bid_request["from_user_id"] != current_user["id"] and bid_request["to_user_id"] != current_user["id"]:
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to view this bid request"
        )
    
    return bid_request


@router.get("/tender/{tender_notice_id}", response_model=List[BidRequestResponse])
async def get_bid_requests_by_tender(
    tender_notice_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get all bid requests for a specific tender notice."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT br.*, 
               u1.username as from_username, u1.full_name as from_full_name,
               u2.username as to_username, u2.full_name as to_full_name
        FROM bid_requests br
        JOIN users u1 ON br.from_user_id = u1.id
        JOIN users u2 ON br.to_user_id = u2.id
        WHERE br.tender_notice_id = ?
        AND (br.from_user_id = ? OR br.to_user_id = ?)
        ORDER BY br.requested_at DESC
    """, (tender_notice_id, current_user["id"], current_user["id"]))
    
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return results


# Initialize table when module is imported
init_bid_request_table()

# After the table exists: adds the ai_summary columns
from api.tender_summary import generate_summary, mark_pending, summary_fields  # noqa: E402
