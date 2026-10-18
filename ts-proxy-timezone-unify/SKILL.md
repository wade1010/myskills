---
title: ts_proxy 时区审计与统一（东八区三处单点）
summary: ts_proxy 全项目时区已统一为东八区，只在三处固定（my.cnf / internal/tz / utils/datetime）。本 skill 记录「判断某个时间列是 UTC 还是 CST」的可复现方法、存量迁移清单与全部坑，用于排查时间差 8 小时类问题或新增时间列时的回归。
description: 当 ts_proxy 出现「时间差 8 小时 / 显示比实际早或晚 / 前端补 Z 后不对 / 新增时间列不知道写 CST 还是 UTC / 改了 MySQL 时区要评估存量数据」时使用。触发词：时区、UTC、CST、东八区、时间不对、差8小时、Asia/Shanghai、default-time-zone、ON UPDATE CURRENT_TIMESTAMP。涵盖：静态扫描脚本、锚点实测法、三处单点位置、混合口径列的判别、存量迁移清单、'Asia/Shanghai' 不能写进 my.cnf 等坑。
agent_created: true
---

# ts_proxy 时区审计与统一

## 结论先记（现状：已经是东八区）

**全项目统一 CST（东八区），只在这三处固定，别在别处再设：**

| # | 位置 | 内容 |
|---|---|---|
| ① | `deploy/mysql/my.cnf` `[mysqld]` | `default-time-zone = '+08:00'` |
| ② | `backend/internal/tz/tz.go` + `main.go` | `CST = time.FixedZone("CST", 8*3600)`；`main()` **首行** `tz.Load()` |
| ③ | `frontend/src/utils/datetime.ts` | `parseBackendTime / formatDateTime / formatTime / formatRelative` |

历史散落的 6 处 `time.FixedZone("CST", 8*3600)` 已全部改为 `tz.CST` 别名（`realtime/session.go`、`database/realtime_minute_log.go`、`api/realtime_minute_admin.go` 的 `cstkZone`、`api/simulation.go` 的 `simCST`、`api/realtime_admin.go`、`cluster/cluster.go`）。

## 触发场景
- 页面时间比实际早/晚 8 小时；前端 `new Date(s + "Z")` 改完反而更错。
- 新增一张带时间列的表，不确定该沿用哪种口径。
- 要改 MySQL 时区 / 判断存量数据是否要迁移。

## 一、判断「某个时间列是 UTC 还是 CST」

**规律**（改造前 MySQL 服务端=UTC、Go=CST 时）：
- Go 显式传 `time.Now()` → **CST**
- `DEFAULT CURRENT_TIMESTAMP` 且 INSERT **未列**该列 → **UTC**
- `ON UPDATE CURRENT_TIMESTAMP` 且某条 UPDATE **未显式 SET** 该列 → **UTC**（⚠️ 同一列可因此变成混合）
- INSERT 里写 SQL 字面量 `CURRENT_TIMESTAMP` / `NOW()` → **UTC**

### 方法 A：静态扫描（快，覆盖全表）
仓库根 `_tz_scan.py` 交叉分析「建表列属性」×「各 INSERT 是否显式传该列」，输出 UTC / 混合 / CST / 待确认四类：

```bash
cd D:/workspace/ts_proxy && "C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe" _tz_scan.py
```
⚠️ 静态分析有盲点：**只看列名不看值**。INSERT 列清单里有该列、但值是 `CURRENT_TIMESTAMP` 字面量时（如 `interface_metas.updated_at`）仍会误判为 CST。必须配合方法 B。

### 方法 B：锚点实测（准，用于定性）
拿「已知事件时刻」对照：
```bash
# 服务端时区与当前时间（本地 CST 20:07 时，UTC 侧应显示 12:07）
mysql -h192.168.1.100 -P13306 -uroot -pa123456 -N -e "SELECT NOW(), @@global.time_zone, @@system_time_zone;"
```
- **跑迁移/写数据的前后时间对照**：如 `schema_migrations.applied_at = 08:51` 而实际操作在 16:51 → 该列是 UTC。
- **同表两列互斥判定**：`updated_at < created_at` 且逻辑上不可能 → 两列口径不同（`realtime_minute_log` 745 行全部如此）。
- **未来时间法**：值落在 `(UTC_now, CST_now)` 区间内 → 必是 CST（UTC 不可能"未来"）。例：`settings.updated_at = 18:55` 而 UTC 才 12:07 → CST。

## 二、新增时间列时怎么写（避免重蹈覆辙）

- **优先让 MySQL 服务端产时间**（`DEFAULT CURRENT_TIMESTAMP`）——my.cnf 已是 `+08:00`，自动 CST，最省事。
- 若要 Go 显式写，一律 `time.Now()`；**不要**再写 `time.Now().UTC()`。
- `ON UPDATE CURRENT_TIMESTAMP` 的列，若某条 UPDATE 想固定时间，必须**显式 SET 该列**（否则 MySQL 用服务端时间，虽然现在是 CST，但显式更可控）。
- 前端展示一律走 `@/utils/datetime`，**永远不要** `new Date(s + "Z")`。

## 三、存量迁移（改造前写坏的行）

需要 +8h 的列（2026-09-23 扫描，dev 库 `tsproxy_test` 行数）：

| 表 | 列 | 行数 | 成因 |
|---|---|---|---|
| `realtime_minute_log` | `updated_at` | 745 | ON UPDATE 未显式 SET |
| `user_ips` | `updated_at` | 300 | INSERT 用 SQL 字面量 |
| `interface_metas` | `created_at` + `updated_at` | 275 | 未列 / SQL 字面量 |
| `alert_history` | `created_at` | 232 | INSERT 未列 |
| `schema_migrations` | `applied_at` | 61 | INSERT 未列 |
| `realtime_apis` | `created_at` + `updated_at` | 4 | 未列 + ON UPDATE |
| `notification_channels` | `created_at` | 2 | INSERT 未列 |
| `alert_rules` | `created_at` + `updated_at` | 1 | INSERT 未列 |
| `global_policy` | `updated_at` | 1 | INSERT 未列 |
| `upstream_forced_params` | `created_at` + `updated_at` | 1 | INSERT 未列 |

**不需迁移**：`null_value_rules`（实测两列全等=CST）、`leadership_lease.expires_at`（读写都在 SQL 内自洽）、空表 `sys_notifications` / `inapp_messages`。

### 要不要迁移？先核「读取点」再决定

**判据：这些列有没有被读出来参与服务端时间比较？** 只要全是「SELECT 出来给前端展示」，不迁移就**不会产生逻辑错误**，只是历史行显示早 8 小时。

2026-09-23 逐个核过上表全部列的读取点 → **无一处参与 `WHERE`/比较/时间运算**，只有 `ORDER BY id` + 展示。老板据此决定**不迁移**（不介意历史时间）。

核对命令（示例，换表名列名即可）：
```bash
cd D:/workspace/ts_proxy/backend
grep -rn "FROM alert_history\|FROM user_ips\|FROM realtime_minute_log" --include=*.go internal/ | grep -iE "select|where|order by"
```

**真正危险的是 SQL 内比较**——若 UTC 列参与 `WHERE col >= NOW()` 这类比较，改时区后会跳变：
```bash
grep -rn "NOW()\|CURDATE()\|CURRENT_TIMESTAMP" --include=*.go internal/ | grep -v _test | grep -iE "where|>|<|between"
```
本仓库这些点用的都是 **Go 写的 CST 列**（`request_logs.created_at`、`upstream_tokens.expires_at`），所以改时区是**修 bug** 而不是引 bug：`alerts.go` 的 `created_at >= DATE_SUB(NOW(),INTERVAL n SECOND)` 改造前因 CST 列 vs UTC 的 NOW() **条件恒真**（"最近 N 秒错误率"实为全历史）。

⚠️ **「前端去掉补 Z」与「存量迁移」原本配套**：只改 MySQL 不迁移 → 存量 UTC 行少显示 8 小时（老板已知情接受）。

## 四、坑（都踩过）

1. **`default-time-zone` 只能写数字偏移 `'+08:00'`**。写 `'Asia/Shanghai'` 需要 `mysql_tzinfo_to_sql` 导入时区表，官方 mysql 镜像默认没有 → **容器起不来**。
2. **改 my.cnf 不会自动生效**，需重启容器；临时可用 `SET GLOBAL time_zone='+08:00'`（重启失效）。
3. **两条部署路径曾不一致**：`docker-compose.single.yml` 的 mysql 有 `TZ: Asia/Shanghai`，`deploy.sh` 的 `docker run` 没有 → single=CST / HA=UTC。现已都在 my.cnf 层统一。`setup-replica.sh` 用 sed 从主库 my.cnf 派生，从库自动带上。
4. **`time.LoadLocation("Asia/Shanghai")` 不要用**：alpine 等无 tzdata，会静默回退 UTC，问题更隐蔽。用 `time.FixedZone`。
5. **`gofmt -l` 会列出大量文件**——仓库 Go 文件普遍 CRLF，不是你的改动引入的。用未改动文件做对照（如 `internal/database/alerts.go`）即可确认。
6. `internal/tz` 只依赖 `time`，任何包 import 它都不会循环依赖，可放胆用。

## 五、落地执行（改 my.cnf → 重启 → 验证）

集群位置：`.88` 主库（server-id=1）、`.89` 从库（server-id=2），my.cnf 都在 `/data/dockerdata/tsproxy/mysql/conf/my.cnf`，容器 `tsproxy-mysql`（host 网络，端口 13306）。root 免密 SSH。

### ⚠️ 头号坑：**集群 my.cnf 不要整文件覆盖**
集群上的 my.cnf 有仓库里没有的**应急遗留改动**（实测 `.88` 末尾有 `# 2026-08-24 紧急恢复：强制 InnoDB 跳过损坏页检查`，且缺仓库版的 `super-read-only` 注释块）。直接 `scp` 仓库版覆盖 = 抹掉现场改动。

**正确做法：拉回 → 原地插入 → 传回。** 可复用脚本 `D:/workspace/ts_proxy/.tzwork/insert_tz.py`（幂等：已有 `default-time-zone` 则跳过；保留原行尾；在 `[mysqld]` 段的 `server-id` 行后插入时区块）：
```bash
ssh root@192.168.1.88 "cat /data/dockerdata/tsproxy/mysql/conf/my.cnf" > .tzwork/88_orig.cnf
ssh root@192.168.1.89 "cat /data/dockerdata/tsproxy/mysql/conf/my.cnf" > .tzwork/89_orig.cnf
python .tzwork/insert_tz.py          # 生成 88_new.cnf / 89_new.cnf
scp .tzwork/88_new.cnf root@192.168.1.88:/data/dockerdata/tsproxy/mysql/conf/my.cnf
scp .tzwork/89_new.cnf root@192.168.1.89:/data/dockerdata/tsproxy/mysql/conf/my.cnf
```
两台差异（从库专属项）必须保留：`server-id=2 / read-only=ON / super-read-only=ON / report_host=192.168.1.89`。

### 执行顺序（照抄）
```bash
TS=$(date +%Y%m%d_%H%M%S)
# 1) 备份
ssh root@192.168.1.88 "cp -a .../conf/my.cnf .../conf/my.cnf.bak.$TS"
ssh root@192.168.1.89 "cp -a .../conf/my.cnf .../conf/my.cnf.bak.$TS"
# 2) 语法预检（关键！避免 my.cnf 有错导致 MySQL 起不来）
ssh root@192.168.1.88 "docker exec tsproxy-mysql mysqld --validate-config; echo \$?"   # 期望 0
ssh root@192.168.1.89 "docker exec tsproxy-mysql mysqld --validate-config; echo \$?"
# 3) 先从库（不影响生产）→ 验证 → 再主库（生产中断约 11s）
ssh root@192.168.1.89 "docker restart tsproxy-mysql"
ssh root@192.168.1.88 "docker restart tsproxy-mysql"
# 4) 等就绪
ssh root@192.168.1.88 "docker exec tsproxy-mysql mysqladmin -uroot -pa123456 ping"   # 期望 "mysqld is alive"
# 5) 验证
ssh root@192.168.1.88 "docker exec tsproxy-mysql mysql -uroot -pa123456 -N -e \"SELECT @@global.time_zone, NOW();\""
#    期望 +08:00 且 NOW() == 本地时钟
#    实测默认值列：INSERT 不带时间列 → created_at 应等于本地时间
ssh root@192.168.1.88 "docker exec tsproxy-mysql mysql -uroot -pa123456 -N -e \"USE tsproxy_test; INSERT INTO sys_notifications (type,title,content,status) VALUES ('消息','时区验证','x','unread'); SELECT id,created_at FROM sys_notifications ORDER BY id DESC LIMIT 1; DELETE FROM sys_notifications WHERE title='时区验证';\""
# 6) 重启后查从库：IO 会自动重连（Connecting→Yes）
ssh root@192.168.1.89 "docker exec tsproxy-mysql mysql -uroot -pa123456 -e 'SHOW REPLICA STATUS\G' | grep -E 'IO_Running|SQL_Running'"
```

### 配套（Go 代码生效需重启后端）
- 本机 dev 后端：`backend/tsproxy.exe`（无参数，cwd=backend，端口 18001）。**Windows 不能覆盖运行中的 exe** → 先 `go build -o tsproxy.new.exe .` → 停旧 PID → 替换 → `Start-Process -WorkingDirectory backend -WindowStyle Hidden`。
- ⚠️ **编译必须 `GOTOOLCHAIN=auto`**：go.mod 要求 `go >= 1.26.1`，本机是 1.26.0，用 `GOTOOLCHAIN=local` 会直接报错退出。
- 生产 backend（Swarm 3 节点）容器本来就有 `TZ=Asia/Shanghai` → `time.Local`=CST，与库同口径，改库不必同步换镜像。

### 验证「生产容器是否与新口径一致」
```bash
ssh root@192.168.1.88 "C=\$(docker ps --filter name=tsproxy_backend --format '{{.Names}}' | head -1); docker exec \$C sh -c 'echo TZ=\$TZ; date'"
# 期望 TZ=Asia/Shanghai 且 date 显示 CST
```
