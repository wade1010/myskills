---
name: ts-data-completeness-issue-diag
description: 当 ts_data 完整性模块出现「明明只回补了一天，其它日期的告警也变成已解决/ resolved」「刚检测出的 issue 过一会儿自己消失了」「回补跑完说N 天仍缺失、但下轮检测说已达标」「回补完成了但告警状态不对」「issue 数量 / total_issues 虚高」「某接口 daily_rows 快照全是 0/ 分钟缺口不知缺哪一档」「回补会不会写入重复数据 / daily_rows 虚高」「ReplacingMergeTree 好几天也不去重 / count 比 FINAL 多」「缺失检测列表的『去重』按钮灰的 / 想手动做分区 OPTIMIZE」时使用。核心手法：① 用 sys_completeness_issues 的 backfill_job_id 是否为空判断「是不是回补干的」② 用 sys_completeness_ops 拉检测/采集 op 时间线对齐 resolved_at ③ 用当前快照复算 detect_* 验证数据到底恢复没有 ④ 判定入口是否唯一（judge_daily_rows / judge_total_rows）⑤ 重复行按「日→freq→标的」三层下钻定位，merge 是否收敛看 parts 数 vs distinct level 数，判断 OPTIMIZE 有没有用必须拆「同 part 内 / 跨 part」。
agent_created: true
---

# ts_data 完整性 issue 状态异常诊断

触发场景（任一）：
- 只回补了某一天，**其它日期**的 issue 也变成「已解决 / resolved」
- 刚检测出critical，过一段时间自己没了
- 回补跑完了，但 issue 状态跟预期不符
- 检测结果的 `total_issues` / 前端列表条数虚高、有大量重复行
- 告警消失了但数据其实还缺（或反之）

## 0. 先分清「谁干的」——三列定责

```sql
SELECT issue_id, issue_type, affected_date, status,
       backfill_count, backfill_job_id, resolved_at, detected_at,
       actual_count, expected_count, ratio
FROM ts_data.sys_completeness_issues FINAL
WHERE interface_name = '<iface>' ORDER BY affected_date DESC LIMIT 30;
```

| backfill_job_id | backfill_count | 结论 |
|---|---|---|
| 空 | 0 | **不是回补干的** → 是 `_auto_resolve_stale_issues` 或人工改的 |
| 有job_id | >0 | 是回补验证干的（`update_issues_after_backfill`） |

**`backfill_job_id` 为空 = 不是回补干的**。这一条能直接甩掉「为什么我点了回补 A 天，B 天也变了」的误判方向。

## 1. 拉 op 时间线，对齐 resolved_at

```sql
SELECT op_id, op_type, status, params, started_at, finished_at, left(result, 200)
FROM ts_data.sys_completeness_ops FINAL ORDER BY created_at DESC LIMIT 20;
```

- `op_type='gap_detect'` → 检测（`params` 里有 `period` / `interface_name`）
- `op_type='snapshot_collect'` → 快照采集
- **告警是在某轮 detect_all 末尾被 resolve 的，不是采集时**。若 `resolved_at` 落在两次
  detect 之间、或没有任何 op 记录 → 说明那轮检测没被记录（被中断/进程重启/直接调了service）

把 `resolved_at` 和 detect op 的 `finished_at` 对上，就能确认是哪一轮干的。

## 2. 🔥 用当前快照复算，验证数据到底恢复没有

不要相信 issue 的 `status`，直接用 `sys_daily_data_stats` 手算ratio：

```sql
-- 当日实际行数 vs 历史中位数基准
SELECT snapshot_date, daily_rows FROM ts_data.sys_daily_data_stats FINAL
WHERE interface_name = '<iface>' AND snapshot_date < '<YYYY-MM-DD>' AND daily_rows > 0
ORDER BY snapshot_date DESC LIMIT 30;

SELECT daily_rows FROM ts_data.sys_daily_data_stats FINAL
WHERE interface_name = '<iface>' AND snapshot_date = '<YYYY-MM-DD>' LIMIT 1;
```

代码口径在 `completeness_service.detect_incomplete`：
- 基准 = 历史 `daily_rows > 0` 的**中位数**，**样本 < 7 条直接 return []**（早退）
- `ratio = actual / expected`，`ratio >= threshold`（默认 0.85）才算通过
- 「静默早退」（样本不足/基准=0/实际=0）**不产出 issue**，旧逻辑会把这个当成「数据已恢复」

**若复算仍ratio < 阈值而 issue 已 resolved → 确认是误 resolve，往第 3 节查根因。**

## 3. 查 `_auto_resolve_stale_issues` 的两道守卫是否被绕过

```python
# completeness_service.py
async def _auto_resolve_stale_issues(self, ifaces, hit_keys, checked_keys=None):
    ...
    if key in hit_keys: continue
    if checked_keys is not None and not self._issue_date_matched(ad, checked_dates):
        continue          # 守卫①：本轮没检测过这一格 → 保留 open
    if verdicts and itype_s in ("incomplete", "count_drop"):
        if verdicts.get((iface, ad)) != "pass":
            continue      # 守卫②：检测器跳过了这一格 → 保留 open
    ... resolve ...
```

排查要点：
- **`detect_all` 必须在 `try` 之前 `checked_keys.add((iface, snapshot_date))`** ——
  检测抛异常也算「检测过」。挪到 `try` 之后 → 跑挂的格子会被当成「数据已恢复」
- **守卫②只作用于 `incomplete` / `count_drop`**。若给 `missing_dates` / `missing_minutes`
  也加 verdict 门禁，它们会**永久卡在 open**（这两个检测器不写 verdict）
- **`_last_verdicts` 每轮必须 `detect_all` 开头清空** —— 否则上一轮的 pass 会让这轮
  跳过的格子被误 resolve
- **`affected_date` 有三种形态**，直接比字符串必漏，必须用 `_issue_date_matched()`归一：
  `YYYY-MM-DD`（incomplete/count_drop）、`YYYYMMDD`（missing_minutes）、`A~B` 范围串（missing_dates）
- **`backfill_job_id` / `backfill_count` 只有 `update_issues_after_backfill` 会写**。
  `_auto_resolve_stale_issues` 只改 `status` + `resolved_at` —— 看到 job_id 空 +
  resolved_at 有值，就是它干的

## 4. `total_issues` 虚高 / 同日重复

`detect_missing_minutes` 在 ts_code 循环里**每个出缺口的标的各产一条 issue**，
而它们的 `affected_date` 同为一天、`expected/actual` 取同一份 `daily_rows` 快照 → 数值全同。
库里由 `_upsert_issue` 按同键去重（只1 行），但 `detect_all` 返回给前端的 `result.issues`
在修之前没去重 → 前端看到 N 条。

核对：
```sql
SELECT interface_name, affected_date, count() c FROM ts_data.sys_completeness_issues FINAL
WHERE issue_type='missing_minutes' GROUP BY interface_name, affected_date HAVING c > 1;
```
**这里返回 0 行但前端显示多条 → 就是返回层没去重**（不是库里有重复）。

## 5. 「检测说好了、回补说没好」—— 判定口径漂移

**症状**：回补跑完提示「N 天仍缺失」，但下一轮检测同一批日期说「已达标 / 没有 issue」。

**根因**：`verify_backfill` 曾经自己写了一套判定，与 `detect_incomplete` 不一致，差三处：

| # | detect_incomplete 有 | verify_backfill 原来没有 | 后果 |
|---|---|---|---|
| ① | `ft_mins`/`opt_mins` **周六只取周六样本**（`toDayOfWeek=6`，窗口 ×5，门槛 3） | 一律用全样本中位数 | 周六被误判「仍缺失」|
| ② | **长假前阈值放宽 0.5**（距下一交易日 >= 3 天） | 无 | 国庆前最后一天回补完被误判失败 |
| ③ | 当日 0 行 → **skipped**（可能是假期/停牌） | `if actual <= 0: still_missing` | **回补了一个节假日就被报成失败** |

**已修（2026-10-06）**：抽 `judge_daily_rows`(date/time型) / `judge_total_rows`(basic型)
两个判定器，返回 `DailyJudge(verdict=pass|fail|skipped, expected, actual, ratio, threshold, reason)`。
`detect_incomplete` / `detect_count_drop` / `verify_backfill` 三处全部调它们。
`verify_backfill` **只把 `fail` 计入 still_missing**。死方法 `_get_expected_rows` /
`_get_snapshot_daily_rows` 已删。

**排查这类问题先确认判定入口是否唯一**：
```bash
grep -n "judge_daily_rows\|judge_total_rows\|_get_expected_rows\|_get_snapshot_daily_rows" \
  backend/app/services/completeness_service.py
```
出现三个以上判定实现 = 又漂移了。新增判定需求一律加在 `judge_*` 里，别新写。

端到端守卫是单测 `TestVerifyAndDetectAgree`：同一份 mock 数据分别喂
`detect_incomplete` 与 `verify_backfill`，断言 fail/pass/skipped 三种情况结论一致。

## 6. `daily_rows` 为 0 —— 先分清「假期」还是「真缺数据」

**别一看到 0 就当 bug。** 基准查询带 `daily_rows > 0` 过滤，**周末/假期的 0 不污染中位数**。

判定步骤：
```sql
-- 看 0 占比
SELECT interface_name, count() n, countIf(daily_rows > 0) nz
FROM ts_data.sys_daily_data_stats FINAL
WHERE snapshot_date >= today() - 30 GROUP BY interface_name ORDER BY n DESC;
-- 0 占比 100% 的接口，再看表里到底有没有数据
SELECT min(trade_time), max(trade_time) FROM ts_data.ts_<iface>_mins;
SELECT partition, sum(rows) FROM system.parts
WHERE database='ts_data' AND table='ts_<iface>_mins' AND active
GROUP BY partition ORDER BY partition DESC LIMIT 5;
```

判读：
- `max(trade_time)` 停在几个月前 + 最新分区 < 当月 → **真缺数据**（数据侧问题，代码改不了）
- `max(trade_time)=1970-01-01` + 无 active 分区 → **表是空的**
- 部分 0 + 基准查询正常 → 0 落在假期/周末，**正常，别动**
- 🔥 **`sys_daily_data_stats` 只有 30 天快照**，`len(rows) < 7 → skipped`。
  新表 / 长期无数据的表非 0 样本 = 0 → **检测器对它们永久失明**。这是数据问题不是代码问题。

查分钟缺口具体缺哪一档（往往比「整天缺」更常见）：
```sql
SELECT freq, count() FROM ts_data.ts_stk_mins
WHERE trade_time >= '<date> 00:00:00' AND trade_time < '<next_date> 00:00:00'
GROUP BY freq ORDER BY freq;
```
实测 2026-09-24/28/29 缺的是 **`1min` 档**（5061 行 vs 完整日 134 万，其余 4 档正常），
1min 是主力档 → 多 freq 回补选 1min 是对的。

## 7. 「回补会不会写重复」/ `daily_rows` 虚高 —— ReplacingMergeTree 语义

**表结构决定一切**（先查 `engine_full`）：
```
ReplacingMergeTree PARTITION BY toYYYYMM(trade_time) ORDER BY (ts_code, trade_time, freq)
```
无 `ver` → 同键保留**最后一行**；无 `DEDUPLICATE` 引擎参数 → 去重**只发生在后台 merge**。
→ 回补写入是盲插，会产生重复行，但不会丢数据、不会翻倍。
🚨 **但「merge 后自动收敛」是错的**（别这么说，官方文档明确否认）：
> Merging occurs in the background **at an unknown time, so you can't plan for it.
> Some of the data may remain unprocessed.** ... **does not guarantee the absence of duplicates.**

### 🔥🔥「好几天了也不去重」的根因 = part 的 level 两两不同，Simple selector 挑不出组
本库`merge_selector_algorithm = Simple` → **只挑 level 相同的相邻 part 合并**。
若一个分区内各part 的 level 全不同 ⇒ 一个都配不上 ⇒ **后台 merge 对这些大 part 完全空闲**。

```sql
-- 一眼看出「无可合组」：parts 数 == distinct level 数就是没得合
SELECT partition, count() parts, uniqExact(level) lvls, groupArray(level)
FROM system.parts WHERE database=currentDatabase() AND table='<tbl>' AND active
GROUP BY partition ORDER BY partition DESC LIMIT 10;
```
生产实测（ts_stk_mins，2026-10-06）：**12 个分区全部 `parts == distinct_levels`**
```
2026098  [32, 312, 1313, 1280, 353, 58, 48, 1]← 8 个 part 8 个 level
202608    5    5                [30, 286, 455, 308, 25]
全库只有 2 组存在 2+ 同 level part：202007/level=30、200906/level=33
```

**断层是谁打出来的 —— ALTER DELETE mutation**。每次 mutation 重写大 part，
新副本 level 从 1 重新长，于是 level 序列碎成 1/48/58/353/1280…，再也凑不齐：
```sql
SELECT event_time, event_type, part_type, part_name, rows, merge_reason
FROM system.part_log WHERE event_type IN ('MutatePart','RemovePart')
  AND table='<tbl>' AND part_name LIKE '%<大part前缀>%'
ORDER BY event_time DESC LIMIT 15;
-- 实测 ts_stk_mins：10-05 一天跑了 5 次 ALTER DELETE（688617.SH / 000001.SZ），
-- 每次都产出 2679 万行 / 360.93 MiB 的新副本，level 仍是 32
```
⚠️ **同期 merge 其实很忙**，只是全在合 level=1 的新小 part（20~92ms 一个），
大 part 一次都没被碰。**看 `system.merges` 为空不能断定「没在merge」**，
要看 `part_log` 确认 merge 到底在合哪些 part。

### 🔥 写入时是否主动去重，取决于 `api_infos.json` 的 `incr_upsert`（不是 `unique_keys`）
```python
# core/database.py::insert_df
upsert_keys = None if skip_upsert_dedup else (
    interface_config.get('unique_keys') if interface_config.get('incr_upsert') else None)
# _dedup_upsert: if not upsert_keys: return← incr_upsert=None 时不触发 OPTIMIZE
```
生产 `stk_mins` 实测 `incr_upsert=null` → **不做主动去重**。
🚫 **绝对不要给 ts_*_mins 开 `incr_upsert`**：每批 insert 触发一次全表 OPTIMIZE，51.97 亿行上等于自杀。

### 🔥 重复行分布要下钻到「日期 × freq × 标的」，只看分区/日期总数必误判
```sql
-- 第1 层：按日分布（能定位集中在哪天）
SELECT toDate(trade_time) d, count() n,
       uniqExact(tuple(ts_code,trade_time,freq)) u, count()-u dup
FROM ts_stk_mins WHERE toYYYYMM(trade_time)=202609
GROUP BY d HAVING dup>0 ORDER BY dup DESC;

-- 第 2 层：单日按 freq（看是整档重跑还是零散）
SELECT freq, count() c, uniqExact(tuple(ts_code,trade_time,freq)) u
FROM ts_stk_mins WHERE toDate(trade_time)='2026-09-23' GROUP BY freq;

-- 第 3 层：单标的按 freq（看是不是整齐的 100%/50%）
SELECT freq, count() c, uniqExact(tuple(ts_code,trade_time,freq)) u
FROM ts_stk_mins WHERE toDate(trade_time)='2026-09-23' AND ts_code='920247.BJ'
GROUP BY freq;
```
生产实测结论链（同一份数据，三层结论完全不同，别跳级）：
- 分区级：45.25 万重复 → 看似均匀
- 日级：99.93% 集中在 09-23 → 看似「这天整体出问题」
- **freq 级**：`5/15/30/60min` 每档恰好 49.9% → 我据此下了「整档被下两遍」的结论
- 🔥 **标的级（真相）**：`920247.BJ` 的 `1min/15min/30min/60min` 全是 **100% 重复**、
  而 `5min` 是 **0%** → **不是整齐的 50%，是每个标的各有自己受影响的 freq 组合**
判读口诀：`dup/freq ≈ 100%` = 该标的该档整个下两遍；`≈ 50%` = 一半标的被下两遍。
**永远下钻到标的级再下结论。**

**对检测的影响**：`sys_completeness_config.use_final=0` → `_query_daily_rows` 不带 `FINAL`
→ `daily_rows` 把未合并重复算进去。⚠️ **本库 level 断层 + use_final=0 是叠加的**：
merge 不会收敛、查询侧也不去重 ⇒ **下游所有 count() 永远含重复**。
偏差 <1% 时不影响 ratio 判定（不值得重写分区）；但 09-23 的 20% 是真误差，会让当天虚高。
实测 09-23 快照 `daily_rows`=2253408 与库里 `count()` **完全相等** → 快照存的就是含重复的值。

### 🔥 为什么好几天了也不去重（不是「等久一点就好」）

- `merge_selector_algorithm = Simple` → **只挑 level 相同的相邻 part 合并**
- ALTER DELETE mutation 把 level 序列打成两两不同（实测 ts_stk_mins 202609 是
  `1/32/48/58/312/353/1280/1313`）→ Simple selector **一个组都配不上** → 后台 merge 对这些分区完全空闲
- 判据：`count(parts) == uniqExact(level)` = 该分区没得合
- ⚠️ **数字别夸大**：实测 ts_stk_mins 只有**近期 9 个分区**（202601~202609）是level 全不同；
  2000~2006 年的历史分区**全是 1 part**（早就合并干净）。etf/idx/sw/ft/opt/hk 六张表
  **2026 年后一个多 part 分区都没有**
- 官方口径：ReplacingMergeTree「does not guarantee the absence of duplicates」——
  **「merge 会自动收敛」是错的**，别再这么说
- 同期 merge 其实很忙，只是合的都是 level=1 的新小 part（20~92ms 一个），大 part 一次没被碰

### 🔥🔥 判重口径必须 = 动作范围（我踩过的设计错误）
`OPTIMIZE ... PARTITION 'p'` 是**分区级**动作（重排整个分区）。所以
`removable_rows`（「这次能真实消掉多少行」）**必须按整个分区统计**，
绝不能收窄到 `affected_date` 那一天。
现场：点 09-24 的按钮，当天重复 0 → 弹窗报「未发现重复，无需去重」，
但 202609 分区实际有 **452,586** 行重复（集中在 09-23）会被一起清掉。
**「这天没重复」≠「这个分区没重复」；`affected_date` 只该用于定位分区，不该参与判重。**
自查：同一分区用不同日期调 preview，`removable_rows` 必须完全一致，不一致就是又犯了。

### 🔥 「同 part 内重复」实测全库 = 0（别拿它恐吓用户）
机制上必然为0：重复只可能来自「同一份数据 insert 两次」，而 CH 每次 insert 写**独立 part**
→ 同一 key 必然横跨 2 个 part = 跨 part 重复。同 part 内重复只有两种可能：
①单 batch 内 key 重复（CH 写入时已去重）②ALTER DELETE 重写出错（实测无）。
所以 UI 上这列该做**中性 tooltip**，不要写成「需ALTER DELETE，本操作无效」的警告。

### ✅ 已建的分区级去重入口（2026-10-06）
`backend/app/services/dedup_service.py` + `/api/completeness/optimize/{preview,start}`
- 行内「去重」按钮在 `gaps.vue`，走 preview 弹窗 → 异步 op（op_type=`dedup_optimize`）
- **只支持分钟级**（接口名含 `_min`）、**只支持有分区键**的表（`ts_rt_min` 系列无分区，会被拒）
- **自动重采快照不是可选项**：不重采则快照里的 `daily_rows` 一直是含重复的旧值
- ⚠️ 对 CH 测localhost 接口要用 `urllib.request.build_opener(ProxyHandler({}))` ——
  shell 里的代理会把 127.0.0.1 请求也带走（curl 报 HTTP 000 / 502）
- 🚨 **与已有 `/api/dedup/*` 能力重叠**（`routers/dedup.py` + `dedup_task_manager.py`）：
  那边是**整表粒度**（ts_stk_mins 51.97 亿行 → 实测 51 分钟），这边分区粒度（≈20s）。
  排查重复问题时会同时碰到两套，别当成同一功能

### 造等价测试表做 E2E（别在生产表上试写路径）
验 OPTIMIZE 这类写操作时，照真实表建`ts_xxx_test_min`（名字要带 `_min` 否则被
`_is_minute_level` 拒），灌两遍相同行造跨 part 重复，跑完即删：
```python
clickhouse_client.execute(f"CREATE TABLE {TBL} (...) ENGINE = ReplacingMergeTree
    PARTITION BY toYYYYMM(trade_time) ORDER BY (ts_code, trade_time, freq)")
# DateTime 列传 datetime 对象，传 str 报 AttributeError: 'str' object has no attribute 'tzinfo'
```
⚠️ **造数必须保证 (ts_code, trade_time, freq) 三元组唯一**，否则 unique key 数 < 预期行数，
断言会假失败（我在这上面浪费了两轮）。验完 `finally: DROP TABLE`。

## 「检测连续回补」必须按交易日历，不能按自然日

**老实现是 `(nxt - prev).days == 1`（自然日差 1），一遇周末/长假就断。**
现场（stk_mins/incomplete，issues 是 09-24、09-28、09-29 三条）：
09-25 周五但**中秋休市**、09-26/27 周末 → 按交易日历这三天是连续的，
但自然日差 4 天被硬切开 → 点 09-24 只拿到 1 天，必须点 3 次。

**正确做法**：从 target 出发**往前后各走**，相邻交易日在 `issue_dates` 里就纳入区间，
没有才停。这样点区间里任意一天都拿到同一段。
```python
cal = _get_trade_cal_table(iface)   # cn→ts_trade_cal / hk_→ts_hk_tradecal /
                                    # us→ts_us_tradecal / ft_|fut_|opt_→ts_fut_trade_cal
trade_days = SELECT DISTINCT cal_date FROM cal FINAL
             WHERE is_open=1 AND cal_date BETWEEN lo AND hi+30天 ORDER BY cal_date
idx = {d: i for i, d in enumerate(trade_days)}
start = end = idx[target]
while start-1 >= 0 and trade_days[start-1] in issue_dates: start -= 1
while end+1 < len(trade_days) and trade_days[end+1] in issue_dates: end += 1
```

### 🔥 `ts_trade_cal` 一天有 7 行，写SQL 必须 `DISTINCT`
- `sorting_key = (exchange, cal_date)`，**7 个交易所各一行**（SSE/SZSE/CFFEX/DCE/INE/CZCE/SHFE）
- 不去重 → 同一日期在列表里出现 7 次，`date_count` 算成 21 而不是 3（我踩过）
- `ts_fut_trade_cal` 同理（5 个交易所）；`ts_hk_tradecal`/`ts_us_tradecal` 排序键只有 `cal_date`
- ⚠️ `ts_trade_cal.cal_date` 是 **String `'YYYYMMDD'` 格式**，用 `'2026-09-24'` 查会**静默返回空**
  （我踩过，看着像"日历表没数据"）
- 其它模块（`data_fetch` / `freshness_service` / `interface_dependency_manager`）都写了 DISTINCT

### ⚠️ 长假期间 `ts_stk_mins` 大段为空是正常的
2026 中秋+国庆连休：09-25(五)休市、09-30~10-07 全空。**别把这些天当成"数据缺失"**。
判定「连续」一律走交易日历，别用自然日推。

## 探针脚本坑（每次都会踩）

1. **必须用项目 anaconda 环境**：`D:\ProgramData\anaconda3\envs\ts_data\python.exe`（无 pytest，用 `python -m unittest`）
2. **复刻 main.py 导入顺序**否则循环导入。最省事的写法：
   ```python
   import sys; sys.path.insert(0, r"D:/workspace/ts_data/backend")
   from app.services.tushare_client import get_tushare_client  # noqa: F401
   from app.core.database import clickhouse_client   # 直接 import 会炸循环导入
   ```
3. **只读验证要替换写库方法**：`svc._upsert_issue = noop` / mock `execute_mutation_async`，
   别在生产库上试
4. **`execute_async` 的 mock 返回值就是行列表本身**，不是 `[行列表]`。多包一层 →
   `len(rows)` 变 1 → 表现为「历史不足 1/7 天」静默早退
5. **`from app.core.database import clickhouse_client` 是对象引用**。测试里从
   `app.core.database` 重新 import 再赋值改的是另一个名字，patch 静默失效。
   正确：`import app.services.completeness_service as mod` + `patch.object(mod, "clickhouse_client")`
6. **AST 锁代码顺序时别取「第一个 Try」**：`detect_all` 有两个 try（`EXISTS TABLE` 探测 + 内层检测），
   要按 body 是否含 `detect_incomplete` 调用来定位
7. **批量给 `return []` 打标不能靠行号硬编码**（前面编辑会让行号漂移），也不可靠「每组最后一个是
   pass」。用 AST 找每个 `return` 的**父级 `If` 条件**判语义（`ratio >=` → pass，其余 skipped）
8. **ClickHouse 不接受中文别名**（Code 62），探针里的 `AS 出现次数` 要改英文
9. `sys_completeness_issues` **没有 `updated_at` 列**（有 `detected_at` / `resolved_at`）；
   `sys_daily_data_stats` 也没有 `updated_at`（有 `snapshot_time` / `created_at`）—— 写 SQL 前先查列名
10. **别在 `%(param)s` 占位符里套字符串字面量**：
    `WHERE trade_time >= toDateTime('%(d)s 00:00:00')` 会把引号一起替换掉 → Code 62。
    要拼就用 f-string 拼好的完整日期
11. 🔥 **用 Edit 在已有 dataclass 附近插入新类时，`old_string` 必须覆盖到下一个装饰器前的完整块**。
    踩过：只匹配到 `snapshot_time: datetime = ...` 就收尾，结果后面 4 个字段被接到新类里 →
    `SnapshotResult.__init__() got an unexpected keyword argument 'success'`
12. 🔥 **改函数结构优先用脚本按行号整块替换 + 立即 `py_compile`，别用长字符串 Edit**。
    踩过两次：① Edit 把新 `def` 行缩进丢了（顶格）→ `IndentationError: unindent does not match`；
    ② 旧函数头（def + docstring）没删干净，把新函数体吞进旧函数里
13. 🔥🔥 **测 ClickHouse 的读/排序代价，绝不能用 `SELECT count() FROM (... ORDER BY ...)`**。
    `count()` 会走 `optimize_trivial_count_query` 直接读 part 元数据，3388 万行也只要 **0.04s** ——
    这个假数字会让人得出「OPTIMIZE 几乎免费」的错误结论（我踩过一次）。
    正确做法：`SELECT sum(cityHash64(*)) FROM (SELECT * ... ORDER BY ...)` 强制真读（0.50s）。
    要禁优化再加 `SETTINGS optimize_trivial_count_query = 0`（仍然偏快，仅作对照）
14. 🔥 **`system.parts` / `system.part_log` / `system.query_log` 列名**（26.8.2.7，写 SQL 前先查 `system.columns`）：
    - `system.parts`：未压缩体积是 **`data_uncompressed_bytes`**（**没有** `bytes_uncompressed`；
      `bytes_on_disk` 是有的，别写反），**没有 `trade_time` 列**，分区边界是 `min_date`/`max_date`（分区粒度，非行级）
    - `system.part_log`：**没有 `level` 列**（part 层级只能查 `system.parts`），
      用 `duration_ms`（不是 duration）/ `size_in_bytes`（不是 bytes_on_disk）/ `peak_memory_usage`（不是 memory_usage）
    - `system.query_log`：判操作类型用 **`query_kind`** 列（`Select`/`Alter`/`Optimize`/`Insert`），
      比 `query ILIKE '%OPTIMIZE%'` 准（后者会把文本里含该词的普通 SELECT 也算进来）；
      有 `client_name` / `client_hostname` 可定位发送方，**没有 `client_port`**
15. 🔥🔥 **`_part` 虚拟列是判断「重复在同 part 内还是跨 part」的唯一可靠手段**。
    `system.parts` 只有 part 元数据，没有行级分布，**看不出一个 part 里装了什么**。
    ```sql
    SELECT countIf(parts_per_key = 1 AND rows_per_key > 1) AS dup_same_part,
           sumIf(rows_per_key - 1, parts_per_key > 1)     AS dup_across_parts
    FROM (SELECT uniqExact(_part) parts_per_key, count() rows_per_key
          FROM {table} WHERE {范围}
          GROUP BY trade_time, ts_code, freq HAVING rows_per_key > 1)
    ```
    再 `GROUP BY _part` 取 part 名 + 行数 + `min/max(trade_time)` 看真实边界。
    🔥 **别用「part 的 block 区间不重叠」推断「数据不跨 part」——那是错的**：
    大 part 的 min/max 通常横跨整个分区（如 `09-01 09:30 ~ 09-30 15:30`），
    同一 sorting key 可以躺在不同 part 里。
    我据这个错推断得出「OPTIMIZE 碰不到重复、方案无效」，`_part` 实测**同 part 内重复 = 0**、
    跨 part = 452,240，结论完全反过来。**只报「总重复数」不足以判断 OPTIMIZE 有没有用，
    必须拆同 part 内 / 跨 part 两栏。**
16. 🔥 **别拿「N 天的样本」推「N 天里每天都有的规律」**，按天分组统计看是否**仍然成立**。
    例：拿 3 天 query_log 就断言「ts_stk_mins 每天凌晨有全表 FINAL 任务」，
    实际那是 09-14~09-21 期间的慢查（被 commit `7092e9e` 改掉），09-22 之后消失
17. **ClickHouse 没有 `chr()` 函数**，SQL 里要换行清理得用 `replaceAll(query, '\n', ' ')`。
    bash 内联 python 写 SQL 更容易被引号坑到，**复杂探针一律写成 .py 文件再跑**
18. 🔥 **探针里嵌套聚合函数容易报 `CollectAggregateFunctionNodesVisitor`**（Code 47）。
    典型触发：`SELECT count()-uniqExact(x) FROM (SELECT uniqExact(x) u, count() cnt ... GROUP BY k)`。
    改成子查询里 `WHERE dup>0 HAVING` 两段式，或把 `uniqExact` 挪到外层同层SELECT。
19. 🔥 **bash 内联 python 的 `%` 格式化会被 `count()` 之类误解析**：`%d format: a real number is required, not list`
    ——因为 SQL 里的 `%` 被当成格式符。**探针一律写成文件跑，别内联。**
20. 🔥 **探针的列名不确定时先查 `system.columns`，别照记忆猜**（本轮连续踩了 `minDate`、
    把 `min(trade_time)` 写到 `system.parts` 上，都 Code 47）。
    拿不准的探针段**先删掉再跑** —— 别让无关的错把关键判据的输出一起吞掉
    （第一版第 1 段就写错，导致后面 5 段判据全白跑）

## 查历史真实耗时（比估算准）
拿 `query_log` 里的实测值 + 比例外推，别纯估算：

```sql
-- 历史上真跑过的 OPTIMIZE（最直接的答案）
SELECT event_time, query_duration_ms, formatReadableSize(read_bytes),
       formatReadableSize(written_bytes), result_rows,
       substring(replaceAll(query, '\n', ' '), 1, 100)
FROM system.query_log
WHERE type='QueryFinish' AND query_kind='Optimize'
  AND event_time > now() - INTERVAL 180 DAY
ORDER BY event_time DESC LIMIT 20;
```
实测：`OPTIMIZE TABLE ts_stk_mins FINAL`（全表 51.97 亿行/ 69.27 GiB）在 2026-09-30 跑了 **3058.8s**；
202609 单分区占 0.655% → 外推 **≈20s**。
`system.parts` 拿分区占全表比例：`sum(bytes_on_disk)` 分区 / 全表。

⚠️ **别把 `query_log` 的短期样本当常态**（我踩过一次）：拿 3 天窗口的慢查询就断言
「这张表每天凌晨都有全表 FINAL 任务」，实际那批慢查询是
`completeness_scheduler.py`（snapshot/detect/backfill 三个 cron，01:00/02:00/03:00）触发的
`detect_missing_dates` min/max 慢查，**2026-09-14~09-21 期间每天有，09-22 之后消失**
——被 commit `7092e9e`（2026-10-04「perf: 优化完整性检测慢查询」，min/max 改走裸列命中 minmax 跳数索引）改掉了。
**结论：单分区 OPTIMIZE（≈20s）风险等级远低于全表，不需特殊避让窗口。**
判断某个慢查是不是「定时任务」：`system.query_log` 有 `client_name` / `client_hostname` 列可对源，
再用「按天分组看条数」确认是每天都有还是只在某段日期里有。

## 修完要验证什么

- 单测：`python -m unittest app.services.tests.test_completeness_service`
  （存量基线 2 failures：`test_missing_dates_found` / `test_collect_snapshot_date_type`，
  用 `git stash` 对比确认不是本轮引入）
- 复算第 2 节里 ratio仍不达标的那几天，确认会重新产出 issue（告警复活）
- **改判定逻辑后必跑 `TestVerifyAndDetectAgree`** —— 它是两条路径口径一致性的端到端守卫
- 生产只读跑一遍新判定器，确认 verdict 与预期一致（尤其长假/周六的 threshold 是否真的放宽了）
- 探针用完即删
- **改完要提示「需重启后端生效」，不要替老板重启**
- **代码注释里的实测依据要核对当前库**。踩过：注释写「实测 ts_data_test 的 stk_mins
  2024-10-10 有 482 行 = 241 根 × 2」，生产库实测 `count() == uniqExact(trade_time)` 无重复。
  防御代码（`set()` 去重）仍要留（ReplacingMergeTree 未合并 part 会有重复），
  但注释依据要改成当前库的真实情况，不能拿测试库的结论当生产依据
