#!/usr/bin/env python3
"""filelock.py — 跨进程互斥锁（O_EXCL 原子创建 + 陈旧锁回收）。

**单一实现**，供需要"编号分配原子化"的两处共用：
- `http_request.next_request_number`（主代理 + 嵌套 deepdive 可同时发请求）
- `experiment.reserve_experiment_dir`（并发探索者会同时建实验）

此处抽为共享实现而非各写一份，理由是本仓库已发生过同类事故：
`protocol_scan` 的 status 归一化与 `diff_scan` 各写一份 → 只修了一半
（见 `runtime/protocol_scan.py` 顶部注释）。复制粘贴的锁会同样漂移。

零判断：只做互斥与陈旧回收，不判业务、不排序、不选主。
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager

# 陈旧判定：锁文件超时未释放（持有者崩溃残留）→ 可回收。
# 2026-10-07 核验修复：必须严格 < DEFAULT_WAIT_TIMEOUT——旧值 30 > 等待上限 20，
# 新鲜孤儿锁在等待窗口内永远等不到回收（wait_timeout 先耗尽），自愈路径等于不存在。
# 10s 远大于任何真实临界区（本仓库临界区只是扫目录+分配号/内存变换，毫秒级）。
DEFAULT_STALE_SECONDS = 10
# 等待上限：超过则判定"另一进程卡死"，抛错而非无限等。
DEFAULT_WAIT_TIMEOUT = 20
# release 删除的瞬态重试次数（Windows AV/索引器对新建文件的短暂句柄竞争）。
RELEASE_REMOVE_ATTEMPTS = 3


def acquire(
    lock_path: str,
    *,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
) -> int:
    """获取锁，返回可关闭的 fd。

    O_EXCL 保证"创建即持有"是原子的；竞争失败方检查陈旧后重试。
    注意：仅凭 mtime 判陈旧，故临界区必须短（本仓库两处都只是扫目录 + 分配号）。

    **Windows 的竞争表现不是 FileExistsError。** 并发 `CreateFile(CREATE_NEW)` 在
    对手正持有/正删除该文件时返回 `ERROR_ACCESS_DENIED`，CPython 把它映射成
    `PermissionError(13)` 而不是 `FileExistsError`。实测（本机 Windows，2026-09-18）：
    `tests/test_experiment_reserve.py` 的 8 线程并发建实验 12 次里崩 1 次，
    异常直接从 `acquire` 抛出、线程死掉、只分到 7 个 id。

    这不是理论问题：并发建实验正是"并行探索"（主 agent 一条消息发多个 deepdive）
    的唯一入口 —— 一次误报就让一个子 agent 起不来。所以 `PermissionError` 也必须
    按竞争重试。

    **不能靠 `os.path.exists()` 区分竞争与真实权限问题。** 第一版这么写过，被
    `test_contended_acquire_does_not_leak_permission_errors` 直接证伪：EACCES 恰恰
    发生在文件被并发创建/删除的瞬间，那一刻 `exists()` 也会是 False → 误判成
    "真实权限问题" → 照样漏出去。所以改为：**先一律重试，但记下原始的
    PermissionError；等待耗尽时若从未见过别的失败形态，就把这个原始异常抛出去**。
    代价是真实权限问题会等满 `wait_timeout` 才报错；换来的是竞争路径永不误报。
    诊断仍然是准的（抛 PermissionError，不是伪装成"别的进程卡死"的 TimeoutError）。
    """
    deadline = time.time() + wait_timeout
    permission_error = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except FileExistsError:
            pass
        except PermissionError as exc:
            permission_error = permission_error or exc
        # —— 竞争失败方共用：陈旧回收 → 退避重试 ——
        try:
            if time.time() - os.path.getmtime(lock_path) > stale_seconds:
                # 陈旧锁：删掉重试。多个竞争者可能同时删，FileNotFoundError 被下方 OSError 吞掉。
                os.remove(lock_path)
                continue
        except OSError:
            pass
        if time.time() > deadline:
            if permission_error is not None:
                raise permission_error
            raise TimeoutError(f"锁等待超时（另一进程可能卡死）: {lock_path}")
        time.sleep(0.05)


def release(lock_path: str, fd: int) -> bool:
    """释放锁。返回文件是否确认消失（供调用方/测试观测；__exit__ 不据此抛错）。

    2026-10-07 核验修复：旧实现对 os.close/os.remove 各吞一次 OSError，删除失败
    零可观测性，且 Windows 上新建锁文件可能遭遇 AV/索引器的瞬态句柄竞争——
    os.remove 做有限次短退避重试；仍失败则返回 False（不再静默假装成功）。"""
    try:
        os.close(fd)
    except OSError:
        pass
    for attempt in range(RELEASE_REMOVE_ATTEMPTS):
        try:
            os.remove(lock_path)
        except OSError:
            if attempt + 1 < RELEASE_REMOVE_ATTEMPTS:
                time.sleep(0.03 * (attempt + 1))
                continue
            return False
        # remove 返回后确认目录项确实消失（删"成功"但文件仍在的环境层劫持/瞬态）
        if not os.path.exists(lock_path):
            return True
        if attempt + 1 < RELEASE_REMOVE_ATTEMPTS:
            time.sleep(0.03 * (attempt + 1))
    return False


@contextmanager
def locked(
    lock_path: str,
    *,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
):
    """with 形式的互斥锁。父目录不存在时自动创建。"""
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = acquire(lock_path, wait_timeout=wait_timeout, stale_seconds=stale_seconds)
    try:
        yield
    finally:
        release(lock_path, fd)
