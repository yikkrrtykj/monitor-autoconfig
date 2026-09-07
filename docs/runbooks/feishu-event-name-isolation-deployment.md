# Feishu EVENT_NAME 隔离修复部署手册（本批专用）

维护日期：2026-09-07。适用批次：[Feishu EVENT_NAME 共享群隔离修复](../iterations/feishu-event-name-isolation.md)。
本手册为本批专用，不复用旧 Batch 5.2 的目标 SHA 或前端文件哈希清单。
服务器命令由用户执行；Agent 不自动连接公司服务器。全程不得输出完整配置或凭据。

目标提交固定为 `7ad540cc75bd955a51656c618a2511163dc8271f`（已独立审计的隔离实现提交）。
本手册的后续修订均为纯文档提交，不改变运行代码；服务器只快进到上述目标完成本批验收，
之后的纯文档提交留待下次例行同步，不在本批混入部署。

每个代码块自带 `set -euo pipefail`，任一断言或命令失败立即停止；停止后不清理、不覆盖，
把失败输出原样回报后再继续。全程保留所有既有 untracked 生产文件。

## 1. 快进到目标提交

```bash
set -euo pipefail
cd /root/monitor-autoconfig
TARGET_SHA="7ad540cc75bd955a51656c618a2511163dc8271f"
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ]
git diff --quiet            # 已跟踪工作区必须干净（untracked 生产文件不受影响）
git diff --cached --quiet   # 暂存区必须干净
git fetch origin
git merge-base --is-ancestor "$TARGET_SHA" origin/main   # 目标必须在远端 main 历史内
git merge-base --is-ancestor HEAD "$TARGET_SHA"          # 只允许快进到目标
git merge --ff-only "$TARGET_SHA"
[ "$(git rev-parse HEAD)" = "$TARGET_SHA" ]
echo "synced to $TARGET_SHA"
```

## 2. 部署前核对（只输出明确选取的非凭据字段）

`.env` 中四个删除开关必须精确断言（`grep -qx` 整行匹配，缺失或不一致即停止）。
同时由执行人明确确认本批的**预期 EVENT_NAME** 并保存为独立快照：部署前 Compose 校验
和部署后运行时校验都与同一份快照比较，防止部署前后实例身份漂移；**允许明确预期为空**
（预期为空 = 该实例按隔离策略静默禁用群命令）：

```bash
set -euo pipefail
cd /root/monitor-autoconfig/librenms+grafana
grep -qx 'DEVICE_PENDING_DELETE_ENABLED=true' .env
grep -qx 'DEVICE_AUTO_DELETE_ENABLED=true' .env
grep -qx 'DEVICE_AUTO_DELETE_DRY_RUN=true' .env
grep -qx 'DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY=false' .env
echo "env switches OK (true/true/true/false)"
grep -E '^EVENT_NAME=' .env   # 当前 .env 值，供确认预期参考；权威校验见下

# 预期 EVENT_NAME：执行人按本批部署预期明确填写；预期禁用群命令则留空（""）。
# 不为通过验收而修改该值或生产配置；与实际不符时按下方说明处理。
EXPECTED_EVENT_NAME="Singapore"
printf '%s' "$EXPECTED_EVENT_NAME" > /tmp/feishu-expected-event-name.txt
echo "expected EVENT_NAME: '$EXPECTED_EVENT_NAME'"
```

Compose 渲染值同样必须断言。解析 Compose JSON，仅断言并打印 Bridge 四开关与
feishu-ws 的 EVENT_NAME（与快照比较），不输出原始 JSON、相邻 YAML 或任何凭据字段：

```bash
set -euo pipefail
cd /root/monitor-autoconfig/librenms+grafana
EXPECTED_EVENT_NAME="$(cat /tmp/feishu-expected-event-name.txt)"
export EXPECTED_EVENT_NAME
python3 - <<'PY'
import json
import os
import subprocess

rendered = subprocess.run(
    ["docker", "compose", "--profile", "feishu", "config", "--format", "json"],
    capture_output=True, check=True, text=True,
)
cfg = json.loads(rendered.stdout)

def service_env(name):
    raw = cfg["services"][name].get("environment") or {}
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    return dict(
        entry.split("=", 1) for entry in raw
        if isinstance(entry, str) and "=" in entry
    )

expected = {
    "DEVICE_PENDING_DELETE_ENABLED": "true",
    "DEVICE_AUTO_DELETE_ENABLED": "true",
    "DEVICE_AUTO_DELETE_DRY_RUN": "true",
    "DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY": "false",
}
bridge = service_env("alertmanager-feishu-bridge")
actual = {key: bridge.get(key) for key in expected}
assert actual == expected, f"bridge compose switches mismatch: {actual}"
print("bridge compose switches OK:", actual)

event = service_env("feishu-ws").get("EVENT_NAME")
assert event is not None, "feishu-ws EVENT_NAME missing from compose"
expected_event = " ".join(str(os.environ.get("EXPECTED_EVENT_NAME") or "").split())
compose_event = " ".join(str(event or "").split())
assert compose_event == expected_event, (
    f"feishu-ws compose EVENT_NAME {event!r} does not match "
    f"expected {os.environ.get('EXPECTED_EVENT_NAME')!r}"
)
if expected_event:
    print("feishu-ws EVENT_NAME matches expected:", expected_event)
else:
    print("feishu-ws EVENT_NAME is empty as expected; group commands disabled by design")
PY
```

预期 EVENT_NAME 为空仅当执行人明确确认为预期禁用状态，校验输出“按预期禁用群命令”。
Compose 或运行时与快照不一致时立即停止：先核对 event-config.yml 与 `.env` 的实际来源，
确属配置错误时按正常配置流程修正后重新执行本手册；不为通过验收临时改预期值或绕过校验。
deploy.sh 会从 event-config.yml 同步 `.env`，因此部署后仍须完成第 3 节的运行时比对。

## 3. 部署

```bash
set -euo pipefail
cd /root/monitor-autoconfig/librenms+grafana
[ "$(git -C .. rev-parse HEAD)" = "7ad540cc75bd955a51656c618a2511163dc8271f" ]
docker compose config --quiet
./deploy.sh </dev/null
./deploy-check.sh configured </dev/null
```

部署后在 Bridge 容器断言四个运行时开关、读取当前 `/health` 并断言 `ready is True`；
feishu-ws 的 EVENT_NAME 单独与同一份快照比较（feishu-ws 只注入
`DEVICE_PENDING_DELETE_ENABLED`，四个开关的运行时权威在 Bridge）：

```bash
set -euo pipefail
cd /root/monitor-autoconfig/librenms+grafana
docker compose --profile feishu ps feishu-ws alertmanager-feishu-bridge
docker compose exec -T alertmanager-feishu-bridge python3 - <<'PY'
import os

expected = {
    "DEVICE_PENDING_DELETE_ENABLED": "true",
    "DEVICE_AUTO_DELETE_ENABLED": "true",
    "DEVICE_AUTO_DELETE_DRY_RUN": "true",
    "DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY": "false",
}
actual = {key: os.environ.get(key) for key in expected}
assert actual == expected, f"bridge runtime switches mismatch: {actual}"
print("bridge runtime switches OK:", actual)
PY
docker compose exec -T alertmanager-feishu-bridge python3 - <<'PY'
import json
import urllib.request

payload = json.load(urllib.request.urlopen("http://127.0.0.1:5005/health", timeout=10))
assert payload.get("ready") is True, payload
print("bridge /health ready OK")
PY
EXPECTED_EVENT_NAME="$(cat /tmp/feishu-expected-event-name.txt)"
docker compose --profile feishu exec -T \
  -e EXPECTED_EVENT_NAME="$EXPECTED_EVENT_NAME" feishu-ws python3 - <<'PY'
import os

expected = " ".join(str(os.environ.get("EXPECTED_EVENT_NAME") or "").split())
actual = " ".join(str(os.environ.get("EVENT_NAME") or "").split())
assert actual == expected, (
    f"feishu-ws runtime EVENT_NAME {os.environ.get('EVENT_NAME')!r} does not "
    f"match expected {os.environ.get('EXPECTED_EVENT_NAME')!r}"
)
if actual:
    print("feishu-ws runtime EVENT_NAME matches expected:", actual)
else:
    print("feishu-ws runtime EVENT_NAME is empty as expected; group commands disabled by design")
PY
for s in alertmanager-feishu-bridge feishu-ws; do
  docker inspect "$(docker compose --profile feishu ps -q "$s")" --format "$s StartedAt: {{.State.StartedAt}}"
done
```

最后一行输出两个容器的实际 `StartedAt`；两者都必须晚于本轮 `./deploy.sh` 的开始时间。
不要用 `docker compose ps` 的 Up 时长推断本轮启动——那是未参与计算的显示值。

## 4. 源码 / 宿主 / 容器一致性

feishu-ws-client.py 挂载并运行在容器内 `/feishu-ws-client.py`（docker-compose.yml
第 572 行挂载、第 582 行入口），不经 HTTP 提供，不编造源码 HTTP 路径；只做三层 SHA
比对，固定工作目录与服务名，不使用按容器名重试的 fallback，不让用户猜路径：

```bash
set -euo pipefail
cd /root/monitor-autoconfig
[ "$(git rev-parse HEAD)" = "7ad540cc75bd955a51656c618a2511163dc8271f" ]
git show HEAD:librenms+grafana/feishu-ws-client.py | sha256sum
sha256sum librenms+grafana/feishu-ws-client.py
(cd librenms+grafana && docker compose --profile feishu exec -T feishu-ws sha256sum /feishu-ws-client.py)
```

三个哈希必须一致。任何一个不一致或命令失败即停止，回报实际值；不猜路径、不重试其它路径。

## 5. 隔离行为离线验证（不触真实投递）

用脱离业务进程的纯函数检查，不修改生产 EVENT_NAME，不调用真实 Bridge：

```bash
set -euo pipefail
cd /root/monitor-autoconfig/librenms+grafana
docker compose --profile feishu exec -T feishu-ws python3 - <<'PY'
import importlib.util

spec = importlib.util.spec_from_file_location("fwc", "/feishu-ws-client.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
route = module.route_event_command
assert route("网络巡检", "") is None
assert route("网络巡检", "   ") is None
assert route("网络巡检", "", allow_unscoped=True) == "网络巡检"
assert route("网络巡检", "Singapore", allow_unscoped=True) is None
assert route("Singapore 网络巡检", "Singapore") == "网络巡检"
assert route("Shanghai 网络巡检", "Singapore") is None
print("isolation checks OK")
PY
```

输出 `isolation checks OK` 即通过。该检查只读路由纯函数，不发消息。

## 6. HTTP 与网页验收

```bash
set -euo pipefail
curl -fsS -m 10 http://127.0.0.1:8088/ -o /dev/null && echo bigscreen-ok
```

网页只检查现有控制台及主要页面可正常打开、登录。实际飞书网络投递未验证时明确记录
“未测”；不得为了验收主动发消息。用户自行观察到真实业务命令结果后再补充证据。

## 7. 回退要求

直接撤销会重新开放空名称群命令。优先追加最小修复；确需 revert 必须经用户明确授权，
使用追加提交和完整部署验证，不 reset/rebase，不自动改生产配置或清理文件。
