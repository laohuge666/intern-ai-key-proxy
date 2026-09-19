from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import keypool
from .config import settings
from .state import state
from .stats import stats

router = APIRouter(prefix="/admin/api", tags=["admin"])

# 会话令牌有效期 7 天；令牌存在内存，容器重启需重新登录
SESSION_TTL = 7 * 24 * 3600
_sessions: dict = {}


class LoginIn(BaseModel):
    password: str


class SetupIn(BaseModel):
    password: str


class ChangePwIn(BaseModel):
    old_password: str
    new_password: str


def _create_session() -> str:
    import secrets

    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def _require_auth(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    token = auth.split(" ", 1)[1] if " " in auth else ""
    exp = _sessions.get(token)
    if not exp or exp < time.time():
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return token


@router.get("/status")
async def admin_status() -> dict:
    """控制台首页数据：网关状态、账号数、密钥数、用量统计。无需登录。"""
    snap = await keypool.pool.snapshot()
    ready = sum(1 for s in snap if s["status"] == "ready")
    st = stats.snapshot()
    return {
        "needs_setup": state.needs_setup,
        "gateway_status": "online" if ready else "degraded",
        "total_accounts": len(snap),
        "ready_accounts": ready,
        "cooling_accounts": sum(1 for s in snap if s["status"] == "cooling"),
        "api_keys": 1,  # 下游统一一个 PROXY_API_KEY
        "stats": st,
        "upstream": settings.upstream_base_url,
    }


@router.post("/setup")
async def admin_setup(body: SetupIn) -> dict:
    if not state.needs_setup:
        raise HTTPException(status_code=409, detail="管理员密码已设置")
    state.setup(body.password)
    return {"ok": True, "token": _create_session()}


@router.post("/login")
async def admin_login(body: LoginIn) -> dict:
    if state.needs_setup:
        raise HTTPException(status_code=409, detail="请先设置初始密码")
    if not state.verify(body.password):
        raise HTTPException(status_code=401, detail="密码错误")
    return {"ok": True, "token": _create_session()}


@router.post("/logout")
async def admin_logout(token: str = Depends(_require_auth)) -> dict:
    _sessions.pop(token, None)
    return {"ok": True}


@router.get("/accounts")
async def admin_accounts(token: str = Depends(_require_auth)) -> dict:
    """账号池详情：每个上游 Key 的脱敏状态。"""
    snap = await keypool.pool.snapshot()
    return {"accounts": snap}


@router.get("/keys")
async def admin_keys(token: str = Depends(_require_auth)) -> dict:
    """下游 API 密钥信息（只展示脱敏后的值）。"""
    k = settings.proxy_api_key
    masked = (k[:6] + "..." + k[-4:]) if len(k) > 12 else "***"
    return {
        "keys": [
            {
                "id": "default",
                "key": masked,
                "created": "—",
            }
        ]
    }


@router.post("/password")
async def admin_change_pw(body: ChangePwIn, token: str = Depends(_require_auth)) -> dict:
    state.change_password(body.old_password, body.new_password)
    return {"ok": True}


@router.get("/models")
async def admin_models(token: str = Depends(_require_auth)) -> dict:
    """模型测试：直接查上游模型列表（走代理自身的 Key 池）。"""
    import httpx

    timeouts = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=15.0)
    idx, api_key, _ = await keypool.pool.acquire()
    url = f"{settings.upstream_base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=timeouts) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code >= 400:
            await keypool.pool.mark_error(idx)
            return {"ok": False, "status": resp.status_code, "detail": resp.text[:500]}
        await keypool.pool.mark_ok(idx)
        data = resp.json()
        ids = [m.get("id") for m in data.get("data", [])]
        return {"ok": True, "models": ids}
    except Exception as exc:
        await keypool.pool.mark_error(idx)
        return {"ok": False, "detail": str(exc)[:300]}


@router.get("/settings")
async def admin_settings(token: str = Depends(_require_auth)) -> dict:
    return {
        "upstream_base_url": settings.upstream_base_url,
        "key_cooldown_seconds": settings.key_cooldown_seconds,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "require_auth": settings.require_auth,
        "total_keys": len(settings.keys),
    }
