---
name: ts-proxy-build-publish
description: ts_proxy 集群管理 UI「构建发布」(code-to-image build) 与「快速发布（热修复）」(deploy-binary) 按钮的修复、部署与端到端验证流程。涉及 backend/internal/cluster/cluster.go 的 BuildServiceImageDetached、DeployBinaryDetached、serviceRolloutCleanupSnippet，以及 88 仓库 /root/workspace/ts_proxy 的镜像构建+同步+滚动更新。当用户改了构建发布/热修复/收尾清理逻辑、或报告“构建发布失败/exit 1”时使用。
user-invocable: true
---

# ts_proxy 构建发布（build-publish）修复与验证

## 流程概览
- 按钮 = `POST /admin/api/cluster/build {"service":"backend","deploy":true}` → `PostClusterBuildImageHandler`（cluster.go）→ `BuildServiceImageDetached` → 在 88 宿主用 `setsid+nohup` 后台跑 detached 脚本（因滚动 backend 自身会掐断 SSE）。
- 脚本落盘：88 的 `/data/dockerdata/tsproxy/data/build/<job>.sh|.log|.status`。SSE 直接 tail `.log`。终态哨兵写 `.status`：`BUILD_STATUS:DONE <img>` / `BUILD_STATUS:FAILED ...`。
- 脚本结构：`git pull --ff-only` → `docker build`(打 `tsproxy/<svc>:vN`+`:latest`) → `docker save|ssh load` 同步到对端(89/90) → `docker service update --force --image vN` → 收尾清理 Exited 容器。

## 快速发布（热修复）= commit + service update，绝不 docker restart task 容器
- 2026-09-17 实测教训：对 Swarm task 容器 `docker cp` 二进制 + `docker restart` → Swarm 判任务失败并**按旧镜像重建容器** → 热修丢失，且与未释放的 ingress 端口（18001）冲突 → `port is already allocated` 崩溃循环。Swarm 托管容器绝不能手工 restart。
- 新流程（`DeployBinaryDetached`，`POST /admin/api/cluster/deploy-binary`）：
  1. `git pull --ff-only` → 本机 task 容器内 `go build`（源码 docker cp 进 /tmp/be-build，**不动运行容器**）→ 二进制 docker cp 导出。
  2. `docker create --name tsfix-tmp tsproxy/backend:vN`（基于当前服务镜像，不启动）→ `docker cp` 二进制进 /app/tsproxy → `docker commit` 出 vN+1（同时 tag :latest）→ `docker rm tsfix-tmp`。
  3. `docker save vN+1 | ssh docker load` 同步 89/90。
  4. `docker service update --image tsproxy/backend:vN+1`（版本号必递增 → spec 字符串必变）→ 轮询 `docker service ps --filter desired-state=running` 直到全部 Running 新版本（300s 上限）→ `serviceRolloutCleanupSnippet` 清 Exited → DONE。
  5. 旧镜像保留各节点作 `--rollback` 保险；commit 是叠加层会缓慢增厚，定期走一次标准构建（docker build）洗白。
- 版本号解析：`docker service inspect --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}'`（注意前缀是 `.Spec.`！顶层只有 ID/Version/Spec/PreviousSpec/Endpoint）→ 去掉 `@sha256:` 后取 tag。

## 部署代码改动的正确路径（贴合用户 scp 习惯）
1. 改 sandbox `D:/workspace/ts_proxy/backend/internal/cluster/cluster.go`。
2. `scp` 改后文件到 88:`/root/workspace/ts_proxy/backend/internal/cluster/cluster.go`。
3. **必须**把新镜像同步到 89/90，否则它们无该版本镜像 → Swarm 任务 Rejected → service 回退旧版本：
   `docker save tsproxy/backend:vN | ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null <peer> docker load`（89、90 各一次）。
4. `docker service update --force --image tsproxy/backend:vN tsproxy_backend`（等 3/3）。
5. 再点按钮（此时 running=vN 生成含新逻辑的脚本）做 vN+1 构建，验证收尾。

## 两个已知坑（改收尾逻辑必看）
1. **set -e / ERR 误报 FAILED**：detached 脚本 `set -eu` + `trap ERR`。任何未兜底的非0命令都会触发 ERR trap 写 `BUILD_STATUS:FAILED` + `exit 1`，即使构建/部署全成功。收尾清理函数必须**只做副作用、永不返回非0**（删除用 `xargs -r docker rm -f || true`）。`set -e` 仅留给真正的失败（build 报错等）。
2. **远端 Exited 清不掉**：`serviceRolloutCleanupSnippet` 里查询按节点取 Exited ID，但删除命令若统一跑在本地 88，远端 89/90 的容器 ID 在 88 不存在 → 删除无效 → 89/90 Exited 永远残留（88=0、远端居高）。**修复：查询+删除必须在同一节点**——本地直跑 `docker ps ... | xargs -r docker rm -f`；远端走 `ssh ... "docker ps ... | xargs -r docker rm -f"`（保留 `-o UserKnownHostsFile=/dev/null -o ServerAliveInterval=5` 防 known_hosts 拒绝/卡死）。注意 `pruneExitedServiceTasksAsync`(Go, 滚动更新/重启 API 用) 用 `runOnLong(addr,cmd)` 是对的，仅 shell 片段有此 bug。

## 收敛判定与 service update 的坑（2026-09-17 新增）
1. **`service ps` 的 CurrentState 含时长**：`{{.CurrentState}}` 渲染为 `Running 5 minutes ago`（有空格）→ Image 不在 `$2`。收敛匹配必须按**整行子串**：`grep -c "$PREFIX:$NEXT"`，不能用 awk `index($2,...)`。
2. **裸 tag 的 service update 是 no-op**：service spec 里 Image 存的是裸 `tsproxy/backend:v30` 时（标准构建 `--force` 更新后即如此），对**同字符串** tag 再 `service update --image` 不会重建任何任务（即使本地 tag 已指向新镜像）。热修复永远 vN+1 递增所以字符串必变；**手工引导/救火时要确认 spec 字符串与新 tag 不同**，否则用 `--force`。
3. **engine 版本差异**：.90 是 engine 28，`docker load` 会重算镜像 ID（与 88 的 29 不同）。验证同步是否成功用**容器内二进制 md5**（`docker create` 临时容器 + `docker cp` 出来 md5sum），别对齐镜像 ID。
4. `docker build` 全缓存命中时镜像 Created 显示旧时间（缓存层时间），别拿它判断构建产物新旧。
5. 全局服务滚动是逐节点串行（约 1 分钟/节点），`docker service update` CLI 的 "converged" 可能早于实际完成；用 `service ps --filter desired-state=running` 复核。

## 端到端验证循环
- 触发：`curl -s -X POST http://192.168.1.88:18001/admin/api/cluster/build -H "Authorization: Bearer tushareproxy-admin-token" -H "Content-Type: application/json" -d '{"service":"backend","deploy":true}'` → 取 `job_id`。
- 监控（在 88 上轮询，~每 15s，共 ~6min）：
  - `cat /data/dockerdata/tsproxy/data/build/<job>.status`（期望最终 `BUILD_STATUS:DONE tsproxy/backend:vN`）。
  - 三节点 Exited 计数（期望收敛到 **0/0/0**）：`for h in 88 89 90; do ssh ... "docker ps -aq --filter status=exited --filter label=com.docker.swarm.service.name=tsproxy_backend | wc -l"; done`。
- 成功判据：status=DONE 且三节点 Exited 全 0，且 `docker service ps tsproxy_backend` 3/3 新版本。

## git（sandbox 怪癖）
- sandbox `git commit` 不前进分支 ref → 用 loose-ref：`git add <file> && TREE=$(git write-tree) && COMMIT=$(git commit-tree $TREE -p <父SHA> -m "...") && mkdir -p .git/refs/heads/feature && printf '%s\n' "$COMMIT" > .git/refs/heads/feature/migrate-sqlite-mysql`。
- push：`git push origin feature/migrate-sqlite-mysql`（SSH 可达；origin 头作父）。
- 88 仓库：`git checkout -- backend/internal/cluster/cluster.go`（丢弃脏树）+ `git pull --ff-only`（快进到推送提交）→ 树干净且含修复，下次按钮 `git pull` 正常。
