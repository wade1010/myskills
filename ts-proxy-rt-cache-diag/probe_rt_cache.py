#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ts_proxy rt_ 实时缓存探针（零依赖：只用 socket 讲 RESP，不需要 redis-py）。

用法：
    python probe_rt_cache.py                 # 用 config.toml 的 [redis] 配置自动连
    python probe_rt_cache.py --host 192.168.1.101 --port 16379 --password a123456
    python probe_rt_cache.py --api rt_min    # 只看某接口
    python probe_rt_cache.py --hold rt_min_daily --meta rt_min   # 看 hold/meta 内容
    python probe_rt_cache.py --code 000001.SZ                    # 统计该标的在各 day 桶的出现次数

它能一眼看出「rt_ 缓存到底写没写、写在哪个 variant」——先取证再改代码，
避免凭猜测连改数轮（这个项目已经踩过）。
"""
import argparse
import gzip
import json
import os
import re
import socket
import sys

KEY_PATTERNS = [
    "rt:day:*",       # 当日逐分钟桶（派生接口「全天数据集」的源）
    "rt:dayall:*",    # 派生接口当日全量累积桶（按 ts_code 累积全天）
    "rt:batch:*",     # 分钟/分组快照（serving 主读路径）
    "rt:snap:*",      # full 模式整份快照
    "rt:hold:*",      # 时段外兜底「最后一份快照」
    "rt:agghold:*",   # 聚合档当日兜底面
    "rt:meta:*",      # 每轮结果摘要（ready/rows/updated_at）
    "rt:codes:*",     # 标的列表缓存
]


class Redis:
    def __init__(self, host, port, password, timeout=15):
        self.s = socket.create_connection((host, port), timeout=timeout)
        # 必须关 Nagle：否则 delayed-ACK 会把单命令往返从 ~10ms 放大到 20ms+，
        # 量「逐条命令 vs pipeline」的差距时会得出错误结论。
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.f = self.s.makefile("rb")
        if password:
            self.cmd("AUTH", password)

    def _send(self, *args):
        out = [b"*%d\r\n" % len(args)]
        for a in args:
            if isinstance(a, str):
                a = a.encode("utf-8")
            out.append(b"$%d\r\n%s\r\n" % (len(a), a))
        self.s.sendall(b"".join(out))

    def _read(self):
        line = self.f.readline()
        if not line:
            raise EOFError("connection closed")
        t, body = line[:1], line[1:-2]
        if t == b"+":
            return body.decode("utf-8", "replace")
        if t == b"-":
            raise RuntimeError(body.decode("utf-8", "replace"))
        if t == b":":
            return int(body)
        if t == b"$":
            n = int(body)
            return None if n == -1 else self.f.read(n + 2)[:-2]
        if t == b"*":
            n = int(body)
            return None if n == -1 else [self._read() for _ in range(n)]
        raise RuntimeError("bad reply: %r" % line)

    def cmd(self, *args):
        self._send(*args)
        return self._read()

    def scan_all(self, match, limit=500000):
        out, cursor = [], "0"
        while True:
            res = self.cmd("SCAN", cursor, "MATCH", match, "COUNT", "1000")
            cursor = res[0].decode()
            out += [k.decode("utf-8", "replace") for k in res[1]]
            if cursor == "0" or len(out) > limit:
                return out


def parse_config_toml(path):
    """极简 [redis] 段解析（只取 host/port/password/enabled），避免引 tomllib 依赖差异。"""
    if not os.path.isfile(path):
        return {}
    section, out = None, {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                section = m.group(1)
                continue
            if section != "redis" or "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            v = v.split("#")[0].strip().strip('"').strip("'")
            out[k.strip()] = v
    return out


def group2(keys):
    """rt:<kind>:<api>:<variant>:... -> {api:variant: n}"""
    c = {}
    for k in keys:
        p = k.split(":")
        c[":".join(p[2:4])] = c.get(":".join(p[2:4]), 0) + 1
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--password")
    ap.add_argument("--config", default="D:/workspace/ts_proxy/backend/config.toml")
    ap.add_argument("--api", help="只展示该接口的桶")
    ap.add_argument("--hold", help="dump 指定接口的 rt:hold（gunzip 后打印 mode/held_at/前几行）")
    ap.add_argument("--meta", help="dump 指定接口的 rt:meta")
    ap.add_argument("--code", help="统计该标的在 rt:day 桶中的出现分钟数")
    a = ap.parse_args()

    cfg = parse_config_toml(a.config)
    host = a.host or cfg.get("host")
    port = a.port or int(cfg.get("port") or 6379)
    pw = a.password if a.password is not None else cfg.get("password")
    if not host:
        sys.exit("缺少 redis host：给 --host 或确认 config.toml 的 [redis]")
    print("# redis %s:%s (config=%s)" % (host, port, a.config))

    r = Redis(host, port, pw)
    print("PING=%s DBSIZE=%s\n" % (r.cmd("PING"), r.cmd("DBSIZE")))

    for pat in KEY_PATTERNS:
        keys = r.scan_all(pat)
        if a.api:
            keys = [k for k in keys if (":" + a.api + ":") in k]
        print("=== %-14s count=%d" % (pat, len(keys)))
        for k, n in sorted(group2(keys).items(), key=lambda x: -x[1])[:15]:
            print("      %-44s %d" % (k, n))
        if keys and pat in ("rt:day:*", "rt:dayall:*"):
            ks = sorted(keys)
            print("      time range: %s .. %s" % (ks[0].rsplit(":", 1)[-1], ks[-1].rsplit(":", 1)[-1]))
            sample = ks[-1]
            print("      HGET %s __fields__ = %s" % (sample, r.cmd("HGET", sample, "__fields__")))
        print()

    if a.meta:
        for k in r.scan_all("rt:meta:*"):
            if a.meta not in k:
                continue
            m = r.cmd("HGET", k, "meta")
            if m:
                d = json.loads(m)
                print("meta %s\n  updated_at=%s ready=%s complete=%s rows=%s total=%s error=%r" % (
                    k, d.get("updated_at"), d.get("ready"), d.get("complete"),
                    d.get("rows"), d.get("total_codes"), d.get("error")))

    if a.hold:
        for k in r.scan_all("rt:hold:*"):
            if a.hold not in k:
                continue
            b = r.cmd("GET", k)
            if not b:
                print("hold %s = (none)" % k)
                continue
            h = json.loads(gzip.decompress(b))
            bc, items = h.get("by_code") or {}, h.get("items") or []
            print("hold %s\n  mode=%s round=%s held_at=%s fields=%s by_code=%d items=%d" % (
                k, h.get("mode"), h.get("round"), h.get("held_at"), h.get("fields"), len(bc), len(items)))
            for i, c in enumerate(list(bc)[:5]):
                print("    %s => %s" % (c, str(bc[c])[:160]))

    if a.code:
        keys = sorted(r.scan_all('rt:day:*'))
        hit = sum(1 for k in keys if r.cmd("HGET", k, a.code))
        print("rt:day 桶 %d 个，含 %s 的有 %d 个（≈该标的当日可用分钟数）" % (len(keys), a.code, hit))


if __name__ == "__main__":
    main()
