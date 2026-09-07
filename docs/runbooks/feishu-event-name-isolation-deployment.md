# Feishu EVENT_NAME 隔离修复部署手册（本批专用）

维护日期：2026-09-07。适用批次：[Feishu EVENT_NAME 共享群隔离修复](../iterations/feishu-event-name-isolation.md)。
本手册为本批专用，不复用旧 Batch 5.2 的目标 SHA 或前端文件哈希清单。
服务器命令由用户执行；Agent 不自动连接公司服务器。全程不得输出完整配置或凭据。

目标提交：即包含本手册的修复提交。快进前用 `git log -1 --format="%H %s"` 核对
`feishu-ws-client.py` 的隔离改动已在其中；完整 SHA 以交付回复或 STATUS 记录为准。

## 1. 快进到目标提交

```bash
cd /root/monitor-autoconfig
git status --porcelain            # 已跟踪文件必须干净；untracked 生产文件保留
git fetch origin
git log --oneline origin/main -3  # 确认目标 SHA 在远端顶端
git merge --ff-only origin/main   # 只允许快进；出现冲突或非快进立即停止并回报
```

版本/路径冲突即停止，不强制覆盖，不清理文件。

## 2. 部署前核对

```bash
cd /root/monitor-autoconfig/librenms+grafana
grep -E '^DEVICE_(PENDING_DELETE_ENABLED|AUTO_DELETE_ENABLED|AUTO_DELETE_DRY_RUN|AUTO_DELETE_DRY_RUN_NOTIFY)=' .env
```

四个删除开关必须为 `true / true / true / false`（顺序对应上述四个变量名）。任何不一致停止部署。

核对 feishu-ws 的 EVENT_NAME 来源和值（预期为非空赛事名称）：

```bash
grep -E '^EVENT_NAME=' .env
grep -n 'EVENT_NAME' docker-compose.yml
docker compose config 2>/dev/null | grep -A2 -B2 'EVENT_NAME' | head -20
```

EVENT_NAME 为空或全空白属于预期拒绝状态：该实例静默丢弃所有群命令。如这不是预期，
先在 `.env` 配置明确名称再部署；不修改代码绕过。

## 3. 部署

```bash
cd /root/monitor-autoconfig/librenms+grafana
docker compose config --quiet
./deploy.sh </dev/null
./deploy-check.sh configured </dev/null
```

部署后核对 Bridge ready 与运行时四开关：

```bash
docker compose ps | grep -E 'alertmanager-feishu-bridge|feishu-ws'
docker compose logs --since=10m alertmanager-feishu-bridge 2>&1 | grep -i ready | tail -3
docker compose exec -T feishu-ws env 2>/dev/null | grep -E '^DEVICE_(PENDING_DELETE_ENABLED|AUTO_DELETE_ENABLED|AUTO_DELETE_DRY_RUN|AUTO_DELETE_DRY_RUN_NOTIFY)='
```

## 4. 源码 / 宿主 / 容器一致性

feishu-ws-client.py 不经 HTTP 提供，不编造源码 HTTP 路径；只做三层 SHA 比对：

```bash
cd /root/monitor-autoconfig
git show HEAD:librenms+grafana/feishu-ws-client.py | sha256sum
sha256sum librenms+grafana/feishu-ws-client.py
docker compose -f librenms+grafana/docker-compose.yml exec -T feishu-ws sha256sum /app/feishu-ws-client.py 2>/dev/null \
  || docker exec feishu-ws sha256sum /app/feishu-ws-client.py
```

三个哈希必须一致；容器内路径以实际镜像为准，可用 `docker exec feishu-ws sh -c 'ls /app'` 先定位。
并确认 feishu-ws 容器本轮已启动（`docker compose ps` 的 feishu-ws 行为 Up 且启动时间在本轮部署之后）。

## 5. 隔离行为离线验证（不触真实投递）

用脱离业务进程的纯函数检查，不修改生产 EVENT_NAME，不调用真实 Bridge：

```bash
docker compose -f librenms+grafana/docker-compose.yml exec -T feishu-ws python - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("fwc", "/app/feishu-ws-client.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
r = m.route_event_command
assert r("网络巡检", "") is None
assert r("网络巡检", "   ") is None
assert r("网络巡检", "", allow_unscoped=True) == "网络巡检"
assert r("网络巡检", "Singapore", allow_unscoped=True) is None
assert r("Singapore 网络巡检", "Singapore") == "网络巡检"
assert r("Shanghai 网络巡检", "Singapore") is None
print("isolation checks OK")
PY
```

输出 `isolation checks OK` 即通过。该检查只读路由纯函数，不发消息。

## 6. HTTP 与网页验收

```bash
curl -fsS -m 10 http://127.0.0.1:8088/ -o /dev/null && echo bigscreen-ok
```

网页只检查现有控制台及主要页面可正常打开、登录。实际飞书网络投递未验证时明确记录
“未测”；不得为了验收主动发消息。用户自行观察到真实业务命令结果后再补充证据。

## 7. 回退要求

直接撤销会重新开放空名称群命令。优先追加最小修复；确需 revert 必须经用户明确授权，
使用追加提交和完整部署验证，不 reset/rebase，不自动改生产配置或清理文件。
