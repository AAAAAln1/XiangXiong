#!/bin/bash
# watchdog.sh — 崩溃兜底。独立进程，每60秒检查 memory.md 状态。
# v3: 监控文件 size 变化（非仅 mtime），防止「只读不写」脑死亡漏检。
# v3: 只报警（写 .watchdog_alert），不 kill。让人类决定。

MEMORY_FILE=".claude/memory.md"
STATE_FILE=".observe_state.json"
ALERT_FILE=".watchdog_alert"
MAX_IDLE_MINUTES=10
CHECK_INTERVAL=60

echo "[watchdog] started at $(date) | pid=$$ | max_idle=${MAX_IDLE_MINUTES}min | mode=alert-only"

# 初始化上次记录的文件大小
last_size=$(stat -c%s "$MEMORY_FILE" 2>/dev/null || stat -f%z "$MEMORY_FILE" 2>/dev/null || echo "0")
echo "[watchdog] initial memory.md size = ${last_size} bytes"

mtime_of() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "0"
        return
    fi
    if [[ "$OSTYPE" == "darwin"* ]]; then
        stat -f %m "$file" 2>/dev/null || echo "0"
    else
        stat -c %Y "$file" 2>/dev/null || echo "0"
    fi
}

size_of() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "0"
        return
    fi
    if [[ "$OSTYPE" == "darwin"* ]]; then
        stat -f%z "$file" 2>/dev/null || echo "0"
    else
        stat -c%s "$file" 2>/dev/null || echo "0"
    fi
}

while true; do
    sleep $CHECK_INTERVAL

    current_size=$(size_of "$MEMORY_FILE")
    memory_mtime=$(mtime_of "$MEMORY_FILE")
    state_mtime=$(mtime_of "$STATE_FILE")
    now=$(date +%s)

    memory_idle=$(( (now - memory_mtime) / 60 ))
    state_idle=$(( (now - state_mtime) / 60 ))

    # ── 核心判定：size 增长 = 有效产出 ──
    if [ "$memory_mtime" != "0" ]; then
        if [ "$current_size" != "0" ] && [ "$current_size" == "$last_size" ] && [ $memory_idle -ge $MAX_IDLE_MINUTES ]; then
            # 文件 10 分钟内没变大，且 mtime 没更新 → 真·脑死亡
            echo "[watchdog] $(date): memory.md size=${current_size} unchanged for ${memory_idle}min → 脑死亡"
            echo "memory_mtime_idle=${memory_idle}min" > "$ALERT_FILE"
            echo "memory_size=${current_size}" >> "$ALERT_FILE"
            echo "checked_at=$(date -Iseconds)" >> "$ALERT_FILE"
        elif [ "$current_size" != "$last_size" ]; then
            # size 变了 → 有产出，更新基准
            last_size="$current_size"
        fi
    fi

    # ── 双 mtime 兜底：两个文件都彻底不动 → 真·脑死亡 ──
    if [ "$memory_mtime" != "0" ] && [ "$state_mtime" != "0" ]; then
        if [ $memory_idle -ge $((MAX_IDLE_MINUTES * 2)) ] && [ $state_idle -ge $((MAX_IDLE_MINUTES * 2)) ]; then
            echo "[watchdog] $(date): BOTH files idle >$((MAX_IDLE_MINUTES * 2))min → 深度脑死亡"
            echo "memory_idle=${memory_idle}min state_idle=${state_idle}min" >> "$ALERT_FILE"
        fi
    fi
done
