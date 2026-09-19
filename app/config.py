from __future__ import annotations

from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 上游 OpenAI 兼容接口基址（不含尾部斜杠）
    upstream_base_url: str = "https://discovery-api.intern-ai.org.cn/v1"

    # 上游 API Key 列表，逗号分隔；支持 "key, key2" 带空格
    upstream_api_keys: str = ""

    # 下游访问本代理时使用的密钥（Bearer Token）
    proxy_api_key: str = ""

    # 命中限流/错误后该 Key 的冷却秒数
    key_cooldown_seconds: int = 60

    # 上游请求超时秒数（流式建议放大）
    request_timeout_seconds: int = 300

    # 未携带正确密钥时是否拒绝访问；调试时可置为空串放行
    require_auth: bool = True

    @property
    def keys(self) -> List[str]:
        return [k.strip() for k in self.upstream_api_keys.split(",") if k.strip()]


settings = Settings()


def validate_settings() -> None:
    """启动时校验必填配置。放在启动阶段而非导入阶段，便于测试注入环境变量。"""
    if not settings.keys:
        raise RuntimeError(
            "未配置 UPSTREAM_API_KEYS：请在 .env 中以逗号分隔填入至少一个上游 API Key"
        )
    if settings.require_auth and not settings.proxy_api_key:
        raise RuntimeError(
            "未配置 PROXY_API_KEY：require_auth=true 时必须设置下游访问密钥"
        )
