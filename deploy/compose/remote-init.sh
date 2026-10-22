#!/usr/bin/env bash
# 在远程 Linux 服务器上初始化并启动 niucai（Kernel + Worker + 远程 Chrome 电脑）。
# 只创建本项目的 .env、镜像、容器和卷；不会覆盖已有 .env，不会清理其他项目。
# 用法（在 deploy/compose 目录）：
#   ./remote-init.sh check     只做环境检查
#   ./remote-init.sh env       首次生成 .env（随机密钥；已存在则拒绝）
#   ./remote-init.sh up        构建镜像、迁移并启动
#   ./remote-init.sh status    查看容器与健康状态
#   ./remote-init.sh register  注册远程 Linux 电脑（需 Kernel 已健康）
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
COMPOSE=(docker compose -f docker-compose.yml -f compose.computer.yml)
PORT="${NIUCAI_KERNEL_HOST_PORT:-18080}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令：$1" >&2; exit 1; }; }
secret() { python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || openssl rand -hex 32; }
env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }

check() {
  need docker; need python3
  docker compose version >/dev/null
  echo "Docker: $(docker --version)"
  echo "Compose: $(docker compose version --short)"
  df -h . | tail -1
  free -h 2>/dev/null | sed -n 2p || true
  if ss -lnt 2>/dev/null | grep -q "127.0.0.1:${PORT} "; then
    echo "端口 ${PORT} 已被占用：请设置 NIUCAI_KERNEL_HOST_PORT 换一个端口，不要停止其他项目。" >&2
  fi
  [ -f .env ] && echo ".env 已存在（不会被覆盖）" || echo ".env 尚未创建，运行：./remote-init.sh env"
}

make_env() {
  [ -e .env ] && { echo ".env 已存在，拒绝覆盖。" >&2; exit 1; }
  umask 077
  sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(secret)|" \
      -e "s|^NIUCAI_API_TOKEN=.*|NIUCAI_API_TOKEN=$(secret)|" \
      -e "s|^NIUCAI_ADAPTER=.*|NIUCAI_ADAPTER=local|" \
      -e "s|^VNC_PW=.*|VNC_PW=$(secret | cut -c1-24)|" \
      .env.example > .env
  # 模型 Key 由使用者填写，脚本不接收、不打印任何密钥。
  echo ".env 已生成（权限 600）。请编辑 NIUCAI_OPENAI_API_KEY / NIUCAI_OPENAI_API_BASE_URL，"
  echo "并确认 ../../services/kernel/models.yaml 中的模型 ID。API Token 位于 .env 的 NIUCAI_API_TOKEN。"
}

up() {
  [ -f .env ] || { echo "请先运行：./remote-init.sh env" >&2; exit 1; }
  for key in POSTGRES_PASSWORD NIUCAI_API_TOKEN VNC_PW; do
    [ -n "$(env_value "$key")" ] || { echo ".env 中 $key 为空" >&2; exit 1; }
  done
  [ "$(env_value NIUCAI_ADAPTER)" = "local" ] || echo "提示：NIUCAI_ADAPTER 不是 local，Worker 将无法操作远程浏览器。" >&2
  [ -n "$(env_value NIUCAI_OPENAI_API_KEY)$(env_value NIUCAI_OPENROUTER_API_KEY)" ] || echo "提示：未填写模型 Key，任务将无法调用模型。" >&2
  IMAGE="$(env_value NIUCAI_KERNEL_IMAGE)"
  (cd ../.. && docker build -f services/kernel/Dockerfile -t "${IMAGE:-niucai-kernel:local}" .)
  "${COMPOSE[@]}" config --quiet
  "${COMPOSE[@]}" build computer
  "${COMPOSE[@]}" up -d --no-build postgres computer desktop-relay api worker
  echo "等待 Kernel 健康…"
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then echo "Kernel 健康。"; status; return; fi
    sleep 3
  done
  echo "Kernel 在 3 分钟内未健康：docker compose logs --tail 100 api worker" >&2
  exit 1
}

status() {
  "${COMPOSE[@]}" ps
  TOKEN="$(env_value NIUCAI_API_TOKEN)"
  curl -fsS -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${PORT}/api/kernel/identity" | python3 -m json.tool
}

register() {
  TOKEN="$(env_value NIUCAI_API_TOKEN)"
  curl -fsS -X POST "http://127.0.0.1:${PORT}/api/computers" \
    -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
    -d '{"name":"remote-linux","kind":"linux"}' | python3 -m json.tool
}

case "${1:-check}" in
  check) check ;; env) make_env ;; up) up ;; status) status ;; register) register ;;
  *) sed -n 2,11p "${BASH_SOURCE[0]}"; exit 2 ;;
esac
