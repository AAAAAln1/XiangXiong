"""memory 事务锁：两条写入路径必须互斥（2026-09-18）。

## 发现的 bug

memory 是**读-改-写**，而写它的有两条路径：

| 路径 | 代码 |
|---|---|
| DELTA 应用 | `merge.read_memory()` → `apply_target_boundary()` → `merge.write_memory()` |
| 实验结论写回 | `experiment._sync_memory()` → `merge.read_memory()` → 改 → `merge.write_memory()` |

两边都没有任何互斥。而"记一笔账 + 关一个实验"是 agent **同一消息两次并行 Bash 调用**
的常见形态（CLAUDE.md 甚至鼓励把 `merge` 与 `experiment.py close` 紧挨着做）——
后写的那次拿到的是**读之前**的世界，于是把先写的那次整个盖掉（lost update）。

CLAUDE.md 那一段是这套系统里唯一让探索可累积的通道，它静默丢一行就是
"我记过了"与"账上其实没有"之间的鸿沟 —— 与 §8.0j 那条 echo 陷阱同一类。

（另一处真并发 —— `http_request` 的请求号锁、`experiment` 的编号锁 —— 一直有锁；
只有 memory 这条主写入路径漏了。）

## 锁死的不变量

1. memory 的「读 → 改 → 写」在同一把锁内；**N 个并发进程各记一行 → N 行都在**
2. 锁文件与 memory 同目录，名为 `.memory.lock`
3. 事务正常结束、抛异常、`sys.exit` 三种路径都不残留锁
4. 锁是**真互斥**：持有者未释放时，另一个事务无法进入
5. `experiment._sync_memory` 走同一把锁（不是自己另开一把）
6. 反证：把锁换成 no-op，用屏障强制"全部线程先读后写" → 更新必然丢
"""

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import experiment, filelock, merge  # noqa: E402

TARGET = "lock.example.test"


class _World:
    """临时 world 文件（`paths.world_active()` 经 env 覆盖）。"""

    def __init__(self, lines=()):
        self.dir = tempfile.mkdtemp(prefix="xx_lock_")
        self.path = os.path.join(self.dir, "memory.md")
        self._saved = os.environ.get("XIANGXIONG_WORLD_ACTIVE")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.writelines(lines or [f"target: {TARGET}\n"])

    def activate(self):
        os.environ["XIANGXIONG_WORLD_ACTIVE"] = self.path

    def lines(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return handle.readlines()

    def cleanup(self):
        if self._saved is None:
            os.environ.pop("XIANGXIONG_WORLD_ACTIVE", None)
        else:
            os.environ["XIANGXIONG_WORLD_ACTIVE"] = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)


class LockTestCase(unittest.TestCase):
    def setUp(self):
        self.world = _World()
        self.world.activate()
        self.addCleanup(self.world.cleanup)

    def merge_process(self, index, delta_path):
        env = dict(os.environ)
        env["XIANGXIONG_WORLD_ACTIVE"] = self.world.path
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.Popen(
            [sys.executable, os.path.join(PROJECT_ROOT, "runtime", "merge.py"),
             "--delta-file", delta_path],
            cwd=PROJECT_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )

    def write_delta(self, index):
        path = os.path.join(self.world.dir, f"_delta_{index}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"DELTA:\nsurface: /conc-{index:02d} 存在\n")
        return path


class TestLockPath(LockTestCase):
    def test_lock_sits_next_to_memory(self):
        self.assertEqual(os.path.dirname(merge.memory_lock_path()),
                         os.path.dirname(self.world.path))
        self.assertEqual(os.path.basename(merge.memory_lock_path()), ".memory.lock")

    def test_lock_file_does_not_survive(self):
        path = merge.memory_lock_path()
        with merge.memory_transaction():
            self.assertTrue(os.path.exists(path), "事务期间应有锁文件")
        self.assertFalse(os.path.exists(path), "事务结束后不得残留锁")

    def test_lock_released_on_exception(self):
        path = merge.memory_lock_path()
        with self.assertRaises(ValueError):
            with merge.memory_transaction():
                raise ValueError("boom")
        self.assertFalse(os.path.exists(path), "异常路径也必须释放")

    def test_lock_released_on_sys_exit(self):
        """`cmd_archive` 在事务里 `sys.exit(1)` —— 也必须释放。"""
        path = merge.memory_lock_path()
        with self.assertRaises(SystemExit):
            with merge.memory_transaction():
                sys.exit(1)
        self.assertFalse(os.path.exists(path))

    def test_memory_transaction_holds_an_exclusive_lock(self):
        """事务期间，同一个锁路径**不可被再次获取** —— 真互斥，不是装饰。

        用 0.3s 的短等待直接探锁（而不是再进一次 `memory_transaction()`：
        它的默认等待是 20s，测一次要 20 秒）。
        """
        path = merge.memory_lock_path()
        with merge.memory_transaction():
            self.assertTrue(os.path.exists(path))
            with self.assertRaises(TimeoutError):
                filelock.acquire(path, wait_timeout=0.3)
        # 事务结束后可再次获取
        fd = filelock.acquire(path, wait_timeout=1)
        filelock.release(path, fd)


class TestConcurrentMergesLoseNothing(LockTestCase):
    """核心回归：N 个并发进程各记一行，N 行都必须落盘。"""

    WORKERS = 6

    def test_all_lines_survive(self):
        deltas = [self.write_delta(i) for i in range(self.WORKERS)]
        procs = [self.merge_process(i, deltas[i]) for i in range(self.WORKERS)]
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)

        body = "".join(self.world.lines())
        missing = [i for i in range(self.WORKERS) if f"/conc-{i:02d} " not in body]
        self.assertEqual(missing, [], f"并发记账丢了这些行：{missing}\n{body}")

    def test_control_without_lock_loses_updates(self):
        """反证：把锁换成 no-op + 屏障强制"全部先读后写" → 必然丢更新。

        没有这条，"加锁之后不丢"证明不了任何事 —— 可能原本就不丢。

        注意 patch 必须**在跑线程之前、于主线程里**施加：`mock.patch.object` 改的是
        模块属性（全进程），在线程里各自 with 会在恢复时互相踩，把假实现永久留下。
        """
        workers = self.WORKERS
        barrier = threading.Barrier(workers)
        real_write = merge.write_memory

        def barrier_write(lines):
            barrier.wait(timeout=10)   # 所有线程都在"已读、未写"处会合
            real_write(lines)

        def worker(index):
            ops = merge.parse_delta(f"DELTA:\nsurface: /race-{index:02d} 存在\n")
            lines = merge.read_memory()
            lines, _, _ = merge.apply_target_boundary(lines, ops)
            merge.write_memory(lines)

        with mock.patch.object(merge, "memory_transaction",
                               lambda: contextlib.nullcontext()), \
             mock.patch.object(merge, "write_memory", barrier_write):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        body = "".join(self.world.lines())
        survived = sum(1 for i in range(workers) if f"/race-{i:02d} " in body)
        self.assertLess(survived, workers,
                        "无锁场景竟然没丢 —— 这条反证失效，测试本身没有区分力")


class TestExperimentSharesTheSameLock(LockTestCase):
    """第二条写入路径必须走同一把锁，否则锁在一条路上等于没锁。"""

    def test_sync_memory_waits_for_the_memory_lock(self):
        path = merge.memory_lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        holding = threading.Event()

        def holder():
            with filelock.locked(path):
                holding.set()
                time.sleep(0.6)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(holding.wait(timeout=5))

        started = time.time()
        result = experiment._sync_memory(
            TARGET, "exp-001", "unknown",
            {"text": "/api/locked 未授权可读", "status": "observed", "evidence": []},
            [],
        )
        elapsed = time.time() - started
        thread.join(timeout=10)

        self.assertIsNotNone(result)
        self.assertGreater(elapsed, 0.4,
                           "_sync_memory 没有等待 memory 锁 → 它走的是另一条路")
        self.assertIn("/api/locked", "".join(self.world.lines()))

    def test_sync_memory_uses_the_shared_transaction(self):
        """结构性断言：它必须调用 merge 的事务，而不是自己拼锁。"""
        calls = []
        real = merge.memory_transaction

        def recorder():
            calls.append(True)
            return real()

        with mock.patch.object(merge, "memory_transaction", recorder):
            experiment._sync_memory(
                TARGET, "exp-001", "unknown",
                {"text": "/api/recorded 存在", "status": "observed", "evidence": []},
                [],
            )
        self.assertEqual(len(calls), 1, "_sync_memory 没有使用 merge.memory_transaction")


if __name__ == "__main__":
    unittest.main()
