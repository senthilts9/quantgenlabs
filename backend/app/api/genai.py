"""GenAI Monitor + Model Library."""
from __future__ import annotations

from fastapi import APIRouter, Query

from ..core import genai

router = APIRouter(prefix="/api/genai", tags=["genai"])


@router.get("/monitor")
def monitor(limit: int = Query(200, le=1000), window: int = Query(50, ge=5, le=500)) -> dict:
    from .oms import oms
    return genai.analyse(oms().store.list_llm(limit), window)


@router.get("/models")
def models() -> dict:
    return genai.inventory()
