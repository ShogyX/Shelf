from fastapi import APIRouter

from ..config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "app": get_settings().app_name}
