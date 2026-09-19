# Intern-AI Key Proxy

把多个上游账号的 API Key 聚合在一个入口后面，对外只暴露一个密钥；内部轮询调度，命中限流自动冷却切换。OpenAI 兼容格式，客户端改个 base_url 就能接入。

- **上游**：`https://discovery-api.intern-ai.org.cn/v1`（Intern InkStone / 书生，OpenAI 兼容）
- **下游**：标准 `Authorization: Bearer <PROXY_API_KEY>`
- 透明转发 `/v1/*` 全部子路径（chat/completions、embeddings、models 等），支持流式 SSE

---

## 一键部署

```bash
UPSTREAM_API_KEYS="sk-key1,sk-key2" PROXY_API_KEY="your-secret" \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/laohuge666/intern-ai-key-proxy/main/deploy.sh)"
```

或先下载再执行（生产环境推荐，可先审查脚本内容）：

```bash
curl -fsSL -o /tmp/deploy.sh https://raw.githubusercontent.com/laohuge666/intern-ai-key-proxy/main/deploy.sh
UPSTREAM_API_KEYS="sk-key1,sk-key2" bash /tmp/deploy.sh
```

**脚本做的事**：检查 docker → 克隆到 `/opt/intern-ai-key-proxy` → 生成 `.env`（权限 600）→ 构建镜像并启动 → 健康检查通过后打印访问信息。重新执行同一条命令即可更新部署。

### 环境变量

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `UPSTREAM_API_KEYS` | 上游 Key，多个用英文逗号分隔 | 必填（否则交互询问） |
| `PROXY_API_KEY` | 下游访问本代理的密钥 | 不填自动生成 32 位 |
| `PORT` | 对外端口 | 8000 |
| `INSTALL_DIR` | 安装目录 | `/opt/intern-ai-key-proxy` |
| `UPSTREAM_BASE_URL` | 上游基址 | `https://discovery-api.intern-ai.org.cn/v1` |

---

## 公网访问（Cloudflare 隧道）

如果服务器不能直接暴露端口（NAT/防火墙/无公网 IP），用 Cloudflare 隧道把本地端口映射到域名。以下假设 cloudflared 已安装并登录（`cloudflared login`）。

**1）创建隧道**

```bash
cloudflared tunnel create <隧道名>
```

**2）配置 ingress**（`/etc/cloudflared/config.yml` 或 `~/.cloudflared/config.yml`）

```yaml
tunnel: <隧道ID>
credentials-file: /root/.cloudflared/<隧道ID>.json

ingress:
  - hostname: api.example.com
    service: http://127.0.0.1:8000   # 对应部署时的 PORT
  - service: http_status:404
```

**3）创建 DNS 记录并启动**

```bash
cloudflared tunnel route dns <隧道名> api.example.com
# 用 systemd 托管（开机自启 + 断线重连）
cloudflared service install
systemctl start cloudflared
```

**4）改配置后必须重启隧道才生效**

```bash
systemctl restart cloudflared
```

验证：`curl https://api.example.com/health`

> **注意**：cloudflared 只在启动时读取 ingress 配置。后续新增 hostname 或修改 service，必须 `systemctl restart cloudflared`，否则新规则不生效（表现为一直 404）。

---

## 构建失败排错

**报错 `Temporary failure in name resolution` / `Could not find a version`**

这是 Docker BuildKit 的已知问题：构建容器的 DNS 与宿主机不一致，导致 `pip install` 连不上 PyPI。普通 `docker run` 正常、只有 `docker build` 失败。

解决方法：在 `/etc/docker/daemon.json` 配置公共 DNS，然后重启 docker。

```bash
echo '{"dns": ["1.1.1.1", "8.8.8.8"]}' > /etc/docker/daemon.json
systemctl restart docker
```

一键部署脚本已内置该问题的自动检测与修复：构建失败且日志匹配 DNS 错误时，会自动写 daemon.json、重启 docker 并重试（已存在 daemon.json 时提示手动处理）。

---

## 客户端使用

任何 OpenAI 兼容客户端，把 base_url 指向本代理：

```python
from openai import OpenAI

client = OpenAI(
    api_key="<PROXY_API_KEY>",
    base_url="https://api.example.com/v1",   # 或 http://<IP>:8000/v1
)
resp = client.chat.completions.create(
    model="deepseek-v4-flash-0731",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

curl 方式：

```bash
curl https://api.example.com/v1/chat/completions \
  -H "Authorization: Bearer <PROXY_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash-0731","messages":[{"role":"user","content":"你好"}]}'
```

流式请求加 `"stream": true` 即可，代理透传 SSE。

---

## 调度逻辑

1. **轮询**：按顺序循环使用可用 Key
2. **自动切换**：上游返回 401/403/429 或网络错误时，该 Key 进入冷却（默认 60 秒），自动换下一个 Key 重试
3. **重试上限**：非流式请求最多重试 `min(Key 数, 5)` 次；全部失败返回 502
4. **降级**：所有 Key 都在冷却中时，返回最早解冻的那一个，不空转

---

## 运维端点

```bash
# 健康状态（无需鉴权）
curl http://<host>:<port>/health

# Key 池详细状态（需鉴权，Key 脱敏显示）
curl -H "Authorization: Bearer <PROXY_API_KEY>" http://<host>:<port>/v1/keys
```

`/v1/keys` 返回每个 Key 的脱敏标识、状态（ready/cooling）、冷却剩余秒数、累计调用数与错误数。

---

## 文件结构

```
├── app/
│   ├── main.py      # FastAPI 入口：路由 / 健康检查 / 启动校验
│   ├── config.py    # 环境变量与 .env 配置
│   ├── keypool.py   # 轮询 + 冷却的 Key 池
│   └── proxy.py     # 转发核心：鉴权、换 Key 重试、SSE 透传
├── tests/           # 13 个单元测试（pytest）
├── deploy.sh        # 一键部署脚本
├── smoke.py         # 冒烟测试脚本
├── Dockerfile
├── docker-compose.yml
├── requirements.txt / requirements-dev.txt
└── .env.example
```

## 本地开发与测试

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
UPSTREAM_API_KEYS=dummy PROXY_API_KEY=dummy .venv/bin/python -m pytest -q
# 13 passed
```

## 运维命令

在安装目录（默认 `/opt/intern-ai-key-proxy`）下执行：

```bash
docker compose logs -f      # 查看日志
docker compose restart      # 重启
docker compose down         # 停止
```

---

## 安全说明

- 下游必须携带 `PROXY_API_KEY`；未授权请求返回 401
- 上游 Key 只存在服务端内存与 `.env` 中，日志和 `/v1/keys` 端点均脱敏
- `.env` 已在 `.gitignore` 中，不会提交到仓库
- 请仅聚合你自己拥有或已获明确授权的账号 Key，并遵守平台服务条款
