#!/bin/sh
# T2 Caddy 路由行为回归（SHAN-342 R1/R2 守卫）：双桩上游 + 桥接网络实测代理路由。
# 断言 4 条核心路由 + 1 条兜底：
#   /api/v1/me  -> SUB2API   (handle /api/* 经 sub2api_proxy snippet)
#   /v1/models  -> SUB2API   (裸代理, 历史行为保持)
#   /           -> SUB2API   (兜底 handle)
#   /api/config -> NEXTCHAT  (R2 合并路由 @nextchat)
#   /_next/*    -> NEXTCHAT  (R2 合并路由 @nextchat)
# 依赖: docker + caddy:2-alpine 镜像；不占用宿主端口（桥接网络内自洽）。
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
NET=sub2api-caddy-guard
FAIL=0

cleanup() {
  docker rm -f sg-stub-sa sg-stub-nc sg-caddy >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
cleanup

docker network create "$NET" >/dev/null
docker run -d --rm --name sg-stub-sa --network "$NET" \
  caddy:2-alpine caddy respond --listen :8080 --body SUB2API >/dev/null
docker run -d --rm --name sg-stub-nc --network "$NET" \
  caddy:2-alpine caddy respond --listen :8080 --body NEXTCHAT >/dev/null
docker run -d --rm --name sg-caddy --network "$NET" -p 127.0.0.1:0:80 \
  -v "$repo_root/proxy/Caddyfile":/etc/caddy/Caddyfile:ro \
  -e SITE_DOMAIN=localhost -e API_DOMAIN=api.localhost \
  -e SUB2API_HOST=sg-stub-sa:8080 -e NEXTCHAT_HOST=sg-stub-nc:8080 \
  caddy:2-alpine caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
sleep 2

# 取 caddy 容器实际映射到宿主的随机端口
PORT=$(docker port sg-caddy 80/tcp | sed 's/.*://')
BASE="http://127.0.0.1:$PORT"

probe() {
  path="$1"; expect="$2"
  got=$(curl -s -m 5 "$BASE$path" -H 'Host: localhost')
  if [ "$got" = "$expect" ]; then
    echo "PASS $path -> $got"
  else
    echo "FAIL $path -> '$got' (期望 $expect)"
    FAIL=1
  fi
}

probe /api/v1/me SUB2API
probe /v1/models SUB2API
probe / SUB2API
probe /api/config NEXTCHAT
probe /_next/static/x.js NEXTCHAT

if [ "$FAIL" -ne 0 ]; then
  echo "caddy-routes FAIL: 路由行为偏离基线，检查 proxy/Caddyfile R1/R2 改动。" >&2
  exit 1
fi
echo "caddy-routes OK: 5 条路由行为与基线一致。"
