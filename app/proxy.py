from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import settings
from .keypool import mask, pool

# 这些响应头不应原样透传给下游（可能是上游网关的连接信息）
_DROP_HEADERS = {"content-encoding", "transfer-encoding", "content-length", "connection"}


def _is_stream_request(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        return True
    # OpenAI 客户端在流式请求里通常显式带 stream=true
    try:
        ctype = request.headers.get("content-type", "")
        if "application/json" in ctype:
            body = request.scope.get("_raw_json_body")
            if isinstance(body, dict) and body.get("stream") is True:
                return True
    except Exception:
        pass
    return False


def _build_headers(source: Dict[str, str], api_key: str) -> Dict[str, str]:
    out = {
        k: v
        for k, v in source.items()
        if k.lower() not in _DROP_HEADERS and k.lower() != "authorization"
        and k.lower() != "host"
    }
    out["Authorization"] = f"Bearer {api_key}"
    return out


async def _read_json_body(request: Request) -> Optional[Dict[str, Any]]:
    """缓存式读取 JSON body，供流式判断与转发共用，避免重复读取。"""
    cached = request.scope.get("_raw_json_body")
    if cached is not None:
        return cached if isinstance(cached, dict) else None
    try:
        raw = await request.body()
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if isinstance(data, dict):
        request.scope["_raw_json_body"] = data
        return data
    return None


def _error_json(status: int, message: str, trace: Optional[str] = None) -> JSONResponse:
    payload: Dict[str, Any] = {
        "error": {
            "message": message,
            "type": "proxy_error",
            "param": None,
            "code": f"proxy_{status}",
        }
    }
    if trace:
        payload["error"]["proxy_detail"] = trace  # type: ignore[index]
    return JSONResponse(status_code=status, content=payload)


async def proxy_request(request: Request, path: str) -> Any:
    """将下游请求转发到上游，自动注入轮询出的 API Key。"""
    # 1) 鉴权
    if settings.require_auth:
        auth = request.headers.get("authorization", "")
        token = auth.split(" ", 1)[1] if " " in auth else ""
        if token != settings.proxy_api_key:
            return _error_json(401, "invalid proxy API key")

    # 2) 只允许安全的路径片段（防止 // 绕过跑到别的域）
    if not path or any(seg in ("..", "") for seg in path.split("/")):
        return _error_json(400, "invalid request path")

    # 3) 取 Key
    idx, api_key, degraded = await pool.acquire()
    upstream_url = f"{settings.upstream_base_url.rstrip('/')}/{path.lstrip('/')}"

    body_bytes = await request.body()
    streaming = _is_stream_request(request)
    headers = _build_headers(dict(request.headers), api_key)

    timeouts = httpx.Timeout(
        connect=15.0,
        read=float(settings.request_timeout_seconds),
        write=30.0,
        pool=15.0,
    )

    # 4) 非流式：一次性请求 + Key 自动重试
    if not streaming:
        attempted: set = set()
        last_err: Optional[str] = None
        for _ in range(max(1, min(len(pool), 5))):
            try:
                async with httpx.AsyncClient(timeout=timeouts) as client:
                    resp = await client.request(
                        request.method,
                        upstream_url,
                        content=body_bytes,
                        headers=headers,
                        params=request.url.query,
                    )
                # 429 / 401 / 403 视为该 Key 有问题，冷却后换 Key 重试
                if resp.status_code in (401, 403, 429):
                    await pool.mark_error(idx)
                    attempted.add(idx)
                    last_err = f"upstream {resp.status_code}"
                    if len(attempted) >= min(len(pool), 5):
                        break
                    idx, api_key, degraded = await pool.acquire()
                    headers = _build_headers(dict(request.headers), api_key)
                    continue
                await pool.mark_ok(idx)
                raw = resp.content
                resp_headers = {
                    k: v for k, v in dict(resp.headers).items() if k.lower() not in _DROP_HEADERS
                }
                ctype = resp.headers.get("content-type") if hasattr(resp.headers, "get") else None
                # JSONResponse 需要可序列化对象；上游可能返回 bytes 或已解析对象
                if isinstance(raw, (bytes, bytearray)):
                    try:
                        parsed = json.loads(raw)
                        return JSONResponse(
                            status_code=resp.status_code, content=parsed,
                            headers=resp_headers, media_type=ctype,
                        )
                    except Exception:
                        return JSONResponse(
                            status_code=resp.status_code, content={"raw": raw.decode("utf-8", "replace")},
                            headers=resp_headers, media_type=ctype,
                        )
                return JSONResponse(
                    status_code=resp.status_code, content=raw,
                    headers=resp_headers, media_type=ctype,
                )
            except httpx.HTTPError as exc:
                await pool.mark_error(idx)
                last_err = f"transport: {exc.__class__.__name__}"
                attempted.add(idx)
                if len(attempted) >= min(len(pool), 5):
                    break
                idx, api_key, degraded = await pool.acquire()
                headers = _build_headers(dict(request.headers), api_key)
        return _error_json(502, f"all attempted upstream keys failed ({last_err})")

    # 5) 流式：SSE 透传，失败时把错误塞进事件流
    async def stream() -> Any:
        client = httpx.AsyncClient(timeout=timeouts)
        try:
            req = client.build_request(
                request.method,
                upstream_url,
                content=body_bytes if body_bytes else None,
                headers=headers,
                params=request.url.query,
            )
            async with client.stream(req.method, req.url, headers=req.headers, content=req.content) as resp:
                if resp.status_code in (401, 403, 429):
                    await pool.mark_error(idx)
                    err = f"[proxy] upstream {resp.status_code}; key {mask(api_key)} cooled down"
                    yield f"data: {json.dumps({'error': err})}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                    return
                await pool.mark_ok(idx)
                async for chunk in resp.aiter_raw():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            await pool.mark_error(idx)
            err = f"[proxy] stream error: {exc.__class__.__name__}; key {mask(api_key)} cooled down"
            yield f"data: {json.dumps({'error': err})}\n\n".encode()
            yield b"data: [DONE]\n\n"
        finally:
            await client.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
