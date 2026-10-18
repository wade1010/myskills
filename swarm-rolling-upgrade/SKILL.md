---
name: swarm-rolling-upgrade
description: |
  Docker Swarm 多节点滚动升级（无镜像仓库场景）。用于保证 Swarm 集群里
  所有节点都升级到同一份最新镜像，并能在升级后验证“逐节点版本一致”。
  触发场景：用户提到 “Swarm 滚动升级”、“多节点版本不一致/版本漂移”、
  “service update 后某些节点没升上去”、“不用 latest 用版本号部署”、
  “docker save | ssh load 分发镜像”、“升级后逐节点校验镜像”。
user-invocable: true
---

# Docker Swarm 多节点滚动升级（无镜像仓库）

适用于：**镜像没有推到任何 registry**，靠 `docker save | ssh load` 手动把镜像
分发到各 Swarm worker；希望每次升级后**所有节点都跑同一份最新版本**，且能
**验证逐节点一致**。

核心思路：用**递增版本号**（如 `v9 → v10`）而不是 `:latest`。版本串变化
才能触发 Swarm 各节点用本地已同步的新镜像重部署；`:latest` 下引用串不变，
`service update` 等于没生效，且节点漂移完全看不出来。

## 何时该用版本号（而不是 latest）

| | 只用 latest（无仓库） | 版本号 + 手动分发 |
|---|---|---|
| 节点漂移能否发生 | 会（忘了分发/分发失败就漂移） | 同样依赖分发，但**可被发现** |
| 漂移可见性 | 所有节点都叫 latest，完全看不出谁落后 | `docker inspect` 比对 tag 即知 |
| 回滚 | `:latest` 回滚无意义 | `service update --image vN-1` 干净 |
| Swarm 是否重拉 | `service update --force` 强制重建但不换引用串 | `--image vN+1` 引用串变化，触发重部署 |

> 关键：Swarm 只在镜像引用串变化时重部署。推 `:latest` 串没变 → 不重拉；
> 推 `v10` 串变了 → 各节点重部署。所以版本号在这里是**必需**的，不是锦上添花。

## 版本号怎么来（零手动、自动递增）

读“当前已部署”的最大版本号 +1；都没有时 v1。**以运行中为准，不读本地
`docker images`**（避免历史残留 tag 把版本算飞）：

```bash
current_deployed_version() {
    local max=0 v src
    for src in \
        "$(docker service inspect -f '{{.Spec.TaskTemplate.ContainerSpec.Image}}' tsproxy_backend 2>/dev/null)" \
        "$(docker service inspect -f '{{.Spec.TaskTemplate.ContainerSpec.Image}}' tsproxy_nginx-frontend 2>/dev/null)" \
        "$(docker inspect -f '{{.Config.Image}}' nginx-api 2>/dev/null)"; do
        [ -z "$src" ] && continue
        v="${src##*:}"; v="${v#v}"          # 截最后冒号后、去 v 前缀（兼容 host:5000/...:v9）
        case "$v" in (*[!0-9]*) continue ;; esac
        [ "$v" -gt "$max" ] && max="$v"
    done
    echo "$max"
}
VER="v$(( $(current_deployed_version) + 1 ))"   # 例：当前 v9 → v10
```

- 取数字用 `${spec##*:}` 再剥 `v` 前缀，能兼容带端口的 registry 路径。
- 多服务一起升级时取三者最大值 +1，避免分开升级互相回退。
- 副作用（可接受）：同一代码连跑两次会变成 v10→v11，版本号虚涨但无害。

## 镜像分发（无仓库，靠 save|load）

构建在 manager 节点，再把镜像 `docker save | ssh load` 到每个 worker：

```bash
sync_image() {
    local img="$1"
    # 未加 --sync 时直接返回（按需开启，避免误分发）
    [ "$SYNC" -ne 1 ] && { warn "未加 --sync：worker 收不到新镜像，可能漂移"; return; }
    local node failed=0 skipped=0
    for node in $SWARM_NODE_IPS; do
        # 先探活：节点宕机/维护属正常运维，不应阻断其余节点升级
        if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$node" true >/dev/null 2>&1; then
            warn "节点 $node 不可达（宕机/维护），跳过同步"
            warn "  → 恢复后补同步：docker save $img | ssh $node docker load && docker service update --force --image $img <service>"
            skipped=1; continue
        fi
        if docker save "$img" | ssh -o ConnectTimeout=10 "$node" docker load; then
            log "镜像已同步到 $node"
        else
            warn "同步到 $node 失败（SSH 通但 load 失败：磁盘满/镜像损坏），该节点将漂移"
            failed=1
        fi
    done
    [ "$failed" -ne 0 ] && return 1   # 配合 set -e：阻断升级，避免活节点偷偷跑旧镜像
    [ "$skipped" -ne 0 ] && warn "有节点不可达已跳过，恢复后务必补同步"
}
```

**区分两种失败（关键）**：
- 节点**不可达**（宕机/断网）→ 跳过 + 告警 + 给恢复命令，**不阻断**其余节点升级。
- 节点**可达但 load 失败**（磁盘满/镜像坏）→ `return 1` **阻断**升级，否则它会静默跑旧镜像。

## 滚动更新

```bash
# global 服务：--force 重建 + --image 换版本串（二者都要）
docker service update --force --image "tsproxy/backend:$VER" tsproxy_backend

# 注意：只写 --force 不写 --image 会保留 spec 里的旧 tag = 等于没升级（常见 bug）
```

host 模式容器（不走 Swarm，如 nginx-api 双节点）则用 `docker run --rm -d
--name nginx-api IMAGE=tsproxy/nginx:$VER ...` 重建，镜像同样要先 save|load 到备节点。

## 升级后验证（防漏验）

```bash
verify() {
    # 1) 副本数：按实际“Ready Active”节点数动态算，别硬编码 2/2
    #    该 Docker 版本 availability/status 往往不是合法 filter，直接解析 node ls
    local n
    n=$(docker node ls --format '{{.Status}} {{.Availability}}' 2>/dev/null | grep -c '^Ready Active$')
    [ "$n" -lt 1 ] && n=1
    local bad
    bad=$(docker service ls --format '{{.Name}} {{.Replicas}}' \
        | awk -v n="$n" '$2 != (n"/"n) {printf "%s(%s) ", $1, $2}')
    [ -z "$bad" ] && log "✔ 所有服务均为 ${n}/${n}" \
        || warn "副本数暂未收敛（期望 ${n}/${n}）: $bad"

    # 2) 逐节点镜像比对：真正能发现“副本数正常但版本漂移”
    local svc imgs
    for svc in tsproxy_backend tsproxy_nginx-frontend; do
        imgs=$(docker service ps --no-trunc --filter "desired-state=running" "$svc" \
            --format '{{.Image}}' 2>/dev/null | sort -u)
        if [ "$(echo "$imgs" | grep -c .)" -le 1 ]; then
            log "✔ $svc 各节点镜像一致: $(echo "$imgs")"
        else
            warn "$svc 各节点镜像不一致（可能漂移/或滚动瞬态）:"
            echo "$imgs" | sed 's/^/      /'
        fi
    done

    # 3) 应用探活（注意 proxy_protocol 端口用裸 curl 会断连得 000，
    #    应探无 proxy_protocol 的内部端口，如 18889）
    curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://VIP:18889/health
}
```

## 升级中的“预期内瞬态”（不要当故障误报）

- `host-mode port already in use on 1 node`：global 服务发布 host 端口时旧容器
  未及时释放，Swarm 重试后收敛。最终 `3/3` 即正常。
- 中途 `3/2` + `v9/v10` 镜像并存 + 某节点 task 显示 `Preparing`：`start-first`
  滚动瞬态，约 1~2 分钟后三节点均 `vN Running` 即正常。
- 验证脚本在滚动期报“vN/vN+1 不一致”属预期——**60s 后仍不一致才算真漂移**。
- 18888（proxy_protocol）裸 curl 得 `000` 是正常的，以 18889（无 proxy_protocol）=200 为准。

## 一键全量升级示例

```bash
# 在 manager 节点（如 192.168.1.88）拉最新代码后：
bash deploy/update.sh all --sync      # backend + 两个 nginx + keepalived，低峰期执行
# 18888 有秒级中断（host 容器 rm+run），前端/后端是 Swarm 滚动不断服务
```

## 检查清单（升级前/后）

- [ ] 升级脚本用版本号（`--image vN`）而非 `:latest`
- [ ] 同步失败处理：宕机跳过、活节点失败阻断
- [ ] 验证按节点数动态算副本期望值
- [ ] 验证含逐节点镜像比对
- [ ] 探活端口避开 proxy_protocol（或带 PROXY 握手）
- [ ] 滚动瞬态告警已做时间窗口容忍（不要一看到 3/2 就 panic）
