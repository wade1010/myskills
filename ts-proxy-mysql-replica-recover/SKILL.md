---
title: ts_proxy MySQL 从库复制中断修复与「按库忽略」配置
summary: ts_proxy 的 .88/.89 MySQL GTID 主从复制中断（Replica_SQL_Running=No、Last_Error 1032/1062/3030）时的标准修复流程：先取证（worker 级错误 + GTID 落差），再按「一次性错误 → skip 该 GTID」「某个库/表反复闯祸 → REPLICATE_IGNORE_DB 过滤器（my.cnf 持久化 + 运行时动态生效）」「副本先天不全 → 全量重克隆」三档选方案，最后用「主从 gtid_executed 逐字一致 + Seconds_Behind=0 + Last_Error 空」验收。
description: 当 ts_proxy 集群出现「从库 Replica_SQL_Running=No / 从库数据落后主库 / Seconds_Behind_Master 为 NULL / 从库日志报 1032 Can't find record / 1062 Duplicate entry / 3030 前一 worker 已失败 / 复制积压几万事务 / 想让从库忽略某个库（如 dev 库 tsproxy_test）不参与复制 / 想删掉从库上多余的库」时使用。涵盖：worker 级错误定位（performance_schema.replication_applier_status_by_worker）、无下游判定、复制过滤器（replicate-ignore-db）在 my.cnf 与运行时的双写一致性、GTID 模式下 CHANGE REPLICATION FILTER 可动态生效且被过滤事务仍计入 gtid_executed、super_read_only 临时解除后立即恢复、SET SESSION sql_log_bin=0 安全删库、my.cnf 必须原地插入不可整文件覆盖、以及「切勿用 sql_log_bin=0 做初始化副本」等实测坑。
agent_created: true
---

# ts_proxy MySQL 从库复制中断修复与「按库忽略」

拓扑：`.88` = 主库（`tsproxy-mysql` 容器，host 网络，端口 13306，`server-id=1`，VIP `.101` 指向它）；
`.89` = 从库（同名容器，`server-id=2`，`read-only=ON` + `super-read-only=ON`，`report_host=192.168.1.89`）。
两边 my.cnf 挂在 `/data/dockerdata/tsproxy/mysql/conf/my.cnf` → 容器 `/etc/mysql/conf.d/my.cnf`。
GTID 模式（`gtid_mode=ON` + `enforce-gtid-consistency=ON`），binlog-format=ROW。

## 触发场景
- `SHOW REPLICA STATUS` 里 `Replica_SQL_Running: No`、`Seconds_Behind_Source: NULL`、`Last_Error` 非空。
- 报错样例：`1032 Can't find record in '<db>.<table>'`（ROW 模式下 UPDATE/DELETE 找不到行 —— **库两侧数据漂移**）、`3030 ... previous worker encountered an error`、`1062 Duplicate entry`。
- 同一个表/库**反复**打断复制（本项目 `tsproxy_test.leadership_lease` 在 08-24、09-23 各闯一次）。
- 想让从库不再复制某个库（典型：dev 库 `tsproxy_test` 只该存在主库），并把它从从库删掉。

## 第 0 步：取证（只读，先做）
```bash
# 整体状态
ssh root@192.168.1.89 'docker exec tsproxy-mysql mysql -uroot -pa123456 -e "SHOW REPLICA STATUS\G"' \
  | grep -E "Replica_IO_Running|Replica_SQL_Running:|Seconds_Behind|Last_Error:|Replicate_Ignore_DB|Executed_Gtid_Set|Retrieved_Gtid_Set"

# ⚠️ 关键：worker 级具体错误（SHOW REPLICA STATUS 只说"有 worker 失败"，不说哪张表）
ssh root@192.168.1.89 'docker exec tsproxy-mysql mysql -uroot -pa123456 -N -e \
 "SELECT WORKER_ID, LAST_ERROR_NUMBER, LEFT(LAST_ERROR_MESSAGE,300) FROM \
  performance_schema.replication_applier_status_by_worker WHERE LAST_ERROR_MESSAGE<>\"\";"'

# 环境参数（决定能用哪些方案）
ssh root@192.168.1.89 'docker exec tsproxy-mysql mysql -uroot -pa123456 -N -e \
 "SELECT @@gtid_mode, @@server_id, @@read_only, @@super_read_only, @@log_replica_updates, @@replica_parallel_workers; \
  SELECT COUNT(*) FROM information_schema.processlist WHERE command LIKE \"%Dump%\";"'
# ↑ 最后一条 = 有没有下游。返回 0 才敢在从库本地执行 DDL / DROP。

# 存量对比
ssh root@192.168.1.88 'docker exec tsproxy-mysql mysql -uroot -pa123456 -N -e \
 "SELECT table_schema, COUNT(*), ROUND(SUM(data_length+index_length)/1048576,1) mb FROM information_schema.tables \
  WHERE table_schema IN (\"tsproxy\",\"tsproxy_test\") GROUP BY table_schema;"'
```
判读：`Retrieved_Gtid_Set` 上界 − `Executed_Gtid_Set` 上界 ≈ 积压事务数（本项目曾积压 15,647）。

## 方案选择
| 情形 | 方案 |
|---|---|
| 一次性错误（如某条 UPDATE 找不到行），之后不会再犯 | **A. skip 该 GTID**（空事务占位），SQL 线程续跑 |
| 某个库/表**反复**闯祸，且从库根本不需要它 | **B. 复制过滤器 `REPLICATE_IGNORE_DB`**（本项目采用） |
| 报错是**全限定名语句**且无默认库（如 `ALTER TABLE tsproxy_test.xxx`，1049 Unknown database） | **B+. 补 `REPLICATE_WILD_IGNORE_TABLE`**（见下，2026-09-27 实战） |
| 副本先天不全（当初用 `sql_log_bin=0` 建的副本 —— 见末尾坑） | **C. 全量重克隆**（唯一解） |

### B+. `REPLICATE_IGNORE_DB` 的盲区（2026-09-27 实测）
`REPLICATE_IGNORE_DB` 只匹配**默认数据库**（USE 语句选中的库）。后端迁移直接执行
`ALTER TABLE tsproxy_test.realtime_apis MODIFY COLUMN trade_calendar TEXT`（无 USE、全限定名）
时库级过滤器**不生效**，从库上又已 DROP 该库 → 1049 Unknown database，SQL 线程停。
修复：补表级通配过滤器（按全限定表名匹配，不看默认库）：
```sql
STOP REPLICA SQL_THREAD;
CHANGE REPLICATION FILTER REPLICATE_WILD_IGNORE_TABLE = ("tsproxy_test.%");
START REPLICA SQL_THREAD;   -- 卡住的事务会被过滤器跳过，无需手动 skip GTID
```
my.cnf 对应加一行 `replicate-wild-ignore-table = tsproxy_test.%`，并与
`deploy/setup-replica.sh` 的 sed 链同步（已加，两分支）。验收：
`performance_schema.replication_applier_filters` 应有两条（IGNORE_DB + WILD_IGNORE_TABLE）。
⚠️ `mysqld --validate-config` 时 `--defaults-file` 必须是**第一个参数**（`mysqld --defaults-file=... --validate-config`），放后面报 unknown variable。

### A. 跳过单个坏事务（GTID 空事务占位）
```sql
STOP REPLICA SQL_THREAD;
SET GTID_NEXT='6c00d6fa-a2ef-11f1-b0f5-84a93e742c8d:1284185';  -- 换成报错的那个 GTID
BEGIN; COMMIT;                                                   -- 空事务 → 该 GTID 记为已执行
SET GTID_NEXT='AUTOMATIC';
START REPLICA SQL_THREAD;
```

### B. 复制过滤器（推荐用于「脏库反复闯祸」）——**双写，两处必须一致**
`CHANGE REPLICATION FILTER` 是**运行时**设置，重启即丢；只有写进 my.cnf 才熬得过重启。
**两处设成同一个值**，否则重启后行为会变。

1) **持久化（my.cnf，⚠️ 原地插入，绝不整文件覆盖）**：集群 my.cnf 有仓库里没有的遗留改动
（如 `# 2026-08-24 紧急恢复...` 注释），覆盖会丢。做法：`ssh cat` 拉回 → 本地插到 `[mysqld]` 段
（本项目插在 `report_host` 之后）→ `diff -u` 确认只多预期几行 → `cp -a` 备份 →
`scp` 传回 → **`docker exec tsproxy-mysql mysqld --validate-config`（EXIT=0 才算过关）**。
```ini
# [mysqld] 段内
replicate-ignore-db = tsproxy_test
```
2) **运行时（零重启，立即生效）**：
```sql
STOP REPLICA SQL_THREAD;
CHANGE REPLICATION FILTER REPLICATE_IGNORE_DB = (tsproxy_test);
START REPLICA SQL_THREAD;
```
✅ **实测结论：GTID 模式下允许动态设置 `REPLICATE_IGNORE_DB`**（不像部分资料说会报错）。
✅ **被过滤掉的事务，其 GTID 仍会计入从库 `gtid_executed`**（否则 Executed 原地不动、复制永远追不上）。
验收落表：`SELECT FILTER_NAME,FILTER_RULE,CONFIGURED_BY FROM performance_schema.replication_applier_filters;`
应得 `REPLICATE_IGNORE_DB / tsproxy_test / CHANGE_REPLICATION_FILTER`；
`SHOW REPLICA STATUS` 的 `Replicate_Ignore_DB` 也应显示该库名。
注意：`SHOW VARIABLES LIKE 'replicate_ignore_db'` **读不到**（返回空），别拿它判断是否生效。

### 删掉从库上多余的库
前置：① 过滤器已生效并验证；② 无下游（Binlog Dump 计数 = 0）；③ 无连接占用
（`SELECT ... FROM information_schema.processlist WHERE db LIKE 'xxx%'` 为空）。
```sql
SET GLOBAL super_read_only=0;     -- 必须先关 super，再关 read_only（顺序反了会报错）
SET GLOBAL read_only=0;
SET SESSION sql_log_bin=0;        -- 本地 DDL 不写从库 binlog、不污染 gtid_executed、不传播下游
DROP DATABASE tsproxy_test;
SET GLOBAL read_only=1;
SET GLOBAL super_read_only=1;     -- 立刻恢复，缩小可写窗口
```
⚠️ 删完从库没有这个库了，但主库仍然有 → 靠过滤器长期屏蔽主库对它的写入事件；
**过滤器丢了 + 主库又来写入 = 复制重新报错**（这就是 my.cnf 必须写的原因）。

## 验收（缺一不可）
```bash
# 1. 主从 GTID 必须逐字一致
ssh root@192.168.1.88 'docker exec tsproxy-mysql mysql -uroot -pa123456 -N -e "SELECT @@gtid_executed;"'
ssh root@192.168.1.89 'docker exec tsproxy-mysql mysql -uroot -pa123456 -N -e "SELECT @@gtid_executed;"'
# 2. Seconds_Behind_Source=0、Last_Error 空、Replica_SQL_Running=Yes
# 3. 从库库列表 / 表数符合预期；只读已恢复（SELECT @@read_only, @@super_read_only → 1 1）
```
追积压需要时间（本项目 15,647 事务约 7 分钟追平）：`Retrieved_Gtid_Set` 会先涨到主库位置，
`Executed_Gtid_Set` 随后跟上；**不要中途反复 START/STOP**。

## 坑（全部实测）
- ⚠️ **`sql_log_bin=0` 初始化出来的副本永远追不上**；本项目的 `.89` 就是这种历史副本，
  数据可能先天缺行（表现为 ROW 模式 UPDATE 找不到行 → 1032）。这类漂移**不会自愈**，
  要么 skip、要么过滤器屏蔽、要么全量重克隆。
- ⚠️ **my.cnf 不能整文件覆盖**：`.88`/`.89` 的 my.cnf 各有仓库里没有的应急改动，覆盖会丢。
- ⚠️⚠️ **只在集群 my.cnf 里加过滤器是不够的 —— 重建会把它冲掉**：`deploy/setup-replica.sh`
  用**主库模板** `deploy/mysql/my.cnf` 派生从库配置（`sed` 只替 server-id / read-only / super-read-only），
  而主库模板不能带 `replicate-ignore-db`。所以过滤器若不写进脚本的 sed 链，重跑
  `setup-replica.sh`（或按它重建从库）就会丢掉 → 重启后复制再次被同一张表打断。
  已在 2026-09-23 把该行写进 sed 链（`s/^server-id = 2$/server-id = 2\nreplicate-ignore-db = tsproxy_test/`）。
  同理 `report_host` 也曾是该脚本留给**人工**改的（模板写死 `.88`，从库要改成自己）。
  2026-09-23 已一并自动化：按「到主库路由的本机出口 IP」派生
  （`ip route get "$MASTER_IP" | awk` 取 `src`）—— 比 `hostname -I` 可靠，
  后者在 `.89` 上会返回 Tailscale/docker/IPv6 一堆地址，首个字段只是碰巧对。
  实机验证：`.89` 取到 `192.168.1.89`，`.88` 取到 `192.168.1.88`。
  派生后**重跑 `setup-replica.sh` 是幂等的**（派生结果与线上 live 语义 diff 为空）。
- ⚠️ `CHANGE REPLICATION FILTER` 需要 SQL 线程先停；但**过滤器本身不是持久化的**。
- ⚠️ 删库前必须确认**无下游**（`information_schema.processlist` 里的 `Binlog Dump`），
  否则本地 DDL 会波及级联从库（加 `sql_log_bin=0` 可免）。
- ⚠️ `super_read_only=ON` 时 `SET GLOBAL read_only=0` 会直接报错，**先关 super 再关 read_only**；
  恢复时顺序相反（先开 read_only 再开 super）。
- ⚠️ 本机（Windows）SSH 到节点后，容器内 mysql 客户端才可用；`.88` 宿主机自带的是 MariaDB 客户端，
  **一律走 `docker exec tsproxy-mysql mysql ...`**。
- 过滤器只作用于 SQL 线程 apply（relay log 照收照写），所以从库磁盘不会因此省下 relay 空间。

## 配置漂移防护：为什么"服务器上改的东西总在 git 外"

**关键认知**：容器实际挂载的 `/data/dockerdata/tsproxy/mysql/conf/my.cnf`
**不在任何 git 工作区里**（`.88`/`.89`/`.90` 的 git 工作区是 `/root/workspace/ts_proxy`，
挂载文件在 `/data/dockerdata/` 下）。git 里只有**模板** `deploy/mysql/my.cnf`。
所以「服务器上改的配置」天然游离于版本控制 —— 它不是"忘了提交"，而是**从来就不在 git**。

唯一可靠办法：**让「仓库模板 + 派生脚本」成为唯一真相源**，使任何脚本重跑都幂等。

覆盖链（三处都会 `cp 仓库模板 → live`，在从库执行即灾难）：

| 脚本 | 位置 | 用途 |
|---|---|---|
| `deploy.sh` | 第 7 步 | 主节点部署（.88） |
| `deploy-single.sh` | 第 3 步 | 单节点部署 |
| `upgrade-to-ha.sh` | 第 5 步 | 单机升级为 HA 主库 |

2026-09-23 已加**从库护栏**（三处一致）：live my.cnf 含从库专属项
（`replicate-ignore-db` / `read-only = ON` / `super-read-only = ON`）时 `exit 1` 拒绝覆盖，
逃逸口 `FORCE_OVERWRITE_MYSQL_CONF=1`。理由：误覆盖会写回 `server-id=1`（与主库冲突）
+ `read-only=OFF`（解除只读保护）→ 破坏复制拓扑。

**回答"我改的东西在 git 外吗"的标准动作**（三步，全只读）：
```bash
# 1) 服务器 git 工作区是否有未提交改动（=改了跟踪文件但没提交）
for h in 88 89 90; do ssh root@192.168.1.$h 'cd /root/workspace/ts_proxy && git status --short'; done
# 2) 服务器上某时间点后动过哪些文件（找"游离文件"）
ssh root@192.168.1.88 'find /data/dockerdata/tsproxy -newermt "2026-09-23 19:00" -type f | grep -vE "/mysql/data/|\.log$"'
# 3) 语义对比（剥注释/空行后逐行）：live vs 仓库模板
strip(){ grep -vE '^[[:space:]]*#' "$1" | grep -vE '^[[:space:]]*$' | sed 's/[[:space:]]*$//' | sort; }
ssh root@192.168.1.88 'cat /data/dockerdata/tsproxy/mysql/conf/my.cnf' > /tmp/live.cnf
diff <(strip deploy/mysql/my.cnf) <(strip /tmp/live.cnf)   # 主库应只剩注释/格式差异
```
`.89` 的预期差异（都是**正确**的从库差异，非漂移）：
`server-id 2`、`read-only ON`、`super-read-only ON`、`report_host .89`、多一行 `replicate-ignore-db`。
