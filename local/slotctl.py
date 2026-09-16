#!/usr/bin/env python3
"""Sub2API 本地多槽位控制面核心逻辑。

唯一入口是 ``local/worktree.sh``；本模块不直接暴露给用户手工调用之外的
用法。租约模型见 ``local/HANDOFF.md`` 与 issue SHAN-271 的架构决策：

    owner.json 只保存两个归属字段： {"environment": <slot>, "issue": <issue>}

不得混入 branch / worktree / SHA / manifest / TTL / heartbeat 等字段。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parent
SLOTS_FILE = LOCAL_ROOT / "slots.json"
COMPOSE_TEMPLATE = LOCAL_ROOT / "docker-compose.slot.yml"

IN_PROGRESS_STATUS = "in_progress"
OWNER_FIELDS = ("environment", "issue")


class SlotError(RuntimeError):
    """槽位控制面的可预期失败（ownership、查询失败、SHA 不一致等）。"""


class QueryError(RuntimeError):
    """Multica Issue 状态查询失败或不可解析。"""


# ---------------------------------------------------------------------------
# 槽位注册表
# ---------------------------------------------------------------------------

def load_slots_registry() -> dict:
    with open(SLOTS_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def slot_dir(slot: str) -> Path:
    return LOCAL_ROOT / slot


def lock_dir(slot: str) -> Path:
    return slot_dir(slot) / ".slot-lock"


def owner_file(slot: str) -> Path:
    return lock_dir(slot) / "owner.json"


def resolve_slot_ports(slot: str) -> dict:
    registry = load_slots_registry()
    if slot not in registry["slots"]:
        raise SlotError(f"未知槽位: {slot}（可用槽位: {', '.join(sorted(registry['slots']))}）")
    index = registry["slots"][slot]["index"]
    base = registry["port_base"]
    return {
        "app": base["app"] + index,
        "postgres": base["postgres"] + index,
        "redis": base["redis"] + index,
        "minio_api": base["minio_api"] + index,
        "minio_console": base["minio_console"] + index,
    }


# ---------------------------------------------------------------------------
# Multica Issue 状态查询（可在测试中打桩）
# ---------------------------------------------------------------------------

def fetch_issue_status(issue: str) -> str:
    """返回 issue 的 status_category；查询失败/不可解析时抛 QueryError。"""
    try:
        result = subprocess.run(
            ["multica", "issue", "get", issue, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QueryError(f"multica issue get 调用失败: {exc}") from exc
    if result.returncode != 0:
        raise QueryError(f"multica issue get 返回非零退出码: {result.returncode}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise QueryError(f"multica issue get 输出不可解析: {exc}") from exc
    status = payload.get("status_category") or payload.get("status")
    if not status:
        raise QueryError("multica issue get 输出缺少 status/status_category 字段")
    return status


# ---------------------------------------------------------------------------
# 原子租约：claim / release / status
# ---------------------------------------------------------------------------

def _read_owner(slot: str) -> dict | None:
    path = owner_file(slot)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {k: data.get(k) for k in OWNER_FIELDS}


def _write_owner_atomic(slot: str, issue: str) -> None:
    lock_dir(slot).mkdir(parents=True, exist_ok=True)
    path = owner_file(slot)
    tmp = path.with_name(f".owner.json.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    payload = {"environment": slot, "issue": issue}
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _with_slot_flock(slot: str):
    """返回一个已加锁的文件句柄；调用方负责在 with 块内完成整个 claim 判定。"""
    lock_dir(slot).mkdir(parents=True, exist_ok=True)
    handle = open(lock_dir(slot) / ".flock", "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def claim_slot(slot: str, issue: str, status_fetcher=fetch_issue_status) -> dict:
    """申请槽位。返回最终 owner 记录；不可申请时抛 SlotError/QueryError。"""
    handle = _with_slot_flock(slot)
    try:
        current = _read_owner(slot)
        if current is None:
            _write_owner_atomic(slot, issue)
            return {"environment": slot, "issue": issue}
        if current["issue"] == issue:
            # 同一 Issue 重复 claim：幂等，不重写。
            return current
        status = status_fetcher(current["issue"])
        if status == IN_PROGRESS_STATUS:
            raise SlotError(
                f"槽位 {slot} 当前 owner {current['issue']} 处于 in_progress，禁止接管"
            )
        # 接管前重验 owner 未被并发修改（flock 已保证串行，这里做防御性复核）。
        recheck = _read_owner(slot)
        if recheck != current:
            raise SlotError(f"槽位 {slot} owner 在接管判定期间发生变化，请重试")
        _write_owner_atomic(slot, issue)
        return {"environment": slot, "issue": issue}
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def release_slot(slot: str, issue: str) -> None:
    handle = _with_slot_flock(slot)
    try:
        current = _read_owner(slot)
        if current is None:
            raise SlotError(f"槽位 {slot} 当前无 owner，无需释放")
        if current["issue"] != issue:
            raise SlotError(
                f"槽位 {slot} 当前 owner 是 {current['issue']}，不是 {issue}，拒绝释放（零写入）"
            )
        owner_file(slot).unlink()
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def check_ownership(slot: str, issue: str) -> None:
    """有副作用命令（up/down/health --write 等）执行前必须调用本函数。"""
    current = _read_owner(slot)
    if current is None or current["issue"] != issue:
        raise SlotError(
            f"槽位 {slot} 当前 owner 不是 {issue}（实际: {current}），fail-closed 拒绝写入"
        )


def slot_status(slot: str) -> dict:
    owner = _read_owner(slot)
    ports = resolve_slot_ports(slot)
    return {"slot": slot, "owner": owner, "ports": ports}


# ---------------------------------------------------------------------------
# Compose env 渲染
# ---------------------------------------------------------------------------

def _compose_project(slot: str) -> str:
    return f"sub2api-slot-{slot}"


def _stable_secret(slot: str, name: str) -> str:
    """为槽位派生稳定但不落盘明文示例的本地专用密钥（仅用于本地槽位隔离，非生产凭据）。"""
    seed = f"sub2api-local-slot::{slot}::{name}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()


def render_slot_env(slot: str, image: str) -> Path:
    ports = resolve_slot_ports(slot)
    project = _compose_project(slot)
    env = {
        "SLOT_APP_IMAGE": image,
        "SLOT_APP_CONTAINER": f"{project}-app",
        "SLOT_POSTGRES_CONTAINER": f"{project}-postgres",
        "SLOT_REDIS_CONTAINER": f"{project}-redis",
        "SLOT_MINIO_CONTAINER": f"{project}-minio",
        "SLOT_APP_PORT": str(ports["app"]),
        "SLOT_POSTGRES_PORT": str(ports["postgres"]),
        "SLOT_REDIS_PORT": str(ports["redis"]),
        "SLOT_MINIO_API_PORT": str(ports["minio_api"]),
        "SLOT_MINIO_CONSOLE_PORT": str(ports["minio_console"]),
        "SLOT_APP_DATA_VOLUME": f"{project}-app-data".replace("-", "_"),
        "SLOT_POSTGRES_DATA_VOLUME": f"{project}-postgres-data".replace("-", "_"),
        "SLOT_REDIS_DATA_VOLUME": f"{project}-redis-data".replace("-", "_"),
        "SLOT_MINIO_DATA_VOLUME": f"{project}-minio-data".replace("-", "_"),
        "SLOT_POSTGRES_USER": "sub2api",
        "SLOT_POSTGRES_DB": "sub2api",
        "SLOT_POSTGRES_PASSWORD": _stable_secret(slot, "postgres"),
        "SLOT_REDIS_PASSWORD": _stable_secret(slot, "redis"),
        "SLOT_JWT_SECRET": _stable_secret(slot, "jwt"),
        "SLOT_TOTP_ENCRYPTION_KEY": _stable_secret(slot, "totp"),
        "SLOT_MINIO_ROOT_USER": "sub2apislot",
        "SLOT_MINIO_ROOT_PASSWORD": _stable_secret(slot, "minio")[:32],
        "SLOT_MINIO_BUCKET": f"{slot}-object",
    }
    slot_dir(slot).mkdir(parents=True, exist_ok=True)
    env_path = slot_dir(slot) / ".env"
    tmp = env_path.with_suffix(".env.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for key, value in env.items():
            fh.write(f"{key}={value}\n")
    os.replace(tmp, env_path)
    os.chmod(env_path, 0o600)
    return env_path


def _compose_cmd(slot: str) -> list[str]:
    return [
        "docker", "compose",
        "-p", _compose_project(slot),
        "--env-file", str(slot_dir(slot) / ".env"),
        "-f", str(COMPOSE_TEMPLATE),
    ]


# ---------------------------------------------------------------------------
# up / down / health
# ---------------------------------------------------------------------------

def image_git_sha(image: str) -> str | None:
    result = subprocess.run(
        ["docker", "inspect", image, "--format",
         '{{index .Config.Labels "org.opencontainers.image.revision"}}'],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def slot_up(slot: str, issue: str, image: str, expect_sha: str | None) -> dict:
    check_ownership(slot, issue)
    actual_sha = image_git_sha(image)
    if expect_sha and actual_sha and expect_sha != actual_sha:
        raise SlotError(
            f"镜像 {image} 的 org.opencontainers.image.revision={actual_sha} "
            f"与期望 SHA {expect_sha} 不一致，拒绝加载"
        )
    render_slot_env(slot, image)
    check_ownership(slot, issue)  # 渲染耗时期间重验，防御性 fail-closed
    result = subprocess.run(
        _compose_cmd(slot) + ["up", "-d"],
        cwd=str(LOCAL_ROOT), check=False,
    )
    if result.returncode != 0:
        raise SlotError(f"docker compose up 失败，退出码 {result.returncode}")
    expected = expect_sha or actual_sha
    if expected:
        (slot_dir(slot) / ".expected-sha").write_text(expected + "\n", encoding="utf-8")
    return {"slot": slot, "image": image, "sha": expected}


def slot_down(slot: str, issue: str) -> None:
    check_ownership(slot, issue)
    result = subprocess.run(_compose_cmd(slot) + ["down"], cwd=str(LOCAL_ROOT), check=False)
    if result.returncode != 0:
        raise SlotError(f"docker compose down 失败，退出码 {result.returncode}")


def slot_health(slot: str) -> dict:
    project = _compose_project(slot)
    services = ["app", "postgres", "redis", "minio"]
    report = {"slot": slot, "services": {}, "ready": True}
    for svc in services:
        container = f"{project}-{svc}"
        result = subprocess.run(
            ["docker", "inspect", container, "--format", "{{.State.Health.Status}}"],
            capture_output=True, text=True, check=False,
        )
        health = result.stdout.strip() if result.returncode == 0 else "absent"
        report["services"][svc] = health
        if health != "healthy":
            report["ready"] = False

    expected_path = slot_dir(slot) / ".expected-sha"
    if expected_path.exists():
        expected_sha = expected_path.read_text(encoding="utf-8").strip()
        app_image = subprocess.run(
            ["docker", "inspect", f"{project}-app", "--format", "{{.Image}}"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        actual_sha = image_git_sha(app_image) if app_image else None
        report["expected_sha"] = expected_sha
        report["actual_sha"] = actual_sha
        if actual_sha != expected_sha:
            report["ready"] = False
            report["sha_mismatch"] = True
    return report


def slot_list() -> list[dict]:
    registry = load_slots_registry()
    return [slot_status(slot) for slot in sorted(registry["slots"])]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="slotctl")
    parser.add_argument("slot")
    parser.add_argument("action", choices=[
        "claim", "release", "status", "list", "up", "down", "health",
    ])
    parser.add_argument("issue", nargs="?")
    parser.add_argument("--image")
    parser.add_argument("--expect-sha")
    args = parser.parse_args(argv)

    try:
        if args.action == "list":
            print(json.dumps(slot_list(), ensure_ascii=False, indent=2))
            return 0
        if args.action == "status":
            print(json.dumps(slot_status(args.slot), ensure_ascii=False, indent=2))
            return 0
        if args.action == "claim":
            if not args.issue:
                raise SlotError("claim 需要 issue 参数")
            owner = claim_slot(args.slot, args.issue)
            print(json.dumps(owner, ensure_ascii=False))
            return 0
        if args.action == "release":
            if not args.issue:
                raise SlotError("release 需要 issue 参数")
            release_slot(args.slot, args.issue)
            print(json.dumps({"released": args.slot}, ensure_ascii=False))
            return 0
        if args.action == "up":
            if not args.issue or not args.image:
                raise SlotError("up 需要 issue 与 --image 参数")
            result = slot_up(args.slot, args.issue, args.image, args.expect_sha)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.action == "down":
            if not args.issue:
                raise SlotError("down 需要 issue 参数")
            slot_down(args.slot, args.issue)
            print(json.dumps({"down": args.slot}, ensure_ascii=False))
            return 0
        if args.action == "health":
            report = slot_health(args.slot)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ready"] else 1
    except (SlotError, QueryError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
