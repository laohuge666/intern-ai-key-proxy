from __future__ import annotations

import json
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import keypool
from .config import settings
from .state import state
from .stats import stats

router = APIRouter(prefix="/admin/api", tags=["admin"])

# 会话令牌有效期 7 天；令牌持久化到文件，容器重启后仍有效
SESSION_TTL = 7 * 24 * 3600
SESSIONS_FILE = os.environ.get("SESSIONS_FILE", "/app/data/sessions.json")
_sessions: dict = {}
_sessions_mtime = 0.0


def _load_sessions() -> None:
    """启动时从文件恢复会话（容器重启不丢失登录态）。"""
    global _sessions_mtime
    try:
        st = os.stat(SESSIONS_FILE)
        if st.st_mtime == _sessions_mtime:
            return
        _sessions_mtime = st.st_mtime
        with open(SESSIONS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            now = time.time()
            _sessions.clear()
            _sessions.update({k: v for k, v in data.items() if isinstance(v, (int, float)) and v > now})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save_sessions() -> None:
    try:
        os.makedirs(os.path.dirname(SESSIONS_FILE) or ".", exist_ok=True)
        tmp = f"{SESSIONS_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_sessions, fh)
        os.replace(tmp, SESSIONS_FILE)
        try:
            os.chmod(SESSIONS_FILE, 0o600)
        except OSError:
            pass
    except OSError:
        pass

# 下游 API 密钥持久化文件（支持多密钥，创建/删除即时生效，重启不丢失）
API_KEYS_FILE = os.environ.get("API_KEYS_FILE", "/app/data/api_keys.json")


class LoginIn(BaseModel):
    password: str


class SetupIn(BaseModel):
    password: str


class ChangePwIn(BaseModel):
    old_password: str
    new_password: str


class AddKeyIn(BaseModel):
    key: str


class CreateApiKeyIn(BaseModel):
    name: str = ""


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    _save_sessions()
    return token


def _require_auth(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    token = auth.split(" ", 1)[1] if " " in auth else ""
    if not token:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    _load_sessions()
    exp = _sessions.get(token)
    if not exp or exp < time.time():
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return token


def _mask_key(k: str) -> str:
    return (k[:6] + "..." + k[-4:]) if len(k) > 12 else "***"


# ---------- 下游 API 密钥（支持多个） ----------

def _load_api_keys() -> list:
    """读取持久化的下游密钥列表；没有则用 .env 里的 PROXY_API_KEY 初始化。"""
    try:
        with open(API_KEYS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list) and data:
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    # 首次：把环境变量的密钥作为默认密钥
    if settings.proxy_api_key:
        return [{"id": "default", "name": "默认密钥", "key": settings.proxy_api_key}]
    return []


def _save_api_keys(keys: list) -> None:
    os.makedirs(os.path.dirname(API_KEYS_FILE) or ".", exist_ok=True)
    tmp = f"{API_KEYS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(keys, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, API_KEYS_FILE)
    try:
        os.chmod(API_KEYS_FILE, 0o600)
    except OSError:
        pass


def _valid_api_keys() -> set:
    """当前有效的下游密钥集合（用于转发鉴权校验）。"""
    return {k["key"] for k in _load_api_keys() if k.get("key")}


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
        "api_keys": len(_load_api_keys()),
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
    _save_sessions()
    return {"ok": True}


@router.get("/accounts")
async def admin_accounts(token: str = Depends(_require_auth)) -> dict:
    """账号池详情：每个上游 Key 的脱敏状态。"""
    snap = await keypool.pool.snapshot()
    return {"accounts": snap}


@router.post("/accounts")
async def admin_add_account(body: AddKeyIn, token: str = Depends(_require_auth)) -> dict:
    """添加一个上游账号（Key 明文写入 data/keys.json，立即生效）。"""
    try:
        idx = await keypool.pool.add(body.key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    snap = await keypool.pool.snapshot()
    return {"ok": True, "index": idx, "account": snap[idx]}


@router.delete("/accounts/{index}")
async def admin_del_account(index: int, token: str = Depends(_require_auth)) -> dict:
    """删除一个上游账号（至少保留一个）。"""
    try:
        await keypool.pool.remove(index)
    except IndexError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "accounts": await keypool.pool.snapshot()}


@router.get("/keys")
async def admin_keys(token: str = Depends(_require_auth)) -> dict:
    """下游 API 密钥列表（脱敏显示）。"""
    keys = _load_api_keys()
    return {
        "keys": [
            {
                "id": k["id"],
                "name": k.get("name", ""),
                "key": _mask_key(k["key"]),
                "created": k.get("created", "—"),
            }
            for k in keys
        ]
    }


@router.post("/keys")
async def admin_create_key(body: CreateApiKeyIn, token: str = Depends(_require_auth)) -> dict:
    """创建新的下游 API 密钥，明文返回一次（之后只脱敏显示）。"""
    keys = _load_api_keys()
    new_key = secrets.token_urlsafe(32)
    entry = {
        "id": "k" + secrets.token_hex(4),
        "name": (body.name or "").strip() or "未命名",
        "key": new_key,
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }
    keys.append(entry)
    _save_api_keys(keys)
    return {"ok": True, "id": entry["id"], "name": entry["name"], "key": new_key}


@router.delete("/keys/{key_id}")
async def admin_del_key(key_id: str, token: str = Depends(_require_auth)) -> dict:
    """删除下游 API 密钥（至少保留一个，否则客户端无法调用）。"""
    keys = _load_api_keys()
    if len(keys) <= 1:
        raise HTTPException(status_code=400, detail="至少保留一个密钥")
    left = [k for k in keys if k["id"] != key_id]
    if len(left) == len(keys):
        raise HTTPException(status_code=404, detail="密钥不存在")
    _save_api_keys(left)
    return {"ok": True}


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
