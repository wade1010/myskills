---
name: ts-data-batch-stuck-diagnose
description: 当 ts_data「批量更新任务一直 running / 进度停在 99% / 某个子任务永不变 / job 更新时间却还是当前」时使用。用 ClickHouse system.query_log 的写库指纹判别 loop 是否空转，顺着 sys_batch_download_jobs → sys_update_tasks → sys_auto_update_runs 三层定位，并检查多实例分工。核心 distinction：「任务在推进」与「调度器在刷库伪装成推进」。
agent_created: true
---

# ts_data 批量更新任务卡死诊断

触发场景（任一）：
- 批量更新列表里 job 状态一直是 running，进度停在 n-1/n
- 子任务列表只剩一个 running，updated_at 是几小时前
- 父任务 updated_at 却一直在刷新到当前时间
- 接口明明跑了半天，智能更新里该接口却被记 failed，或 run 已结束而 job 还在跑

## 核心心智模型（三层状态，一层比一层"上层"）

```
sys_batch_download_jobs   job（父）    status/updated_at/completed/created
   └─ sys_update_tasks    child（子）  status/updated_at   ← 真实推进在这里
sys_auto_update_runs/logs run（上层）  依据 job 的 updated_at 做心跳
```

**上层判定 job 是否活着，只看 `job.updated_at` 有没有推进**。
任何会刷新 updated_at 的底层行为，都会让上层误以为任务健康。

## 诊断五步（只读，别动数据）

### 1. 看 job 与未终态子任务
```sql
SELECT job_id, status, total_tasks, created_tasks, completed_tasks, failed_tasks,
       concurrency, source, run_id, created_at, updated_at
FROM ts_data.sys_batch_download_jobs FINAL WHERE job_id = '<job_id>' FORMAT Vertical;

SELECT id, status, updated_at FROM ts_data.sys_update_tasks FINAL
WHERE startsWith(id, '<job_id>') AND status IN ('running','pending','paused') FORMAT Vertical;
```
判据：`completed+failed ≈ total-1` 且只剩 1 条 `running` 且 `updated_at` 冻结 = 僵尸子任务。

### 2. 🔥 用 system.query_log 判别「真推进」还是「空转刷库」
```sql
SELECT event_time,
       extract(query, 'completed_tasks = ([0-9]+)') AS ct,
       extract(query, 'created_tasks = ([0-9]+)')   AS crt,
       extract(query, 'status = .([a-z]+).')        AS st
FROM system.query_log
WHERE type = 'QueryFinish'
  AND position(query, 'ALTER TABLE sys_batch_download_jobs UPDATE') > 0
  AND position(query, '<interface_name>') > 0
  AND event_time > now() - INTERVAL 10 MINUTE
ORDER BY event_time DESC LIMIT 20 FORMAT TabSeparatedRaw;
```
判据：
- 频率稳定、且 `ct/crt/st` **取值恒定** → 空转刷库，没有子任务在推进（= `_check_job_completion` 被反复调用且无条件写库）
- `ct` 缓慢递增 → 真在推进，只是慢，别误按卡死处理

顺带看写入来源是否多进程（`initial_address` / `client_hostname`）：
```sql
SELECT initial_address, count() FROM system.query_log
WHERE type='QueryFinish' AND position(query,'ALTER TABLE sys_batch_download_jobs UPDATE')>0
  AND event_time > now() - INTERVAL 15 MINUTE GROUP BY initial_address;
```

### 3. 看上层 run 是否还活着
```sql
SELECT * FROM ts_data.sys_auto_update_runs FINAL WHERE run_id = '<run_id>' FORMAT Vertical;
```
注意：`_detect_and_fail_stale_runs` **只扫 status='running' 的 run**。
run 已 completed/failed（等待超时或停滞转后台收尾）后，关联的 job 无人接管 → 孤儿 job 长期偷跑。

### 4. 定位是哪个进程在执行（88 上多实例！）
```
ps -eo pid,etimes,lstart,cmd | grep -E "launcher.py|uvicorn"   # 宿主 9001 + 容器 9002/9003
docker ps --format "{{.Names}}\t{{.Status}}"
```
- ⚠️ 别预设「宿主 9001 才是跑下载的那个」——**三个实例都可能跑下载**，取决于用户请求被
  nginx 分发到哪个实例。定位方法：把 `job.updated_at` 的最后时刻与三份日志
  （`/root/workspace/ts_data/backend/logs/`、容器 `/app/backend/logs/`）的最后一条业务日志
  对齐，**时间戳能对上的那份就是执行实例**。
- ts-data-scheduler / ts-data-scheduler9003 多为 `APSCHEDULER_DISABLED=true` 的 API 副本
  （日志会打「跳过批量任务自愈」，日志在容器内 `/app/backend/logs/YYYY-MM-DD.log`）
- 子任务日志形如 `[BatchDownloadManager] 执行子任务: <iface>|<code>`，**不含 job_id**；
  查 job 相关要用 `[BatchDownloadManager] 任务 <job_id>` 或 HTTP 记录
- 日志中文在 `docker exec sh -c "grep ..."` 里可能显示成 GBK 乱码，能读就行，别为此纠结

### 5. 看完要不要动手（修复方向）
- `_check_job_completion` 脏值比对：只在 status/completed/failed/created 变化时写库
- 子任务看门狗 `_reap_stale_children`：running 子任务 `updated_at` 超
  `batch_child_stale_minutes`（默认 15 分钟；仍在跑的协程放宽 4 倍）→
  cancel 协程 + `_fail_task`（cancelled + error_msg）+ 回算 job
- `_get_next_expand_key` 只认 paused/pending，running 僵尸永远不会被重派 —— 必须靠看门狗
- `task_manager.get_status(force_db=True)` 必须带 FINAL（ReplacingMergeTree 未合并行会读到假 paused）

## 排查时的坑
- 先分清是四套调度里的哪一套（见上表），别拿 batch 的结论套
- 别只看 job.updated_at 判断任务活着：它可能被空转写库刷出来
- 别只看日志里有没有「暂停」字样就认定有人 pause：先确认是全实例日志（9001/9002/9003）
- 修改 `_check_job_completion` 前先确认有没有其它模块依赖它「每次必写」
- 本机跑 tests：必须先 `import app.services` 再 `import app.routers`
  （否则踩 app.services ⇄ app.core.database 循环导入），且本机通常没有 pytest

---

# 附：分钟任务（/#/tasks/minute）卡死 —— 完全另一套

**表与代码都不同，别混用上面的结论。**

| | 批量更新 | 分钟任务 |
|---|---|---|
| 父任务表 | `sys_batch_download_jobs` | `sys_min_download_jobs` |
| 子任务 ID | `{job_id}\|{iface}\|{code}` | `{job_id}_{ts_code}` |
| 代码 | `services/batch_download_manager.py` | `services/minute_downloader.py`（`MinuteDownloadScheduler`） |
| 循环形态 | 持续 while 轮询派发 | 一次性 `asyncio.gather` 全部标的 + semaphore 限流，末尾**只判定一次** |

### 症状
进度停在 99.9x%（n-k/n），状态「下载中」，子任务**却已全部终态**（completed + cancelled = total）。

### 诊断
```sql
SELECT * FROM ts_data.sys_min_download_jobs FINAL WHERE job_id='<id>' FORMAT Vertical;
SELECT status, count() FROM ts_data.sys_update_tasks FINAL
WHERE startsWith(id,'<id>') GROUP BY status;
-- 子任务全终态却仍 running = 计数漂移 + 无兜底结算
```
再看计数写入序列是否单调、有没有回退（`ct` 序列最后值 < 实际成功数 = 漂移）：
```sql
SELECT event_time, extract(query,'completed_targets = ([0-9]+)') ct,
       extract(query,'failed_targets = ([0-9]+)') ft
FROM system.query_log WHERE type='QueryFinish'
  AND position(query,'ALTER TABLE sys_min_download_jobs UPDATE')>0
  AND position(query,'<job_id>')>0
  AND event_time BETWEEN '<t1>' AND '<t2>' ORDER BY event_time DESC LIMIT 20
  FORMAT TabSeparatedRaw;
```
对账「日志里下载成功的标的」与「DB 里 completed 的标的」做差集，能确认是计数漂移还是漏跑：
```
# 日志侧（去重要小心：9002 常并行多个 job，差集非空说明混了别的 job）
grep -oE 'MinuteDownload] [0-9]{6}\.[A-Z]{2} 下载完成' logs | grep -oE '[0-9]{6}\.[A-Z]{2}' | sort -u
```

### 三个已知成因（2026-10-03 stk_mins/9914cddd-4）
1. `_save_job` 用 `execute_mutation_unsynced`，并发提交时滞后值可能最后落库 → DB 计数落后于内存
2. 🔥 `get_job()` 为「跨进程一致」把 DB 值**回写内存计数**，DB 一滞后就抹掉内存已累加的进度
   —— 读接口污染写状态，前端轮询越多越容易被打回
3. `_download_loop` 末尾只有一次 `completed+failed >= total` 判定，**不成立就什么都不做**：
   无重试、无对账、无告警 → 永久 running

### 修复方向（已落地）
- `get_job()` 计数同步改为**只取 max**，绝不回退
- `_reconcile_and_finish()`：以 DB 子任务实际状态（带 FINAL）对账，active==0 时兜底结算
- `_stale_job_watchdog()`：120s 扫描，running 且停滞超 `minute_job_stale_minutes`（默认 30 分钟）
  且本进程无活跃下载循环时触发对账；惰性挂在 `_ensure_job_loaded` 与 `list_jobs`
- semaphore 泄漏：`_download_single`/`_resume_single`/`_handle_main_contract_task` 的提前
  return 不在 try/finally 内。⚠️ 补 release 时先确认调用方有没有放
  （`asyncio.Semaphore.release()` 无上界，双重释放会永久放大并发）

## 相关 skill
- 慢但不是卡死：`ts-data-perf-diagnose`
- 插入报错后不续传：`ts-data-insert-err-diag`
- 未 await 的协程（`'coroutine' object is not iterable`）：`ts-data-async-await-diag`

---

# 附：给后台任务加「停止」按钮 —— 终态被覆盖的三条竞态

需求形态通常是「任务进行中我重启了，然后就没办法操作」。诊断结论是
**僵尸 running 行占住了「进行中禁止重复执行」的闸门**。这时加停止按钮要覆盖两类僵尸：

| 类型 | updated_at | 处理 |
|---|---|---|
| **新鲜僵尸**（重启时差 < STALE 阈值） | 很新 | ⚠️ 闸门**仍被占** =用户真实痛点，必须能手动停 |
| 超龄僵尸 | > stale 阈值（如 300s） | stale 判定自动标 failed 放行，无需人工 |

⚠️ **测「重启场景」必须造新鲜僵尸**（updated_at 设为 10s 前）。
拿超龄僵尸当样本时 `get_active` 会返回 None（已自动回收），会误判成代码有问题。

## 🔥 cancel 必守三条：一个不守就出现「停了又显示进行中」

后台任务取消不是「协程一死就完事」，**收尾路径还会跑**，把已取消的任务改回running：

1. **进度回调复活任务**——`progress()` 里若无条件 `rec["status"] = "running"`，
   cancel 后在途的 `gather` 子任务还会回调几次，把 `cancelled` 改回 `running`。
   前端于是又显示「进行中」，再点停止又放行不了。
   → 开头加 `if rec.get("status") in TERMINAL_STATUSES: return`
2. **迟到终态覆盖**——cancel 后 `gather(..., return_exceptions=True)` 仍return，
   `run_xxx` 会继续往下走到 `finish()`，把 `cancelled` 改成 `done`。
   → `finish()` / `fail()` 开头同样加终态守卫并 return
3. **`except Exception` 抓不到 `CancelledError`**——它继承 `BaseException` 而非 `Exception`。
   好消息是 cancel 不会误标 failed；坏消息是**不能靠它兜住，必须靠上面显式守卫**

## 顺序：先落终态，再 cancel 协程

反过来会有窗口——协程还没死，进度回调又把状态写回 running。

```
内存无 rec → 从 DB 加载        # 覆盖重启场景
已终态 → 返回 already_finished  # 幂等，别报错
落 cancelled 终态（同步写）
task.cancel() + wait_for(shield(task), 5s)   # 超时也放行，不让闸门被卡
无协程（僵尸）→ 只落 DB 终态
```

Semaphore 只要是 `async with` 写的，退出即释放，cancel **不泄漏**；
若是手写 `acquire()/release()` 就要核对释放路径（见上文semaphore 泄漏条目）。

## 测试这类逻辑的两个坑

- **TestClient 自带事件循环**：`create_task` 跑在它那边，在 `asyncio.run` 里
  `await event.wait()` **永远等不到**（脚本输出直接空，还看不出是超时）。
  测后台任务必须在**同一个 loop** 内驱动 manager。
- **打桩状态会跨场景污染**：前一个场景遗留的 running 行会让后一个场景的
  `get_active` 命中更新更晚那条。每个场景开头清理。
  另：`updated_at` 列可能混 `datetime` 与 `str`，`max(key=r[11])` 会 TypeError，
  统一按 `str(r[11])` 比。
