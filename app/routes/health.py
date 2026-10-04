from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from app.db import get_session

router = APIRouter()


@router.get("/health")
def health(session: Annotated[Session, Depends(get_session)]) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "degraded", "database": "down"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "up"})
