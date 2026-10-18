---
name: ts-data-frontend-hmr-stale
description: 当在 ts_data 前端「改了代码但页面上看不到效果/新加的组件或按钮不显示/控制台无报错」时使用。核心手法：按「代码在不在 → vite 编译产物有没有 → import 语句对不对 → 接口数据条件是否成立」四层递进排除，最后锁定为浏览器加载了旧 bundle。适用于新增 .vue 文件后 HMR 不生效、老 vite 进程跑了几天没重启、刷新页面无效等场景。
agent_created: true
---

# ts_data 前端「改了却看不到」排查

适用：`D:/workspace/ts_data/frontend`（Vite 8 + Vue3 + pure-admin）。
**先记住一件事：代码改对 ≠ 用户能看到。** 前端问题的默认答案是「浏览器跑的是旧 bundle」，
但**必须四层验证完再下这个结论**，否则就是猜。

## 铁律：四层递进排除，别跳步

### 第 1 层：代码到底在不在
```bash
cd /d/workspace/ts_data
grep -n "按钮文字" frontend/src/views/tushare/plan/index.vue
```
不存在 → 就是没写/写错文件，直接改。

### 第 2 层：vite 编译产物里有没有（关键分水岭）
```bash
curl -s "http://127.0.0.1:10001/src/views/tushare/plan/index.vue" > /tmp/idx.js
wc -c /tmp/idx.js
grep -o "按钮文字" /tmp/idx.js | wc -l
grep -o 'import MyComp from[^;]*;' /tmp/idx.js
```
- **产物里有** → vite 侧编译正常，问题在浏览器，继续第 3、4 层
- 产物里没有 → vite 没重新编译，看第 5 步「老进程问题」

### 第 3 层：新组件本身能否独立编译
```bash
curl -s "http://127.0.0.1:10001/src/views/tushare/plan/YourComp.vue" | head -20
```
返回 JS 源码 = 编译通过。返回 404 或报错 = 组件本身有语法错误。
**这一步能单独验一个新文件，不依赖任何其他文件。**

### 第 4 层：渲染条件的数据是否成立
条件按钮挂在 `v-if` 上时，数据不对就等于没渲染。**直接打真接口看字段，别看代码猜。**
```bash
curl -s "http://127.0.0.1:9001/api/scheduled-plans" | python -c "
import sys,json; d=json.load(sys.stdin)
ps = d if isinstance(d,list) else d.get('data',[])
for p in ps:
    if p.get('plan_type')=='table_sync':
        print(repr(p.get('plan_type')), sorted(p.keys())[:12])
"
```
常见坑：后端 `_row_to_plan` 忘了把新字段转成前端要的类型、或字段压根不在 `PLAN_COLUMNS` 里。

## 🔥 第 5 步：老 vite 进程（最常见根因）
**判据**——看 vite 依赖缓存的生成时间：
```bash
ls -ld --time-style=full-iso frontend/node_modules/.vite
```
若这个时间戳是**几天前**，说明 vite dev server 已经连续跑了几天没重启。

**为什么新增 .vue 文件会「刷新也无效」**：
- 已存在的文件（如 `index.vue`）靠 chokidar 文件监听，HMR 能自动生效
- **新增文件不在老进程的模块图里**，监听器不会把它当新依赖加进去 →
  `index.vue` 刷了、import 语句在，但运行时拉不到组件 → 白屏或按钮不渲染
- 本项目实测：dev server 从 09-30 23:53 连续跑到当天，21:49 新增 `.vue` 后
  服务端产物里 import 正确、组件能独立编译、`plan_type` 数据正常，
  **唯一的解就是浏览器 bundle 旧**

**先排除「后端没重启」这种反射式猜测**——前端渲染问题跟后端重启无关。
本项目后端**无热重载**，改后端才需要重启（且不要替用户重启）。

## 收尾：让用户刷浏览器
按 SOUL.md 边界，**不替用户重启 dev server、不主动 build**。
确认四层都通过后，直接告诉用户：
> 代码和 vite 产物都验证过了，服务端没问题。你那边 **Ctrl+Shift+R 强刷**一下，
> 强刷还不行就是 dev server 太久没重启（已经连续跑了 N 天），
> 需要重启 dev server 才能让新增的 `.vue` 进模块图 —— 你自己来，我不碰。

## 配套
- 前端真跑起来的端口是 **10001**（vite dev server），**不是**构建产物；
  10001 上任何路径请求都会返回编译后源码，可直接 curl 验编译
- 端口确认：`netstat -ano | grep :10001`；配置在 `frontend/.env`（`VITE_PORT`/`VITE_BACKEND_PORT`）
- **编译通过 ≠ 条件成立**。四层里第 2、3 层查「编译」，第 4 层查「数据」，
  两类问题症状一样（都是「看不到」）但根因完全不同，别混为一谈
- 存量 vue-tsc 报错（~50 条，如 `plan/index.vue:98` `statusTagType`）不是本次引入的，别背锅
  ```bash
  cd frontend && node.exe node_modules/vue-tsc/bin/vue-tsc.js --noEmit --skipLibCheck 2>&1 | grep "你的文件名"
  ```
