"""`runtime/filelock.py` 的 Windows 竞争回归。

## 发现的 bug

`acquire()` 原先只捕 `FileExistsError` 当作"锁被别人持有"。Windows 上这个假设不成立：
并发 `CreateFile(CREATE_NEW)` 在对手正持有/正删除该文件时返回 `ERROR_ACCESS_DENIED`，
CPython 映射为 **`PermissionError(13)`** 而非 `FileExistsError` —— 于是异常穿透
`acquire`，调用方线程直接死掉。

**实测（本机 Windows，2026-09-18）**：`tests/test_experiment_reserve.py` 的
8 线程并发建实验，连跑 12 次崩 1 次（≈8%），异常正是
`PermissionError: [Errno 13] Permission denied: '...\\.id_lock'`，
8 个线程只分到 7 个 exp id。

## 为什么这不是"偶发小问题"

并发建实验是**并行探索唯一的入口**：主 agent 一条消息发多个 `deepdive` 子 agent，
每个子 agent 第一件事就是 `experiment.py create` → `reserve_experiment_dir` → 本锁。
一次误报 = 一个子 agent 起不来或返回 blocked，而"跑不跑得出并行探索"正是这套
架构要解决的问题本身。这个锁同时被 `http_request.next_request_number` 使用，
所以主会话与子 agent 同时发包也会踩到。

## 锁死的不变量

1. 锁文件存在时的 `PermissionError` **必须**按竞争处理（退避重试，最终成功）
2. 真实权限问题耗尽 `wait_timeout` 后**必须**抛 `PermissionError`，
   不得伪装成"别的进程卡死"的 `TimeoutError`
   （第一版实现靠 `os.path.exists()` 区分二者，被本文件的压力测试直接证伪：
   EACCES 发生时 `exists()` 恰好也是 False）
3. 陈旧锁（超过 `stale_seconds`）必须被回收
4. 持有者不释放 → `wait_timeout` 后抛 `TimeoutError`，不许无限等
5. `locked()` 必须在正常与异常路径上都释放锁
6. 并发 `locked()` 的临界区必须真的互斥（不丢更新）
"""

import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from runtime import filelock  # noqa: E402


class _LockDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="xx_filelock_")
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = os.path.join(self._tmp.name, "sub", ".id_lock")


class TestPermissionErrorIsContention(_LockDirTestCase):
    """Windows 的竞争表现：EACCES 而不是 EEXIST。"""

    def test_contended_create_with_permission_error_is_retried(self):
        """锁文件在 → PermissionError 是竞争，必须退避重试到成功。"""
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        Path(self.lock_path).write_text("999", encoding="ascii")  # 竞争者持有

        real_open = os.open
        calls = {"n": 0}

        def fake_open(path, flags, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(self.lock_path):
                calls["n"] += 1
                if calls["n"] == 1:
                    # Windows 对该路径的并发 CREATE_NEW 真实报错形态
                    raise PermissionError(13, "Access is denied")
                if calls["n"] == 2:
                    os.remove(self.lock_path)  # 竞争者在这一刻释放
                    raise PermissionError(13, "Access is denied")
            return real_open(path, flags, *args, **kwargs)

        with unittest.mock.patch("runtime.filelock.os.open", side_effect=fake_open):
            fd = filelock.acquire(self.lock_path, wait_timeout=5)

        self.addCleanup(filelock.release, self.lock_path, fd)
        self.assertEqual(calls["n"], 3, "前两次 EACCES 应被当成竞争并重试，第三次才真创建")
        self.assertEqual(Path(self.lock_path).read_text(encoding="ascii"), str(os.getpid()))

    def test_real_permission_problem_surfaces_as_permission_error(self):
        """真实权限问题必须报 `PermissionError`，不能被伪装成"另一进程卡死"。

        注意它**不会立刻抛出** —— 竞争与真实权限问题在那一刻无法可靠区分
        （见 `test_contended_acquire_does_not_leak_permission_errors` 证伪的那版实现），
        所以它等满 `wait_timeout` 后再抛。这里锁的是"抛出的异常类型是对的"。
        """
        with unittest.mock.patch(
            "runtime.filelock.os.open",
            side_effect=PermissionError(13, "Access is denied"),
        ):
            with self.assertRaises(PermissionError) as ctx:
                filelock.acquire(self.lock_path, wait_timeout=0.3)
        self.assertNotIn("超时", str(ctx.exception),
                         "真实权限问题不得报告成锁超时")


class TestStaleAndTimeout(_LockDirTestCase):
    def test_stale_lock_is_reclaimed(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        Path(self.lock_path).write_text("999", encoding="ascii")
        old = time.time() - 120
        os.utime(self.lock_path, (old, old))

        fd = filelock.acquire(self.lock_path, wait_timeout=5, stale_seconds=30)
        self.addCleanup(filelock.release, self.lock_path, fd)
        self.assertEqual(Path(self.lock_path).read_text(encoding="ascii"), str(os.getpid()))

    def test_fresh_lock_times_out_instead_of_waiting_forever(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        Path(self.lock_path).write_text("999", encoding="ascii")  # mtime 是现在

        started = time.time()
        with self.assertRaises(TimeoutError):
            filelock.acquire(self.lock_path, wait_timeout=0.3, stale_seconds=300)
        self.assertLess(time.time() - started, 3.0)

    def test_default_window_can_reclaim_orphan(self):
        """2026-10-07 核验：默认参数下孤儿锁必须能在一次等待窗口内自愈。

        旧默认 stale=30 > wait=20，孤儿锁在等待期内永远不满足回收条件。
        锁龄取 11s：新默认 stale=10 已过，wait=20 未耗尽。"""
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        Path(self.lock_path).write_text("999", encoding="ascii")
        old = time.time() - 11
        os.utime(self.lock_path, (old, old))
        started = time.time()
        fd = filelock.acquire(self.lock_path)  # 全默认参数
        self.addCleanup(filelock.release, self.lock_path, fd)
        self.assertLess(time.time() - started, 5.0)
        self.assertEqual(Path(self.lock_path).read_text(encoding="ascii"), str(os.getpid()))

    def test_missing_parent_directory_is_created(self):
        self.assertFalse(os.path.isdir(os.path.dirname(self.lock_path)))
        with filelock.locked(self.lock_path):
            self.assertTrue(os.path.exists(self.lock_path))
        self.assertFalse(os.path.exists(self.lock_path))


class TestLockedContextManager(_LockDirTestCase):
    def test_releases_on_normal_exit(self):
        with filelock.locked(self.lock_path):
            self.assertTrue(os.path.exists(self.lock_path))
        self.assertFalse(os.path.exists(self.lock_path))

    def test_releases_on_exception(self):
        with self.assertRaises(ValueError):
            with filelock.locked(self.lock_path):
                raise ValueError("boom")
        self.assertFalse(os.path.exists(self.lock_path),
                         "异常路径也必须释放，否则后续所有调用都会等到超时")


class TestConcurrency(_LockDirTestCase):
    """并发是这套锁唯一的存在理由 —— 它必须既不崩、也不丢更新。"""

    def test_concurrent_critical_sections_are_mutually_exclusive(self):
        state = {"in_critical": 0, "max_seen": 0, "value": 0}
        guard = threading.Lock()

        def worker():
            for _ in range(6):
                with filelock.locked(self.lock_path, wait_timeout=10):
                    with guard:
                        state["in_critical"] += 1
                        state["max_seen"] = max(state["max_seen"], state["in_critical"])
                    time.sleep(0.001)  # 拉长临界区，让重叠可被观测
                    with guard:
                        state["value"] += 1
                        state["in_critical"] -= 1

        errors = []

        def run():
            try:
                worker()
            except Exception as exc:  # noqa: BLE001 - 测试要收集任何异常
                errors.append(repr(exc))

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], "并发获取锁时抛出异常 —— 竞争未被识别")
        self.assertEqual(state["value"], 48, "临界区丢更新 → 锁没真正互斥")
        self.assertEqual(state["max_seen"], 1, "同时有两个线程在临界区内")

    def test_contended_acquire_does_not_leak_permission_errors(self):
        """回归本体：复现原 bug 的触发方式（高频竞争），断言零异常。

        原实现下 8 线程 × 多轮就有可观概率抛 `PermissionError`；
        修好后必须恒为 0 异常。
        """
        errors = []

        def worker(rounds):
            for _ in range(rounds):
                try:
                    with filelock.locked(self.lock_path, wait_timeout=10):
                        pass
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))
                    return

        for _ in range(15):
            threads = [threading.Thread(target=worker, args=(2,)) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            if errors:
                break

        self.assertEqual(errors, [], f"竞争路径漏出了非竞争异常：{errors[:3]}")


if __name__ == "__main__":
    unittest.main()
