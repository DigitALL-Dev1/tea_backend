"""Lumen development API. Replace bootstrap auth with GAXIS M1 for production."""
import os
from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from api.get_tender import router as tender_router
from api.auth import router as auth_router
from api.bid_request import router as bid_request_router
from api.approved_tenders import router as approved_tenders_router
from api.ai_tenders import router as ai_tenders_router
from api.dashboard import router as dashboard_router


# Load environment variables
load_dotenv()

# Create FastAPI app
app = FastAPI(title="Gaxis Lumen API", version="1.0.0")

# Global exception handler for better debugging


# CORS configuration
FRONTEND_ORIGINS = [origin.strip() for origin in os.getenv("FRONTEND_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)

# Include routers
app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(bid_request_router, prefix="/api/v1/bid-requests", tags=["Bid Requests"])
app.include_router(approved_tenders_router, prefix="/api/v1/approved-tenders", tags=["Approved Tenders"])
app.include_router(ai_tenders_router, prefix="/api/v1/ai-tenders", tags=["AI Tenders"])
app.include_router(dashboard_router, prefix="/api/v1/dashboard", tags=["Dashboard"])
app.include_router(tender_router, prefix="/api/v1/tenders", tags=["Tenders"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "Gaxis Lumen API", "status": "running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)
