#!/bin/sh
# T1 env 键守卫（SHAN-342 处方：含 viper 点分键映射反查，防 A 组误判复发）。
#   1) 样例非注释键必须被 compose 插值（防止样例提供 compose 不消费的键）
#   2) compose 必填插值(:?)键必须在样例出现（非注释或注释登记均可，防止 DA1 类缺口）
#   3) 样例注释键必须能映射到 tests/env-known-keys.txt 登记表（防幽灵键）
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/sub2api-env-guard.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

grep -oE '^[A-Z][A-Z0-9_]*' .env.example | sort -u > "$tmp/active"
grep -oE '^# [A-Z][A-Z0-9_]*=' .env.example | sed 's/^# //;s/=$//' | sort -u > "$tmp/commented"
grep -ohE '\$\{[A-Z][A-Z0-9_]*' docker-compose.yaml | sed 's/\${//' | sort -u > "$tmp/interpolated"
grep -ohE '\$\{[A-Z][A-Z0-9_]*:\?' docker-compose.yaml | sed 's/\${//;s/:?$//' | sort -u > "$tmp/required"
awk '$1 != "" && $1 !~ /^#/ {print $1}' tests/env-known-keys.txt | sort -u > "$tmp/known"

fail=0

# 1) 样例非注释键 ⊆ compose 插值键
unbacked=$(comm -23 "$tmp/active" "$tmp/interpolated")
if [ -n "$unbacked" ]; then
  echo "env-guard FAIL: 样例非注释键未被 compose 插值（A 组误判复发）：" >&2
  echo "$unbacked" >&2
  echo "处置: 恢复注释态并登记 tests/env-known-keys.txt，或让 compose 消费该键。" >&2
  fail=1
fi

# 2) compose 必填键 ⊆ 样例出现集(非注释∪注释登记)
cat "$tmp/active" "$tmp/commented" | sort -u > "$tmp/documented"
missing=$(comm -23 "$tmp/required" "$tmp/documented")
if [ -n "$missing" ]; then
  echo "env-guard FAIL: compose 必填插值键在样例中无登记（DA1 类缺口）：" >&2
  echo "$missing" >&2
  echo "处置: 在 .env.example 增加该键（必填键保持注释态要求部署者手填亦可）。" >&2
  fail=1
fi

# 3) 注释键 ⊆ known-keys
ghost=$(comm -23 "$tmp/commented" "$tmp/known")
if [ -n "$ghost" ]; then
  echo "env-guard FAIL: 样例注释键未登记 tests/env-known-keys.txt（幽灵键）：" >&2
  echo "$ghost" >&2
  echo "处置: 补登记一行 <键> <viper点分键|消费方> <生效形态>。" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi
echo "env-guard OK: 非注释键 $(wc -l < "$tmp/active") 个全部被 compose 插值；必填键全部有样例登记；注释键 $(wc -l < "$tmp/commented") 个全部在 known-keys 映射表内。"
