"""Вход и выход: ссылка из бота, initData мини-приложения, healthz."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from hub.auth import redeem_login_link, revoke_session, tma_login
from hub.web.deps import COOKIE, require_write, session_token, templates

router = APIRouter()


class TmaIn(BaseModel):
    init_data: str


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=request.app.state.settings.session_days * 86400,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get("/auth/link")
async def auth_link(request: Request, t: str = "") -> Response:
    state = request.app.state
    result = await redeem_login_link(state.db, t, state.clock, state.settings.session_days)
    if result is None:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"title": "Ссылка устарела", "text": "Ссылка устарела — попроси новую в боте /web"},
            status_code=400,
        )
    response = RedirectResponse("/", status_code=303)
    _set_cookie(request, response, result[0])
    return response


@router.post("/auth/tma")
async def auth_tma(request: Request, body: TmaIn) -> Response:
    state = request.app.state
    result = await tma_login(state.db, body.init_data, state.settings, state.clock)
    if result is None:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    response = JSONResponse({"token": result[0]})
    _set_cookie(request, response, result[0])
    return response


@router.post("/logout", dependencies=[Depends(require_write)])
async def logout(request: Request) -> Response:
    state = request.app.state
    token = session_token(request)
    if token is not None:
        await revoke_session(state.db, token, state.clock)
    response = Response(status_code=204, headers={"HX-Redirect": "/"})
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response
