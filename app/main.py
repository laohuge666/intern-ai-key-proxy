from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings, validate_settings
from . import keypool
from .proxy import proxy_request
from .admin import router as admin_router
from .stats import stats
from .state import state

app = FastAPI(
    title="Intern-AI Key Proxy",
    description="OpenAI 兼容接口的多 Key 反向代理（轮询 + 限流自动切换）",
    version="1.0.0",
)


@app.get("/")
async def root() -> Any:
    return {"service": "intern-ai-key-proxy", "status": "ok", "version": "1.0.0"}


@app.get("/health")
async def health() -> Any:
    snap = await keypool.pool.snapshot()
    ready = sum(1 for s in snap if s["status"] == "ready")
    return {
        "status": "ok" if ready else "degraded",
        "total_keys": len(snap),
        "ready_keys": ready,
        "cooling_keys": sum(1 for s in snap if s["status"] == "cooling"),
        "upstream": settings.upstream_base_url,
    }


@app.get("/v1/keys")
async def keys_status(request: Request) -> Any:
    """下游密钥本身只做一次校验，避免未授权者探测 Key 池状态。"""
    if settings.require_auth:
        auth = request.headers.get("authorization", "")
        token = auth.split(" ", 1)[1] if " " in auth else ""
        from .proxy import _authorized

        if not _authorized(token):
            return JSONResponse(status_code=401, content={"error": "invalid proxy API key"})
    return {"keys": await keypool.pool.snapshot()}


# /v1/models 列表缓存：客户端插件常每隔数十秒探活，避免每次都打到上游
_models_cache: dict = {"data": None, "expire": 0.0}
_MODELS_TTL = 60.0


@app.api_route("/v1/models", methods=["GET"])
async def models_cached(request: Request) -> Any:
    import time as _time

    now = _time.time()
    if _models_cache["data"] is not None and _models_cache["expire"] > now:
        return JSONResponse(content=_models_cache["data"])
    # 鉴权与 catch_all 一致，直接复用转发逻辑并缓存结果
    resp = await proxy_request(request, "models")
    try:
        body = resp.body if hasattr(resp, "body") else None
        if body is not None and resp.status_code == 200:
            import json as _json

            _models_cache["data"] = _json.loads(body)
            _models_cache["expire"] = now + _MODELS_TTL
    except Exception:
        pass
    return resp


app.include_router(admin_router)
app.mount("/admin/static", StaticFiles(directory="app/static"), name="static")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page() -> str:
    with open("app/static/admin.html", "r", encoding="utf-8") as fh:
        return fh.read()


# 所有 /v1/... 请求统一转发到上游（chat/completions、embeddings、models 等）
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, path: str) -> Any:
    return await proxy_request(request, path)


@app.on_event("startup")
async def _startup_check() -> None:
    from .config import validate_settings

    validate_settings()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
