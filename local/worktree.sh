#!/usr/bin/env bash
# =============================================================================
# Sub2API 本地多槽位控制面 — 唯一入口
# =============================================================================
# 用法： ./local/worktree.sh <slot> <action> [issue] [--image <ref>] [--expect-sha <sha>]
# 详见 local/HANDOFF.md。所有有副作用的动作（claim/release/up/down）必须设置
# SUB2API_CALLER_OWNER 且与传入的 issue 参数一致，脚本据此在写入前重验 ownership。
# 禁止绕过本入口直接执行 `docker compose` 操作槽位目录。
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "" ] || [ "${2:-}" = "" ]; then
  echo "用法: $0 <slot> <claim|release|status|list|up|down|health> [issue] [--image <ref>] [--expect-sha <sha>]" >&2
  exit 64
fi

SLOT="$1"
ACTION="$2"
shift 2

case "$ACTION" in
  claim|release|up|down)
    if [ "${1:-}" = "" ]; then
      echo "动作 $ACTION 需要 issue 参数" >&2
      exit 64
    fi
    ISSUE="$1"
    if [ "${SUB2API_CALLER_OWNER:-}" = "" ]; then
      echo "必须设置 SUB2API_CALLER_OWNER 环境变量才能执行有副作用的动作 ($ACTION)" >&2
      exit 64
    fi
    if [ "${SUB2API_CALLER_OWNER}" != "$ISSUE" ]; then
      echo "SUB2API_CALLER_OWNER=${SUB2API_CALLER_OWNER} 与传入的 issue=${ISSUE} 不一致，拒绝执行" >&2
      exit 64
    fi
    shift
    exec python3 "$HERE/slotctl.py" "$SLOT" "$ACTION" "$ISSUE" "$@"
    ;;
  status|list|health)
    exec python3 "$HERE/slotctl.py" "$SLOT" "$ACTION" "$@"
    ;;
  *)
    echo "未知动作: $ACTION" >&2
    exit 64
    ;;
esac
