"""意图认领协议的回归测试（融合架构 P2 / L2 层）。

核心不变量：
1. **互斥**：N 个并发认领者，恰好一个成功（O_EXCL 原子性）
2. **租约**：过期 claim 可被抢占；未过期不可
3. **所有权**：心跳/释放仅持有者可为
4. **可认领集**：= 前沿 ID 集合 − 未过期认领集合（机械差集）
5. **ID 往返**：`h:xxxxxx` ⇄ `h-xxxxxx.claim` 无损（Windows 文件名不允许 `:`）
"""

import os
import sys
import tempfile
import threading
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import intents  # noqa: E402


class TestIntentClaim(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="xx_intents_")
        self._orig = intents.RESULTS_DIR
        intents.RESULTS_DIR = self._tmp.name
        self.target = "example.test"
        self.iid = "h:8a01be"

    def tearDown(self):
        intents.RESULTS_DIR = self._orig
        self._tmp.cleanup()

    def test_claim_then_second_worker_rejected(self):
        first = intents.claim(self.target, self.iid, "w1")
        self.assertTrue(first["ok"])
        self.assertEqual(first["status"], "claimed")

        second = intents.claim(self.target, self.iid, "w2")
        self.assertFalse(second["ok"])
        self.assertEqual(second["status"], "held")
        self.assertEqual(second["holder"], "w1")

    def test_same_worker_claim_renews(self):
        intents.claim(self.target, self.iid, "w1")
        again = intents.claim(self.target, self.iid, "w1")
        self.assertTrue(again["ok"])
        self.assertEqual(again["status"], "renewed")

    def test_expired_claim_is_preemptable(self):
        intents.claim(self.target, self.iid, "w1", lease=900)
        path = intents.claim_path(self.target, self.iid)
        # 把 mtime 拨回 10 分钟前 → 视为租约过期
        old = time.time() - 600
        os.utime(path, (old, old))

        taken = intents.claim(self.target, self.iid, "w2", lease=300)
        self.assertTrue(taken["ok"], taken)
        self.assertEqual(taken["status"], "claimed")
        self.assertEqual(intents.list_claims(self.target, lease=300)[0]["worker"], "w2")

    def test_not_expired_claim_cannot_be_preempted(self):
        intents.claim(self.target, self.iid, "w1", lease=900)
        taken = intents.claim(self.target, self.iid, "w2", lease=900)
        self.assertFalse(taken["ok"])
        self.assertEqual(taken["status"], "held")

    def test_release_only_by_holder(self):
        intents.claim(self.target, self.iid, "w1")
        denied = intents.release(self.target, self.iid, "w2")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["status"], "held_by_other")

        ok = intents.release(self.target, self.iid, "w1")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["status"], "released")
        self.assertEqual(intents.list_claims(self.target), [])

    def test_heartbeat_only_by_holder(self):
        intents.claim(self.target, self.iid, "w1")
        denied = intents.heartbeat(self.target, self.iid, "w2")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["status"], "held_by_other")

        path = intents.claim_path(self.target, self.iid)
        old = time.time() - 100
        os.utime(path, (old, old))
        beat = intents.heartbeat(self.target, self.iid, "w1")
        self.assertTrue(beat["ok"])
        self.assertLess(time.time() - os.path.getmtime(path), 5)

    def test_heartbeat_on_unclaimed(self):
        result = intents.heartbeat(self.target, self.iid, "w1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_claimed")

    def test_release_unclaimed_is_ok(self):
        result = intents.release(self.target, self.iid, "w1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "not_claimed")

    def test_claim_path_is_filesystem_safe(self):
        """claim 文件名不得含 `:`（Windows 非法）；往返必须无损。"""
        path = intents.claim_path(self.target, self.iid)
        self.assertNotIn(":", os.path.basename(path))
        self.assertEqual(
            intents._unsafe_id(os.path.basename(path)[: -len(intents.CLAIM_SUFFIX)]),
            self.iid,
        )

    def test_id_roundtrip_survives_real_file(self):
        intents.claim(self.target, self.iid, "w1")
        intents.claim(self.target, "f:4419c2", "w1")
        ids = sorted(c["intent"] for c in intents.list_claims(self.target))
        self.assertEqual(ids, ["f:4419c2", "h:8a01be"])

    def test_claimable_subtracts_held_and_expired(self):
        intents.claim(self.target, "h:aaa111", "w1")
        intents.claim(self.target, "h:bbb222", "w1")
        path = intents.claim_path(self.target, "h:bbb222")
        old = time.time() - 600
        os.utime(path, (old, old))

        frontier = ["h:aaa111", "h:bbb222", "h:ccc333"]
        # lease 短 → bbb222 已过期 → 回到可认领
        self.assertEqual(
            sorted(intents.claimable(frontier, self.target, lease=300)),
            ["h:bbb222", "h:ccc333"],
        )
        # lease 长 → bbb222 仍在租约内 → 不可认领
        self.assertEqual(
            intents.claimable(frontier, self.target, lease=99999), ["h:ccc333"]
        )

    def test_concurrent_claims_only_one_wins(self):
        """并发原子性：8 个线程同时认领同一个 intent，恰好一个 claimed。"""
        workers = 8
        barrier = threading.Barrier(workers)
        results = []
        lock = threading.Lock()

        def attempt(name):
            barrier.wait()
            outcome = intents.claim(self.target, self.iid, name, lease=900)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt, args=(f"w{i}",)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        claimed = [r for r in results if r["status"] == "claimed"]
        held = [r for r in results if r["status"] == "held"]
        self.assertEqual(len(claimed), 1, f"应当恰好一个认领成功: {results}")
        self.assertEqual(len(held), workers - 1, f"其余应全部被拒: {results}")
        # 磁盘上恰好一个持有者，且就是成功者的名字
        self.assertEqual(intents.list_claims(self.target)[0]["worker"], claimed[0]["worker"])


if __name__ == "__main__":
    unittest.main()
