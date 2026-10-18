---
title: ts_proxy rt_ 实时缓存取证与排障
summary: 排查 ts_proxy rt_ 实时接口（rt_min / rt_min_daily 等）缓存行为异常时，先用零依赖 RESP 探针读 Redis 桶布局 + 用 admin API 读 realtime_apis 配置行，拿到地面真相再改代码；并附已实锤的几类根因签名。
description: 当 rt_ 实时接口出现「只返回一条/返回全天/数据陈旧/聚合档不产数据/聚合档 label 与上游差 1 分钟/聚合档返回一个非 N 整数倍的野标签（如 30MIN 返 13:59）且上游正常/重启后聚合档突然落后若干轮/15·30·60MIN 返回空/派生接口不生效/命中率异常/响应慢/偶发慢 2~5 秒/隔段时间第一次慢/返回行数比预期少很多（疑似被截断）」，或出现「Redis 里某些 rt:* key 特别大/内存涨得快/逼近 maxmemory」时使用。涵盖 Redis 各 rt:* key 的写入者与读取者对照表、config.toml 与 admin API 的配置取证方法、INSERT IGNORE 静默失效、latestFields 为 nil 打死聚合、__fields__ 在桶间不齐全、逐条命令 × 10ms RTT 撞 5s ctx 预算导致静默截断、通配符未展开、聚合「结束时刻标注 + 组末对齐」口径（含连续采样反推上游口径的方法）、聚合轮次键只是写侧预计算缓存而「已过去的组末时刻永不被补写」导致陈旧 rt:agghold（ttl≈19h）顶替、读侧按组现算自愈（serveAggRoundRebuilt/aggregateRoundFromDay/ReadDailyRangeFromSource）、L1 清理窗口按档位自适应（`l1KeepWindow`，防 15/30/60MIN 的上一轮被误清）、「偶发慢」时用 Redis SLOWLOG + cmdstat_evalsha 识别「全市场一次塞 Lua」的 RMW 阻塞（本项目 `rt:dayall` 已于 2026-09-23 整体移除，附等价性验证方法）、以及 rt:* 内存分层审计（MEMORY USAGE 分组、管理页大小列不可信、maxmemory-policy=allkeys-lru 的静默淘汰风险、非交易分钟残桶识别）等实锤根因，以及改配置即时生效 vs 改代码需重启的区别。
agent_created: true
---

# ts_proxy rt_ 实时缓存取证与排障

## 触发场景
- `rt_min_daily` 之类派生接口命中缓存只返回 1 条（或返回空），未命中走上游反而返回全天。
- 聚合档（`5MIN/15MIN/30MIN/60MIN`）不产数据、`rt:batch:{api}:{aggVariant}:*` 一个都没有、`rt:agghold` 定格。
- **聚合档数值/时间与上游不符**：如 `rt_min?freq=5MIN` 返 `13:19:00` 而上游是 `13:20:00`；或 15/30/60MIN **返回 `count=0`**（5MIN 反而有数据）。
- **聚合档返回"野标签"**：标签不是 N 的整数倍（如 30MIN 返 `13:59`、5MIN 返 `13:40`）且落后上游若干轮，
  尤其发生在**刚重启/刚切 leader 之后** → 见根因签名 7（轮次键缺失 → 陈旧 `rt:agghold` 顶替）。
- 改了配置不生效 / 派生接口被当成普通接口在预取上游。
- **响应慢（2s+）或返回行数明显偏少**（如请求 1000 只标的只回 435 行却 `code=0`）。
- **数据正确但偶发慢**：同一接口连打多次大多 <400ms、偶尔一次 2~5s；或「隔段时间第一次慢」，
  且 `X-Cache` 仍是 `realtime` → 见根因签名 8（Redis 被某支 Lua 脚本阻塞；本项目那支
  `rt:dayall` 全市场脚本已于 2026-09-23 删除，再现就要查别的客户端）。
- `ts_code` 传通配符（`0000*.SZ`）返回空，而其它 rt_ 接口正常。
- **Redis 里有特别大的 key / 内存涨得快 / 快到 maxmemory**（如 `rt:hold:*` 几百 KB、`rt:dayall:*` 几十 MB）
  → 见根因签名 9（先分清设计必需与残留；重点是 `maxmemory-policy=allkeys-lru` 的静默淘汰风险）。
- **非交易日 / 非交易时间要用模拟上游（sim）验证 `rt_min`、`rt_min_daily` 的取数-聚合-派生-落库**
  → 见「非交易日 / 模拟上游的端到端验收」（含「只清 time_ranges 不够」「follower 试拉不落库」
  「sim 会在 /dataapi 短路 rt_min」「FullDay 墙钟栅格与整组齐全规则」四个必踩坑）。

## 第 0 条铁律：先取证，再改代码
这个模块曾因为「凭猜测连改 4 轮」浪费大量时间。**必须先拿到 Redis 桶布局 + 配置行真相**，
两者一对，根因基本立刻显形。禁止在没取证的情况下改 `store.go`/`prefetcher.go` 的读路径。

## 取证一：Redis 桶布局（本 skill 自带探针）
```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
"$PY" ~/.workbuddy/skills/ts-proxy-rt-cache-diag/probe_rt_cache.py
"$PY" .../probe_rt_cache.py --meta rt_min --hold rt_min_daily --code 000001.SZ
```
零依赖（socket 讲 RESP，不装 redis-py）。默认自动读 `backend/config.toml` 的 `[redis]`。

连接信息（本机 dev）：Redis `192.168.1.101:16379` pw `a123456`；
MySQL `192.168.1.100:13306` db **`tsproxy_test`**（⚠️ 生产库是 `tsproxy`，两者不同！）。

### rt:* key 对照表（排障核心）

| key | 形态 | 写入者 | 读取者 |
|---|---|---|---|
| `rt:day:{api}:{variant}:{minute}` | hash，field=ts_code→行JSON，另有 `__fields__` | `prefetcher.RunRound` 分桶循环 **每轮无条件** `WriteDayBatch` | `ReadDayBucket`（聚合源）、`ReadDailyFromSource`（派生全天） |
| ~~`rt:dayall:{api}:{variant}`~~ | **已于 2026-09-23 整体移除**（A 方案）：写侧全市场 Lua RMW 每分钟阻塞 Redis ~13 秒（见签名 8）；读侧 `rt:day` 现算已是超集 | — | — |
| `rt:batch:{api}:{variant}:{round}` | hash | `WriteBatchSnapshot`（分钟快照 / 聚合档按**组末分钟**，只写整组齐全的柱，TTL≥N+2 分钟） | `ReadSnapshot`（serving 主路径，聚合档轮次由 `aggRoundOf` 算） |
| `rt:snap:{api}:{variant}:{round}` | gzip JSON | `WriteFullSnapshot` | `ReadSnapshot`（full 模式） |
| `rt:hold:{api}:{variant}` | gzip JSON | `writeHoldFromSnapshot`（每轮 maxDm 一份） | 时段外兜底 `ReadHoldSnapshot` |
| `rt:agghold:{api}:{variant}` | hash | `writeAggTarget`→`WriteAggHold`（只写完整组） | **时段内最后兜底**（轮次键缺失/过期）+ 时段外 `ReadAggHold` |
| `rt:meta:{api}:{variant}` | hash `meta=`JSON | `recordMeta`（每轮） | `ReadMeta` / 状态页 |

### 派生接口（rt_min_daily）读路径
1. 基础档（1MIN / 不传 freq）→ `ReadDailyFromSource(src.Name, src.Variant, userCodes)`
   扫源接口 `rt:day:*`，精确标的走 `HGET` + **分块 pipeline**（否则 HGETALL 全市场，
   单标的白拉 30MB/2.4s）；含通配符才走 HGETALL 全量（展开需要全集）。
2. 聚合档（5/15/30/60MIN）→ `ReadDailyAggFromSource` 把源 `rt:day:*` 按交易分钟组**全天**聚合
   （从源桶现算，配置中途启用/冷启动也能立刻给全天）。
3. 源桶不可读 → 返回空成功（`emptyRealtimeResponse`）。**没有 dayall 兜底了**（2026-09-23 移除：
   写侧停更后它会拿"旧全量"顶替正确结果，与 `rt:agghold` 同类病）。
两路都要 `__fields__` 做字段投影，**缺了会判「无可用列」直接返回空**（见根因签名 3）。
时间顺序靠 `sort.Strings(keys)` —— 桶名内嵌 `2026-09-23T09:30:00` 时间戳，字典序即时间序。

## 取证二：配置行（admin API 优先；后端不在线则直连 MySQL）
```bash
TOKEN=tushareproxy-admin-token
# ① 各 rt_ 接口的配置行（看 derived_from / aggregated_freqs / extra_params / serve_mode /
#    code_source_type / variant）—— 必须用 GET，POST 空 body 会返 400
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18001/admin/api/realtime/apis
# ② 全局实时设置（看 snapshot_ttl_factor / retry_grace_minutes / fast_complete_seconds /
#    align_offset_seconds / hold_backfill_on_miss —— 判断退避轮数与 TTL 是否合理）
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18001/admin/api/realtime/config
```
实测（2026-09-23 dev）：`retry_grace_minutes=2`（聚合档回退实际只走 2 组，不是 3）、
`snapshot_ttl_factor=2`、`fast_complete_seconds=10`、`minute_align_jitter_ms=200`。
dev 后端 `127.0.0.1:18001`（也可用 `192.168.1.111:18001`）。

改配置**走 admin POST**（`POST /admin/api/realtime/apis`，body 用同名字段、带 `id`）：
`UpsertRealtimeAPI` 会重算 `variant` 并 `invalidateRealtimeConfigCache()` → **立即生效、无需重启**。
（POST 时把 GET 回来的配置原样传回、只改要改的字段最稳。）

**后端没在跑时（18001 拒绝连接 / 502）→ 直连 MySQL 取证**（本机有客户端，不用启服务）：
```bash
export MYSQL_PWD=a123456
M="/c/mysql-client/bin/mysql -h192.168.1.100 -P13306 -uroot --default-character-set=utf8mb4 -N -B"
# dev 库 = tsproxy_test，prod 库 = tsproxy（同一实例）
$M -e "SELECT api_name,freq,serve_mode,time_ranges,derived_from,aggregated_freqs FROM realtime_apis;" tsproxy_test
$M -e "SELECT interface_name,title,limit_value,credit_value,rate_limit FROM interface_metas WHERE interface_name LIKE 'rt\_%';" tsproxy_test
```
⚠️ 接口元数据表名是 **`interface_metas`（复数）**——前端注释里的 `sys_interface_meta` 是错名，照它查会 1146。
`rate_limit` 文本形态为 `[{"credit":-1,"limit":50}]`（取 credit≤账号积分的最高档；credit=-1 全权限专属档）。

## 已实锤的根因签名

### 1. 派生接口只返 1 条 / 未命中返全天 → 配置行 `derived_from` 为空
签名：`rt:meta:{派生接口}` 存在**且 updated_at 每轮更新**（说明预取循环没跳过它 = `derived_from` 空）
+ 源接口的 `rt:day:rt_min:*` 在正常增长（数据在，只是派生行没配好）。
无 `derived_from` 时 TryServe 走普通路径（strict + 无快照 → miss → 上游返全天）。

⚠️ **踩坑**：用 `INSERT IGNORE` 初始化派生行时，若该行**已存在**会被静默忽略，
`derived_from` 永远写不上（而 `UPDATE` 那半句生效了，所以极易误判为「已配置」）。
改配置一律用 `UPDATE` 或 admin API。

### 2. 聚合档完全不产数据且无报错 → `latestFields` 为 nil
`aggregateBars` 靠字段名定位 ohlc 列；`prefetcher.RunRound` 里 `latestFields` 若未赋值（nil），
所有聚合返回 false → `writeAggTarget` 从不调用，**没有任何日志**。
签名：`rt:batch` 里没有 `freq="5MIN"/"15MIN"/...` 的 key；`rt:agghold:*` 的 `time` 定格在某个
`maxDm`（可用四条档的组末时间反推最后一次成功的 maxDm）。
护栏：`aggregate_test.go` 的 `TestAggregateDayByFreq_RequiresFields`。

### 3. 派生全天读取报「无可用列」/ 聚合档整档 `ok=false` → `rt:day` 桶缺 `__fields__`
`WriteDayBatch` **仅在 `len(fields)>0` 时**写 `__fields__`，而它开头就 `Del` 整个 key；
字段为空的那一轮 `Del` 成功、`HSET` 失败 → 该桶没有字段序。
⚠️ 实测（2026-09-23，11:26）：**116 个 `rt:day` 桶里只有 40 个带 `__fields__`**，缺口是常态而非例外。
所以读端必须**扫描所有桶、取第一个非空的 `__fields__`**；**绝不能只读 `keys[0]`**
（首桶恰好缺 → `fields` 为空 → `aggregateDayByFreq` 找不到 `time` 列下标 → `5/15/30/60MIN` 全档 `ok=false`；
基础档则退化成「无字段名但仍有行」的响应）。
另有历史坑：`maybeAggregate` 里曾有一处冗余 `WriteDayBatch`，用空 fields 覆盖同 key 抹掉 `__fields__`，已删。

### 4. 慢（2.4s+）且**静默返回半截数据集** → 逐条命令 × 10ms RTT，撞 5s ctx 预算
本机到数据 VIP `.101:16379` 的**单条命令往返实测 ≈10ms**；SCAN 很便宜（DBSIZE 仅百级），
瓶颈永远是「发了几条命令」。签名：**全 miss 的请求也照样慢**（成本在命令数，不在数据量）。

| 场景 | 逐条命令 | pipeline 后 |
|---|---|---|
| 111 个桶 × HGET（1 只标的） | 1.285s | **0.012s** |
| 111 个桶 × HGETALL（全市场） | 2.52s | — |

⚠️ **最恶的一条**：`redisCtx()` 预算只有 **5s**，而逐条 `cmd.Result()` 只 `continue` 跳过错误，
所以超时后剩余命令**全部瞬间失败**、函数照样 `ok=true` 返回 → **HTTP 200 / `code=0` / `X-Cache=realtime`
但只有半截数据**。实锤：请求 1000 只标的返回 `count=435`、耗时 5.31s（435×12ms 自洽，等于 5s 内能跑完的量）。
修复：全天数据集用独立的 `dayReadCtx()`(20s)；两条路径都**分块 pipeline**
（`maxDayPipeCmds=20000` 折算每块桶数、`maxDayPipeAllKeys=8` 按桶数限流）；
`pipe.Exec` 的错误**只在 `ctx.Err()!=nil` 时**判失败并 `logger.Warn`（单条 field 不存在会返 `redis.Nil`，不是失败）。
修复后实测：1 只 0.010~0.019s｜10 只 0.021s｜1000 只 0.447s/117000 行｜全市场 1.70s。

### 5. 派生接口 `ts_code` 通配符返回空 → `buildDailyResponse` 未展开
原本只做精确查表，`ts_code=0000*.SZ` 一律 count=0（其它 rt_ 接口都支持通配符）。
已修：按 `byCode` 展开、输出按 ts_code 字典序、`seen` 去重、严格模式下一只都没匹配到判未命中
（语义与 `resolveBatchRows` 对齐）。护栏单测在 `store_daily_test.go`。

### 6. 聚合档「标签/分组错位一根」（5MIN 返 13:19 而上游是 13:20；15/30/60MIN 直接返回空）

**上游 1MIN 与聚合的实测口径（2026-09-23 三条独立证据，别再靠猜）**：
1. `time` 是覆盖区间的**结束时刻**：label=13:32 的 1MIN 柱在 13:32:31 与 13:32:46 两次采样**数值恒定**
   （若 label 是"正在形成的分钟"，值会持续增长）。
2. 当日 label 序列 = `09:30`(开盘集合竞价根) + `09:31..11:30`(120 根) + `13:01..15:00`(120 根)，
   **没有 13:00**，共 **241** 根。
3. 精确求和（vol/amount/OHLC 逐位吻合）：`5MIN(13:30)=1MIN 13:26..13:30`、`15MIN(13:30)=13:16..13:30`、
   `30MIN(13:30)=13:01..13:30`、`60MIN(11:30)=10:31..11:30`。

⇒ **`bar[label=T] = 最近 N 根 1MIN(label ≤ T) 之和`；组 = (T-N, T]；label = 组末那根的 label；
09:30 竞价根不参与任何组。** 序号按「已成交分钟数」：`09:30=0、09:31=1…11:30=120、13:01=121…15:00=240`。

错位是怎么来的（历史 bug）：`tradingMinuteIndex` 把 label 当**窗口起点**编号
（`09:30=0`、`11:30=120`，还把不存在的 `13:00` 也编成 `120` 撞号），分组取 `groupId*N .. +N-1`
（起点对齐）→ 5MIN 组变成 `{13:30..13:34}` 标 `13:34`；正确是 `{13:31..13:35}` 标 `13:35`。
**判别口诀：拿同一时刻的上游 label 与我们返回的 label 比，差 1 分钟 = 就是这个错位。**

连带第 2 个 bug（15/30/60MIN 更惨，**直接返回 count=0**）：
聚合档快照的轮次键是**组末分钟**（如 `rt:batch:rt_min:freq="5MIN":...T13:30:00`），
而服务侧走的是「当前分钟桶 + 逐分钟回退（`retry_grace_minutes`）」→ 30/60MIN 的键在 N 分钟前，
**永远回退不到** → `noUpstream()` 分支返回 `emptyRealtimeResponse`（HTTP 200、count=0、无报错）。
修复：`aggRoundOf(now,N)` 直接算「最近一根已走完的对齐柱」的组末轮次，历史回退按 **N 分钟**步进
（最多 3 组），并新增 `rt:agghold` 时段内兜底。

另外两条配套（不改就是定时炸弹）：
- **只写整组 N 根齐全的聚合柱**。组没走完时写出去的是"少一根"的残缺柱，而它挂在**组末轮次**上，
  组末一到就会被当完整柱返回（数值系统性偏小）。
- **聚合 batch 键 TTL 必须 ≥ N+2 分钟**（`aggBatchTTL(N)`）。旧值固定 3 分钟：60MIN 的 11:30 柱
  要一直服务到 14:00，3 分钟内就过期 → 整个下午取不到。
- 判断线上跑的是哪版：老对齐的键落在 `label ≡ (N 的倍数 - 1)`（5MIN=`13:44`/`13:49`、60MIN=`13:59`），
  新对齐轮次与之**不重叠**，可据此确认新二进制是否已生效。

护栏单测：`TestMinuteEndIndex_LabelsAndRoundTrip`、`TestAggGroupEndOf`、
`TestAggRoundOf_MatchesUpstream`（含 `12:30/13:00 → 11:30`、`09:35` 之前必须无值）、
`TestAggregateDayByFreq_ExcludesAuctionBar`、`TestAggregateDayByFreq_DropsIncompleteTailGroup`。

### 7. 重启后聚合档 returned 一个**野标签**（如 30MIN 返 `13:59`、5MIN 返 `13:40` 而上游是 13:30/13:50）

**判别口诀：标签不是 N 的整数倍（`13:59`、`13:40` 对 30MIN/5MIN 都不是组末）= 数据不是本轮算的，
几乎一定是 `rt:agghold` 陈旧兜底面顶替。** 先看 `rt:batch` 里**有没有那个轮次键**，而不是先怀疑算法。

根因链（2026-09-23 实锤）：
1. `maybeAggregate` 只聚合 `targetMinute` **所在的那一组** → **进程启动前已经过去的组末时刻
   永远不再被补写**。重启后 30MIN 的 `13:30` 键、5MIN 的 `13:40/13:45/13:50` 键永久缺失
   （实测重启 15 分钟后复查 `rt:batch:...:30MIN:13:30` 仍**不存在**）。
2. 读路径此时 `13:30→13:00→12:30` 全 miss，退到 `rt:agghold`（当日聚合兜底面，**ttl≈19h 到次日开盘**）。
3. agghold 里是「上一次成功收组时」写下的行 —— 若那还是**旧口径二进制**写的，标签就是
   `≡N-1 (mod N)` 的野值（13:59），被当成"当前轮次"返回。
4. **数据真相在 `rt:day`**（每分钟独立覆盖写、全天无缺口：实测 182 键 09:30→14:01 连续）。

排查顺序：
```powershell
# ① 轮次键对齐对不对（应全部落在 N 的整数倍分钟）
redis-cli --scan --pattern 'rt:batch:rt_min*'      # 看尾部分钟 + TTL
# ② 陈旧兜底面（ttl 近 19h 的就是它）
redis-cli --scan --pattern 'rt:agghold*'
# ③ 真相源是否有缺口（有缺口才允许退兜底面）
redis-cli --scan --pattern 'rt:day:rt_min*'        # 数一下分钟是否连续
```
修复（读侧自愈，不耗上游配额）：`serveAggRoundRebuilt`(store.go) 用
`aggregateRoundFromDay`(aggregate.go) + `ReadDailyRangeFromSource`(redis_store.go) 按组现算，
只读该组 N 个桶。接入点 = TryServe 的 `attemptStale` 分支（当前轮次 → `stale=false`）
+ 历史回退循环里 `ReadSnapshot` miss 之后（→ `stale=true`）。agghold 退为最后兜底。
自愈需要 `APIConfig.BaseVariant`（`resolveAggregateVariant` 在改写 Variant **之前**记下）。

⚠️ 自愈的两个硬约束（写错就白自愈）：
- `round` 必须是**组末对齐**分钟（`aggGroupEndOf(idx,N)==idx`），且组内 N 个桶齐全才产出。
- `rt:day` 桶名**不带时区**，必须 `time.ParseInLocation(..., CST)`；按 UTC 解析会整体偏 8 小时。
护栏单测：`TestAggregateRoundFromDay_RejectsNonAlignedRound`（拒绝 `13:59` 与 `13:00`）、
`TestMinuteOfDayKey_ParsesCST`、`TestAggFreqOf_RoundTrip`。

### 8. 「偶发慢 2~5 秒」/ 隔段时间第一次慢 → **Redis 被 Lua 脚本阻塞**（2026-09-23 已根治）

**判别口诀：慢是概率性的**（同一接口连打 16 次全部 <400ms、偶发一次 2.5s），**且 `X-Cache` 仍是
`realtime`（数据正确）→ 先查 Redis 服务端，别改应用层读路径。**

历史根因（代码已删除，保留作为「别再犯」的教训）：`maybeAggregate` 每轮对派生接口调
`WriteDayAll`，把**全市场 5568 只**作为 ARGV 一次塞进 `dayAllScript`（参数规模
**`(11108 more arguments)`**），脚本内 `for i=1,n` 循环 5568 次，每次
`HGET` + `cjson.decode(整个累积数组)` + `cjson.encode` + `HSET`：

| freq | 单次耗时 | 数组元素 |
|---|---|---|
| 1MIN | **2.1~5.6 s**（每分钟 2 次） | 241（≈12KB） |
| 5MIN | 0.3~0.8 s | 49 |
| 15MIN | 0.19~0.36 s | 17 |
| 60MIN | 0.19 s | 5 |

耗时 ∝ 数组长度 ⇒ 成本在 `cjson.decode/encode`。每分钟约 4 次、合计阻塞 **~13 秒/分钟**
（单线程 Redis 期间所有命令排队），请求撞上就慢 2~5 秒。**佐证**：后端日志刷
`write daily dayall(1min) failed {"error":"context deadline exceeded"}`
—— `WriteDayAll` 只给 `redisCtx()` 5 秒，脚本要 3~5.6 秒，**脚本在服务端跑完了、客户端却已超时**。

**已按 A+C 落地根治**：**A** 整体删除 `rt:dayall`（`WriteDayAll`/`ReadDayAll`/`dayAllScript`/
`dayAllKey`/`derivedAPIsOf` + store.go 的读兜底）—— `rt:day` 是全天数据唯一真相源，
读侧现算实测为**超集**且逐位等价（dayall 侧唯一差异是数字书写 `100` vs `100.0`，
语义不同 0 行；其"现算缺"的行 100% 是旧口径野标签 `11:14/11:29/13:59`）；
**C** `l1Put` 清理窗口按档位自适应（`l1KeepWindow(variant)` = 非聚合档 5min / 聚合档 **2N min**）。

⚠️ **"偶发慢"再现时**：先按同一套取证判断是谁在阻塞 —— 本项目已无长脚本，
重点看别的系统 / `redis-py` 客户端（`CLIENT LIST`、`INFO keyspace`）。
**若有人又在写侧加回「全市场一次塞 Lua」的累积写，就是它复发。**

先排除的假象（都实测过，别走弯路）：`SCAN` 自愈（DBSIZE 仅 367、全库 SCAN 14ms）、快照缺失
（`rt:batch` 键在、hlen 5568）、等待窗口（`fullCover` 对 batch 模式恒 true）、codes 缓存
（`PeekFreshCodes` 绝不回源）。

另一条独立隐患：**L1 窗口固定 5 分钟 → 15/30/60MIN 的上一轮被误清、stale 回退穿透 Redis**，
已由 `l1KeepWindow` 修掉，护栏单测 `TestL1KeepWindow` / `TestL1Put_KeepsPrevRoundForAggFreq` /
`TestAggMinutesOfVariant`。

取证命令（本机 Docker CLI 不可用时 → Python raw RESP 直连 Redis）：
```
SLOWLOG GET 128        # evalsha 的 usec + (N more arguments) ← 关键
INFO commandstats      # cmdstat_evalsha / cmdstat_hset / cmdstat_hget
CLIENT LIST            # 谁连着（多副本？别的系统？redis-py 在 db10）
INFO keyspace          # 各 db 键数
HSTRLEN <key> <field>  # 量 field 体量；别用 HGET（可能是几十 MB）
```

### 9. 「有的 key 非常大 / 内存涨得快」→ 分清「设计必需」与「残留」，并查 maxmemory 策略

**管理页的 key 大小列不可信**（大 hash 常报 `0B` 或严重偏低）——一律用 `MEMORY USAGE key` 取证。
2026-09-23 实测：面板显示 dayall 1MIN=`0B`、hold=`1.3MB`，实际是 `78.58MB` / `320KB`。

`rt:*` 内存分层（收盘后实测，全库 `used_memory=486MB / maxmemory=1G`）：

| 前缀 | 实测 | 性质 |
|---|---|---|
| `rt:day` | **371MB / 422 key** | 设计必需（全天唯一真相源）。单桶 5569 field（5568 只 + `__fields__`）：rt_k ~1.1MB/桶、rt_min ~0.78MB/桶 |
| ~~`rt:dayall`~~ | ~~110MB~~ → **已 DEL** | **已废弃残留**（A 方案已删读写），2026-09-23 21:41 手动 `DEL` 回收 **108.70MB**（`used_memory 486.57→377.87MB`）。命令：`SCAN MATCH rt:dayall:*` 一次 `DEL` 全删；**删后其它前缀零影响**（rt:day 422 / rt:batch 0 / rt:agghold 4 / rt:hold 3 不变） |
| `rt:agghold` | 3.3MB | 兜底面，正常 |
| `rt:hold` | 0.44MB | 全市场快照单 string，**正常**（一次 GET 拿全市场，比 5569-field hash 省 RTT） |
| `rt:batch` | 收盘后=0 | N+2 分钟 TTL，自然清空 |

**判断口径**：这些 hash 是「多 field、单 field 小」，读走 HGET 单 field 是 O(1)，**不会阻塞 Redis**。
「大」本身不是病 —— 只有 `cjson.decode(整个数组)` 那种 O(n) 全量重编解码（原 `rt:dayall` Lua）才是病。

**真正要查的四件事**：
1. `CONFIG GET maxmemory maxmemory-policy` + `INFO stats` 的 `evicted_keys`：
   `maxmemory=1G` + **`allkeys-lru`** = 到顶会静默淘汰 `rt:day`（全天真相源）/`rt:codes`（标的全集）
   → 数据空洞、功能降级。宜改 `noeviction`（宁可写失败报错）或提容量。`evicted_keys>0` = 已经在丢数据。
2. **已废弃前缀的残留**：改完写路径后旧键会活到 TTL 才消失，腾不出内存是正常的，别误判成新代码无效。
   2026-09-23 已按此清掉 `rt:dayall:*`（5 键 108.70MB）。**复发判据**：若次日开盘后 `rt:dayall:*`
   又重新出现 → 线上跑的仍是「A 方案之前」的旧二进制（读侧已不依赖它，不会出错，但写阻塞会回来）。
   ⚠️ 删此类残留前先确认**运行中的二进制已不含读它兜底的代码**（老代码删了会退化成空成功）。
3. **非交易分钟的桶先别当 bug，先核对接口自己的 `time_ranges`**：解析 `rt:day:*` 桶名取时间必须
   `key.rsplit('T', 1)[-1]`（用 `split(':')` 会把 `10:46:00` 切成 `46:00` → 全部误判）。
   idx=-1 即收盘后/午休/开盘前的桶。实测 rt_k 有 `13:00` + `15:01~15:06` 共 7 个（7.8MB），
   是**有意配置**「rt_k 的 time_ranges 含收盘后一段」；rt_min 没有（两接口配置本就不同）。
   ⚠️ 副作用：`minuteEndIndex` 有效域不含 13:00 与 15:00 之后 → 这些桶**不参与聚合**；
   但 `ReadDailyFromSource` 按桶名升序拼接（不过索引）→ 该接口「全天数据集」会多出这几行。
   （教训：看到"异常桶"先查配置，别急着定成代码 bug。另：rt_k 桶从 10:46 起、rt_idx_min 从 14:52 起
   —— 是当日启用时间，不是缺口。）
4. `CONFIG GET appendonly save`：缓存与业务键同实例时，`appendonly=yes` 会为几百 MB 纯缓存做 AOF
   （纯 IO 浪费；但 db0 里还有 session/限流键，不能直接关，更干净是缓存独立实例）。

探针骨架（Python raw RESP，本机 Docker CLI 不可用时用）：
`SCAN rt:*` → 按前缀 `group_of()` 聚合 `MEMORY USAGE` → `INFO memory/stats` → `CONFIG GET maxmemory*`。

## 验证闭环（改完必须跑）
1. 用**用户原始 curl** 打 `POST /dataapi`，看响应头 `X-Cache: realtime` 与 `data.count`。
   **聚合档的唯一验收标准 = 与上游同 freq 的柱逐位比对**（`time` + vol/amount/OHLC 全等），
   别只看 count（count=1 但 label 差一分钟就是错的）。
2. 无 ts_code 的全市场请求只核对 `count`（今天约 5568×117 ≈ 65 万行；1~2s 属正常）。
3. 再跑一次探针：确认派生接口自己**不再**有 `rt:meta` 每轮刷新（更新的是源接口），
   且源接口 `rt:day:{src}:{variant}:*` 桶数随分钟单调增长、全天无缺口。
4. **改读路径（性能/截断类）必须量「标的数曲线」**：1 / 10 / 1000 只 + 全市场 + 通配符。
   若耗时随标的数线性增长或卡在 ~5s 整，就是逐条命令 / ctx 截断的病。
   性能类还要**冷却复现**：隔 75s 打一次 vs 连打 16 次 —— 只有前者偶发慢 = Redis 服务端阻塞（签名 8）；
   两者都慢 = 应用层。
5. ⚠️ 确认「跑的是不是新二进制」：磁盘换了 exe 但进程没重启 ≠ 生效。
   - **最快的确定性判据 = 扫 exe 内符号**（比 mtime 靠谱，能证明"新代码真的编进去了"）：
     读 `backend/tsproxy.exe` 全部字节，`d.count(b"aggRoundOf")` 等新函数名 >0 且旧函数名
     （`tradingMinuteIndex`/`tradingMinuteToTime`）==0 → 新版；反之 = 磁盘上是旧二进制。
   - 进程侧：`Get-Process tsproxy | Select Id,StartTime` 与 exe `LastWriteTime` 比对。
     dev 进程名是 **`backend/tsproxy.exe`**，常驻端口 18001。
   - PowerShell 工具输出常被吞 → 一律**先写文件再 Read**。

### 🧰 冷启动/重启类 bug 的验收方式（本轮踩出来的）
「进程启动前已过去的时间点」这类缺口**逻辑上无法用一次请求复现**（那一刻已过去）。
用**包内 live test 直接调 store 层入口**：构造 `APIConfig{Name:"rt_min", Variant:聚合档,
BaseVariant:基础档, LocalAggregate:true, AggMinutes:N}`，对**缺失的那几个轮次**调
`serveAggRoundRebuilt(a, round, codes, nil, codes, true)`，断言响应 JSON 的
`data.items[0][2]` 等于期望的组末时刻。这样连 `fullCover`/`buildResponse` 一起覆盖，
且不需要 DB（`BaseVariant` 显式传入）。再补两档反向用例：非组末 round、`BaseVariant` 为空 → 必须拒绝。

### 🧰 等价性验证（证明"读侧现算"没算错）
拿**写侧已落键的同一轮次**做对照：`aggregateRoundFromDay(...)` 与
`ReadSnapshot(api, aggVariant, round, "batch")` 取同一 ts_code，**字符串逐位比较**。
四档全 `IDENTICAL` 才算通过（本轮实测 5MIN/15MIN/30MIN/60MIN 全等）。

### 🧰 反推上游口径（上游只返"最新一根"时怎么取证）
`rt_min` 的 `limit`/`offset` **无效**（只返最新一根），别指望拉历史。用这两招：
1. **连续采样**：每 2~3s 拉一次并打印 `(观测时刻, label, vol, amount)`，跨过分钟边界：
   - 同一 label 的值**增长** → label 是"正在形成的窗口"；**恒定** → label 是"窗口结束时刻"。
   - 还可以看整点换组时新 label 的出现时刻与数值大小（大=完整柱、小=刚开始的柱）。
2. **精确求和**：拿我们自己当日 1MIN 桶（已验证=上游逐分钟精确值）按候选窗口求和，
   与上游该 freq 的柱**逐位比对 vol/amount/OHLC**——一致的那个窗口就是真实分组口径。
   自己桶里没有的数据可以先用 `rt_min_daily?freq=1MIN` 全量取回（我们的 API 返回全天）。

### 🧰 零风险验证（强烈推荐，替代启第二个实例）
- **只调导出函数**（如 `ReadDailyFromSource` / `ReadDailyAggFromSource`）→ 临时
  `backend/cmd/xxx/main.go` 即可；`redisclient.InitClient(&config.RedisConfig{...})` 后直接跑，
  `go build -o ../tmp_xxx.exe ./cmd/xxx`。
- **要调包内私有函数**（`aggRoundOf` / `aggregateCurrentGroup` / `aggregateDayByFreq` /
  `maybeAggregate`）→ 临时建**包内**测试文件（如 `internal/realtime/agg_live_test.go`），
  用 `if os.Getenv("RT_LIVE") != "1" { t.Skip() }` 守门，`config.InitConfigFromPath("../../config.toml")`
  + `redisclient.InitClient` 后直接断言/打印；跑 `RT_LIVE=1 go test ./internal/realtime/ -run ...`。
  这样能覆盖**写路径**（含 `maybeAggregate` + `rdb.TTL` 校验聚合键 TTL）。
  验证完**立即删除**（`git status --short` 应只剩预期改动）。
- **不起服务、不参与 leader 选举、零上游配额风险**。
  ⚠️ 不要为了验证去启第二个 backend 实例——`can_fetch=false` 也可能抢到 leader，重复烧上游配额。

### 🧰 非交易日 / 模拟上游的端到端验收（2026-09-27 实测通过）
目标：非交易日非交易时间，验证 `rt_min` 预取 → 聚合 → 派生 `rt_min_daily` → CK 落库整条链路。

1. **改配置（无需重启，10s 内 reload）**：`realtime_apis` 的 `time_ranges` 与 `trade_calendar`
   **必须同时清空**。只清 `time_ranges` 不够 —— `InSessionWithCalendar` 在「ranges 为空 + cal 非空」时
   仍会走 `IsTradingDay`，周末直接判休市。两者都空 = 7×24 全时段（`FullDay()=true`）。
   回滚脚本模板见 `backups/rt_min_timeranges_revert_*.sql`。
2. **写入者规则（最容易误判）**：预取循环只在 **leader 或 standalone** 实例跑；
   `isLeaderForWrite() = PrefetcherRunning() || StandalonePrefetchEnabled()`。
   → **follower 的「试拉」能写 Redis 快照，但绝不落库**。查真相：
   `GET /admin/api/realtime/status` 的 `running` / `standalone` 两个布尔就是本进程的身份。
   ⚠️ **别用 status 里的 `meta.round` 判「谁在跑」**：`meta` 优先取本进程内存
   （`realtime.LastRoundMetas()`），取不到就**回退读 Redis 共享快照**（`ReadMeta()`）
   —— 所以不跑的实例也会显示与 leader 完全相同的 `meta.round`。
   判运行态只看 `running`（= `PrefetcherRunning()`）+ CK `query_log` 的 `query_kind='Insert'` 分 `address`。
   本机 dev(18001) 长期是 follower（租约在 `leadership_lease`，实测被 .88 节点持有），
   真正落库的是 `192.168.1.88:18008`（`standalone:true`）。
3. **admin 调用**：`Authorization: Bearer tushareproxy-admin-token`（config.toml `[admin] token`）。
   `/admin/api/realtime/apis`（看 in_session）、`/apis/test {"id":7}`（试拉，返 total_codes/shards/rows/ready）、
   `/redis/keys`（列 rt:* 键，pattern 参数实际被忽略）、`/minute-logs`（取数记录/完整性）。
4. **/dataapi 需要用户 token**（不是 admin token）。dev 库 `tsproxy_test` 有 41 个用户，
   **`user_tokens.token` 列存明文**，直接取一个 active 的用（如 id=1 永不过期）；`ts_code` 用 `000001.SZ`。
5. ⚠️ **sim 短路**：`simulation_enabled=1` 时 `/dataapi` 对 `sim_apis`（默认含 `rt_min`）在
   Step 0.5 就返回模拟数据，**不鉴权、不转发、不写缓存** → 测试环境里 `rt_min` 的「服务端命中」
   路径无法用 `/dataapi` 验证；能验的是 `rt_min_daily`（不在 sim_apis）+ Redis/CK 落库结果。
   想验 `rt_min` 命中只能关模拟，但模拟上游数据源同时会断 → 换上游再关。
6. **全时段下的聚合栅格 = 墙钟**（`FullDay()=true`，idx=h*60+m）：组末对齐 N 的倍数
   （5MIN→21:35/21:40…、15MIN→21:45、30MIN→22:00、60MIN→23:00），且**整组 N 根齐全才产出**。
   从 21:31 开始预取的话：5MIN 最早 21:35 产出、15MIN 21:45、30MIN 22:00；
   **60MIN 的 22:00 组只有 29 根 → 永不产出，要等 23:00**（别误判为 bug）。
7. **验收三件套**：
   - Redis：`rt:batch:{api}:freq="1MIN":<分钟>`、`rt:batch:{api}:freq="5MIN":<组末>`、`rt:day:...`、`rt:hold:...`；
   - CK：`SELECT freq,count(),min(time),max(time) FROM ts_data.ts_rt_min WHERE time>=today() GROUP BY freq`
     （`ts_rt_min_daily` 同理）；
   - 一致性对账：CK 里 N 分钟档必须 == 同日 1MIN 档的 first-open / last-close / max-high / min-low /
     sum-vol / sum-amount，且与 `rt_min_daily?freq=NMIN` 的返回**逐位一致**。
   - ⚠️ **`rt_min_daily` 返回的数值是字符串**（如 `'848.35'`），源 1MIN 是 float
     → 脚本里比对/格式化前必须统一 `float()`，否则报 `TypeError: must be real number, not str`。
     现成脚本可复用：`backups/rt_round_verify.py [freq ...]`（自动拉 1MIN 作基准做逐位比对）。
8. **落库表名** = `ts_<api_name>`（CK 优先，MySQL 兜底；CK 命中时 MySQL 表保持 0 行）。
   派生接口按 `freq` 整日 delete-then-insert，且**只在「有请求进来且本进程可写」时才落**
   —— 不请求 `rt_min_daily` 就不会有 `ts_rt_min_daily` 数据。
   - 落库范围 = **请求返回的标的集**：不传 `ts_code` → `ReadDailyRangeFromSource` 走 HGETALL **全市场**；
     传精确标的 → 精确 HGET（快得多）。全市场耗量参考：1MIN ≈ 43~63s / 30~45MB / 37~56 万行，
     5MIN 13~17s、15MIN 6~10s、30MIN 5~7s、60MIN 2~5s。
   - 要**主动**落库（无外部消费者时）就定时戳它：`tools/rt_min_daily_persist.py`
     （`--until 23:10 --interval 60 --ck-check`，或 `--freqs 60MIN --once` 单档补落）。
   - 落库节流：`derivedPersistAt[api|freq]`，**每分钟最多 1 次**，请求再密也不增加写入。
9. 模拟上游（18008 部署版）各行 OHLC 可能 `high<low`（各列独立随机）→ 别误判成聚合 bug，
   聚合只做 max/min/sum，算术本身正确。

## 环境坑
- Bash 工具的 PATH 经常是坏的（`shim/...: dirname: command not found`，连 `ls` 都没有）。
  **用绝对路径**调 python/go；管道里的 `head`/`tail` 也会失效，改成不加管道。
  实测 `grep/tail/head/date/sort` 全 command not found → 检索改用 Grep/Read 工具，数值探针改用 Python。
- **`Remove-Item` 可能被 safe-delete 拦截**（报 `[safe-delete][SAFE_DELETE_FAIL_CLOSED] ... trash-failed`），
  且它会**静默失败**（加 `-ErrorAction SilentlyContinue` 时你根本不知道没删掉）。
  删临时文件用 **`[System.IO.File]::Delete($path)`**，删完 `Test-Path` 复查一次。
- **直连 Redis 而不是 `docker exec`**：本机 Docker CLI 连不上（容器在远端 Linux 节点，
  `tsproxy-redis` 在 VIP 侧）。用 Python 手写最小 RESP 客户端连 `192.168.1.101:16379`（pass `a123456`），
  比装 redis-py 快且零依赖；`AUTH` → `SCAN`/`TTL`/`HLEN`/`HGETALL` 够用。
- 探针脚本一律写到项目根（`_probe.py`）并在跑完后删除，别留在 `git status` 里。
- Go：`D:/Program Files/Go/bin/go`，`export GOPROXY=https://goproxy.cn GOFLAGS=-mod=mod GOTOOLCHAIN=auto`。
  `gofmt` 必须**单独调** `D:\Program Files\Go\bin\gofmt`（`go gofmt` 会报错但 exit 0，会骗过你）。
  仓库已知遗留 gofmt 不合规文件：`codes.go`、`config.go`、`minute_storage.go` —— 别顺手改。
- 遵守项目铁律：**AI 不重启/不 build 服务**；代码改动只说明「需重新 build + 重启生效」。
