from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from typing import Optional

# 状态文件位置：docker-compose 挂载 ./data:/app/data，重启容器后密码与配置不丢失
DEFAULT_STATE_FILE = os.environ.get("STATE_FILE", "/app/data/admin.json")


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()


class State:
    """管理后台的持久化状态：管理员密码哈希、会话令牌。

    首次打开时若不存在密码，进入「设置初始密码」流程；之后登录用密码换令牌。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DEFAULT_STATE_FILE
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._data = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @property
    def needs_setup(self) -> bool:
        return not self._data.get("password_hash")

    def setup(self, password: str) -> None:
        if len(password) < 6:
            raise ValueError("密码至少 6 位")
        if not self.needs_setup:
            raise RuntimeError("管理员密码已设置，请走登录流程")
        salt = secrets.token_hex(16)
        self._data["password_hash"] = _hash_password(password, salt)
        self._data["salt"] = salt
        self._data["created_at"] = time.time()
        self._save()

    def verify(self, password: str) -> bool:
        stored = self._data.get("password_hash")
        salt = self._data.get("salt")
        if not stored or not salt:
            return False
        return secrets.compare_digest(_hash_password(password, salt), stored)

    def change_password(self, old: str, new: str) -> None:
        if not self.verify(old):
            raise ValueError("原密码错误")
        if len(new) < 6:
            raise ValueError("新密码至少 6 位")
        salt = secrets.token_hex(16)
        self._data["password_hash"] = _hash_password(new, salt)
        self._data["salt"] = salt
        self._save()


state = State()
