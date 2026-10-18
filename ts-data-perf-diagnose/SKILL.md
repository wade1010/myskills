---
name: ts-data-perf-diagnose
description: 诊断 ts_data「启动多个下载/多任务并发时整个后台很卡」以及「某个接口莫名几百毫秒~几秒才返回」的问题。核心手法：① 用 logs/YYYY-MM-DD.log 的 [HTTP] ➤/◀ 毫秒时间戳按路径配对还原真实接口耗时；② 用 ClickHouse system.query_log 按表归因（has(tables,'ts_data.xxx') + type='QueryFinish'）算出哪张表吃掉了多少 DB 时间；③ 识别 sys_update_tasks 的 ALTER UPDATE mutation 串行队列、async 路由内未包 to_thread 的同步 CH 调用、无缓存的元数据查询这三类根因。触发词：多下载很卡、并发下载卡、接口变慢、响应慢、轮询卡、mutation 堆积、query_log 归因、慢查询定位、ts_data 性能、卡顿分析、慢 SQL、db-status、全表扫描、DISTINCT 很慢、读行数很大、完整性检测慢、快照采集超时、detect 慢。
agent_created: true
---

# ts_data 并发下载卡顿 / 接口慢 诊断

适用：`D:/workspace/ts_data`（FastAPI + ClickHouse）。生产实例在 `.88:/root/workspace/ts_data`，
日志 `backend/logs/YYYY-MM-DD.log`（**485M/天**，别整文件读）。

## 先给结论模板（老板要「先判断、再证据」）

1. **不是 CPU/带宽瓶颈，是单进程单事件循环**：要么被共享控制表 `sys_update_tasks` 的串行
   mutation 拖住，要么被**一段纯 Python 同步代码（pandas / 同步 CH 驱动）直接卡死几十秒**。
2. 量级用 query_log 说：某窗口内对 `sys_update_tasks` 的查询 N 次 = 窗口 X% 时间，其中 ALTER UPDATE n 次（x 次/秒）。
3. 传导路径：async 路由/协程里直接调同步 CH 或 CPU 密集 pandas → 事件循环被占 →
   本该 100ms 的接口要几秒到几十秒。**所有请求一起卡 = 事件循环问题，不是单个接口慢**。
4. 🔥 **用户报的耗时与日志不符时（日志正常但用户很卡），几乎一定是事件循环被卡死** ——
   排队时间不落在中间件日志里。直接跳第 0 步用 py-spy 抓栈，别纠结 slow.py 的数字。

## 🔥🔥 第 0 步（最高优先级）：先排除「事件循环被同步代码卡死」

**2026-10-04 实测教训：用户报「接口 26~44 秒」，但 slow.py 量出来中位 0.046s、max 0.354s，
完全正常。日志骗人。** 原因是 `request_logging.py` 的 ➤/◀ 只包住**路由执行**，
事件循环被 freeze 时新请求**根本没进到中间件**，排队时间不体现在日志里。

**所以：日志时间正常 ≠ 用户不卡。任何「用户很卡但日志正常」的报告，直接跳到第 0 步，别先信 slow.py。**

### 0.1 线程级 CPU —— 一条命令定性

```bash
ssh root@192.168.1.88 "ss -ltnp | grep :9001"                    # 拿 uvicorn pid
ssh root@192.168.1.88 "top -b -n 1 -H -p <pid> | head -20"      # 关键：看 tid == pid 那行
ssh root@192.168.1.88 "cat /proc/loadavg"
```

判读表：

| 现象 | 结论 |
|---|---|
| **主线程（tid==pid）99% CPU + R 状态**，其余线程全 sleeping | 🔥 单线程纯 Python 烧 CPU，**事件循环被卡死**，往下走 0.2 |
| load 很低（104 核只有 2.0）但接口几十秒 | 同上 —— 不是 CPU 不够，是单线程串行 |
| 线程都 idle、CPU 分散 | 才是真的在等 I/O，走第 1/2 步（mutation / CH） |
| 大量线程 R 状态 | 才是真 CPU 饱和 / GIL 争用 |

### 0.2 py-spy 抓栈 —— 直接指到行号

机器**没有 `pip3`**，用 venv 装：

```bash
ssh root@192.168.1.88 "cd /root/workspace/ts_data/backend && ./venv/bin/python -m pip install py-spy"
ssh root@192.168.1.88 "cd /root/workspace/ts_data/backend && ./venv/bin/py-spy dump --pid <pid> | head -40"
```

看 `Thread <pid> (active+gil): "MainThread"` 那一段栈顶。**连采 3 次（间隔 3s）**
—— 偶发卡顿要多次采样确认命中同一处。`active+gil` 意味着持有 GIL 在跑纯 Python。

栈里出现这些关键词的判读：

| 栈顶关键词 | 含义 |
|---|---|
| `pandas` `map_array` `_map_values` `algorithms.py` | 🔥 `.map(lambda)` 逐元素归一化，数据量大会烧几十秒 |
| `clickhouse_driver` `read_items` `_read_data` | 同步 CH 驱动跑在事件循环上（async 里没包 to_thread） |
| `iterrows` / `itertuples` | 逐行遍历 DataFrame |
| `to_thread` / `run_in_executor` | 正常，线程池里干活，不卡循环 |

### 0.3 A/B 对照 —— 甩掉前端/网关

```bash
# 直连后端
ssh root@192.168.1.88 "curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' 'http://127.0.0.1:9001/api/xxx'"
# 经 10001（vite dev server）
ssh root@192.168.1.88 "curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' 'http://127.0.0.1:10001/api/xxx'"
```

**10001 是 vite dev server，不是构建产物** —— 它慢不代表前端有问题，
要是「直连慢、经 vite 快」，瓶颈 100% 在后端，别去查前端构建。

### 0.4 连续探测 —— 量出冻结的形态

一次性 curl 容易撞上间隙，写个循环探测更有说服力：

```bash
ssh root@192.168.1.88 "python3 - <<'EOF'
import urllib.request, time
t_end=time.time()+90
while time.time()<t_end:
    t0=time.time()
    try: urllib.request.urlopen('http://127.0.0.1:9001/api/batch/jobs?page=1&page_size=20',timeout=90).read()
    except Exception as e: print('ERR',e); continue
    dt=time.time()-t0
    print(f'{time.strftime(\"%H:%M:%S\")}  {\"SLOW\" if dt>0.5 else \"ok  \"} {dt:.2f}s')
    time.sleep(1)
EOF"
```

典型输出（2026-10-04 实测）：`SLOW 57.20s` / `ok 58ms` / `SLOW 42.04s`
→ **形态是「整段冻结 + 间隙正常」，不是持续慢**。持续慢才去查 CH/mutation；
间歇性整段冻结 = 某个 CPU 密集的同步段。

## 第 1 步（仅当第 0 步排除事件循环冻结）：从 HTTP 日志还原每个接口的真实耗时

日志格式（`backend/app/middlewares/request_logging.py` 打的）：

```
2026-10-01 01:23:45,678 - INFO - [HTTP] ➤ GET /api/batch/jobs
2026-10-01 01:23:45,679 - INFO - [HTTP]   Query: {}
2026-10-01 01:23:45,722 - INFO - [HTTP] ◀ GET /api/batch/jobs → 200
```

时间戳带**毫秒** → 按 path 配对求差 = 真实响应时间。
直接用 `scripts/slow.py`（本 skill 自带）：

```bash
# 本地
<managed-python> scripts/slow.py "D:/workspace/ts_data/backend/logs/2026-09-27.log"
# 线上（脚本靠 stdin 送过去，避免 scp）
cat scripts/slow.py | ssh root@192.168.1.88 "python3 - /root/workspace/ts_data/backend/logs/2026-10-01.log"
```

输出「median / max / n / endpoint」，按中位耗时降序。重点看**中位就已经是秒级**的只读列表接口——
那说明它在排队等事件循环，不是它自己慢。

只取某个时段（凌晨多下载窗口）：先 awk 切窗口再喂脚本

```bash
ssh root@192.168.1.88 "cd /root/workspace/ts_data/backend/logs && awk '\$2 >= \"00:30:00\" && \$2 <= \"03:30:00\"' 2026-10-01.log > /tmp/win.log"
cat scripts/slow.py | ssh root@192.168.1.88 "python3 - /tmp/win.log"
```

### 第 2 步：用 system.query_log 把 DB 时间归因到表

```bash
ssh root@192.168.1.88 "docker exec clickhouse clickhouse-client --query \"
SELECT normalized_query_hash, count() n, round(sum(query_duration_ms)/1000,1) total_s,
       round(avg(query_duration_ms),1) avg_ms, substring(replaceAll(any(query),'\n',' '),1,80) sample
FROM system.query_log
WHERE event_time >= '<start>' AND event_time < '<end>' AND type='QueryFinish'
  AND has(tables,'ts_data.sys_update_tasks')
GROUP BY normalized_query_hash ORDER BY n DESC LIMIT 15 FORMAT PrettyCompact\""
```

- 换 `has(tables,'ts_data.<目标表>')` 即可换表。
- **别用 `query LIKE 'ALTER%'` 判断**：SQL 文本常带前导空格/换行，用
  `positionCaseInsensitive(query,'ALTER TABLE') > 0`。
- 窗口总占比：再跑一条不带 hash 的 `count(), sum(query_duration_ms)/1000`，除以窗口秒数。

想找高峰：`toStartOfHour(event_time)` 分组。

### 第 3 步：查表本身的形态

```bash
# 行数 / part 数 / 有无重复版本
docker exec clickhouse clickhouse-client --query "SELECT table, sum(rows) rows, count() parts FROM system.parts WHERE active AND database='ts_data' AND table='sys_update_tasks' GROUP BY table"
docker exec clickhouse clickhouse-client --query "SELECT count() raw, uniqExact(id) uniq FROM ts_data.sys_update_tasks"
docker exec clickhouse clickhouse-client --query "SELECT count() FROM ts_data.sys_update_tasks FINAL"
docker exec clickhouse clickhouse-client --query "SHOW CREATE TABLE ts_data.sys_update_tasks"
```

⚠️ 判据：`raw == uniq == FINAL count` → 读侧**不需要**加 FINAL，别去"修"。
`SHOW CREATE` 看有没有 `PARTITION BY` —— 没有的话每次 mutation 重写整表相关 part。

## 🔥 已知根因清单（按实测权重）

| 权重 | 根因 | 位置 |
|---|---|---|
| 🔥🔥最大<br>2026-10-04 | **快照表去重的范围查询只按单个日期字段夹逼，丢掉其他 key 维度 → 拉回近乎全表，再用 `Series.map(lambda)` 逐元素归一化。实测 `stk_rewards`（unique_keys=`ts_code,name,ann_date,title`）单子任务 161.7 万行 × 4 列 → **6.34s 纯 CPU 卡死事件循环**，叠加后全站冻结 42~57s。CH 侧该 SQL 只要 0.117s，时间全在 Python 侧** | `services/incremental_update_core.py:1427-1438`（WHERE 构造）、`:1463`/`:1470`（`.map(lambda)`）、`:1455`（`query_df` 同步驱动也在主线程） |
| 最大 | 状态/进度全走 `ALTER TABLE sys_update_tasks UPDATE`，`mutations_sync=2` 阻塞等 mutation；表无 PARTITION BY → 每次重写整表 part；同表 mutation 串行 | `core/database.py:322 execute_mutation`、`services/task_manager.py:888 update_status`、`:972 _sync_progress_to_db` |
| 最大 | **同一次派发写两次 running**：`_get_next_expand_key` 写一次、`_execute_child_task` 又写一次 | `batch_download_manager.py:682/694` + `:917` |
| 中 | 单接口批量展开路径每个子任务后固定 `sleep(0.5)`、无 pending 时 `sleep(2)` 轮询 | `routers/data.py:644/695` |
| 中 | `PermissionError` 分支 `sleep(3)` 之后仍然 raise（白睡 3s）；频率限制固定 `sleep(10)` | `data_fetch.py:1294` / `:1312` |
| 大 | `update_progress` 每页/每日期段调一次，且每次额外 `get_task` 整行读 2 次 | `services/task_manager.py:908`、`data_fetch.py:1697/1919/2292/2927/2992/3237` |
| 大 | async 路由里直接调同步 CH 服务（未包 `to_thread`）→ 阻塞事件循环 | `routers/scheduled_plan.py`（全文件）、`routers/minute.py:304/313`、`routers/batch.py:209/263/274`、`routers/data.py:936(_apply_auto_end)` |
| 大 | `_get_next_expand_key` 每次派发都 `get_tasks_by_prefix` 扫全部子任务（含两个大 JSON）→ O(N²) | `services/batch_download_manager.py:651/666`；`minute_downloader.py:545/3300/4027` |
| 中 | 每次 insert 打 5~7 次库，元数据查询**无缓存** | `services/dynamic_table.py:573`；`interface_meta.py:220 get_order_by`、`get_enable_dedup`（不走 `_meta_cache`） |
| ~~已修~~ | ~~`SELECT 1` 探活 / `system.tables` / `system.columns` 无缓存~~ **2026-10-01 已加 TTL 缓存**，再看这类计数先确认改动是否已上线：`clickhouse_ping_interval`(30s)、`table_exists_cache_ttl`(60s)、`table_columns_cache_ttl`(60s) | `core/database.py`（`_get_client`/`table_exists`/`_get_table_meta`） |
| 中 | `system_config.get()` 每次调用查一次 CH（167 个调用点） | `services/system_config.py:44` |
| 中 | 每个子任务都新建 TushareClient | `services/tushare_client.py:1066`（注释写明故意不缓存） |
| 中 | `OPTIMIZE TABLE ... FINAL` 全表重写（实测单表 51 分钟），跑到白天就全站卡 | `routers/dedup.py:108`、`dedup_task_manager.py:354`、`routers/data.py:1273`、`interface_meta.py:1192/1932` |
| 小 | 日志同步 FileHandler+StreamHandler，热点路径 logger.info 在事件循环线程阻塞写盘 | `main.py:90-95` |
| 小 | 82 处 `asyncio.to_thread` 共用默认 executor（52 核 → 32 线程），DB 与 tushare HTTP 抢线程 | 未设 `set_default_executor` |
| 大 | **「全表 DISTINCT / GROUP BY 取值集合」退化成全表扫描**：分区是月/季粒度时跨年区间裁剪失效；`minmax` 跳数索引**不服务 DISTINCT** | 见下方「全表扫描专项」 |

## 🔥 全表扫描专项（2026-10-04 慢 SQL 榜定位法）

`/#/operation/db-status` 慢 SQL 榜出现 `SELECT distinct toString(toDate(日期列))` 这类
「取日期集合」的查询，**几乎必然是全表扫**。定位与修复三板斧：

### 1. 先证明它扫了全表（别信有 WHERE）
```sql
-- 慢 SQL 的 read_rows vs 表总行数
SELECT table, sum(rows) FROM system.parts
WHERE database='ts_data' AND table='ts_opt_mins' AND active GROUP BY table;
```
`read_rows == sum(rows)` → **100% 全表**，WHERE 没起任何裁剪作用。
读字节接近 `formatReadableSize(sum(bytes_on_disk))` 也说明数据全解压了。

### 2. 判为什么裁剪失效
- `SELECT engine_full FROM system.tables WHERE table='x'` → 看 `PARTITION BY`：
  - `toYYYYMM(date)` = **月粒度**，跨年区间（如 2015-02~2026-10）= 选中全部月分区 → 裁剪等于没裁
  - `toYYYYMMDD(date)` = 日粒度，区间裁剪才真正生效
- `SELECT name,type,expr,granularity FROM system.data_skipping_indices` → **但 minmax 只服务
  min/max 类查询**，`DISTINCT`/`GROUP BY` 不命中。别指望加索引救 DISTINCT。
- 陷阱：`date_range`/`period` 参数**传了但只用于 Python 侧收窄结果、没下推到 SQL WHERE** →
  period=1m 也扫全历史。**这类「参数没下推」是本项目最常见的隐形慢查询来源**。

### 3. 修复模板（保持语义等价）
- **窗口下推**：`lo = max(分区极值lo, range_min)`、`hi = min(分区极值hi, range_max)`，
  把 period/date_range 变成 SQL WHERE 的上下界。
- **边界别靠 DISTINCT 反推**：DISTINCT 已被窗口收窄，`min(table_dates)` 只是「窗口内最早」，
  用来当全表边界是**语义回归**。改用裸列 `min()/max()`，Python 侧格式化。
- **恒等式换索引**：`toDate(min(c)) == min(toDate(c))`；`toString(min(c))[:10] == min(toString(toDate(c)))`。
  实测 86 亿行表：`min(trade_time)` 5ms、`min(toDate(trade_time))` 1.4s、
  `min(toString(toDate(trade_time)))` **273s**。**包一层函数就退化成全表扫。**
- **类型分派必须按实际 CH 类型**，不能按列名猜（`'time' in col` 之类）：
  DateTime / Date / String 三种形态，String 与 Date 无公共超类型 → 写错直接 **Code 386**。
  用 `get_table_columns_with_types_async` 取真类型，且 `Nullable(...)` 要就地剥掉。

### 4. 验证（改完必做，别只跑单测）
- 等价性：窗口内 DISTINCT 集合 `==` 全表 DISTINCT ∩ 窗口；裸列 min/max 边界 `==` DISTINCT 反推边界
- 性能：`docker exec clickhouse clickhouse-client --time --query "..."` + 查 query_log 新 read_rows
- **回归对照**：`git stash` 跑一遍单测存基线，改完 `diff` 失败项列表。存量红的项直接说「基线就红」，
  别背锅也别忽略。已有失败不因你而增即可。

实测效果（.88 真实库，period=1m 窗口）：
ts_opt_mins 86.6 亿行/16.9s → 1.4 亿行/188ms；ts_stk_mins 51.9 亿/8.0s → 6920 万/123ms。


## 🔥 修复 pandas 去重路径的三个坑（2026-10-04 实测，全部踩过）

症状：`py-spy` 栈顶是 `pandas map_array` / `_normalize_key_value`。修的时候直接踩了三个坑，
每个都会造成**静默的错误去重结果**（该删的没删 → 脏数据重复写入），所以**改这块必须写等价性回归测试**。

### 坑 1：`s.astype(str)` 不等价于逐元素 `str(v)`

| 输入 | `str(v)` | `s.astype(str)` |
|---|---|---|
| `pd.NaT` | `'NaT'` | `nan`（float） |
| `None` | `'None'` | `nan` |
| `np.nan` | `'nan'` | `nan` |
| `Timestamp('2023-01-01')` | `'2023-01-01 00:00:00'` | `'2023-01-01'` |

pandas 把 NaT/None/nan 都当 NA，`astype(str)` 一律给 `nan`。**datetime64 列更会丢时分秒。**

### 坑 2：任何 Python 逐元素循环都会把向量化收益吃光

实测 100 万行：`[f(v) for v in s]` 光循环 **0.38s**，而 `s.map(str)` 只要 **0.182s**。
加了循环后端到端 0.9x = 白改。所以**判缺失也不能用循环** —— 用 `s.isna()` 表达式。

### 坑 3：用 `out.eq('None')` 回填空值会误伤字面字符串

原实现只把 **真** `None` 和 `float('nan')` 当空值；字面字符串 `'None'`/`'nan'` 是普通数据该保留。
写成 `out.eq('None') | out.eq('nan')` 会把字面量也清成 `''` → 去重元组不匹配 → 错删。
**正确做法：`s.isna()` 对字面量返回 False、对真缺失返回 True，天然区分。**

### ✅ 正确写法（就是 `map(str)` + `isna()`）

```python
out = s.map(str)                       # C 层，100 万行 0.182s，NaT/None/nan 语义与 str() 一致
if not s.hasnans:
    return out                         # 干净列走零开销快路径
return out.mask(s.isna() & ~out.eq("NaT"), "")   # NaT 保留成 'NaT'，其余 NA -> ''
```

日期列再叠一层（**原实现只在值里含 `-` 时才处理，不能无条件 `[:8]`**）：

```python
has_dash = out.str.contains("-", regex=False)
out = out.where(~has_dash, out.str.replace("-", "", regex=False).str[:8])
```

⚠️ 判定条件是 `key in DATE_FIELDS`，**不含 `DATETIME_FIELDS`**（`trade_time` 走原样返回，
不去横杠也不截断）。写成 `in DATE_FIELDS or in DATETIME_FIELDS` 会让 `'2023-1-1'` 变 `'202311'`、`'-1'` 变 `'1'`。

回归测试见 `backend/tests/test_incremental_dedup_normalize.py`（随机对拍 10 key × 500 组
+ 10 项 dtype 边界 + 性能断言）。**改这块先跑它。**

## 🔥 ClickHouse 大 IN 子句：用 `has([...], col)` 不是 `col IN (...)`

| 设置 | .88 实测值 |
|---|---|
| `max_query_size` | 262144 |
| `max_ast_elements` | **50000** |

`col IN ('a','b',...)` 的**每个取值各占一个 AST 元素**。stk_rewards 一个分页的 `ts_code`
就 5000+ 取值，多列叠加必触顶报错 —— 而报错被 `_filter_existing_records` 的 `except`
吞成「去重失败, 将插入所有数据」→ **静默失去去重**，比慢严重得多。

`has(['a','b',...], col)` 的数组字面量**只算 1 个 AST 元素**，实测 0.012s 与 IN 完全等价：

```sql
SELECT count() FROM t WHERE ann_date >= '20230101' AND has(['600000.SH','000001.SZ'], ts_code)
```

实测效果（`ts_stk_rewards`，单子任务）：预筛拉回 **242,518 → 89 行**，
去重比对 **0.814s → 0.004s（232x）**，去重结果 `index.equals()` 完全一致。

配套阈值：单值 > `_SQL_MAX_VALUE_LEN`(100) 视为长文本不进 SQL；取值 > `_SQL_MAX_VALUES`(20000)
也不进（数组体积保护）。**被排除的 key 仍进 Python 侧精确比对**——这段 SQL 只是「预筛」，
少条件只会更宽松、不会误删。全部 key 都被排除且无日期字段时必须兜底拼一个，
否则预筛完全失效 = 等于没去重。

## 🔥 「全站一起卡」的第二个根因：async 里裸调同步 CH（2026-10-04 修完）

`clickhouse_client` 全部是**同步驱动**（clickhouse_driver）。在 `async def` 里直接调
`query_df` / `execute` / `execute_mutation` 会**占住事件循环**，本该 100ms 的接口全部排队。

**一眼扫出全量**（比逐个 grep 快，也比 grep 准 —— 能区分同步/异步上下文）：

```bash
cd backend && python check_sync_db_in_async.py $(find app -name "*.py" -not -path "*/tests/*")
# 退出码 0 = 干净
```

首扫 157 处，分布在 19 个文件。**改造后剩 130 处**（多是低频管理接口，改动面大收益低，
按需分批做）。配套自测 `tests/test_check_sync_db_in_async.py`（13 用例）。

### 改法一：整段丢线程池（适合本来就重的函数）

```python
async def _filter_existing_records(...):
    return await asyncio.to_thread(_filter_existing_records_sync, table, df, keys, iface)

def _filter_existing_records_sync(...):   # 原实现整体挪进来，不改一行逻辑
    ...
```

一次同时解决「CH 同步查询」+「pandas CPU」两类阻塞，且**去重/计算语义完全不变**。

### 改法二：换 `*_async` 版本（机械替换，优先用这个）

`core/database.py` 已备好，内部都是纯 `asyncio.to_thread` 包装：

| 同步（会阻塞） | 异步版 |
|---|---|
| `execute` | `execute_async` |
| `execute_mutation` | `execute_mutation_async` |
| `query_df` | `query_df_async` |
| `query_records` | `query_records_async` |
| `execute_with_columns` | `execute_with_columns_async` |
| `get_table_columns_with_types` | `get_table_columns_with_types_async` |
| `get_table_count` | `get_table_count_async` |
| `table_exists` | `table_exists_async` |

⚠️ `execute_mutation` 是 `mutations_sync=2`，**同步等 mutation 完成**，大表可能几十秒 ——
这是最危险的一类，尤其在循环里逐条写。

### 🔥 别手写 `asyncio.to_thread(clickhouse_client.xxx, ...)`（2026-10-04 code review 指出）

能跑，但 `*_async` 就是它的封装，手写属**重复包装 + 风格不统一**，一律换成现成的：

```python
# ❌ 重复包装
rows = await asyncio.to_thread(clickhouse_client.query_records, sql)
# ✅
rows = await clickhouse_client.query_records_async(sql)
```

⚠️ 但这**不是无脑全换**，以下 `to_thread` 是正确写法，别动：
- `to_thread(clickhouse_client.delete_by_keys_precise / select_by_keys_precise / get_table_sorting_key, ...)`
  —— 项目自有方法，**database.py 里没有 async 版**
- `to_thread(task_manager.get_status, ...)`、`to_thread(_filter_existing_records_sync, ...)`
  —— 包的是普通函数 / pandas 逻辑，不是 CH 方法
- Redis 的 `await pipe.execute()` —— `redis.asyncio` 本来就是异步客户端

扫描器已内置这条规则（报「to_thread 手动包装已有 async 版的 CH 方法」）。
⚠️ 写这规则时踩过一个 AST 坑：**`clickhouse_client.query_records`（无参调用）在 AST 里是
`Attribute` 节点而不是 `Call`**，只判 `Call` 会漏报，两种都要认。


### 🔥 三条铁律（都是踩过的）

1. **循环里的同步调用要「收集后一次性提交」**，别一条一条 `await`：
   `minute_downloader._download_concat_tushare` 原本 `for code in batch_codes:` 里逐票
   `execute_mutation` → 改成包成一个 `_mark_completed()` 丢 `to_thread`。
   同理 `completeness_service.init_all_configs` 原本**逐表 `count()`**（几百张表 = 几百次阻塞）
   → 改成一次 `SELECT DISTINCT ... FINAL` 批量查 + 集合判断。

2. **改这类文件必须用 AST 定位，不能用文本 `replace_all`**：
   `update_time_scheduler.py` 里同步 `def` 和 `async def` 有**同名方法**，文本替换命中了同步那个
   → `SyntaxError: 'await' outside async function`。用 AST 按行号替换（排除嵌套 def/async def）。
   出错先 `git checkout <file>` 回滚，别硬改。

3. **扫描器自己也要有自测**：我写的第一版误报了 Redis `pipe.execute()`
   （项目用 `redis.asyncio`，await 是对的）和任意对象的 `.execute()` —— 会把排查方向带偏。
   只认 CH 句柄：`clickhouse_client` / `ch` / `client` / `self.ch`。

### 验证：用心跳协程量，且判据是「心跳次数」

```python
async def _heartbeat(stop, gaps, interval=0.005):
    last = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(interval)
        now = time.perf_counter(); gaps.append(now - last); last = now
```

⚠️ **判据必须是心跳次数，不能是 p95 延迟**。改造前循环被完全占死 → 一次心跳都跳不出来 →
`gaps` 为空 → 算出来 p95 = 0ms，**看着像"最快"其实是最坏**。2026-10-04 实测：
改造前 **0 次心跳** → 改造后 **13 次**（p95 16ms）。完整用例见
`backend/tests/test_filter_existing_not_blocking.py`。

### 存量测试基线对比（改异步代码必做）

改 `async` 语义后跑存量测试可能变红，**先 `git stash` 存基线再对比**：

```bash
python -m unittest discover -s app/services/tests -t . -p "test_*.py" 2>&1 \
  | grep -E "^(FAIL|ERROR):" | sort > /tmp/after.txt
git stash push backend/app/services/ ; <再跑一遍> > /tmp/before.txt ; git stash pop
comm -13 /tmp/before.txt /tmp/after.txt     # 我引入的失败项（应为空）
```

2026-10-04 实测基线 **60 项失败**（多数是 `unittest.loader._FailedTest`，pytest 未装），
改动后仍 60 项、**零差异** → 本次改动未破坏任何存量行为。
⚠️ 本机 `envs/ts_data` **没装 pytest**，只能用 `python -m unittest`；部分测试文件 import pytest 会直接 error。

## 2026-10-01 实测基线（可直接引用，别重新量）

用「日志时间戳配对子任务」脚本量（`[task:<id>] ➤ 增量更新` = 起点，`[TaskManager] 任务 <id> 完成` = 终点）：

- 12,340 个子任务：**p25=1.54s / 中位数 7.12s / p75=8.90s / p95=13.31s / max=541s**，均值 6.94s
- 峰值 106 个/分钟，全天均值 17 个/分钟
- 典型任务内部（pledge_detail 300677.SZ）：派发 `执行子任务` → **静默 9.4s** → 开始执行 → API 0.16s
  → **静默 6.3s** → 完成。⚠️ **这两段静默不能全算在 mutation 头上**（2026-10-01 二次测量后修正，
  见下）；只可以说 mutation 是"可归因部分里最大的一块"
- 限速**不是**瓶颈：生产 `global_rate_limit=800`、`rate_limit_backend=memory`、接口档位 pledge_detail 500/min、
  `Blocked by global/interface limit` 计数 **均为 0**、`waiting for slot` 全天仅 169 行

### 二次测量（2026-10-01 13:00，更可信：按小时分族 + 日志时间线，别再用 3h 聚合值）
繁忙时段取 09/10 点档（10 点档完成 386 个子任务）：

| 族 | 次数/小时 | CH 总耗时 | avg | p95 | max | 摊到每子任务 |
|---|---|---|---|---|---|---|
| `ALTER ... sys_update_tasks UPDATE` | 1,110 | **140.7s** | 127ms | 425ms | 1,110ms | **2.9 次 / ~370ms** |
| `get_tasks_by_prefix`（派活扫描） | 1,014 | 23.1s | 23ms | 34ms | 41ms | 2.6 次 / 60ms |
| `get_task` | 1,114 | 18.7s | 17ms | 23ms | 35ms | 2.9 次 / 48ms |
| `get_status` | 824 | 11.5s | 14ms | 19ms | 27ms | 2.1 次 / 30ms |
| `sys_config` | 8,170 | 24.9s | 3ms | 4ms | 43ms | 21 次 / 65ms |
| `SELECT 1`（ping） | 31,137 | 39s | 1ms | 2ms | 49ms | 81 次 / 100ms |
| `system.columns`+`system.tables` | ~8,800 | 40s | 4~5ms | — | — | 23 次 |

- 单机 104 核、load 1.56、uvicorn 进程瞬时 CPU ≈0（生命周期均值 23% 单核）→ **不是 CPU 瓶颈，是在等 I/O**
- 日志时间线实测（10:35 `01e52548-d|fund_share`）：创建 13.603 → 取数 13.746 → 上游返回 15.425 → 完成 15.480，
  **全程 1.9s**；其中上游 API 占 ~1.6s。→ 子任务耗时里 DB 约 0.5~0.7s，上游 API 才是大头
- 结论修正：**白天时段 mutation ≈ 0.37s/子任务，占 DB 时间的 ~55%、占子任务耗时的 20~35%**，
  不是 9.4s 那种量级。⚠️ 但白天是空闲窗口，**夜间峰值完全是另一个量级，见下**
- sleep 也不是主因：`data_fetch.py:2361` 空片段重试 `sleep(0.5)` 最多 5 次看似很吓人，
  但实测 09-12 点日志里**实际命中 0 次**（`/5 ` 全是 cron 表达式），别背锅

### 每子任务 mutation 来源清单（2026-10-01 逐行核对，改前必看）
1. `_get_next_expand_key:682/694` → `update_status(running)`（仅 DB 里已有 pending/paused 行时）— 必要
2. `_create_child_task:1035` → `update_status(pending)` — **纯浪费**（`create_task` 默认就写 running，这行把它改回 pending）
3. `_execute_child_task:917` → `update_status(running)` — **纯浪费**（与 1 或 2 重复）
4. `_complete_task`/`_fail_task` → 终态同步 mutation + 回读 `get_task` — 必要，可合批
5. `_sync_progress_to_db` → unsynced mutation（`mutations_sync=0`，不阻塞），节流 2s — 可放宽

→ 新建路径 2 次、恢复路径 1 次纯冗余；加上终态合批，可从 ~2.9 次/子任务降到 ~0.2 次

### 🔥 三次测量（2026-10-01 13:20，24h 全窗口）—— 前面"白天基线"全部作废，以本节为准
⚠️ **教训：只量 3h 白天窗口会低估 10 倍。生产是夜间跑批，必须按小时看 24h 曲线再取峰值。**

`sys_update_tasks` mutation 按小时（近 24h，峰值在 00–05 点）：

| 小时 | mutation 次数 | 客户端累计等待 | avg | p95 | max | 子任务数 | **次/子任务** |
|---|---|---|---|---|---|---|---|
| 01:00 | **17,663** | 2,549s | 144ms | 654ms | 1,866ms | 1,011 | **17.5** |
| 00:00 | 17,212 | 2,654s | 154ms | 701ms | 1,572ms | 1,094 | 15.7 |
| 02:00 | 14,733 | 2,098s | 142ms | 661ms | 1,310ms | 878 | 16.8 |
| 03:00 | 9,065 | 1,287s | 142ms | 528ms | 1,213ms | 2,081 | 4.4 |
| 04:00 | 5,304 | 437s | 82ms | 93ms | 187ms | — | — |
| 05:00 | 2,702 | 223s | 82ms | 93ms | 155ms | — | — |
| 10:00（白天） | 1,110 | 141s | 127ms | 425ms | 1,110ms | 386 | 2.9 |

**服务端真实耗时（part_log `event_type='MutatePart'` 的 `duration_ms` 求和，这是最硬的证据）**：

| 小时 | MutatePart 次数 | 服务端耗时 | **占该小时比例** | 重写行数 |
|---|---|---|---|---|
| 01:00 | 107,512 | **1,650s** | **45.8%** | **2.756 亿** |
| 00:00 | 101,160 | 1,571s | 43.6% | 2.169 亿 |
| 02:00 | 90,945 | 1,325s | 36.8% | 2.290 亿 |
| 03:00 | 59,981 | 855s | 23.8% | 0.933 亿 |

→ **峰值小时 CH 有 45.8% 的时间在重写 `sys_update_tasks` 的 part**（表本身仅 24.8 万行 / 9MB / 6 个 part，
41,413 行/part）。重写量 = 表大小的 1,110 倍。这部分直接挤占 ts_* 数据入库、去重、元数据查询。

### ❌ "同表 mutation 串行" 是错的，别再这么说
实测：每分钟 mutation 数从 1 涨到 100+ 时，单次耗时只从 **42ms 涨到 ~110~127ms**（约 2.5 倍，
是 I/O 争用）。严格串行的话 100 次/分 × 42ms = 4.2s 远不到 60s 上限，根本不会排队——所以没有观测到串行排队。
CH 真实语义：mutation 按版本顺序**应用到 part**，不同 part 可并行；不是"整表一次只能跑一个"。

### 🔥 修正后的主次关系：大头是进度写，不是终态写
17.5 次/子任务怎么来的：夜间子任务耗时长（1,011 个/小时 ÷ 并发 ≈ 每个 30~60s），
`_sync_progress_to_db` **每 2s 一次 unsynced mutation** → 单子任务 ~28 次进度写占绝大多数。
→ 收敛重点应从"终态合批"改为"**进度写**"：
1. 进度节流 2s → 10s，或改为按"进度变化 ≥5% 或记录数 +10%"触发（长任务自然变少）
2. 更彻底：进度只更新内存、前端从内存读（`get_job_children` 改走内存），DB 只在**终态写一次**；
   崩溃恢复从 0 重新开始即可（增量幂等）→ 17.5 次/子任务降到 ~1.5 次（降 91%）

## sleep 判定规则（老板必问）

先分类，再下结论，**不要笼统说"sleep 有问题"**：

1. `time.sleep()` 在 `threading.Thread`/APScheduler sync job 里 → **没问题**。
   已确认属于此类：`update_time_scheduler._poll_loop`、`scheduled_plan._cron_loop`、
   `scheduled_task`、`completeness_scheduler`、`backup.py`、`quark_client`。
2. `asyncio.sleep()` 在 async 里 → 不阻塞别人，但看是不是**轮询式**（`while True` + sleep(0.1~0.5)），
   任务多时会变成 N 倍唤醒；轮询里如果还带 `logger.warning`/查库，就是放大器。
3. `while True` 里先查库再 sleep 的 → 查库本身是同步 CH 调用，才是真开销。
4. **要验证是否真触发**：在日志里数 `acquire blocked` / `waiting for slot`。
   实测 0 → 说明限流忙等**不是**当前主因，别背锅。

已知可改进点（非 bug）：`rate_limiter.py:202-229`（0.1s 忙等 + 失败必 log）、
`batch_download_manager.py:774/799/805`、`data_fetch.py:2356`（空片段 sleep(0.5)×5 = 固定 2.5s）、
`data_fetch.py:1655/1880`（`for _ in range(n): await asyncio.sleep(1)` 逐秒写法）。

## 环境注意事项

- 只读巡检即可答完这类问题：`docker exec clickhouse clickhouse-client --query "..."` + `ssh root@192.168.1.88`。
  **不要重启服务、不要动生产数据。**
- 本机 `backend/.env` 的 `CLICKHOUSE_HOST=192.168.1.88` → 本地实例与线上实例共用同一套 `sys_*` 表，
  本地起下载会与线上互挤 mutation 队列（诊断时务必问清是哪套实例）。
- 服务器上还有 rt_ 实时预取服务在同一台 CH 上跑 `ALTER TABLE ts_rt_* DELETE`（每次 ~1.3s，每分钟一次），
  归因时别把它算到 ts_data 头上。
- 日志编码是 UTF-8，本地 Git Bash 下 grep 中文可能显示乱码，用 `python3` 读更稳。
