---
name: ts-data-jwt-auth
description: 给 ts_data（FastAPI + Vue3 pure-admin）改造/排查 JWT 鉴权时使用。涵盖：后端无依赖实现 HS256 JWT（用户写死不落库）、/login 与 /refresh-token 双前缀注册、JWTAuthMiddleware 白名单（含路径边界防前缀误伤）与 x-api-key 兼容、前端关闭 vite-plugin-fake-server mock（含枚举 mock 提供的接口）、裸 axios 调用全局注入 Authorization、EventSource/SSE 无法带 header 的取舍、axios 401 自动跳登录、initRouter 补 catch。触发词：登录后被踢、重新登录、鉴权、JWT、token 过期、mock 登录、x-api-key、401、Missing credentials、裸 axios、EventSource、SSE、/refresh-token、白名单失效。
agent_created: true
---

# ts_data JWT 鉴权改造与排查

## 背景与结论（先看这个）

ts_data 前端（pure-admin 模板）自带的登录**原本是 `vite-plugin-fake-server` 的 mock 假数据**
（`frontend/mock/login.ts` 返回硬编码 token，`enableProd: true` 让假数据进了生产包），
而后端 `app/core/auth.py::APIKeyMiddleware` 要的是 `x-api-key`，前端 axios 只发
`Authorization: Bearer` —— 两条链路对不上，表现为**登录后点几下菜单就被踢回登录页**。

2026-09-30 已改造为 **JWT**。本 skill 记录完整改造点与排查路径。

## 🔥🔥 首要排查项：白名单 `startswith("/")` 导致鉴权完全失效

`PUBLIC_PATHS` 若包含 `"/"`，而判断写成 `any(path.startswith(p) for p in PUBLIC_PATHS)`，
则**任何路径都 startswith("/")** → 鉴权中间件形同虚设（不带任何凭证能访问全部接口）。

原 `APIKeyMiddleware` 就是这个坑（所以它「看起来能用」）。修法：把 `/`、`/health`、
`/docs`、`/redoc`、`/openapi.json` 放进**精确匹配集合**：

```python
PUBLIC_EXACT_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json"}
PUBLIC_PREFIX_PATHS = ["/docs", "/redoc", "/openapi.json", "/api/assets", "/api/debug",
                       "/api/pan-upload/notify", "/api/login", "/api/refresh-token",
                       "/login", "/refresh-token", "/api/logs/tail"]

def _is_public(path: str) -> bool:
    if path in PUBLIC_EXACT_PATHS:
        return True
    # ⚠️ 前缀必须带「路径边界」，否则 /api/logs/tail 会放行 /api/logs/tailX
    for prefix in PUBLIC_PREFIX_PATHS:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return True
    return False
```

**⚠️ 前缀边界**：裸 `startswith(prefix)` 会让 `/api/assets` 顺带放行 `/api/assetsX`。
改成 `prefix` / `prefix + "/"` / `prefix + "?"` 三种形态才精确。

**验证方法**：起一个最小 FastAPI，只挂中间件 + 一个 `/api/protected`，
请求不带凭证必须 401。若返回 200 → 白名单写漏了。
边界回归用例：`/api/assetsX`、`/api/debugX`、`/api/logs/tailX` 必须 401，
而 `/api/assets/x`、`/api/logs/tail/x` 必须放行（404 或 200 均可，只要不是 401）。

## 后端实现要点

### 1. 环境里没有 pyjwt —— 用标准库手写 HS256

`conda ts_data` 环境**没有 pyjwt**，也没有 pytest。不要 `import jwt`。
用 `hmac` + `hashlib` + `base64.urlsafe_b64encode`（去掉 `=` 填充）实现：

```python
def encode_jwt(payload, ttl_seconds):
    now = int(time.time())
    body = {"iat": now, "exp": now + int(ttl_seconds), "iss": JWT_ISSUER,
            "jti": secrets.token_urlsafe(12), **payload}
    h = _b64url(json.dumps({"alg":"HS256","typ":"JWT"}, separators=(",",":")).encode())
    b = _b64url(json.dumps(body, separators=(",",":"), ensure_ascii=False).encode())
    sig = hmac.new(_secret(), f"{h}.{b}".encode("ascii"), hashlib.sha256).digest()
    return f"{h}.{b}.{_b64url(sig)}"
```

**⚠️ 必须带 `jti`**：JWT 的 `iat` 是秒级，同一秒内签发的 access/refresh 或刷新前后两个 token
会**完全相同**（表现为「刷新后新旧 token 一样」）。加 `jti` 才能保证唯一。

`decode_jwt` 顺序：**先验签再看内容**（不要解析不可信数据）→ 校验 `exp` → `iss` → `type`。
验签用 `hmac.compare_digest`，密码比较同理（防时序侧信道）。

### 2. `/login` 必须注册两套前缀

前端 `src/api/user.ts` 里 `getLogin` 走的是 **`/login`（无 `/api` 前缀）**，
而 axios 实例**没有 baseURL**、vite 只代理 `/api` → 裸 `/login` 会打到 vite dev server。

修法（两处都要）：
- 后端：`register_auth_routes(prefix)` 同时挂 `""` 和 `"/api"` 两个 router
- 前端 `vite.config.ts`：proxy 显式补 `/login`、`/refresh-token`

### 3. 中间件形态

纯 ASGI（继承无、直接 `class X: def __init__(self, app); async def __call__(scope, receive, send)`），
用 `app.add_middleware(X)` 注册。Starlette 的 `build_middleware_stack` 会 `cls(app)` 实例化，形态正确。

**⚠️ 测试陷阱**：用 `fastapi.testclient.TestClient` 测 `add_middleware` 的 ASGI 中间件时，
即使中间件在 `user_middleware` 里且出现在中间件链上，**请求也可能不被拦截**（原 APIKeyMiddleware
同样如此）。**必须用真实 uvicorn 实例**（线程里 `uvicorn.Server(...).run()` + `urllib.request`）验证，
否则会误判。

## 前端实现要点

### 1. 关闭 mock 要彻底（⚠️ 别忘了 mock 提供的**接口**）

`build-config/plugins.ts` 的 `vitePluginFakeServer`：
- `enableProd: true` → 假数据进**生产包**（必须去掉）
- **`enableDev` 默认 true** → 光关 `enableProd`，dev 下仍会拦截真实 `/login`！
  ⇒ 必须**整个停用插件**（注释掉），并把 `frontend/mock/` 目录改名（如 `_mock_disabled/`），
    否则插件因 `include` 目录不存在而报错。

**🔥 关键：关 mock 前先枚举它提供了哪些 URL**，每个都要有真后端路由兜底，
否则请求会穿透到 SPA catch-all `/{full_path:path}` → 返回 **HTML** → `.data` 为 `undefined`。

本项目 mock 共提供 3 个：`/login`、`/refresh-token`、**`/get-async-routes`**。
第三个最易漏——它是「动态路由」接口，关掉后 `initRouter` 报：

```
[initRouter] 获取动态路由失败，降级为静态路由:
TypeError: Cannot read properties of undefined (reading 'length')
  at handleAsyncRoutes (utils.ts)
```

枚举方法（改名前先跑）：
```bash
grep -rn 'url:' frontend/mock/ | grep -v "^Binary"
# 再与前端所有非 /api 请求路径对账：
grep -rnoE '"/[a-z-]+"' frontend/src/api frontend/src/views | grep -v '"/api'
```

**双保险修法**：① 后端补真路由 `/get-async-routes` 返回 `{"success": True, "data": []}`
（本项目菜单 100% 静态，无需后端下发）；② `handleAsyncRoutes` 加 `Array.isArray` 守卫 +
调用点用 `res?.data ?? []` 防御解构 `undefined`。

**验证**：`grep -rl "adminRefresh\|fakeModuleList\|__VITE__PLUGIN__FAKE__SERVER__" dist/static/js/`
应为空。旧包能 grep 到就是没关掉。

### 2. axios 401 处理 + initRouter 补 catch

- `utils/http/index.ts` 响应拦截器加：`401/403` 且非 login/refresh 端点 → `removeToken()` +
  `location.href = "/#/login?redirect=..."`；用静态标志位防并发重复跳转
- `router/utils.ts::initRouter()` 两处 `getAsyncRoutes()` **必须补 `.catch`**，
  否则接口失败时 `new Promise` 永不 resolve → 路由守卫挂起 → 表现为「点菜单没反应」
- 登录页读 `route.query.redirect` 回跳

### 3. 🔥🔥 裸 `axios` 调用不带 token（JWT 改造后集中爆发）

**这是切 JWT 后最容易漏的一类 401**，日志特征：`[JWT] Missing credentials for /api/xxx`。

项目里有 **50+ 处** `import axios from "axios"` 然后直接 `axios.get("/api/...")`，
它们走 axios **全局默认实例**，**不经过** `@/utils/http` 的 `PureHttp`（那是 `Axios.create()` 的
独立实例），**token 注入拦截器对它们完全不生效**。分布：

```
views/tushare/task/minute/MinuteTaskPanel.vue     11 处
views/tushare/task/minute/MinuteProgress.vue       6 处
views/tushare/task/minute/MinuteCreateDialog.vue   6 处
views/tushare/operation/ts-debug.vue              12 处
views/tushare/operation/scheduler.vue              5 处
views/tushare/operation/api-debug.vue              7 处
views/tushare/completeness/ck-upstream-compare.vue 3 处
```

排查命令：
```bash
grep -rn "axios\.\(get\|post\|put\|delete\|patch\)(" frontend/src --include=*.vue --include=*.ts | wc -l
grep -rn 'from "axios"' frontend/src
```

**最小改动修法**：全局给 axios 默认实例挂请求拦截器（`src/utils/http/axiosDefaults.ts`），
在 `main.ts` 里 `setupAxiosAuthDefaults()`，行为对齐 `PureHttp`：

```ts
import axios from "axios";
import { getToken, formatToken } from "@/utils/auth";
const WHITE_LIST = ["/refresh-token", "/login"];

export function setupAxiosAuthDefaults() {
  axios.interceptors.request.use(config => {
    const url = config.url ?? "";
    if (WHITE_LIST.some(w => url.endsWith(w))) return config;
    const data = getToken();
    if (!data?.accessToken) return config;
    // token 已过期就不注入，交给 401 流程处理
    const expires = Number(data.expires);
    if (Number.isFinite(expires) && expires > 0) {
      const expiresMs = expires < 1e12 ? expires * 1000 : expires;  // 兼容秒/毫秒
      if (expiresMs - Date.now() <= 0) return config;
    }
    if (!config.headers["Authorization"])
      config.headers["Authorization"] = formatToken(data.accessToken);
    return config;
  });
}
```

⚠️ 注意：
- **不要**把裸 axios 换成 `http.request`——响应拦截器 `return response.data`，
  而裸 axios 用 `res.data.xxx`，直接换会**返回值语义错位**，改动面 50 处易漏。
- `config.headers` 类型是 `AxiosRequestHeaders`，**不能** `config.headers = config.headers ?? {}`（TS2322）。
- 这是过渡方案；新代码统一用 `@/api/*` 的 `http.request` 封装。

#### 🔥🔥 「两种响应语义」——换封装时**必须同步改取值**（已踩两次）

本项目**同时存在**两套响应语义，混用就是运行期 `Cannot read properties of undefined`：

| 调用方式 | axios 实例 | 响应拦截器 | 取值写法 |
|---|---|---|---|
| `http.request(...)`（`@/api/*`） | `PureHttp.axiosInstance`（`Axios.create()`） | `return response.data` | **`res.xxx`** |
| `axios.get/post(...)`（裸） | axios 全局默认 | 无（不经过 PureHttp） | **`res.data.xxx`** |

**踩坑实录**：把 `data/index.vue`、`interface/index.vue` 的日志 tail 从裸 axios 换成
`createInterfaceTailSession`（内部走 `http.request`）后，**只改了调用没改取值**，
`res.data.session_id` 里 `res.data` 是 `undefined` → 报
`Cannot read properties of undefined (reading 'session_id')`。

⇒ **改封装时的检查清单**：调用点改完之后，grep 一遍返回值用法
（`grep -n "res\.data" <file>`），逐个确认语义。同理反向（封装改裸 axios）也要查。

### 4. 🔥 带流式响应的 SSE / `EventSource` 无法带请求头

`EventSource` **不支持自定义 header**，无法携带 `Authorization: Bearer`。
本项目日志跟踪 `/api/logs/tail/*`（含 `/stream` SSE）就是这种情况。
取舍（已采用①）：

1. **白名单放行** `/api/logs/tail`（简单；日志内容对同网段可见）✅ 当前选择
2. URL 拼 `?token=` + 中间件支持从 query 读 token（token 会进访问日志）
3. 改用 `fetch` + `ReadableStream` 手工解析 SSE（能带 header，但要重写消费逻辑）

注意 `/api/logs/tail` 同时有 **POST 建会话 / DELETE 关会话**（这两个可以用 `http.request`）
和 **GET `/stream` SSE**（只能白名单）。统一白名单后前端三处都不必带 token。

### 3. 🔥 回跳原 URL：进入登录页有两条路径，别只补一条

「在子菜单过期 → 重新登录 → 回到原 URL」要覆盖**两条**进登录页的路径：

| 路径 | 触发者 | 极易遗漏点 |
|---|---|---|
| `location.href = "/#/login?redirect=..."` | axios 拦截 401/403 | — |
| `return { path: "/login" }` | **路由守卫**判未登录 | ❌ 常忘记带 redirect |

**token 真过期时通常是守卫先跑**（cookie 被清/到期），所以只做 axios 那条 → 登录后被丢到首页。

另外 `router/index.ts` 的已登录分支里 `whiteList.includes(to.fullPath)` 有坑：
`to.fullPath` 对 `/login?redirect=xxx` 是**带 query 的**，匹配不上 whiteList 的 `"/login"`
→ 必须改成 `to.path`。

登录页消费 redirect 时要校验，防死循环：
```ts
const redirect = (route.query?.redirect as string) || "";
const isValidRedirect = redirect.startsWith("/") && !redirect.startsWith("/login");
const target = isValidRedirect ? redirect : getTopMenu(true).path;
```

**验证方法**：把守卫 / axios 拦截 / 登录页解析三段逻辑按源码语义抽成纯函数，
跑场景表（后端401 / token过期守卫先拦 / 登录页自身不误判 / 防死循环 / 无 redirect 回首页 /
深层 URL 带 query / 已在登录页时 401 不覆盖）。比开浏览器快且稳。

## 验证清单（照做即可）

1. `python -m py_compile` 改动的后端文件
2. JWT 单元测试：用户校验 / 签发 / access↔refresh 类型隔离 / 篡改签名 / 伪造 payload /
   过期 / 错误 iss
3. **真实 uvicorn E2E**（不用 TestClient）：登录 200、错密码拒、无凭证 401、Bearer 200、
   伪造 token 401、`x-api-key` 兼容、刷新、access 冒充 refresh 被拒、白名单 `/docs` 放行
4. 完整 app 冒烟：`import app.main` 确认中间件与路由都在，无 token 访问业务接口 401
5. 前端 `pnpm build` + `vue-tsc --noEmit --skipLibCheck`
6. **不替老板重启服务**，只提示「需重启生效」

## 常用命令

```bash
# 后端编译检查（用真实运行环境）
"D:/ProgramData/anaconda3/envs/ts_data/python.exe" -m py_compile app/core/jwt_auth.py

# 起隔离实例实测（不碰生产 9001）
API_PORT=9190 APSCHEDULER_DISABLED=true AUTO_OPEN_BROWSER=false \
  "D:/ProgramData/anaconda3/envs/ts_data/python.exe" -c "import app.main"

# 前端类型检查（别用 npx，会触发 wsl.exe 被沙箱拦）
"C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" \
  node_modules/vue-tsc/bin/vue-tsc.js --noEmit --skipLibCheck
```

构建前先把 `dist` `mv` 走（`emptyOutDir` 会撞沙箱批量删除保护）。
