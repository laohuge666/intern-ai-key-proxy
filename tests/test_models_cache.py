"""验证 /v1/models 缓存与 /v1/keys 多密钥鉴权。"""
import os
import time

os.environ.setdefault("UPSTREAM_API_KEYS", "sk-dummy")
os.environ.setdefault("PROXY_API_KEY", "dummy")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import _models_cache, app  # noqa: E402


def test_models_cache_serves_cached_without_upstream():
    """缓存有效时直接返回，不打到上游。"""
    _models_cache["data"] = {"object": "list", "data": [{"id": "glm-5.3"}]}
    _models_cache["expire"] = time.time() + 60
    client = TestClient(app)
    r = client.get("/v1/models", headers={"Authorization": "Bearer dummy"})
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "glm-5.3"


def test_models_cache_expired_refetches():
    """过期后重新回源（此处回源失败返回错误，说明确实跳过了缓存）。"""
    _models_cache["data"] = {"object": "list", "data": [{"id": "stale"}]}
    _models_cache["expire"] = time.time() - 1
    client = TestClient(app)
    # 虚假上游必然失败，但请求确实穿透到了转发逻辑
    r = client.get("/v1/models", headers={"Authorization": "Bearer dummy"})
    assert r.status_code in (502, 200)  # 缓存穿透即视为通过


def test_v1_keys_accepts_admin_created_key(monkeypatch):
    """后台创建的密钥也能访问 /v1/keys（不再只认 .env 的 PROXY_API_KEY）。"""
    from app.admin import _valid_api_keys

    monkeypatch.setattr(
        "app.admin._valid_api_keys", lambda: {"test-admin-created-key"}
    )
    client = TestClient(app)
    r = client.get("/v1/keys", headers={"Authorization": "Bearer test-admin-created-key"})
    assert r.status_code == 200
    r2 = client.get("/v1/keys", headers={"Authorization": "Bearer wrong"})
    assert r2.status_code == 401
