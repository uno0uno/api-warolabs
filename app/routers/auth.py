from fastapi import APIRouter, Request, Response
from app.services.auth_service import get_session_data
from app.models.auth import SessionResponse

router = APIRouter()

@router.get("/session", response_model=SessionResponse)
async def get_session(request: Request, response: Response):
    """
    Get current session data
    """
    return await get_session_data(request, response)

@router.post("/signout")
async def signout_placeholder():
    return {"message": "Auth signout endpoint - coming soon"}