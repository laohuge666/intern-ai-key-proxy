from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.keypool import KeyPool
from app.main import app


def _fake_response(status_code=200, body=b'{"ok": true}', content_type="application/json"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = {"content-type": content_type}
    return resp


def _fake_json_response(status_code=200, payload=None):
    """真实可序列化的假响应，避免 MagicMock 进 JSONResponse。"""
    import json as _json

    body = _json.dumps(payload if payload is not None else {"ok": True}).encode()
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = {"content-type": "application/json"}
    return resp


def _patch_pool(monkeypatch, keys=("k1", "k2")):
    """让 proxy 与 main 模块都使用同一个独立测试池。"""
    test_pool = KeyPool(list(keys), cooldown_seconds=30)
    monkeypatch.setattr("app.proxy.pool", test_pool)
    monkeypatch.setattr("app.keypool.pool", test_pool)
    monkeypatch.setattr("app.proxy.settings.proxy_api_key", "secret")
    monkeypatch.setattr("app.proxy.settings.require_auth", True)
    return test_pool


def _patch_http(monkeypatch, recorder, responses):
    """把 httpx.AsyncClient.request 换成记录调用的假实现。

    responses 可以是：单个假响应对象、假响应列表、或 次数->假响应 的函数。
    注意 MagicMock 本身可调用，所以只接受 types.FunctionType 作为函数形式。
    """

    import types

    is_func = isinstance(responses, types.FunctionType)

    async def fake_request(self, method, url, content=None, headers=None, params=None):
        recorder.append(
            {
                "url": str(url),
                "auth": (headers or {}).get("Authorization"),
                "params": params,
                "content": content,
            }
        )
        if is_func:
            return responses(len(recorder))
        if isinstance(responses, (list, tuple)):
            return responses[min(len(recorder) - 1, len(responses) - 1)]
        return responses  # 单个响应对象，直接返回

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


def test_missing_auth_is_rejected(monkeypatch):
    _patch_pool(monkeypatch)
    client = TestClient(app)
    r = client.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "invalid proxy API key"


def test_path_traversal_is_rejected(monkeypatch):
    _patch_pool(monkeypatch)
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer secret"})
    r = client.get("/v1/../secret")
    assert r.status_code in (400, 404)


def test_non_stream_success_uses_round_robin_key(monkeypatch):
    pool = _patch_pool(monkeypatch)
    rec = []
    _patch_http(monkeypatch, rec, _fake_json_response())
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer secret"})

    r = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert rec[0]["url"].endswith("/v1/chat/completions")
    assert rec[0]["auth"] == "Bearer k1"  # 轮询从第一个 Key 开始


def test_429_triggers_key_switch_and_retry(monkeypatch):
    pool = _patch_pool(monkeypatch)
    rec = []
    _patch_http(monkeypatch, rec, lambda n: _fake_json_response(429 if n == 1 else 200))
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer secret"})

    r = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert r.status_code == 200
    assert [c["auth"] for c in rec] == ["Bearer k1", "Bearer k2"]
    snap = pool.snapshot_sync()
    assert snap[0]["status"] == "cooling"  # k1 被冷却
    assert snap[1]["status"] == "ready"


def test_all_keys_fail_returns_502(monkeypatch):
    _patch_pool(monkeypatch)
    rec = []
    _patch_http(monkeypatch, rec, lambda n: _fake_json_response(429))
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer secret"})

    r = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert r.status_code == 502
    assert "failed" in r.json()["error"]["message"]


def test_query_params_are_forwarded(monkeypatch):
    _patch_pool(monkeypatch)
    rec = []
    _patch_http(monkeypatch, rec, _fake_json_response())
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer secret"})

    client.get("/v1/models?limit=5")
    assert rec[0]["params"] == "limit=5"


def test_health_and_keys_endpoints(monkeypatch):
    pool = _patch_pool(monkeypatch)
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer secret"})

    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["total_keys"] == 2
    assert r.json()["ready_keys"] == 2

    r = client.get("/v1/keys")
    assert r.status_code == 200
    assert len(r.json()["keys"]) == 2

    # 无鉴权访问 keys 状态应被拒绝
    client.headers.clear()
    r = client.get("/v1/keys")
    assert r.status_code == 401


def test_missing_upstream_config_fails_loudly(monkeypatch):
    """require_auth=True 但未设置 PROXY_API_KEY 时必须启动失败。"""
    import os

    monkeypatch.setattr("app.config.settings.upstream_api_keys", "k1")
    monkeypatch.setattr("app.config.settings.proxy_api_key", "")
    from app.config import validate_settings

    with pytest.raises(RuntimeError):
        validate_settings()
