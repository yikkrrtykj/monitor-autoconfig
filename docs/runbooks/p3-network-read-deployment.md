# P3 Network Read 前端部署与网页验收

本手册仅供 PR 通过代码审查、CI 并合入 `main` 后，由公司服务器操作人员执行。Agent 不连接公司服务器。将 `<目标 main SHA>` 换成合并后的完整提交 SHA；目标不符、四开关不符或任一命令失败时停止，不改动服务器现有未跟踪文件。

## 服务器命令

在公司服务器 Bash 中执行。开始前确认服务器位于 `main`、已跟踪文件和暂存区无改动。已有未跟踪的 `.env` 备份、日志、运行目录及 `VERSION` 原样保留。

```bash
set -euo pipefail
cd /root/monitor-autoconfig
target_commit='<目标 main SHA>'
test "$(git branch --show-current)" = main
test "$(git remote get-url origin)" = https://github.com/yikkrrtykj/monitor-autoconfig.git
git diff --quiet
git diff --cached --quiet
git fetch origin main
test "$(git rev-parse origin/main)" = "$target_commit"
git merge-base --is-ancestor HEAD "$target_commit"
git merge --ff-only "$target_commit"
test "$(git rev-parse HEAD)" = "$target_commit"

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
            words = shlex.split(match[2], comments=True)
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
./deploy-check.sh configured </dev/null
docker compose exec -T alertmanager-feishu-bridge python -c "$check_delete" runtime

bigscreen_port=$(docker compose port bigscreen 80 | head -n 1)
bigscreen_port=${bigscreen_port##*:}
[[ "$bigscreen_port" =~ ^[0-9]+$ ]]
for file in network-read.js app.js; do
    source_sha=$(sha256sum "bigscreen/$file" | awk '{print $1}')
    container_sha=$(docker compose exec -T bigscreen sha256sum "/usr/share/nginx/html/$file" | awk '{print $1}')
    http_sha=$(curl --fail --silent --show-error --max-time 20 "http://127.0.0.1:${bigscreen_port}/$file?verify=${target_commit}" | sha256sum | awk '{print $1}')
    test "$source_sha" = "$container_sha"
    test "$source_sha" = "$http_sha"
    printf 'SHA OK %s %s\n' "$file" "$source_sha"
done
cd /root/monitor-autoconfig
test "$(git rev-parse HEAD)" = "$target_commit"
git diff --quiet
git diff --cached --quiet
git status --short
```

## 网页验收

1. 已登录访问 `/control`：状态条显示各域来源；API 正常时为正常，设备状态未知时不得显示为离线。
2. 已登录访问 `/topology`：确认设备、ISP、LLDP 边及延迟。API 拓扑陈旧时仍显示快照并标“快照陈旧”；单域故障只显示该域“兼容读取”。
3. 已登录访问 `/infra`：确认 ISP 清单来源、5 条现场 ISP（如现场仍为 5 条）及实时流量图；趋势、丢包和 uptime 图仍正常。
4. 匿名访问 `/topology`、`/infra`：页面可用，状态提示兼容读取；浏览器网络面板不应每 5/10 秒重复请求受保护的 `/platform-api/network/*` 并得到 401。
5. 登录态过期后确认停止连续 401；重新登录或等待下一次会话检查后自动恢复统一读取。故障恢复后下一轮也应从兼容读取切回 API。

将目标 SHA、`configured` 结果、三层四开关、两个文件的 `SHA OK`、网页验收结果和失败点回填 [A-03 Issue #25](https://github.com/yikkrrtykj/monitor-autoconfig/issues/25)。不贴凭据、完整配置或事故原文；不执行真实 DELETE、不发送测试飞书。
