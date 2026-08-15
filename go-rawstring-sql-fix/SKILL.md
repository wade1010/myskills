---
title: Go 原始字符串 SQL 拼接修复 (tsproxy)
summary: 诊断并修复 tsproxy Go 后端中 db.Exec/db.Query 用反引号原始字符串拼接 SQL 时出现的"unexpected newline in argument list / unterminated string / rune literal"等编译错误。
description: 当修改或包裹带反引号(raw string)的 SQL 拼接语句（如 db.Exec(`...` + UpsertClause(`...`)），出现 Go 编译错误时，用 cat -A 与反引号位置 dump 定位缺失/多余的反引号、缺失的 + 运算符、或误放到串外的 SQL 括号 )，并立即用系统 Go 重新构建验证。
---

# Go 原始字符串 SQL 拼接修复

tsproxy 后端大量使用 Go 反引号原始字符串拼接 SQL，例如：

```go
db.Exec(`INSERT INTO t (a,b) VALUES (?,?)` +
    UpsertClause(`
    ON CONFLICT(a) DO UPDATE SET b=excluded.b`),
    arg1, arg2)
```

这种写法极易在编辑时把字符串边界搞坏，导致编译错误。

## 触发场景
- 编辑任何含 `` `...` `` 反引号 SQL 的 `.go` 文件后出现编译错误。
- 错误特征：`syntax error: unexpected newline in argument list`、`unterminated string`、`more than one character in rune literal`、`unexpected literal ` ` `。
- 典型背景：给 SQLite 方言 SQL 包一层方言 helper（如 `UpsertClause`），或把 `ON CONFLICT` 包进 helper。

## 关键规则（字符串边界）
1. **INSERT/SELECT 原始字符串**：`db.Exec(` 紧跟反引号（开头），到 `VALUES (?,?,?)` 处的反引号**关闭**字符串，反引号**之后**是 `+` 运算符。
   - SQL 自己的 `)`（如 `VALUES (...)` 的收尾括号）必须放在反引号**里面**（即写在反引号前），不能写到反引号外面（否则会被当成 Go 语法吃掉）。
2. **helper 调用**：`UpsertClause(` 紧跟反引号（开头），SET 子句结尾处反引号关闭原始字符串，**然后 `)` 关闭 helper 调用**，再 `,` 分隔实参。
3. **绝不能**让任何被包裹的 SQL 反引号原始串以 `,` 或不带 `)` 结尾——必须是 `...`),` 或 `...`)` 形态。

## 诊断步骤
1. **先用系统 Go 构建**（`/d/Program Files/golang/Go/bin/go build ./...`）——本项目没有托管版 Go，用系统 Go。
2. 若报"unexpected newline in argument list / rune literal"，用 `cat -A <file> | sed -n 'Lp'` 看反引号（原样显示）和制表符（`^I`）。
3. 用 Python dump 每行的反引号位置，找失衡处：
   ```python
   bt = chr(96)
   for i, ln in enumerate(open(f, encoding="utf-8").read().split("\n"), 1):
       pos = [j for j, c in enumerate(ln) if c == bt]
       if pos: print(i, pos, ln[:50])
   ```
   - 反引号成对：开/闭；若某行 `)` 在反引号**之后**，说明 SQL 括号跑到了串外。
   - 若某 `helper(` 后缺反引号，或结尾只有 `,` 没 `)` 关闭 helper，就会一直吞到几十行后报错。
4. 定位后按"关键规则"修正：补齐/挪动反引号、`+` 运算符、或把 `)` 塞回串内（反引号前）。

## 易错清单（逐项核对）
- [ ] `db.Exec(` / `db.Query(` 后第一个反引号（字符串开头）存在。
- [ ] INSERT/SELECT 串在 `)` `+` 处用反引号关闭，且 SQL 的 `)` 在反引号内。
- [ ] 串关闭后紧跟 `+`（拼接下一个串/helper 结果）。
- [ ] `UpsertClause(` 后反引号存在（字符串开头）。
- [ ] SET 子句结尾：反引号关闭串 → `)` 关闭 helper → `,` 分隔实参。
- [ ] 最后一个实参行以 `)` 关闭 `db.Exec`/`db.Query`。

## 注意
- 跨方言 helper（如 `UpsertClause`）用正则把 `excluded.x`→`VALUES(x)`、`ON CONFLICT`→`ON DUPLICATE KEY UPDATE`，保留 `col=col+excluded.col` / `MAX` / `CASE` 语义（不要改成列清单式 helper，会破坏累加逻辑）。
- `go vet` 在本项目会报 `alerts/notify.go` 既有的 IPv6 格式告警，与本次无关，可忽略。
