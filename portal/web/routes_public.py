from fastapi import APIRouter, Request

from .access import PUBLIC
from .render import page

router = APIRouter()


@router.get("/healthz")
def healthz(principal=PUBLIC):
    return {"ok": True}


@router.get("/")
def start_here(request: Request, principal=PUBLIC):
    return page(request, "start_here.html")
