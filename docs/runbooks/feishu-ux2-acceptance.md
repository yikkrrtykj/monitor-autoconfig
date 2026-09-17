# FEISHU-UX2 部署与验收

本手册用于网络巡检分页卡的后续生产验收。当前实现的前提是：同一飞书 App 只有一个有效
长连接接收客户端。多实例共享同一个 App 时不要开启该功能。

## 部署前

在公司服务器 `/root/monitor-autoconfig` 执行。已有未跟踪配置、日志和运行目录保持原样；
跟踪文件有修改时停止，不执行覆盖或清理。

```bash
set -euo pipefail
cd /root/monitor-autoconfig

test "$(git branch --show-current)" = main
git diff --quiet
git diff --cached --quiet

git fetch origin main
git log --oneline HEAD..origin/main
git pull --ff-only origin main

echo "HEAD=$(git rev-parse HEAD)"
git status --short
```

确认 `.env` 中仅在满足单接收客户端前提后设置：

```ini
INSPECTION_PAGINATION_ENABLED=true
```

公司删除安全开关必须继续是：

```ini
DEVICE_PENDING_DELETE_ENABLED=true
DEVICE_AUTO_DELETE_ENABLED=true
DEVICE_AUTO_DELETE_DRY_RUN=true
DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY=false
```

只输出非凭据开关：

```bash
cd /root/monitor-autoconfig/librenms+grafana
grep -E '^(INSPECTION_PAGINATION_ENABLED|DEVICE_PENDING_DELETE_ENABLED|DEVICE_AUTO_DELETE_ENABLED|DEVICE_AUTO_DELETE_DRY_RUN|DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY)=' .env
docker compose config --quiet
```

## 部署及运行一致性

不要同时在网页执行“应用配置”。

```bash
set -euo pipefail
cd /root/monitor-autoconfig/librenms+grafana

./deploy.sh
./deploy-check.sh configured </dev/null

HOST_BRIDGE_SHA=$(sha256sum alertmanager-feishu-bridge.py | awk '{print $1}')
HOST_WS_SHA=$(sha256sum feishu-ws-client.py | awk '{print $1}')

docker compose exec -T -w /app alertmanager-feishu-bridge \
  python -c 'import hashlib,pathlib,sys; actual=hashlib.sha256(pathlib.Path("/app/bridge.py").read_bytes()).hexdigest(); assert actual==sys.argv[1], (actual,sys.argv[1]); print("PASS: Bridge source SHA")' \
  "$HOST_BRIDGE_SHA"

docker compose exec -T feishu-ws \
  python -c 'import hashlib,pathlib,sys; actual=hashlib.sha256(pathlib.Path("/feishu-ws-client.py").read_bytes()).hexdigest(); assert actual==sys.argv[1], (actual,sys.argv[1]); print("PASS: WS source SHA")' \
  "$HOST_WS_SHA"

docker compose exec -T alertmanager-feishu-bridge printenv \
  | grep -E '^(INSPECTION_PAGINATION_ENABLED|DEVICE_PENDING_DELETE_ENABLED|DEVICE_AUTO_DELETE_ENABLED|DEVICE_AUTO_DELETE_DRY_RUN|DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY)='

docker compose exec -T feishu-ws printenv \
  | grep '^INSPECTION_PAGINATION_ENABLED='
```

## 飞书验收

使用脱敏测试群和正常巡检命令，不操作 Pending Delete，不发送真实 DELETE。

1. 结果不超过 6 项：完整显示，无页码和按钮。
2. 结果 7～12 项：首页只有“下一页”，末页只有“上一页”，点击后更新原卡。
3. 结果超过 12 项：中间页同时有上一页和下一页，页码为绝对页号。
4. 同一页重复点击：内容相同，不产生新卡。
5. 同群两名成员点击：都能更新同一张卡。
6. 同时创建 A/B 两次巡检：各自翻页，内容不串。
7. 桌面端和手机端快速连续点击 Next/Prev：记录是否出现旧响应覆盖新响应。
8. 等待超过 20 分钟：提示“本次巡检结果已过期，请重新执行巡检。”，原卡不替换。
9. 重启 Bridge 后点击旧卡：提示结果不可用，不能错误声称一定过期。
10. 日志不得出现分页触发的新 LibreNMS/Prometheus 查询、StackWise 学习写入或删除操作。

查看有限日志：

```bash
docker compose logs --since=15m alertmanager-feishu-bridge feishu-ws \
  | grep -E 'inspection|card action|\[BOT\]|StackWise|pending-delete|auto-delete' \
  | tail -200
```

真实飞书若出现快速点击响应乱序，只记录复现步骤、客户端类型和时间，不自行引入跨实例存储；
返回开发阶段评估最小 revision 或串行化补充。
