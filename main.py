"""Lumen development API. Replace bootstrap auth with GAXIS M1 for production."""
import os
from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from api.get_tender import router as tender_router


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
app.include_router(tender_router, prefix="/api/v1/tenders", tags=["Tenders"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "Gaxis Lumen API", "status": "running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)
