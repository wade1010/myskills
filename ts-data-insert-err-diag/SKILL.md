---
title: ts_data 写入异常诊断与数值越界自愈 (ClickHouse)
summary: 诊断 ts_data 后端「Tushare 分钟接口写入报 Code 53 / 'q' format / argument out of range 后整段下载中断」以及「上层收到 RuntimeError: No active exception to reraise」这类异常被吞/被覆盖的问题，并说明 insert_df 数值越界自愈链路的正确形态与验证方法。
description: 当 ts_data 后端出现「某批 insert 报 Code 53，之后分页/日期分段下载不再续传」「日志有 insert_df failed 但没有 [DecimalFix]」「最终错误是 No active exception to reraise」「待办通知/示例没出现」时使用。核心排查点：_insert_with_retry 的裸 raise 是否写在 except 块外（会吞掉真实异常）；以及 insert_df 的数值越界 catch 是否真的被命中。
agent_created: true
---

# ts_data 写入异常诊断与数值越界自愈

适用项目：`D:/workspace/ts_data/backend`（FastAPI + ClickHouse，分钟接口 ts_ft_mins / ts_opt_mins / ts_stk_mins 等）。

## 症状 → 病根对照表

| 现象 | 病根 |
|---|---|
| 日志有 `[Database] insert_df failed ... Code: 53 ... 'q' format`，但**没有** `捕获数值越界异常` / `[DecimalFix]` | `insert_df` 的 catch 没命中 —— 大概率是拿到的是被覆盖后的异常 |
| 最终错误 `✗ 更新失败3: No active exception to reraise` | **`_insert_with_retry` 里有裸 `raise` 写在 `except` 块之外**（见下） |
| 满页返回（如 8000 行 = limit）却不再翻下一页 | 异常从 `insert_df` 上抛 → 下载循环 `except: raise` → `_fail_task` → 循环退出 |
| 待办通知里没有"问题数据示例" | catch 没命中，`_notify_decimal_overflow` 没被调用 |

## 🔥 头号坑：裸 `raise` 写在 except 块外，静默吞掉真实异常

`backend/app/core/database.py::ClickHouseClient._insert_with_retry` 首次尝试段：

```python
try:
    self.execute(sql, values)
    return
except Exception as e:
    last_exc = e          # ← except 块到此结束

if not _is_retryable_insert_error(last_exc):
    _log_insert_failure_detail(...)   # 打印 "insert_df failed ..."
    raise                             # ❌ 裸 raise，已在 except 之外！
```

Python 在无活动异常时执行裸 `raise`，抛 `RuntimeError: No active exception to reraise`，**把真正的 `Code 53 / 'q' format` 覆盖掉**。后果：

1. 上层 `insert_df` catch 拿到的 `e` 是这个 RuntimeError；
2. `_is_numeric_overflow_error(e, col_type_list)` 关键字全落空 → False；
3. 走 `else: raise` 上抛 → `更新失败3: No active exception to reraise` → 任务失败 → 循环停。

**正确写法：`raise last_exc`**（跨 except 边界必须显式带上异常对象）。同样地，重试循环内"变为不可重试"那一支的 `raise` 因为**在** except 内所以是安全的；但改动时别把它挪出去。

### 全局扫描脚本（找所有"except 块外裸 raise"）

```python
import ast, pathlib
# 对所有 app/**/*.py：找出 ast.Raise(exc=None)，再用缩进回溯判断最近的
# 外层关键字是 except / else / finally / try；只有 enclosing 不是 except 才可疑。
# 注意：`else: raise`（if 的 else、在 except 块内）是安全的，扫描器会误报，需人工看。
```

实测本轮只有 `database.py` 的该处是"except 外"，其余（含 `insert_df` 里的 `else: raise`）都安全。

## insert_df 数值越界的自愈链路（正确形态）

位置：`backend/app/core/database.py`

- `insert_df` **主流程保持"先插"**：`try: self._exec_insert(...)`；
- `except Exception as e:` → 若 `_is_numeric_overflow_error(e, col_type_list)` 命中：
  - `_fix_numeric_overflow(processed_values, columns, col_type_list)` 把异常值**置 0**：
    - Decimal 列：None / NaN / inf / 超出 `(precision, scale)` 精度
    - Float64 列：None / NaN / inf / `|v| > _FLOAT_OVERFLOW_THRESHOLD(=1e20)`
  - 打 `[NumericFix] row=.. col=.. 原值=.. -> 0 ts_code=.. trade_time=..`
  - **修正后重试一次**；成功 → `_notify_decimal_overflow(table, bad_cells, outcome)`（outcome 由调用方在结果确定后传入，措辞真实），正常返回，循环续传。
  - **两级守卫，绝不静默吞异常**：
    1. `bad_cells` 为空（关键词误判、无坏值可修）→ `raise` 原始异常；
    2. 修正后重试仍失败 → 丢弃异常行、插其余行；**若好行也写不进去（或整批都是坏行）→ `raise`**，绝不静默丢数。
- `_is_numeric_overflow_error` 关键词（收紧后）：`argument out of range` / `code: 53` / `'q' format`，且表含 Decimal 或 Float 列。**不能用裸 `decimal`**——任何消息里提到 decimal 的其它错误都会误入修复路径。

> 关键：`_is_numeric_overflow_error` 依赖异常**消息文本**。一旦上游用裸 `raise` 覆盖了异常，这里必然失配。所以先修裸 `raise`，再看自愈链路。

## 调用链路（ft_mins 走的是"日期分段"循环，不是分页循环）

```
data_fetch.py::_fetch_by_adaptive_date_range_loop   (insert 在 ~2687)
  └─ dynamic_table.ensure_table_and_insert_async
       └─ ensure_table_and_insert → upsert_data   (dynamic_table.py:635)
            └─ self.ch.insert_df(...)             (database.py，唯一插入点)
```

`insert_df` 是唯一插入点，`upsert_data` 没有额外 try 包裹 → 只要 `insert_df` 不抛异常，循环就会 `offset += limit` 继续。

## 验证方法（务必隔离实测）

环境：用 **ts_data venv** 的 python（`/d/ProgramData/anaconda3/envs/ts_data/python.exe`，含 pandas/numpy/clickhouse_driver）。托管 python 没装 numpy，会误报 `NameError`。

隔离测试要点（避免真实连接 / 循环导入）：
1. 预先往 `sys.modules` 塞 stub：`app`、`app.services`、`app.services.incremental_update_config`(提供 `get_interface_config`)、`app.core`、`app.core.config`(提供 `settings`)；
2. 用 `importlib.util.spec_from_file_location` 单独加载 `database.py`；
3. 造 `ClickHouseClient()` 实例，直接覆写实例方法：`execute`（第 1 次抛 `Code 53 / 'q' format`、第 2 次返回）、`get_table_columns_with_types`、`_dedup_upsert`、`_notify_decimal_overflow`（置空）；
4. 断言 A：`_insert_with_retry` 抛的是**原始异常**（消息含 `'q' format`），不是 RuntimeError；
5. 断言 B：`insert_df` 走完链路**不抛异常**，且 execute 被调用 2 次（首插失败 + 重试成功）。

## 运维约定
- 改完后端**需重启生效**（项目无热重载；不替用户重启）。
- 历史脏行（如 `amount=3.3e32` 已被旧代码写进库）不会被自愈链路回改，需单独用 `ALTER TABLE ... UPDATE ...`（mutation，需用户确认）。
