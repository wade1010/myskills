---
name: ts-data-frontend-dual-request
description: 当 ts_data 前端出现「数据先出来一份、1 秒后又刷新一次」「loading 拜拜一直转」「明明只点了一次却发两个请求」这类重复请求/竞态现象时使用。核心手法：① Playwright 实测数网络请求（别靠推理）② initializing / refreshing 双守卫覆盖程序赋值与响应回填 ③ reqSeq 请求序号丢弃过期响应。适用于 el-date-picker / el-select / el-table 等组件 @change 联动，以及任何异步请求可能竞态的场景。
agent_created: true
---

# 前端「重复请求 / 数据闪两次」排查

适用：`D:/workspace/ts_data/frontend`（Vite 8 + Vue3 + Element Plus）。
**症状**：数据先出来一份 → 1 秒后又被刷新；或 loading 图标（拜拜）一直转不停；
或"我只点了一次，怎么发了两个请求"。

## 🔥 第一原则：别靠推理，直接实测数请求

这个坑我**连错两次**（都以为找到了根因，改完还是复现），第三次用 Playwright 数请求
一次就定位。**组件 `@change` 到底触发几次、值变化会不会引发额外 update，
读代码看不出来，只有实测知道。**

### 用 Playwright 数请求（本项目已装 playwright-core，用本机 Chrome）
脚本要放在 **frontend 目录内**（放 /tmp 找不到 node_modules）：

```js
// frontend/probe.tmp.cjs
const { chromium } = require('playwright');
(async () => {
  // ⚠️ 必须指定本机 Chrome，playwright 的 chromium 没下载过
  const b = await chromium.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe'
  });
  const p = await b.newPage();
  const reqs = [];
  p.on('request', r => { const u = r.url(); if (u.includes('/api/')) reqs.push({ t: Date.now(), u }); });

  await p.goto('http://127.0.0.1:10001/#/login', { waitUntil: 'networkidle' });
  const ins = p.locator('input');
  for (let i = 0; i < await ins.count(); i++) {
    const t = await ins.nth(i).getAttribute('placeholder');
    if (t && t.includes('用户')) await ins.nth(i).fill('admin');
    if (t && t.includes('密码')) await ins.nth(i).fill('admin123');
  }
  await p.locator('button:has-text("登录")').click();
  await p.waitForTimeout(3000);

  // ⚠️ 路由要从 src/router/modules/*.ts 里查真实 path，别猜
  //    （定时计划是 /tasks/schedule，不是 /tushare/plan）
  await p.goto('http://127.0.0.1:10001/#/tasks/schedule', { waitUntil: 'networkidle' });
  await p.waitForTimeout(2500);

  await p.locator('button:has-text("数据对比")').first().click();
  await p.waitForTimeout(4000);

  const t0 = reqs.length ? reqs[0].t : 0;
  reqs.forEach((r, i) => console.log(`${i + 1}. +${r.t - t0}ms  ${r.u.slice(0, 95)}`));
  await b.close();
})();
```

```bash
cd /d/workspace/ts_data/frontend && node probe.tmp.cjs && rm -f probe.tmp.cjs
```

**Git Bash 注意**：JS 里用模板字符串 `${}` 会被 bash 当变量替换报
`Bad substitution`。要么改字符串拼接，要么用 heredoc（`<<'EOF'`）避免展开。
用 `<<'EOF'`（引号包住 EOF）就不会展开。

实测基线（正常应该长这样）：
```
① 打开弹框    1 个请求
② 点「对比」  +2 个（summary → rows 串行，rows 依赖 summary 的 key）
③ 手动切换    +2 个
```
**每个动作恰好 1 次**，多了就是 bug。

## 🔥 第零步：先分清「竞态」还是「用户操作了两次」

**这一步做错，后面全白改。** 我在这个坑上连错三次，都是闷头修竞态。

| 线索 | 判别 | 该做什么 |
|---|---|---|
| 用户贴的 console **URL 参数变了**（如 `freqs=60min` 而你预选的是 `1min`） | **用户手动改过条件** | 查是不是「改条件查一次 + 点按钮又查一次」= **交互设计问题**，不是竞态 |
| 同一次操作 console 出现**两条参数相同**的请求 | 竞态 | 往下看根因 A/B/C |
| 让你贴 console 他只贴了 1 条 | 可能还有一条被折叠/被丢弃 | 先问清"一共几条、你点了几下" |
| **网络数正常（点一次=+2）但视觉仍像两次** | ⚠️ **串行 + 双层遮罩**，不是重复请求 | 见根因 D |

**修竞态只在「同一次操作发了两条」时才成立。**
「多刷一次」很可能只是**设计上就查了两次** —— 问一句或看一眼 URL 参数就能区分，
比改十遍代码快。

## 🔥 数完请求还要数 DOM：网络对了≠视觉对了

网络层「点一次 +2」是**正常**的（summary + rows），但用户照样说"刷新两次"。
这时候必须抓 **DOM 轨迹**（每 80ms 采一次，状态变化才记一行）：

```js
// 在点击前注入轮询，比截图靠谱 —— 截图抓不到「遮罩叠几层」
window.__trace = []; window.__t0 = Date.now();
window.__timer = setInterval(() => {
  const dlg = document.querySelector('.el-dialog'); if (!dlg) return;
  const line = [
    dlg.querySelector('.el-empty') ? 1 : 0,        // 空态
    dlg.querySelector('.summary-body') ? 1 : 0,    // 结果区
    dlg.querySelectorAll('.el-loading-mask').length, // ⚠️ 遮罩层数
    dlg.querySelectorAll('.el-table__body tr').length,
    Math.round(dlg.getBoundingClientRect().height),
  ].join('|');
  const tr = window.__trace;
  if (!tr.length || tr[tr.length-1].s !== line) tr.push({ t: Date.now()-window.__t0, s: line });
}, 80);
```

病态轨迹长这样（**遮罩数出现 2** 就是铁证）：
```
  105  0|0|0|0|685   ← 全空白（v-if / v-else-if 都不成立）
 2395  0|1|2|0|685   ← 结果区出现，但两层遮罩叠着转
 3613  0|1|0|0|685   ← 遮罩才散
```

## 交互层的坑：条件变更不该自动查询

如果「对比」是主动作，那么 `date`/`freq` 的 `@change` **只能重置、不能查询**：

```js
// ❌ 改一次条件自动查一次，用户再点按钮又查一次 = 两次刷新
const onFilterChange = () => { page.value = 1; loadSummary(); };

// ✅ 条件变更只清旧结果，查询交给按钮
const onConditionChange = () => {
  if (initializing || refreshing) return;
  page.value = 1;
  summary.value = null;
  rowsData.value = { items: [], keys: [], total: 0 };
  reqSeq++;                 // 让在途请求失效，别把旧条件结果填回来
};
```

配套：空状态写明当前条件 + 「点击对比开始」，用户不会以为点击无效。
实测基线随之变成：打开=1、**改条件=0**、点对比=+2。

## 根因A：程序赋值触发组件 @change

`v-model` 的**程序赋值**和用户手动改值一样会 emit `change`。
打开弹框时给 `el-date-picker` / `el-select` 赋初值 → 各触发一次联动 → 两个并发请求。

```js
let initializing = false;   // 普通 let，不是 ref —— 守卫不需要触发渲染

const open = async () => {
  initializing = true;      // ⚠️ 必须覆盖**整个**流程，含 await
  reqSeq++;
  try {
    dialogVisible.value = true;
    const init = await fetchInit();      // 这 180ms 里守卫必须已生效
    date.value = init.date;
    applyFreqOptions(init.freqs);
    freqFilter.value = opts[0];
  } finally {
    await nextTick();       // ⚠️ 必须等，否则 watch 派发的 change 晚于 finally
    initializing = false;
  }
};
```

踩过的坑：**守卫设在 `await` 之后** → 赋值时守卫已放开 → 完全无效。
`finally` 里只 `initializing = false` 不 `await nextTick()` 也无效。

## 根因B：改 options 数组本身也会触发 change ⚠️（更隐蔽）

`applyFreqOptions(res)` 改的是 `freqOptions` 数组。**options 从空变非空，
el-select 内部会再更新一次并 emit change**。这次发生在**响应回填期**，
`initializing` 已经放开了，拦不住 → 首次点对比还会再刷新一次（拜拜转圈）。

```js
let refreshing = false;     // 专治响应回填期

// loadSummary 里
refreshing = true;
try { applyFreqOptions(res); }
finally { await nextTick(); refreshing = false; }

const onFilterChange = () => {
  if (initializing || refreshing) return;   // 两个守卫都要查
  page.value = 1;
  loadSummary();
};
```

**凡是改 `v-model` 绑定的数据、或改 options 数组，都要先设守卫。**
`loadSummary` 里那段"freq 没选时自动补"的逻辑同样要包 `refreshing`。

## 根因C：并发响应竞态（loading 拜拜不灭的元凶）

慢请求晚返回会覆盖新结果。用请求序号：

```js
let reqSeq = 0;

const seq = ++reqSeq;
try {
  const res = await http.request(...);
  if (seq !== reqSeq) return;         // 过期，直接丢弃
  data.value = res;
} catch (e) {
  if (seq !== reqSeq) return;
  ElMessage.error(e.message);
} finally {
  if (seq === reqSeq) loading.value = false;   // ⚠️ 这里也要比对
}
```

⚠️ **`finally` 里漏了比对 → 被丢弃的旧请求不会关 loading → 拜拜永远转。**
这是"拜拜一直转"的直接原因。

`onClose` 里 `reqSeq++`，让关闭时在途响应失效（否则往已销毁组件塞数据）；
`open()` 开头也 `reqSeq++`，让上一次残留请求失效。

## 🔥 通用坑：带参函数不能直接绑模板事件

事件对象会被当第一个参数传进去，导致逻辑全错：

| 错误绑定 | 后果 |
|---|---|
| `@click="loadSummary"` | MouseEvent 被当 `silent` 参数 |
| `@current-change="loadRows"` | **页码被当 `seq` → 所有响应被判过期 → 明细永远空白** |
| `@click="loadRows"` | 同上 |

**一律包一层无参函数**：`onCompareClick` / `onPageChange` / `onTabChange`。

## 根因D：串行两段查询 + 双层遮罩（网络正常但视觉两次）⚠️ 最隐蔽

网络层数出来「点一次 = summary + rows 两条」是**对的**，用户还是会喊"刷新两次"。
真凶是两个查询**串行**、且外层 loading 挂到第二个请求完成才关：

```js
// ❌ 病态：2.3s 白屏 → 内容带双层遮罩 → 1.2s 后才散
await summaryReq;                    // 第一段
data.value = res;
await rowsReq;                       // 第二段，外层 loading 还开着
// finally 里才 loading = false      ← 遮罩跨了两段
```

配套还有个模板坑：`v-if="!x && !loading"` + `v-else-if="x"` —— loading 期间
**两个分支都不成立 → 整块空白**，用户看到"点下去 2 秒啥也没有，然后蹦两下"。

✅ 修法（两条一起做）：
1. **参数互不依赖就并发**：`Promise.all([reqA, reqB])` 一次拿全，一次渲染
2. **空态无条件保留**：`v-if="!x"` 就显示空态，`v-loading` 挂到**容器**上，
   子组件不再各自 v-loading（只在切 tab/翻页这类局部刷新时才用）

```html
<div class="content" v-loading="loading" element-loading-text="正在对比…">
  <el-empty v-if="!summary" />        <!-- ✅ 不要再 && !loading -->
  <div v-else class="body">…</div>    <!-- ✅ 去掉这里的 v-loading -->
</div>
```

⚠️ 并发改造后：局部刷新函数（切 tab/翻页）会 `++reqSeq` 把并发那轮顶掉 →
并发的 `finally` 因 `seq !== reqSeq` **不关 loading → 遮罩永远转**。
必须加闸：`if (!summary.value || loading.value) return;`

修完的正常轨迹（**遮罩数恒为 1**，只有一次内容切换）：
```
   89  1|0|1|0|685   ← 空态 + 1 层遮罩
 2512  0|1|1|0|685   ← 一次渲染出结果
 2889  0|1|0|0|685   ← 遮罩散
```

## 排查清单
1. Playwright 实测数请求，别推理（本项目 login `admin/admin123`）
2. 数出**每个动作**分别发几个，对比基线（打开=1，点对比=+2）
3. 多出来的请求来自 `onDateChange` / `onFilterChange`？→ 查 initializing 守卫范围
4. 还多？→ 查 `applyFreqOptions` 所在期有没有 refreshing 守卫
5. 拜拜不灭？→ 查 `finally` 里 `if (seq === reqSeq)` 是否漏写
6. 路由 path 从 `src/router/modules/*.ts` 查，别猜
7. **网络数正常但还是"刷新两次"？→ 抓 DOM 轨迹，看遮罩层数是不是 2、有没有空白帧**（根因D）
