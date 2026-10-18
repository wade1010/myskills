---
title: ts_proxy 本机 Go 改动验证流程
summary: 在本机（Windows，Go 1.26.0，go.mod 要求 1.26.1）编译、vet、单测 ts_proxy/ts_data 的 Go 后端改动，并避免 PowerShell 输出捕获、sed 行尾、logger 死锁等反复出现的坑。
description: 改完 ts_proxy (D:/workspace/ts_proxy/backend) 或 ts_data 的 Go 代码后，需要 go build / go vet / go test 验证时使用。涵盖：用 Bash 工具而非 PowerShell、go.mod 版本临时降级与还原（必须用 Edit 不用 sed -i）、GOPROXY 走 goproxy.cn、go test 卡死时用栈定位、单测必须避开 DB 与未初始化 logger。
agent_created: true
---

# ts_proxy 本机 Go 改动验证流程

## 触发场景
- 改完 `D:\workspace\ts_proxy\backend`（或 `ts_data`）的 `.go` 代码，要跑编译 / vet / 单测。
- 出现 `go.mod requires go >= 1.26.1 (running go 1.26.0)` 之类的 toolchain 错误。
- `go test` 挂住不返回、超时被 kill。

## 铁律（每条都踩过）

### 1. 跑 go 命令只用 Bash 工具，绝不用 PowerShell
PowerShell 会把 native 命令的 stderr 当错误中断脚本（`NativeCommandError`），输出捕获也经常被吞；`*>` 重定向写 UTF-16，Read 工具会判为二进制。为此曾浪费 30+ 次工具调用。

Bash 工具里 `git`、`ls`、`sed`、`rm`、`head` 都可用（`git -C <path>` 有怪癖，先 `cd` 再用）。

### 2. 改 `go.mod` 版本必须用 Edit 工具，不能用 `sed -i`
`sed -i` 在 Windows 下会把行尾写成 CRLF，git 会把 go.mod 整文件判为改动（假 diff 100+ 行）。
万一踩了：`git checkout -- backend/go.mod` 兜底（原文件本就是 `go 1.26.1`）。

### 3. 单测必须避开 DB 与未初始化 logger
- 单测里不要间接调 `database.*`：如 `clickhouse.resolveDBField` → `GetParamMappings` → `loadMappingCache` → DB 全表查询；且 `loadMappingCache` 失败不置 `loaded`，**每次调用都重试**，会让测试挂到超时。要测把映射表当参数注入（纯函数化，如 `resolveDBFieldWith`）。
- 不要在 logger 未初始化时调 `logger.*`。`pkg/logger.GetLogger()` 原本在 `RLock` 内调 `InitDefaultLogger()`（内部取写锁）→ 不可重入死锁，**已于 2026-09-14 修复**。若回归，症状是 `go test` 卡死 + 栈为：
  `... → logger.Debug → GetLogger → InitDefaultLogger → InitLogger → semacquireRWMutex`

## 标准命令（照抄）

⚠️ **优先 `GOTOOLCHAIN=auto` 且不要动 `go.mod`**（2026-09-23 实测：系统 Go + auto 直接通过
`go build ./...` / `go vet` / `go test`）。老做法「把 go.mod 降到 1.26.0 + GOTOOLCHAIN=local」
已不需要；只有当 auto 拉工具链失败/超时才回退降级，且用完必须改回 `go 1.26.1`。

⚠️ **Bash 工具的 PATH 经常是坏的**：会看到
`...shim/shell-runtime-bash-env.sh: line 3: dirname: command not found`，且 `ls`/`head`/`tail`
全部 `command not found`（连 pipe 到 head 都会失败）。对策：**一律用绝对路径**调用，
并且**不要在管道里用 head/tail**，直接让输出全量返回。

```bash
export PATH="C:/Users/Administrator/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Windows/System32:$PATH"
cd "D:/workspace/ts_proxy/backend"
export GOPROXY=https://goproxy.cn GOFLAGS=-mod=mod GOTOOLCHAIN=auto
GO="D:/Program Files/Go/bin/go"
GOFMT="D:/Program Files/Go/bin/gofmt"
"$GOFMT" -l internal/<pkg>/                    # 改完先看格式化（有输出=需格式化）
"$GOFMT" -w internal/<pkg>/<file>.go           # 需要时再 -w 落盘
"$GO" build ./... 2>&1; echo "BUILD_EXIT=$?"
"$GO" vet ./internal/<pkg>/ 2>&1; echo "VET_EXIT=$?"
"$GO" test ./internal/<pkg>/ -v 2>&1; echo "TEST_EXIT=$?"
```

⚠️ **不要写 `"$GO" gofmt`**：`go` 没有 `gofmt` 子命令（会报 `go gofmt: unknown command`
并且**退出码仍是 0**，极易误判为"格式化通过"）。`gofmt` 是 GOROOT/bin 下的独立程序，
子命令形式的 `go fmt ./pkg/` 只是它的包装（无 `-l` 能力）。

只跑目标包更快；查具体用例：`"$GO" test -v -run 'TestXxx' ./internal/<pkg>/`

漏了 `gofmt` 会在后续 `git diff` 里混入格式噪音，改完顺手跑一次。

## 诊断 `go test` 卡死
1. 加 `-timeout 25s` 跑，让它自己 panic 并打印全部 goroutine 栈。
2. 栈里找 `sync.(*RWMutex).Lock` / `semacquireRWMutex` 上阻塞的 goroutine，看它的调用链落到了哪个包（logger / database / redis）。
3. 用 `-run 'TestPureOnly'` 单跑一个不碰 IO 的用例：若 0.1s 过，就是 IO/锁问题而非编译慢。

## 临时日志文件
调试期若落盘过 `backend/*.log`，完事必须删掉，别留脏文件：
```bash
cd /d/workspace/ts_proxy && git --no-pager status --short   # 确认没有多余的 ?? 未跟踪文件
```
