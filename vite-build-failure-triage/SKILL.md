---
name: vite-build-failure-triage
description: 前端 vite/rolldown 构建失败的定位流程。当 docker build 或 pnpm build 只报 `ENOENT scandir 'dist'`、`Rolldown failed to resolve import`、`UNRESOLVED_ENTRY`，或新增第三方组件（如 monaco-editor）后构建挂掉时使用。
agent_created: true
---

# Vite 构建失败排查

## 核心原则：看到的报错往往不是真错误

rolldown/vite 构建失败时**仍会调用 `closeBundle`**。如果自定义插件在 closeBundle 里做了异步 IO 并 `throw`，
未捕获异常会先杀掉 node 进程，真正的构建错误**根本来不及打印**。
所以第一条铁律：**先怀疑"看到的错误是不是被掩盖后的假象"**。

## 步骤

1. **本地复现**（docker 里看不全）
   ```bash
   cd <repo>/frontend && pnpm build 2>&1 | tail -60
   ```
   - 构建前先把旧产物挪走：`mv dist dist.bak.$(date +%s)`。
     直接 `rm -rf dist` 或让 vite 的 `emptyOutDir` 删，在受控沙箱里会撞
     `SAFE_DELETE_BULK_CONFIRM_REQUIRED`（>50 文件），报出假错误。
2. **如果只看到 `ENOENT: no such file or directory, scandir 'dist'`**
   → 就是被掩盖了。检查 `build-config/utils.ts::getPackageSize`：
   它用相对路径 `readdir(folder)`，err 时 `throw err`。
   临时把 throw 改成 `console.warn + callback(0)` 再跑一次，就能看到真错误
   （长期应保留 warn 版本）。
3. **真错误是 `UNRESOLVED_ENTRY` / `Rolldown failed to resolve import`**
   → 按"谁引用了这个 id"分两类：
   - `new Worker(new URL("<pkg>/...", import.meta.url))`：
     vite 的 `vite:worker-import-meta-url` 插件对**裸包名**解析失败时会退化成
     `path.resolve(dirname(importer), url)`，于是把包名拼到源码目录下 → 必然找不到。
     **正确写法**：静态 import + `?worker` 后缀
     ```ts
     import W from "pkg/path/to/worker.js?worker";
     new W();
     ```
   - 单纯的子路径解析失败：去查包的 `package.json` **exports 字段**，
     不要凭记忆写路径。
4. **改完再验**：`pnpm build` 过 + `vue-tsc --noEmit --skipLibCheck` 与基线对比
   （很多仓库有存量类型报错，只要新增文件没进报错列表就行）。

## monaco-editor 专项（0.5x，尤其 0.57）

- `exports = { ".": {...}, "./*.js": "./esm/vs/*.js", "./*": "./esm/vs/*.js" }`
  → 子路径**必须**写 `monaco-editor/editor/editor.worker.js`，
  **不能**写 `monaco-editor/esm/vs/editor/editor.worker.js`（会拼成 `esm/vs/esm/vs/...`）。
- 0.57 **没有 sql.worker**。SQL 只有 Monarch 高亮（语言定义迁到
  `esm/vs/languages/definitions/<lang>/`），只有 css/html/json/typescript 四个语言 worker。
  `MonacoEnvironment.getWorker` 直接统一返回 editor.worker 即可。
- 全量 `import * as monaco from "monaco-editor"` 会拉出 ts.worker(6.9M) / editor.api(2.8M) /
  css|html|json worker 等 chunk；若组件是路由懒加载，首屏不受影响，只是产物变大。
  要瘦身：`monaco-editor/editor/editor.api.js` + 手动 import 需要的 contrib
  （`contrib/suggest/browser/suggestController.js`、`contrib/find/browser/findController.js`、
  `contrib/hover/browser/hoverContribution.js` 等）。
- 完全不配 worker 时 monaco 会 `console.warn('Could not create web worker(s). Falling back
  to loading web worker code in main thread')` —— 能用但会卡主线程。

## 快速验证小技巧

不确定某个 import 能不能被 vite 解析时，别跑全量构建（几分钟）：
在项目里建个临时目录，写 3 行 entry + 3 行 vite config，`node node_modules/vite/bin/vite.js build --config <tmp>/vite.config.ts`，
20 秒出结果，用来二分定位路径写法。
