# 公司服务器部署与 Batch 5.1 验收

维护日期：2026-09-06。执行者：用户；Agent 不连接服务器。
以下命令会快进服务器 main 并运行部署，可能重建/重启服务；本轮纯文档更新本身不要求部署。
2026-09-06 已收到 Batch 5.1 服务器部署通过的反馈及用户网页检查全部通过的确认，详见 [迭代记录](../iterations/batch-5.1.md)。**本批次已收口，无需重复部署或验收。** 以下步骤保留作为操作参考，运行所需工具为 Docker Compose v2、Git、Python 3、curl、sha256sum 和 Bash。

## 版本与停止条件

交接中的 `6754094` 后已有测试补丁 `8135c85`。为避免未来 main 新增业务改动被顺带部署，本手册固定部署到
`8135c8542bc7c98076961959aae00ff586fb2ca3`，该提交包含 Batch 5.1 的实现，三个运行 JS 与 6754094 相同。
使用 `fetch` 后在 main 上 `merge --ff-only <固定SHA>` 完成固定版本拉取，不创建分支、不进入 detached HEAD。
如果改用更新版本，先核对该完整 SHA 的差异和 CI，再只替换 `target_commit`；不可直接改为未经核对的远端最新值。

服务器必须在 main，已跟踪内容和暂存区无修改。既有 untracked `.env` 备份、日志、运行目录和 VERSION 允许存在，禁止清理。
任何版本分歧、未跟踪文件覆盖冲突、四开关不符、SHA 不符、configured 失败均立即停止，不自动修复配置、不回退、不继续宣称部署成功。
固定版本已落后于服务器时停止，不能 reset 回退；重新核对服务器现有版本再制定命令。

## 完整服务器命令

以下整个代码块一次复制到公司服务器 Bash 执行。脚本不打印完整 `.env` 或 Compose 配置，只输出四个指定非凭据值。
本手册代码块已做静态核对。用户实际使用先前提供的 `BATCH51_DEPLOY` 命令部署 6754094 并通过检查，再快进到仅修改测试的 8135c85；不要将该结果记为下面这个代码块已实际执行。

```bash
bash <<'BATCH51'
set -euo pipefail
cd /root/monitor-autoconfig
target_commit=8135c8542bc7c98076961959aae00ff586fb2ca3
test "$(git branch --show-current)" = main
test "$(git remote get-url origin)" = https://github.com/yikkrrtykj/monitor-autoconfig.git
git diff --quiet
git diff --cached --quiet
git status --short
before_commit=$(git rev-parse HEAD)
git fetch origin
git cat-file -e "${target_commit}^{commit}"
git merge-base --is-ancestor 675409458a02ac21d478aa01634d71b84f981dd5 "$target_commit"
git merge-base --is-ancestor "$target_commit" origin/main
git merge-base --is-ancestor HEAD "$target_commit"
git merge --ff-only "$target_commit"
test "$(git rev-parse HEAD)" = "$target_commit"
printf 'Before: %s\nTarget: %s\n' "$before_commit" "$target_commit"
cd /root/monitor-autoconfig/librenms+grafana

# 只解析所需键；不 source .env，不打印其它配置。
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

# bigscreen 启动时从 /app 复制到 nginx 服务目录，三处都要验证。
bigscreen_port=$(docker compose port bigscreen 80 | head -n 1)
bigscreen_port=${bigscreen_port##*:}
[[ "$bigscreen_port" =~ ^[0-9]+$ ]]
for file in app.js control/auth-controller.js config/config-editor.js; do
  source_sha=$(sha256sum "bigscreen/$file" | awk '{print $1}')
  container_sha=$(docker compose exec -T bigscreen sha256sum "/usr/share/nginx/html/$file" | awk '{print $1}')
  url="http://127.0.0.1:${bigscreen_port}/${file}?verify=${target_commit}"
  http_sha=$(curl --fail --silent --show-error --max-time 20 "$url" | sha256sum | awk '{print $1}')
  test "$source_sha" = "$container_sha"
  test "$source_sha" = "$http_sha"
  headers=$(curl --fail --silent --show-error --max-time 20 -D - -o /dev/null "$url")
  cache_control=$(printf '%s\n' "$headers" | tr -d '\r' | grep -i '^Cache-Control:')
  printf '%s\n' "$cache_control" | grep -qi 'no-store'
  printf '%s\n' "$cache_control" | grep -qi 'no-cache'
  printf 'SHA OK %s %s\n%s\n' "$file" "$source_sha" "$cache_control"
done

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
BATCH51
```

预期：四开关三层匹配，三个 JS 各输出 `SHA OK`，缓存头同时含 no-store/no-cache，configured 成功，Bridge ready=true，最终 HEAD 为目标 SHA。
历史 configured 曾为 39/39；以当前脚本成功状态和实际检查内容为准，不能只数文本。
`git status --short` 的既有 `??` 可保留，不能将其误判为清理指令。

## 网页验收

1. 打开大屏，`Ctrl+F5` 强制刷新。
2. 登录控制台，等待至少 15 秒，确认刷新没有倒退、闪回登录页或错误。
3. 控制台与普通大屏之间切换两次。
4. 点击退出后等待超过 10 秒，确认旧认证响应不会重新打开控制台。
5. 重新登录，确认不会被旧未认证响应踢回登录页。
6. 确认未保存配置草稿保护、事故区域、普通大屏 5 个 ISP 和其它主要页面正常；Tournament 仍为 2、2、1。
7. 不需要故意断网或点击“应用配置”；不执行真实 DELETE，不发送测试飞书。

返回目标 SHA、configured 摘要、三个 SHA OK、缓存头、四开关与网页结果（不贴凭据或完整日志）。
如失败，保留输出中的安全摘要并停止；Agent 根据具体失败提供最小追加补丁，不删除生产文件、不自动回滚数据。
服务器和网页都通过后，更新 [Batch 5.1 记录](../iterations/batch-5.1.md) 与 [STATUS](../STATUS.md)，追加提交并 push 后收口。
