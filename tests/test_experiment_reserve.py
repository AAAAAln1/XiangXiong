"""实验编号分配的原子性回归测试（融合架构 P2）。

回归点：`next_experiment_id` 原为 scan-then-create，无跨进程锁。
并发探索者会扫到同一个 max 号 → 分配出同一个 ID（目录尚未创建，扫不到）。

现不变量：
1. 编号在**锁内**分配**并立即创建目录**（占位），故并发下 ID 唯一
2. `.id_lock` 不被误认为实验目录
3. 显式传入 experiment_id 的路径行为不变
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

from runtime import experiment  # noqa: E402


class TestReserveExperimentDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="xx_expres_")
        self._orig = experiment.RESULTS_DIR
        experiment.RESULTS_DIR = self._tmp.name
        self.target = "example.test"

    def tearDown(self):
        experiment.RESULTS_DIR = self._orig
        self._tmp.cleanup()

    def test_sequential_allocation(self):
        first, path1 = experiment.reserve_experiment_dir(self.target)
        second, path2 = experiment.reserve_experiment_dir(self.target)
        self.assertEqual(first, "exp-001")
        self.assertEqual(second, "exp-002")
        self.assertTrue(os.path.isdir(path1))
        self.assertTrue(os.path.isdir(path2))

    def test_reserve_creates_directory_immediately(self):
        """占位必须在锁内完成——否则并发下扫不到未建的目录。"""
        exp_id, path = experiment.reserve_experiment_dir(self.target)
        self.assertTrue(os.path.isdir(path), "reserve 应已创建目录（占位）")

    def test_id_lock_is_released_and_does_not_affect_numbering(self):
        """锁用后即删（不残留污染实验列表）；陈旧锁被回收且不干扰编号推进。"""
        experiment.reserve_experiment_dir(self.target)
        root = experiment.experiments_dir(self.target)
        self.assertEqual(os.listdir(root), ["exp-001"], "锁释放后应只剩实验目录")

        # 模拟崩溃残留的陈旧锁：应被 filelock 回收，且不参与编号扫描
        stale = os.path.join(root, experiment.ID_LOCK_NAME)
        with open(stale, "w", encoding="utf-8") as handle:
            handle.write("99999")
        old = time.time() - 600
        os.utime(stale, (old, old))

        nxt, path = experiment.reserve_experiment_dir(self.target)
        self.assertEqual(nxt, "exp-002", "`.id_lock` 不得被当作实验参与编号")
        self.assertTrue(os.path.isdir(path))

    def test_concurrent_reserve_yields_unique_ids(self):
        """8 线程并发分配 → 8 个互不相同的 ID（无锁实现会撞号）。"""
        workers = 8
        barrier = threading.Barrier(workers)
        ids = []
        lock = threading.Lock()

        def reserve():
            barrier.wait()
            exp_id, _ = experiment.reserve_experiment_dir(self.target)
            with lock:
                ids.append(exp_id)

        threads = [threading.Thread(target=reserve) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(ids), workers)
        self.assertEqual(len(set(ids)), workers, f"ID 撞号: {sorted(ids)}")
        self.assertEqual(sorted(ids), [f"exp-{i:03d}" for i in range(1, workers + 1)])

    def test_create_experiment_uses_reserved_dir(self):
        result = experiment.create_experiment(self.target, hypothesis="H", action="A")
        self.assertEqual(result["experiment_id"], "exp-001")
        meta = experiment.load_experiment(self.target, "exp-001")
        self.assertEqual(meta["hypothesis"], "H")
        self.assertFalse(meta["closed"])

    def test_explicit_id_path_unchanged(self):
        result = experiment.create_experiment(
            self.target, hypothesis="H", experiment_id="exp-042"
        )
        self.assertEqual(result["experiment_id"], "exp-042")
        self.assertTrue(
            os.path.isdir(experiment.experiment_dir(self.target, "exp-042"))
        )

    def test_explicit_id_collision_still_fails_loudly(self):
        experiment.create_experiment(self.target, hypothesis="H", experiment_id="exp-007")
        with self.assertRaises(FileExistsError):
            experiment.create_experiment(
                self.target, hypothesis="H2", experiment_id="exp-007"
            )


if __name__ == "__main__":
    unittest.main()
