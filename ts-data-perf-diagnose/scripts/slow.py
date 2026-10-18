#!/usr/bin/env python3
"""从 ts_data 的 HTTP 访问日志还原每个接口的真实响应耗时。

用法:
    python3 slow.py <日志文件>
    cat slow.py | ssh root@192.168.1.88 "python3 - /root/workspace/ts_data/backend/logs/2026-10-01.log"

原理: 日志每处理一个请求打 3 行
    <ts> - INFO - [HTTP] ➤ GET /path
    <ts> - INFO - [HTTP]   Query: {...}
    <ts> - INFO - [HTTP] ◀ GET /path → 200
时间戳带毫秒，按 (method, path) 配对求差即得响应时间。
注意: 同一路径并发时采用 FIFO 配对，属近似估计；对只读列表接口足够用。
"""
import re
import statistics
import sys
from collections import defaultdict, deque

# 箭头用 \uXXXX 转义，避免不同终端/编码下粘贴损坏
PAT_START = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) - \w+ - \[HTTP\] \u27a4 (\w+) (\S+)'
)
PAT_END = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) - \w+ - \[HTTP\] \u25c0 (\w+) (\S+) \u2192 (\d+)'
)


def to_sec(dt: str, ms: str) -> float:
    h, m, s = dt.split(' ')[1].split(':')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)

    pending: dict[tuple, deque] = defaultdict(deque)
    durations: dict[tuple, list] = defaultdict(list)

    with open(sys.argv[1], encoding='utf-8', errors='replace') as fh:
        for line in fh:
            m = PAT_START.match(line)
            if m:
                pending[(m.group(3), m.group(4).split('?')[0])].append(to_sec(m.group(1), m.group(2)))
                continue
            m = PAT_END.match(line)
            if m:
                key = (m.group(3), m.group(4).split('?')[0])
                q = pending.get(key)
                if q:
                    t0 = q.popleft()
                    durations[key].append(to_sec(m.group(1), m.group(2)) - t0)

    rows = []
    for (_, path), vals in durations.items():
        if len(vals) < 2:
            continue
        vals = sorted(vals)
        rows.append((statistics.median(vals), vals[-1], len(vals), path))
    rows.sort(key=lambda r: -r[0])

    print(f"{'median(s)':>10} {'p_max(s)':>9} {'n':>6}  endpoint")
    for med, mx, n, path in rows[:30]:
        print(f"{med:>10.3f} {mx:>9.3f} {n:>6}  {path}")


if __name__ == '__main__':
    main()
