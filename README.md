# Intern-AI Key Proxy

OpenAI 兼容接口的多 Key 反向代理：把多个上游账号的 API Key 聚合在一个入口后面，对外只暴露一个密钥，内部轮询调度、命中限流自动冷却切换。

- 上游：`https://discovery-api.intern-ai.org.cn/v1`（Intern InkStone，OpenAI 兼容格式）
- 下游：标准 `Authorization: Bearer <PROXY_API_KEY>`，兼容 OpenAI 客户端
- 透明转发 `/v1/*` 全部子路径（chat/completions、embeddings、models 等），支持流式 SSE

## 文件结构

```
intern-ai-key-proxy/
├── app/
│   ├── main.py      # FastAPI 入口：路由 / 健康检查 / 启动校验
│   ├── config.py    # 环境变量与 .env 配置
│   ├── keypool.py   # 轮询 + 冷却的 Key 池
│   └── proxy.py     # 转发核心：鉴权、换 Key 重试、SSE 透传
├── tests/           # 13 个单元测试（pytest）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt / requirements-dev.txt
└── .env.example
```

## 快速开始

```bash
cp .env.example .env
# 填入你的上游 Key（逗号分隔）与下游访问密钥
vi .env

docker compose up -d
```

## 配置项（.env）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `UPSTREAM_BASE_URL` | 上游基址 | `https://discovery-api.intern-ai.org.cn/v1` |
| `UPSTREAM_API_KEYS` | 上游 Key 列表，逗号分隔 | 必填 |
| `PROXY_API_KEY` | 下游访问本代理的密钥 | require_auth=true 时必填 |
| `KEY_COOLDOWN_SECONDS` | Key 出错后的冷却秒数 | 60 |
| `REQUEST_TIMEOUT_SECONDS` | 上游请求超时秒数 | 300 |
| `REQUIRE_AUTH` | 是否强制下游鉴权 | true |

## 调度逻辑

1. 轮询：按顺序循环使用可用 Key
2. 上游返回 401/403/429 或网络错误时，该 Key 进入冷却（默认 60 秒），自动换下一个 Key 重试
3. 非流式请求最多重试 `min(Key 数, 5)` 次；全部失败返回 502
4. 所有 Key 都在冷却中时，返回最早解冻的那一个（降级，不空转）

## 运维端点

```bash
curl -H "Authorization: Bearer $PROXY_API_KEY" http://localhost:8000/health
curl -H "Authorization: Bearer $PROXY_API_KEY" http://localhost:8000/v1/keys
```

`/v1/keys` 返回脱敏后的 Key 状态（仅显示首尾若干字符）、冷却剩余时间、调用次数与错误次数。

## 客户端使用

任何 OpenAI 兼容客户端，把 base_url 指向本代理即可：

```python
from openai import OpenAI
client = OpenAI(
    api_key="<PROXY_API_KEY>",
    base_url="http://<服务器>:8000/v1",
)
resp = client.chat.completions.create(
    model="<上游模型名>",
    messages=[{"role": "user", "content": "你好"}],
)
```

## 测试

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
UPSTREAM_API_KEYS=dummy PROXY_API_KEY=dummy .venv/bin/python -m pytest -q
# 13 passed
```

## 安全说明

- 下游必须携带 `PROXY_API_KEY`；未授权请求返回 401
- 上游 Key 只存在服务端内存与 `.env` 中，日志和 `/v1/keys` 端点都做脱敏
- `.env` 已在 `.gitignore` 中，不要提交到仓库
- 请仅聚合你自己拥有或已获明确授权的账号 Key，并遵守平台服务条款
