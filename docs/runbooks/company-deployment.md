# 公司服务器部署与 Batch 5.2 验收

维护日期：2026-09-06。执行者：用户；Agent 不连接服务器。
Batch 5.2 已由用户确认正式收口；P3 `/incident` 直接刷新问题 DEFERRED，以下命令仅保留为历史参考，无需重跑。当前主线见 [Feishu EVENT_NAME 隔离方案](../iterations/feishu-event-name-isolation.md)，本手册不是该修复的部署命令。
本手册会快进服务器 `main` 并运行部署，可能重建或重启服务。运行前把 `target_commit` 替换为本次交付回复给出的完整 SHA；不可使用未核对的远端最新值。

## 停止条件

服务器必须位于 `main`，已跟踪工作区和暂存区无修改。既有 untracked `.env` 备份、日志、运行目录和 `VERSION` 允许存在并必须保留。
版本不是当前 HEAD 的后代、四开关不符、目标 SHA 不符、部署或 configured 失败时立即停止；不自动修配置、不回退、不清理生产文件。

## 完整服务器命令

将下方 `<交付回复中的完整 SHA>` 替换后，整个代码块一次复制到公司服务器 Bash 执行。脚本只输出四个指定非凭据值，不打印完整 `.env` 或 Compose 配置。

```bash
bash <<'BATCH52'
set -euo pipefail
cd /root/monitor-autoconfig
target_commit='<交付回复中的完整 SHA>'
test "$(git branch --show-current)" = main
test "$(git remote get-url origin)" = https://github.com/yikkrrtykj/monitor-autoconfig.git
git diff --quiet
git diff --cached --quiet
git status --short
before_commit=$(git rev-parse HEAD)
git fetch origin main
test "$(git rev-parse origin/main)" = "$target_commit"
git merge-base --is-ancestor HEAD "$target_commit"
git merge --ff-only "$target_commit"
test "$(git rev-parse HEAD)" = "$target_commit"
printf 'Before: %s\nTarget: %s\n' "$before_commit" "$target_commit"

cd /root/monitor-autoconfig/librenms+grafana
check_delete=$(cat <<'PY'
import json
import os
from pathlib import Path
import re
import shlex
import sys

expected = {
    "DEVICE_PENDING_DELETE_ENABLED": "true",
    "DEVICE_AUTO_DELETE_ENABLED": "true",
    "DEVICE_AUTO_DELETE_DRY_RUN": "true",
    "DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY": "false",
}
mode = sys.argv[1]
if mode == "env":
    values = {}
    for line in Path(".env").read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if match and match[1] in expected:
            key = match[1]
            if key in values:
                raise SystemExit("STOP: duplicate safety key: " + key)
            try:
                words = shlex.split(match[2], comments=True)
            except ValueError:
                raise SystemExit("STOP: invalid safety value: " + key)
            values[key] = words[0] if len(words) == 1 else None
elif mode == "compose":
    values = json.load(sys.stdin)["services"]["alertmanager-feishu-bridge"]["environment"]
elif mode == "runtime":
    values = os.environ
else:
    raise SystemExit("STOP: unknown validation mode")
for key, value in expected.items():
    if values.get(key) != value:
        raise SystemExit("STOP: " + mode + " safety mismatch: " + key)
    print(mode + " " + key + "=" + value)
PY
)

python3 -c "$check_delete" env
docker compose config --quiet
docker compose config --format json | python3 -c "$check_delete" compose
./deploy.sh </dev/null

file='incident/incident-panel.js'
source_sha=$(sha256sum "bigscreen/$file" | awk '{print $1}')
container_sha=$(docker compose exec -T bigscreen sha256sum "/usr/share/nginx/html/$file" | awk '{print $1}')
bigscreen_port=$(docker compose port bigscreen 80 | head -n 1)
bigscreen_port=${bigscreen_port##*:}
[[ "$bigscreen_port" =~ ^[0-9]+$ ]]
url="http://127.0.0.1:${bigscreen_port}/${file}?verify=${target_commit}"
http_sha=$(curl --fail --silent --show-error --max-time 20 "$url" | sha256sum | awk '{print $1}')
test "$source_sha" = "$container_sha"
test "$source_sha" = "$http_sha"
headers=$(curl --fail --silent --show-error --max-time 20 -D - -o /dev/null "$url")
cache_control=$(printf '%s\n' "$headers" | tr -d '\r' | grep -i '^Cache-Control:')
printf '%s\n' "$cache_control" | grep -qi 'no-store'
printf '%s\n' "$cache_control" | grep -qi 'no-cache'
printf 'SHA OK %s %s\n%s\n' "$file" "$source_sha" "$cache_control"

./deploy-check.sh configured </dev/null
docker compose exec -T alertmanager-feishu-bridge python -c "$check_delete" runtime
docker compose exec -T alertmanager-feishu-bridge python -c 'import json, urllib.request; data=json.load(urllib.request.urlopen("http://127.0.0.1:5005/health", timeout=10)); assert data.get("ready") is True; print("Bridge ready=true")'

cd /root/monitor-autoconfig
test "$(git rev-parse HEAD)" = "$target_commit"
git diff --quiet
git diff --cached --quiet
git rev-parse HEAD
git status --short
printf '服务器命令通过；仍需完成网页验收。\n'
BATCH52
```

预期：目标 SHA、三层四开关、事故面板源码/容器/HTTP SHA、缓存头、configured 和 Bridge ready 全部通过。`git status --short` 中既有 `??` 可保留，不是清理指令。

## 网页验收

1. 打开 `/incident`，按 `Ctrl+F5` 强制刷新，确认页面正常加载且浏览器控制台无新错误。
2. 选择一组条件 A 并提交，随即改动时间窗口或阈值为 B 再提交；最终地址栏和表单必须保持 B。
3. 等待两次请求都结束，确认页面不回退到 A，不出现来自 A 的迟到错误；重复一次 B 先完成的快速提交检查。
4. 离开事故页再返回，确认 stop/restart 后页面正常；普通大屏 5 个 ISP、Tournament 2/2/1 和控制台仍正常。
5. 不点击“应用配置”，不修改四开关，不执行真实 DELETE，不发送测试飞书。

返回目标 SHA、configured 摘要、`SHA OK incident/incident-panel.js`、缓存头、三层四开关与网页结果，不贴凭据、完整配置或事故内容。通过后更新 [Batch 5.2](../iterations/batch-5.2-proposal.md) 和 [STATUS](../STATUS.md) 收口。
