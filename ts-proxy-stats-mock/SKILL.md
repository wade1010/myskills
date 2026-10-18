---
name: ts-proxy-stats-mock
description: 给 ts_proxy 的「调用监控」统计模块（request_logs + stats_minute / stats_token_hour / stats_token_day）批量灌入模拟调用数据，用于本地/演示环境演示接口榜、Token 榜、趋势图、区间排行。当用户要"造点调用数据看看统计页面"、"模拟 token 调用"、"给统计页填数据"、或排查统计数据为何为空时触发。涵盖 live 滚动窗口、批量 INSERT 避坑、token_prefix 必须进聚合表、复用真实 token 等关键要点。
---

# ts-proxy 统计模块模拟数据生成

用于给 ts_proxy 后端统计接口填充可演示的数据。核心脚本已打包在 `scripts/`：
`gen_sim.py`（生成 + 直灌 MySQL）、`verify.py`（打全量接口做断言校验）。

## 前置与约定

- 目标库：生产/共享库 `tsproxy`（默认 `192.168.1.100:13306`，user `root` / pass `a123456`）。
  改连接串见 `gen_sim.py` 顶部常量；请勿对真实用户数据做破坏。
- 管理后台登录：`POST /admin/api/login` `{"username":"admin","password":"admin123"}` →
  `data.token` 作为 `Authorization: Bearer <token>`。
- 统计接口前缀：`/admin/api/stats/apis/{live,trend,ranking,export,{name}/detail}`、
  `/admin/api/stats/tokens/{live,ranking,export,{id}/trend,{id}/detail}`。

## 关键坑（必读，否则白干或出 bug）

1. **live 是滚动窗口**：`apis/live` 默认 5min（上限 60）、`tokens/live` 默认 15min（上限 180）。
   模拟数据必须用 `NOW()` 相对当前时间生成，否则打开页面时数据已滑出窗口、显示 0。
   ranking/trend 长窗口不受影响。
2. **所有请求都落 request_logs（含缓存命中）**：缓存命中带 `from_cache=1` + `cache_source`
   （BadgerDB/CK/Realtime 等），被拒请求也有明细（reject_reason）。实证：handler.go 的
   logRequest 是所有完成请求唯一出口（FromCache=cacheSource!=""），生产库 from_cache=1
   达 14 万+ 行。mock 仍推荐直接 `INSERT`：不受真实限流 / 上游耗时 / 返回内容影响，口径可控。
   注意：缓存命中不计 upstream_calls、不消耗上游配额、不参与熔断（仅 cacheSource=="" 才计）。
3. **聚合表的 token_prefix 必须进 INSERT 列**：`stats_token_hour/day` 的唯一键是
   `(token_id, token_prefix, period_start)`。漏写 token_prefix → MySQL 落空串 → 与
   request_logs 尾巴桶的 merge key 不一致 → **token 排行每个 token 重复成两行**。
   （生产 `AggregateTokenHour` 一直带前缀，仅测试桩易踩。）
4. **批量 INSERT 防炸**：`stats_minute` 在 2 天窗口下可达 20 万+行。若把所有行塞进
   **单条** `INSERT ... VALUES (...),(...)` 会撞 `max_allowed_packet` 或拖垮服务端。
   必须按块拆分（gen_sim.py 用 1000/2000 行一条语句）。
5. **cache_hits 默认会是 0**：mock 时若所有行 `from_cache=0`，页面"缓存命中"列永远 0%。
   给约 30~35% 行打 `from_cache=1`（且 `reject_reason=''` 才算 upstream_calls）即可让该列有数。

## 用法

```bash
cd <skill>/scripts
# 1) 重建 tokmap.txt（从真实 user_tokens 取 N 个 token，名称/用户名能正确解析、不污染表）
mysql -h192.168.1.100 -P13306 -uroot -pa123456 tsproxy --skip-column-names -B \
  -e "SELECT id,user_id,token_prefix FROM user_tokens WHERE id BETWEEN 1 AND 20 ORDER BY id;" > tokmap.txt
# 2) 生成 SQL
python gen_sim.py        # 产出 sim_insert.sql
# 3) 导入
mysql -h192.168.1.100 -P13306 -uroot -pa123456 tsproxy < sim_insert.sql
# 4) 校验（无重复行、cache 有数、新 token 能解析）
python verify.py
```

## 调参（gen_sim.py 顶部常量）

- `DETAIL_MIN`：request_logs 覆盖的最近分钟数（建议 360 = 6h，撑 live≤3h + token 趋势≤6h）。
- `AGG_MIN`：stats_minute / stats_token_hour 覆盖的分钟数（2880 = 2 天）。
  **注意**：`stats_token_day` 的天桶数 = `AGG_MIN/1440`，所以 2880 只给今/昨 2 个 day 桶；
  要用 `granularity=day` 看 3 天以上，需把 AGG_MIN 调大到 3~7 天重跑。
- `CACHE_RATE`：缓存命中比例（0.35）。
- `APIS`：要模拟的接口名列表（需与前端展示一致）。

## 清理

- 明细：`DELETE FROM request_logs WHERE request_id LIKE 'SIM%';`
- 聚合表无 SIM 标记，会随 60 天保留期自然滑出；如需立刻清空指定 token 的聚合，按
  `(token_id, period_start)` 范围 `DELETE`。

## verify.py 校验项

token 排行三档（今天 / 2d-hour / 3d-day）行数 == token 数、无重复 token_id、
cache_hits 合计 > 0；api live/trend/ranking 接口全覆盖、cache 命中率≈CACHE_RATE、
users 数 == token 数；抽样新 token 的 detail/trend 字段齐全。
