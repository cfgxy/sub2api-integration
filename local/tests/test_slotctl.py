#!/usr/bin/env python3
"""local/worktree.sh 控制面定向测试。

覆盖 SHAN-271 验收标准第 7 条：
- 原子竞争下单一获胜者
- in_progress owner 阻止接管
- 终态(done/cancelled/todo/blocked/in_review) owner 可被接管
- Multica 查询失败时零写入
- ownership 丢失时零写入
- SHA 不一致时拒绝就绪

运行： python3 -m unittest local/tests/test_slotctl.py -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import slotctl  # noqa: E402


class SlotctlTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sub2api-slotctl-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self._orig_root = slotctl.LOCAL_ROOT
        slotctl.LOCAL_ROOT = Path(self.tmpdir)
        self.addCleanup(self._restore_root)
        registry = {
            "slots": {"dev1": {"kind": "dev", "index": 1}, "qa1": {"kind": "qa", "index": 4}},
            "port_base": {"app": 18100, "postgres": 15400, "redis": 16400,
                           "minio_api": 19100, "minio_console": 19200},
        }
        (Path(self.tmpdir) / "slots.json").write_text(json.dumps(registry), encoding="utf-8")
        self._orig_slots_file = slotctl.SLOTS_FILE
        slotctl.SLOTS_FILE = Path(self.tmpdir) / "slots.json"
        self.addCleanup(self._restore_slots_file)

    def _restore_root(self):
        slotctl.LOCAL_ROOT = self._orig_root

    def _restore_slots_file(self):
        slotctl.SLOTS_FILE = self._orig_slots_file

    # -- owner.json 数据模型 -------------------------------------------------

    def test_owner_record_has_only_two_fields(self):
        owner = slotctl.claim_slot("dev1", "SHAN-1")
        self.assertEqual(set(owner.keys()), {"environment", "issue"})
        on_disk = json.loads(slotctl.owner_file("dev1").read_text(encoding="utf-8"))
        self.assertEqual(set(on_disk.keys()), {"environment", "issue"})

    # -- 原子竞争 --------------------------------------------------------------

    def test_concurrent_claim_only_one_winner_on_empty_slot(self):
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def worker(issue):
            barrier.wait()
            try:
                results.append(slotctl.claim_slot(
                    "dev1", issue,
                    status_fetcher=lambda i: "in_progress",
                ))
            except (slotctl.SlotError, slotctl.QueryError) as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(f"SHAN-{i}",)) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, f"应只有一个赢家，实际 results={results} errors={errors}")
        self.assertEqual(len(errors), 1)
        winner_issue = results[0]["issue"]
        on_disk = json.loads(slotctl.owner_file("dev1").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["issue"], winner_issue)

    # -- 接管规则 --------------------------------------------------------------

    def test_in_progress_owner_blocks_takeover(self):
        slotctl.claim_slot("dev1", "SHAN-1", status_fetcher=lambda i: "todo")
        with self.assertRaises(slotctl.SlotError):
            slotctl.claim_slot("dev1", "SHAN-2", status_fetcher=lambda i: "in_progress")
        on_disk = json.loads(slotctl.owner_file("dev1").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["issue"], "SHAN-1", "被拒绝的接管不得修改 owner")

    def test_terminal_and_non_progress_statuses_allow_takeover(self):
        for status in ("todo", "blocked", "in_review", "done", "cancelled"):
            with self.subTest(status=status):
                slotctl.claim_slot("dev1", "SHAN-OLD", status_fetcher=lambda i: "todo")
                owner = slotctl.claim_slot(
                    "dev1", "SHAN-NEW", status_fetcher=lambda i: status,
                )
                self.assertEqual(owner["issue"], "SHAN-NEW")

    def test_query_failure_yields_zero_write(self):
        slotctl.claim_slot("dev1", "SHAN-1", status_fetcher=lambda i: "todo")
        before = slotctl.owner_file("dev1").read_text(encoding="utf-8")

        def boom(issue):
            raise slotctl.QueryError("multica 不可用")

        with self.assertRaises(slotctl.QueryError):
            slotctl.claim_slot("dev1", "SHAN-2", status_fetcher=boom)
        after = slotctl.owner_file("dev1").read_text(encoding="utf-8")
        self.assertEqual(before, after, "查询失败必须零写入")

    def test_idempotent_reclaim_by_same_issue(self):
        slotctl.claim_slot("dev1", "SHAN-1", status_fetcher=lambda i: "in_progress")
        owner = slotctl.claim_slot("dev1", "SHAN-1", status_fetcher=lambda i: "in_progress")
        self.assertEqual(owner["issue"], "SHAN-1")

    # -- ownership 闸门（写操作前重验）--------------------------------------

    def test_ownership_lost_blocks_release(self):
        slotctl.claim_slot("dev1", "SHAN-1", status_fetcher=lambda i: "todo")
        slotctl.claim_slot("dev1", "SHAN-2", status_fetcher=lambda i: "todo")
        with self.assertRaises(slotctl.SlotError):
            slotctl.release_slot("dev1", "SHAN-1")
        self.assertTrue(slotctl.owner_file("dev1").exists(), "release 失败不得删除现任 owner 记录")

    def test_ownership_lost_blocks_side_effecting_check(self):
        slotctl.claim_slot("dev1", "SHAN-1", status_fetcher=lambda i: "todo")
        slotctl.claim_slot("dev1", "SHAN-2", status_fetcher=lambda i: "todo")
        with self.assertRaises(slotctl.SlotError):
            slotctl.check_ownership("dev1", "SHAN-1")
        slotctl.check_ownership("dev1", "SHAN-2")  # 现任 owner 正常通过

    def test_release_by_current_owner_succeeds(self):
        slotctl.claim_slot("dev1", "SHAN-1", status_fetcher=lambda i: "todo")
        slotctl.release_slot("dev1", "SHAN-1")
        self.assertFalse(slotctl.owner_file("dev1").exists())

    def test_release_without_owner_rejected(self):
        with self.assertRaises(slotctl.SlotError):
            slotctl.release_slot("dev1", "SHAN-1")

    # -- 运行来源 SHA 校验 -------------------------------------------------

    def test_sha_mismatch_blocks_ready(self):
        project = slotctl._compose_project("dev1")
        with mock.patch.object(slotctl, "image_git_sha", return_value="deadbeef" * 5):
            with mock.patch("subprocess.run") as run:
                def fake_run(cmd, *a, **k):
                    class R:
                        returncode = 0
                        stdout = "healthy"
                        stderr = ""
                    if cmd[:2] == ["docker", "inspect"] and "{{.Image}}" in cmd:
                        R.stdout = "sha256:fakeimageid"
                    return R()
                run.side_effect = fake_run
                (slotctl.slot_dir("dev1")).mkdir(parents=True, exist_ok=True)
                (slotctl.slot_dir("dev1") / ".expected-sha").write_text(
                    "cafebabe" * 5 + "\n", encoding="utf-8"
                )
                report = slotctl.slot_health("dev1")
        self.assertFalse(report["ready"])
        self.assertTrue(report.get("sha_mismatch"))

    def test_sha_match_allows_ready_when_services_healthy(self):
        with mock.patch.object(slotctl, "image_git_sha", return_value="cafebabe" * 5):
            with mock.patch("subprocess.run") as run:
                def fake_run(cmd, *a, **k):
                    class R:
                        returncode = 0
                        stdout = "healthy"
                        stderr = ""
                    if cmd[:2] == ["docker", "inspect"] and "{{.Image}}" in cmd:
                        R.stdout = "sha256:fakeimageid"
                    return R()
                run.side_effect = fake_run
                (slotctl.slot_dir("dev1")).mkdir(parents=True, exist_ok=True)
                (slotctl.slot_dir("dev1") / ".expected-sha").write_text(
                    "cafebabe" * 5 + "\n", encoding="utf-8"
                )
                report = slotctl.slot_health("dev1")
        self.assertTrue(report["ready"])
        self.assertFalse(report.get("sha_mismatch", False))

    # -- 端口/卷/项目名隔离 ----------------------------------------------------

    def test_ports_and_volumes_are_distinct_across_slots(self):
        p1 = slotctl.resolve_slot_ports("dev1")
        p2 = slotctl.resolve_slot_ports("qa1")
        for key in p1:
            self.assertNotEqual(p1[key], p2[key], f"端口 {key} 在两个槽位间必须不同")


if __name__ == "__main__":
    unittest.main()
