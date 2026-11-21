"""Authentication API with SQLite database."""
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import os

router = APIRouter()
security = HTTPBearer()

# Database file path
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth.db")

# ==================== Database Functions ====================

def get_db_connection():
    """Create and return a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database with users table and seed data."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            email TEXT,
            full_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create sessions table for token management
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    # Check if users already exist
    cursor.execute("SELECT COUNT(*) as count FROM users")
    count = cursor.fetchone()["count"]
    
    if count == 0:
        # Add the two users with hashed passwords
        users_data = [
            {
                "username": "kowsalya",
                "password": "kowsalya123",  # Plain password, will be hashed
                "email": "kowsalya@tea.com",
                "full_name": "Kowsalya"
            },
            {
                "username": "bernard",
                "password": "bernard123",  # Plain password, will be hashed
                "email": "bernard@tea.com",
                "full_name": "Bernard"
            }
        ]
        
        for user in users_data:
            password_hash = hash_password(user["password"])
            cursor.execute("""
                INSERT INTO users (username, password_hash, email, full_name)
                VALUES (?, ?, ?, ?)
            """, (user["username"], password_hash, user["email"], user["full_name"]))
        
        print("✓ Database initialized with 2 users (kowsalya, bernard)")
    
    conn.commit()
    conn.close()


def hash_password(password: str) -> str:
    """Hash a password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    return hash_password(plain_password) == password_hash


def create_session_token(user_id: int) -> tuple[str, datetime]:
    """Create a new session token for a user."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=24)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO sessions (user_id, token, expires_at)
        VALUES (?, ?, ?)
    """, (user_id, token, expires_at))
    conn.commit()
    conn.close()
    
    return token, expires_at


def get_user_by_token(token: str) -> Optional[dict]:
    """Get user information by session token."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT u.id, u.username, u.email, u.full_name, u.created_at
        FROM users u
        JOIN sessions s ON u.id = s.user_id
        WHERE s.token = ? AND s.expires_at > datetime('now')
    """, (token,))
    
    user = cursor.fetchone()
    conn.close()
    
    if user:
        return dict(user)
    return None


def delete_session(token: str):
    """Delete a session token (logout)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def get_user_by_username(username: str) -> Optional[dict]:
    """Get user by username."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()
    
    if user:
        return dict(user)
    return None


# ==================== Pydantic Models ====================

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: dict
    expires_at: str


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str]
    full_name: Optional[str]
    created_at: str


# ==================== Dependency Functions ====================

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Dependency to get the current authenticated user."""
    token = credentials.credentials
    user = get_user_by_token(token)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )
    
    return user


# ==================== API Endpoints ====================

@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """Login endpoint - authenticate user and return token."""
    user = get_user_by_username(request.username)
    
    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )
    
    # Create session token
    token, expires_at = create_session_token(user["id"])
    
    # Return user info without password hash
    user_data = {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "full_name": user["full_name"]
    }
    
    return {
        "token": token,
        "user": user_data,
        "expires_at": expires_at.isoformat()
    }


@router.post("/logout")
async def logout(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Logout endpoint - invalidate session token."""
    token = credentials.credentials
    delete_session(token)
    return {"message": "Successfully logged out"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: dict = Depends(get_current_user)):
    """Get current authenticated user information."""
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "email": current_user["email"],
        "full_name": current_user["full_name"],
        "created_at": current_user["created_at"]
    }


@router.get("/users")
async def list_users(current_user: dict = Depends(get_current_user)):
    """List all users (protected endpoint - requires authentication)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, email, full_name, created_at FROM users")
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return {"users": users}


# Initialize database when module is imported
init_db()
