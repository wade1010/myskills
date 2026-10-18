---
title: ts_data 异步/await缺陷排查（未 await 的协程）
summary: 排查 ts_data 后端「启动或接口报 'coroutine' object is not iterable / 'NoneType' object is not subscriptable / coroutine was never awaited」这类只有运行期才暴露的 async 缺陷。核心是「同步 def 里调 *_async 未 await」——py_compile 与 vue-tsc 完全查不出，必须靠 AST 静态扫 + 行为测试，并给出 DB 访问方法 async 化时的落库三档规范。
description: 当 ts_data 后端出现「'coroutine' object is not iterable」「'NoneType' object is not subscriptable」「coroutine 'ClickHouseClient.execute_async' was never awaited」「RuntimeWarning: coroutine ... was never awaited」，或新加的 DB 访问方法疑似漏 await 时使用。也用于新增任何 clickhouse_client 异步调用前的规范检查。该缺陷已连续复发三轮（2026-09-30 ASGI receive 伪造、2026-10-03 Pydantic 丢字段、2026-10-04 completeness_op reap_stale_on_startup）。
agent_created: true
---

# ts_data 异步/await 缺陷排查

适用项目：`D:/workspace/ts_data/backend`（FastAPI + ClickHouse + Vue3）。

## 核心病根：同步 def 里调异步方法没 await

**这是本项目复发最高频的缺陷类型，且所有静态类型/语法检查都查不出来。**

`clickhouse_client` 同时提供同步（`execute` / `execute_mutation` / `execute_mutation_unsynced`）
与异步（`execute_async` / `execute_mutation_async` / `query_records_async` / `to_thread`）两套方法。
名字只差 `_async` 后缀，**IDE 补全和肉眼都不容易发现混用**。

典型错误写法：

```python
def reap_stale_on_startup(self):          # ← 同步 def
    rows = clickhouse_client.execute_async(...)   # ← 返回 coroutine，没 await
    for r in rows or []:                  # ← TypeError: 'coroutine' object is not iterable
        ...
```

报错组合拳（两条同时出现基本可确诊）：

```
WARNING - [xxx] 启动清理失败: 'coroutine' object is not iterable
RuntimeWarning: coroutine 'ClickHouseClient.execute_async' was never awaited
```

### 连带症状：为什么「看起来」能跑通

未 await 的 coroutine **不会立刻崩**，取决于后续怎么用，两种都很难查：

| 用法 | 现象 | 说明 |
|---|---|---|
| `for x in coro` / `len(coro)` / `coro[0]` | 立刻 `TypeError` | 最幸运，一眼定位 |
| `if not coro: return None` | **静默恒 False**（coroutine 永远 truthy） | 🔥 最坑：函数返回 None，上层以为「查不到」 |
| `if coro: ...` | **静默恒 True** | 🔥 同上，闸门永远认为「有任务在跑」 |

第三、四行是 2026-10-04 那个bug 的实际危害来源：`_load_from_db()` 里`if not rows: return None`
恒不成立，于是拿 `rows[0]` 去下标coroutine，错误现场被推迟到很远的地方。

## 排查步骤

### 1. 先用 AST 静态扫（别肉眼找）

`backend/check_async_mismatch.py` 已内置，直接跑：

```bash
cd /d/workspace/ts_data/backend
"D:/ProgramData/anaconda3/envs/ts_data/python.exe" check_async_mismatch.py $(find app -name "*.py")
```

它查两类：
- **[A]** 同步 `def` 函数体内出现协程调用未await
- **[B]** `for ... in <协程调用>` 或对协程调用结果下标/取长

零缺陷输出：`--- 无 async/await 缺陷 ---`。

**⚠️ 脚本有两处刻意的例外，误报要先核实再改代码**（别把误报当bug 修）：
- `*_unsynced` 后缀是**同步**方法（`execute_mutation_unsynced` 是 `def`），不算协程
- `asyncio.run_coroutine_threadsafe(coro, loop)` / `create_task(coro)` / `gather(...)`
  的**实参里**出现 coroutine 属合法 fire-and-forget

### 2. 读真实调用链，确认要改哪些 await 点

改一个 async 方法会**传染整条链**，必须把每个调用点都找齐：

```bash
# 列出模块内所有 async def / def、以及所有 clickhouse_client 调用
grep -n "^\s*\(async \)\?def \|clickhouse_client\.\|self\._\(load_from_db\|find_active\|mark_stale\)(" app/services/xxx.py
```

调用点常在三处，**最容易漏**：
1. `routers/xxx.py` 的 async 端点 → 补 `await`
2. `main.py` 的 startup 钩子（`async def startup_event`）→ 补 `await`
3. **同步回调/回调工厂**（`def cb(...)`）→ 不能 await，见下面「同步回调」一节

### 3. 行为测试：把 RuntimeWarning 升级为硬失败

语法检查证明不了什么，必须跑。关键手法是把 warning 变异常：

```python
import warnings
warnings.simplefilter("error", RuntimeWarning)   # never awaited → 直接抛
```

配合 fake client（打桩 `clickhouse_client`）覆盖：僵尸回收 / 闸门放行 / 重复执行拦截 /
刷新后从DB 恢复 / 回调签名对齐 / 终态写落库。

### 4. 反向验证测试有效性（必做）

**改完必须把修复临时回退，确认测试会红。** 否则你不知道测试是不是根本没测到东西。

```bash
cp app/services/xxx.py /tmp/fixed.bak   # 备份
# 用脚本把 async def 改回 def、去掉 await，模拟原始 bug
python _tmp_test.py                    # 预期复现原报错
cp /tmp/fixed.bak app/services/xxx.py# 恢复
python _tmp_test.py                    # 预期全绿
```

## 规范：DB 访问方法 async 化后的落库三档

新增/改造时**按语义选档，别一律用同步 execute**：

| 场景 | 用法 | 理由 |
|---|---|---|
| 非终态进度写 | `execute_mutation_unsynced`（mutations_sync=0） | 幂等覆盖，丢一次下周期补；不必等服务端改 part |
| 终态写（完成/失败/标中断） | `execute_mutation_async`（内部 `to_thread` + mutations_sync=2） | 终态必须落库，进程退出不能丢 |
| 读查询 | `execute_async` | — |

**为什么不能图省事全用同步 `execute`**：前端常有 1.5s 轮询的 `get_op` 这类入口，
同步查库会**阻塞整个事件循环**。`completeness_op` 就是这个坑的实例。

`progress()` 这类**由同步回调调用**的方法保持同步 `def` + `execute_mutation_unsynced`
（socket 往返几ms + 2s 节流可接受），**不要**为了「统一」改成 async。

## 写这类测试时自己踩的坑

- 🔥 打桩 `app.core.database` 会触发**循环导入**（core→services→core，ImportError）。
  必须先 `import app.services.<被测模块>` 让 import 顺序自然展开，再替换 `opmod.clickhouse_client`
- Fake client 解析 SQL 更新子句时，`'failed'` 这类**字面量要剥引号**，否则断言恒失败，
  一度误判成被测代码有 bug
- `progress()` 在 `_ops` 里找不到 rec 时**静默 no-op**（设计如此，rec 由 `begin()` 创建）。
  测试要走 `begin() → run_xxx()` 真实链路，不要「先 pop 内存再 run」的假路径

## 关联

- 症状同源但不同因：`if not coro` 恒 False 与 tushare `DataApi.__getattr__` 返回 partial
  导致 `hasattr` 永远 truthy 是**同一种truthiness 陷阱**（见 MEMORY.md「FastAPI/调试」）
- 异步阻塞类问题见 skill `ts-data-perf-diagnose`