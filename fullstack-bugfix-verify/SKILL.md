---
name: fullstack-bugfix-verify
description: 前后端接口 Bug 的闭环修复流程——复现、定位根因、修复、独立实例实测、写回归测试、重启验证。当用户报告接口报错/返回 500/删除失败/数据不一致，并要求"你自己测试并修复"、"修复并验证"、"帮我调通这个接口"时使用。尤其适用于 FastAPI + ClickHouse + Vue/Element Plus 技术栈。
agent_created: true
---

# 接口 Bug 闭环修复与验证

目标：**不要只改代码就交付**。必须实际跑起来验证，并留下可重复执行的回归测试。

## 流程

### 1. 先读日志，不要先猜

后端日志几乎总能直接给出根因。优先看服务的日志目录（如 `backend/logs/YYYY-MM-DD.log`）。

```bash
tail -n 150 backend/logs/$(date +%F).log
```

重点找 `WARNING` / `ERROR` 行紧邻 HTTP 500 的位置。像
`'str' object has no attribute 'strftime'` 这种，一行就定位到问题。

### 2. 用 curl 复现全部问题

把用户描述的每个症状都变成一条可执行命令，确认都能复现，再动手改。
同时确认"缺失的功能"（如批量删除）返回 404 还是 405——**405 说明被动态路由吞了**。

### 3. 定位根因时优先怀疑"同一个函数被多处调用"

典型模式：`create` / `delete` / `update` 都在末尾调用同一个 `get_xxx()` 做回读，
`get_xxx()` 一崩就表现为"三个接口全挂，但数据其实写进去了"。
症状特征：**接口报错但刷新页面发现操作已生效** → 几乎一定是响应构造阶段抛异常，
而不是写库失败。

### 4. 修复时同步加固

- 写操作成功后的**回读失败不应让整个接口报错**，降级返回本地构造的结果
- 把 `logger.warning(f"...{e}")` 改成 `logger.exception(...)`，下次能直接看到堆栈
- 对外部输入用统一的 `_clean_str` / `_fmt_dt` / `_parse_dt` 工具做类型兼容，
  而不是在每个调用点零散判断

### 5. 起独立端口实例测试，不要动用户正在跑的服务

先查清楚服务用的是哪个解释器（依赖往往只装在特定 venv/conda env 里）：

```powershell
$p = Get-Process -Id <PID>
Set-Content -Path ".\logs\_probe.txt" -Value "PATH=$($p.Path)" -Encoding UTF8
```

> PowerShell 在部分环境下标准输出不回显，写文件再 Read 最稳。
> `wmic` 可能被安全策略禁用，不要依赖它。

然后换个端口起实例：

```bash
cd backend && API_PORT=9099 AUTO_OPEN_BROWSER=false nohup <python路径> launcher.py > /tmp/test.log 2>&1 &
```

轮询就绪，不要盲目 sleep：

```bash
for i in $(seq 1 45); do
  code=$(curl -s -m 3 -o /dev/null -w "%{http_code}" "http://127.0.0.1:9099/<健康检查路径>")
  [ "$code" = "200" ] && { echo "READY after ${i}s"; break; }
  sleep 1
done
```

### 6. 回归测试用 Python，不要用 bash + curl

**Git Bash 下 `curl --data-raw` 传中文 JSON 会编码错乱**，报
`There was an error parsing the body`（这是测试脚本的问题，不是接口的问题，
极易误判）。用 Python + requests，显式 UTF-8 编码：

```python
requests.request(m, url, headers=H,
                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"))
```

测试脚本要求：
- 独立可运行（`python tests/test_xxx_e2e.py [BASE_URL]`），不强依赖 pytest
- 每个历史缺陷都有对应断言，并在注释里写清复现的是哪个 bug
- 用随机 tag 前缀隔离测试数据，结尾 `cleanup()` 兜底清理，**不留脏数据**
- 覆盖边界：空参数、不存在的 id、重复 id、重复删除

### 7. 重启正式服务并复跑

确认服务是否开了热重载（如 `LAUNCH_RELOAD`）。**没开就必须重启**，否则用户
刷新页面还是旧代码，会认为"还是不行"。重启后对生产端口再跑一遍回归测试。

### 8. 前端改动做类型检查

```bash
cd frontend && npx vue-tsc --noEmit --skipLibCheck 2>&1 | grep -E "<改动文件名>"
```

## 技术栈特定陷阱

### ClickHouse

- `ALTER TABLE ... DELETE` 是**异步 mutation**。必须用带 `mutations_sync=2`
  的封装（本项目是 `execute_mutation`），否则"删除成功"后刷新数据还在
- `ReplacingMergeTree` 查询要带 `FINAL` 去重
- 排查时直接用 `clickhouse_driver.Client` 连，避免项目内 `app.core.database`
  的循环导入；查 `system.mutations` 能看到删除是否真的执行过

### 数据访问层类型不一致

同一个 client 的不同方法返回类型可能不同（如 `execute()` 返回 datetime 对象，
而经过 pandas 的 `query_records()` 返回字符串）。**跨方法复用同一个格式化函数前
先确认类型**。

### FastAPI 路由顺序

静态路径必须声明在动态路径之前：

```python
@router.post("/batch-delete")   # ✅ 必须在前
@router.put("/{item_id}")
@router.delete("/{item_id}")
```

否则 `POST /batch-delete` 会匹配到 `{item_id}` 而返回 405。

### Element Plus 表格批量操作

- `<el-table>` 加 `row-key` + `<el-table-column type="selection" reserve-selection />`
- `ElMessageBox.confirm` 的取消会 **reject**，取消和真实错误要分开 try 块，
  否则用户点"取消"会弹出"删除失败"
- 批量删完当前页要回退页码，避免停在空列表

## 沙箱注意

命令因权限升级可能被**执行两次**。写操作（删除、创建）看到"第二次返回 404"
属正常，不要误判为 bug——查数据库或 `system.mutations` 确认真实状态。

## 交付前自检

- [ ] 每个用户报告的症状都有对应的验证通过
- [ ] 回归测试文件已落盘且全绿
- [ ] 测试产生的脏数据已清理
- [ ] 正式服务已重启到新代码
- [ ] 临时文件、探针文件已删除
