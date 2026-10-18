---
title: ts_proxy 新增后台功能模块（DB + API + 前端页面）
summary: 在 ts_proxy 里从零加一个管理后台功能（新表 + Go DB 层 + HTTP 接口 + 路由 + 前端 api/页面/菜单）的固定五步流程与踩过的坑，含迁移双写、时区已统一东八区、函数重名、el-dialog 确认、端到端探针验证。
description: 当需要在 D:/workspace/ts_proxy 里新增一个后台功能模块时使用。触发词：给 ts_proxy 加个 XX 功能/页面/管理项、新建一张表给后台用、把某个界面接上后端并落 MySQL、参考 ts_data 实现一个模块（ts_data 存 ClickHouse，本项目存 MySQL）。涵盖：baseline+vN 迁移双写、database 层函数命名冲突（atoiDefault 等）、/admin/api/* 路由注册（auth/admin 中间件）、前端 api 模块 + 路由菜单注册、时间列统一走 internal/tz 与 @/utils/datetime（东八区，见 skill ts-proxy-timezone-unify）、临时探针端到端验证后删除。
agent_created: true
---

# ts_proxy 新增后台功能模块

## 触发场景
- 「给 ts_proxy 加个通知/消息/XX 管理功能」「把某个只有界面的页面接上后端」「参考 ts_data 的实现搬过来（ts_data 存 ClickHouse，ts_proxy 存 MySQL）」。
- 需要新建表、新接口、新页面。

## 固定五步（照这个顺序做，别跳）

### 1. DB 层：`backend/internal/database/<模块>.go`
- 结构体放本文件或 `models.go`，json tag 用下划线小写（与前端一致）。
- 表结构常量、`normalizeXxx()` 默认值补齐、`buildXxxWhere()` 条件拼装（纯函数，便于单测）。
- ⚠️ 时间列读出来用 `DATE_FORMAT(col,'%Y-%m-%d %H:%i:%s')`，不要裸扫 DATETIME（原因见第 5 节时区）。
- ⚠️ `UPDATE ... SET x=?, y=` + `nowExpr()` 时注意 `?` 顺序（`fmt.Sprintf` 只负责插 NOW()，不占位）。

### 2. 迁移：`backend/internal/database/db.go` **两处都要写**
- `baselineMigrationsMySQL` 数组里加 `CREATE TABLE IF NOT EXISTS ...`（新库开箱即用，内联 `KEY idx_...` 写法有先例 `realtime_minute_log`）。
- 数组尾部 `getVersionedMigrations` 里加下一个版本号 `{version: vN, name: "...", sql: ...}`（存量库补建，`IF NOT EXISTS` 幂等）。**当前最大是 62，下一个用 63。**
- baseline/versioned 里都别用 `CREATE INDEX IF NOT EXISTS`（MySQL 不支持，baseline 会被特判剥离，versioned 会 ReplaceAll）。

### 3. API + 路由
- `backend/internal/api/<模块>.go`：handler 统一 `sendJSONResponse(w, &AdminResponse{Code: 0, Msg: "success", Data: ...})`（**成功码是 0 不是 200**）。路径参数用 `extractID(r.URL.Path, prefix)`。
- ⚠️ **新辅助函数先 grep 重名**：`internal/api` 的测试文件（`integration_test.go`）已占用 `atoiDefault`、`getenvDefault` 等，重名会让 `go vet` 报 `redeclared`（生产代码与 `_test.go` 同包）。本次把新函数命名为 `queryIntDefault`。
- `backend/internal/server/http_server.go` 注册：字面量路径（`/admin/api/x/bell-list`）优先于前缀路径（`/admin/api/x/`），ServeMux 最长匹配，放心写。读接口 `auth(...)`，写接口 `auth(admin(...))`，超管 `auth(superAdmin(...))`。

### 4. 前端
- `frontend/src/api/<模块>.ts`：从 `@/api/admin` 导入 `httpAdmin` / `authHeaders`（**这两个已 export**）。
- 页面 `frontend/src/views/admin/<模块>.vue`，路由/菜单在 `frontend/src/router/modules/admin.ts`：`views` 映射加一行 + 对应 `parentMenu` 里加 `childMenu(path, Name, 标题, "ri/xxx-line", roles)`（@iconify/json 全量，任意 remixicon 名可用）。
- ⚠️ **破坏性写操作统一 el-dialog + `.op-explain` 面板（做什么/完成后看到/注意）+ 底部按钮**，不用 `ElMessageBox.confirm`（参考 `cluster-nodes.vue`；`alerts.vue` 里的 ElMessageBox 是老风格）。
- ⚠️ 时间展示统一走 `@/utils/datetime` 的 `formatDateTime`；**不要再写 `new Date(s.replace(" ","T")+"Z")`** —— 时区已统一东八区，补 Z 会多加 8 小时。

### 5. 验证（缺一不可）
```bash
# 见 skill: ts-local-go-verify —— 只用 Bash 工具 + 绝对路径
"$GO" build ./... ; "$GO" vet ./internal/<pkg>/ ; "$GO" test ./internal/database/ -run 'Xxx' -v
cd frontend && ./node_modules/.bin/vue-tsc --noEmit --skipLibCheck
```
- ⚠️ `vue-tsc` 有 **4 个既有错误**（cluster-nodes 2 / plans 1 / users 1），只要没新增就是干净的。
- **端到端探针**：临时写 `backend/cmd/<x>probe/main.go`，`config.InitConfigFromPath(os.Args[1])` + `database.InitDB(&cfg.Database)` → 跑一遍 CRUD。`config.toml` 指向 dev 库 **tsproxy_test**（探针里 assert `cfg.Database.DBName == "tsproxy_test"` 防误伤生产）。跑完 **`rm -rf backend/cmd/<x>probe`**，别留脏文件（`git status --short` 复核）。

## ⏰ 时间与时区（2026-09-23 起已统一东八区，别再按 UTC 处理）
唯一权威见 **`backend/internal/tz`（Go）+ `deploy/mysql/my.cnf` 的 `default-time-zone='+08:00'` + `frontend/src/utils/datetime.ts`**。完整诊断/迁移见 skill `ts-proxy-timezone-unify`。
- 库里存的就是东八区时间 → `time.Parse` 后**不要再 `.In(time.Local)` 补偿**；用户选的本地日期范围与库**同口径直接比较**（不必再转 UTC）。
- 前端展示统一走 `@/utils/datetime`，**不要补 `Z`**。
- 时间列取出仍建议显式 `DATE_FORMAT(col,'%Y-%m-%d %H:%i:%s')`：DSN 带 `parseTime=true`，裸扫 DATETIME 会产出带 `+08:00` 的 RFC3339 串，破坏前端「裸串」约定。
- 新增时间列：优先用 `DEFAULT CURRENT_TIMESTAMP`（my.cnf 已是 +08:00，自动 CST）；Go 显式写一律 `time.Now()`，不要 `.UTC()`。

## ⚠️ 工作区纪律
- `frontend/src/views/admin/cluster-nodes.vue`、realtime 相关文件经常带着**用户自己的在途改动**，`git status` 里看到 M 不代表是你改的 —— 动前先 `git diff` 确认，别碰。
- 改完只说明「需重启生效」，**不替用户 build / 重启服务 / 跑 pnpm build**。
