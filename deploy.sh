#!/usr/bin/env bash
# Intern-AI Key Proxy 一键部署脚本
#
# 用法（推荐：先下载再执行，可审查内容）：
#   curl -fsSL -o /tmp/deploy.sh https://raw.githubusercontent.com/laohuge666/intern-ai-key-proxy/main/deploy.sh
#   UPSTREAM_API_KEYS="sk-aaa,sk-bbb" PROXY_API_KEY="your-secret" bash /tmp/deploy.sh
#
# 也支持管道（与上面等价）：
#   curl -fsSL https://raw.githubusercontent.com/laohuge666/intern-ai-key-proxy/main/deploy.sh | bash
#
# 可选环境变量：
#   UPSTREAM_API_KEYS  上游 API Key，多个用英文逗号分隔（必填，否则交互式询问）
#   PROXY_API_KEY      下游访问密钥（不填则自动生成）
#   PORT               对外端口，默认 8000
#   INSTALL_DIR        安装目录，默认 /opt/intern-ai-key-proxy
#   UPSTREAM_BASE_URL  上游基址，默认 discovery-api.intern-ai.org.cn/v1

set -euo pipefail

REPO="laohuge666/intern-ai-key-proxy"
BRANCH="main"
INSTALL_DIR="${INSTALL_DIR:-/opt/intern-ai-key-proxy}"
PORT="${PORT:-8000}"
UPSTREAM_BASE_URL="${UPSTREAM_BASE_URL:-https://discovery-api.intern-ai.org.cn/v1}"

c_info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[1;31m[ERR]\033[0m %s\n' "$*"; }

# 1) 依赖检查
if ! command -v docker >/dev/null 2>&1; then
    c_err "未检测到 docker，请先安装 Docker 与 docker compose 插件"
    c_err "安装文档：https://docs.docker.com/engine/install/"
    exit 1
fi
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    c_err "未检测到 docker compose 插件"
    exit 1
fi
c_ok "docker 就绪：$($COMPOSE version 2>&1 | head -1)"

# 2) 拉取 / 更新代码
if [ -d "$INSTALL_DIR/.git" ]; then
    c_info "更新已有部署：$INSTALL_DIR"
    cd "$INSTALL_DIR"
    git fetch --depth 1 origin "$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    c_info "克隆仓库到：$INSTALL_DIR"
    git clone --depth 1 -b "$BRANCH" "https://github.com/$REPO.git" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 3) 读取配置
KEYS="${UPSTREAM_API_KEYS:-}"
PROXY="${PROXY_API_KEY:-}"
if [ -z "$KEYS" ]; then
    if [ -t 0 ]; then
        read -rp "粘贴上游 API Key（多个用英文逗号分隔）: " KEYS
    else
        c_err "非交互环境下必须通过 UPSTREAM_API_KEYS 环境变量传入上游 Key"
        c_err "示例：UPSTREAM_API_KEYS=\"sk-aaa,sk-bbb\" bash deploy.sh"
        exit 1
    fi
fi
[ -z "$KEYS" ] && { c_err "上游 Key 不能为空"; exit 1; }

if [ -z "$PROXY" ]; then
    PROXY="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-32)"
    c_info "未指定 PROXY_API_KEY，已自动生成"
fi

# 4) 写 .env（权限 600，只有 root 可读）
cat > .env <<EOF
UPSTREAM_BASE_URL=$UPSTREAM_BASE_URL
UPSTREAM_API_KEYS=$KEYS
PROXY_API_KEY=$PROXY
KEY_COOLDOWN_SECONDS=60
REQUEST_TIMEOUT_SECONDS=300
PORT=$PORT
EOF
chmod 600 .env
c_ok ".env 已生成（权限 600）"

# 5) 构建并启动
c_info "构建镜像并启动（首次需要拉取基础镜像，请稍候）..."
$COMPOSE up -d --build 2>&1 | tee /tmp/iakp-build.log || BUILD_FAIL=1
if [ "${BUILD_FAIL:-0}" = "1" ] && grep -qiE "name resolution|Temporary failure" /tmp/iakp-build.log; then
    c_err "构建容器内 DNS 解析失败（BuildKit 网络与宿主机 DNS 不一致）"
    if [ ! -f /etc/docker/daemon.json ]; then
        c_info "未检测到 daemon.json，自动配置 dns 并重启 docker"
        mkdir -p /etc/docker
        echo '{"dns": ["1.1.1.1", "8.8.8.8"]}' > /etc/docker/daemon.json
        systemctl restart docker || service docker restart
        c_ok "已配置 docker dns 并重启，重试构建..."
        $COMPOSE up -d --build || { c_err "构建仍然失败，请检查网络"; exit 1; }
    else
        c_err "已存在 /etc/docker/daemon.json，请手动加入 {\"dns\": [\"1.1.1.1\",\"8.8.8.8\"]} 后重试"
        exit 1
    fi
fi

# 6) 健康检查
c_info "等待服务就绪..."
HEALTH_URL="http://127.0.0.1:${PORT}/health"
for _ in $(seq 1 60); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    c_ok "部署成功，服务已上线"
else
    c_err "服务未在预期时间内就绪，查看日志：$COMPOSE logs"
    exit 1
fi

# 7) 输出访问信息
echo
echo "==================== 访问信息 ===================="
curl -s "$HEALTH_URL"; echo
echo
echo "下游 base_url : http://<本机IP>:${PORT}/v1"
echo "下游 api_key  : $PROXY"
echo "管理目录      : $INSTALL_DIR"
echo
echo "常用命令（在 $INSTALL_DIR 下执行）："
echo "  查看日志     : $COMPOSE logs -f"
echo "  重启         : $COMPOSE restart"
echo "  停止         : $COMPOSE down"
echo "  更新部署     : 重新执行本脚本即可"
echo "=================================================="
