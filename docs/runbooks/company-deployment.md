# 公司服务器发布与验收

公司服务器的版本更新以本手册和对应 Issue/PR 批准的变更清单为准。由用户在授权窗口执行。仅改文档或测试的交付无需部署或重启。

Batch 5.2 的命令和结果留在[固定版本原文](https://github.com/yikkrrtykj/monitor-autoconfig/blob/a7ee0354e5579dc59ab9c5eac5fb96e6378f1a0e/docs/runbooks/company-deployment.md)及[归档 Issue #22](https://github.com/yikkrrtykj/monitor-autoconfig/issues/22)。其中的 Bash heredoc、容器命令和 `/incident` 验收仅供追溯，禁止用于当前发布。现行流程的依据见 [D2+D3 生产复盘](https://github.com/yikkrrtykj/monitor-autoconfig/issues/121#issuecomment-7007897513)。

## 1. 确认发布范围

- 记录批准的完整目标 Merge SHA、目标提交的 CI、服务器当前 SHA、变更文件、受影响的服务、镜像和挂载，以及网页验收项。串行执行发布，避免配置应用或其他发布同时写入。
- 确认仓库为 `/root/monitor-autoconfig`（或实际路径），生产工作树在 `main`，已跟踪工作树和暂存区干净，且无进行中的 Git 操作。允许已有未跟踪和 ignored 文件，遇到路径冲突即停。保留 `.env`、日志、运行目录、备份、未跟踪的 `VERSION` 和数据卷。
- 按数据格式及写入风险决定备份范围和是否暂停写入。记录实际卷、写入者、兼容读取版本及恢复条件。涉及冷备份时，先完成镜像准备，暂停最小必要范围的写入者，在切换源码和服务前完成并核验备份。
- 目录绑定挂载内的内容可能在 Git 快进时就对运行中的进程可见。按变更单决定哪些服务需在快进前暂停，哪些需在快进后重新创建。单文件挂载可能仍指向旧 inode；仅重启进程或仅核对容器名称不能证明加载了新文件。

## 2. 用独立脚本执行

在 Bash 4.4 或更新版本的终端定义下方函数。每个阶段用 `run_release_script [参数] <<'RELEASE'` 将脚本内容写入独立临时文件，末尾用单独一行 `RELEASE` 结束。临时目录须为可信路径，脚本文件为 0600。函数输出脚本路径，执行前先做 `bash -n`，再将脚本 stdin 指向 `/dev/null`。临时文件留作核对行号；不要写入凭据或打开 `set -x`。

<!-- release-example: runner -->
```bash
run_release_script() (
  set -Ee -o pipefail
  stage=script-write
  trap 'rc=$?; printf "STOP stage=%s line=%s exit=%s\n" "$stage" "$LINENO" "$rc" >&2; exit "$rc"' ERR
  script=$(mktemp -- "${TMPDIR:-/tmp}/company-release.XXXXXXXX")
  chmod 600 -- "$script"
  printf 'Release script %s\n' "$script"

  {
    cat <<'RELEASE_HEADER'
set -Ee -o pipefail
shopt -s inherit_errexit
stage=release
release_complete=0
trap 'rc=$?; printf "STOP stage=%s line=%s exit=%s\n" "$stage" "$LINENO" "$rc" >&2; exit "$rc"' ERR
trap 'rc=$?; if (( rc == 0 && release_complete == 0 )); then
  printf "STOP stage=%s line=%s exit=1 (incomplete)\n" "$stage" "$LINENO" >&2
  exit 1
fi' EXIT
RELEASE_HEADER
    cat
    printf '\nrelease_complete=1\nprintf "COMPLETE stage=%%s\\n" "$stage"\n'
  } >"$script"
  stage=script-syntax
  bash -n "$script" </dev/null
  stage=script-start
  exec bash "$script" "$@" </dev/null
)
```

脚本内用 `stage=阶段名` 标记检查或变更；失败以实际退出码停止，诊断行号对应打印的脚本文件。保持必经命令在普通语句位置。Bash 的 `if`、`!`、`&&` 和 `||` 可抑制对条件中命令的错误处理，需要检查时显式处理退出状态。`COMPLETE` 表示该脚本所有步骤已到达末尾。

每条非交互 `docker exec` 或 `docker compose exec -T` 显式加 `</dev/null`。`-T` 只关闭伪终端。有意接收数据的命令使用独立输入，例如下面 Compose JSON 由管道传给 Python，Python 代码由 `-c` 提供。

## 3. 取回并核验版本，准备镜像和备份

把示例中的仓库路径（如有差异）和两处完整 SHA 换成变更单上的值。此阶段只取回 Git 对象和生成变更清单。比较清单与批准范围，并完成下述准备，再进入快进。每次阶段脚本都须成功结束才能执行下一阶段；脚本间的变量需按变更单重新传入。

```bash
run_release_script /root/monitor-autoconfig '<当前完整SHA>' '<目标Merge SHA>' <<'RELEASE'
stage=git-preflight
cd "$1"
before=$2
target=$3
[[ "$before" =~ ^[0-9a-f]{40}$ && "$target" =~ ^[0-9a-f]{40}$ ]]
test "$(git branch --show-current)" = main
test "$(git remote get-url origin)" = https://github.com/yikkrrtykj/monitor-autoconfig.git
test "$(git rev-parse HEAD)" = "$before"
git diff --quiet
git diff --cached --quiet

stage=git-fetch
if [[ -n "${RELEASE_PROXY:-}" ]]; then
  timeout 180s git -c "http.proxy=$RELEASE_PROXY" fetch --no-tags origin refs/heads/main </dev/null
else
  timeout 180s git fetch --no-tags origin refs/heads/main </dev/null
fi
test "$(git rev-parse --verify 'FETCH_HEAD^{commit}')" = "$target"
git merge-base --is-ancestor "$before" "$target"
git diff --name-status "$before" "$target"
RELEASE
```

- 外网访问确实需要代理时，只为该次调用提供 `RELEASE_PROXY`，按现场填写代理地址，不把认证信息写进命令历史或脚本。独立的构建命令可用 Compose `build --build-arg HTTP_PROXY=… --build-arg HTTPS_PROXY=…`（按构建工具需要提供小写变量和 `NO_PROXY`），仅影响构建步骤内的联网。Docker daemon 或 BuildKit 拉取基础镜像/服务镜像使用它们各自的出网配置，构建参数不会替 daemon 设置下载代理。使用经批准的现有方式提前拉取或预置镜像。此流程不改全局 Git、Docker、DNS 或默认路由。
- 需要新镜像时，从精确目标 SHA 的隔离源码准备镜像，避免提前改动现场绑定挂载。使用计划中相同的 Compose 镜像名或明确记录发布所用标签，预先核对镜像 ID 并在无生产挂载、无生产配置的临时容器中验证所需依赖。停机前完成这些检查，变更时使用已验证镜像。

按 [项目约束](../PROJECT_CONTEXT.md#公司设备删除安全) 核对以下四开关。片段在部署目录执行。在暂停或重建任何服务之前运行至 Compose 核对；更新后重跑并加上运行时核对。仅输出四个非凭据值，不显示完整配置。

```bash
stage=delete-safety
check_delete=$(cat <<'PY'
import json
import os
import re
import shlex
import sys
from pathlib import Path

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
                raise SystemExit("duplicate safety key: " + key)
            try:
                words = shlex.split(match[2], comments=True)
            except ValueError:
                raise SystemExit("invalid safety value: " + key) from None
            values[key] = words[0] if len(words) == 1 else None
elif mode == "compose":
    values = json.load(sys.stdin)["services"]["alertmanager-feishu-bridge"]["environment"]
elif mode == "runtime":
    values = os.environ
else:
    raise SystemExit("unknown safety check mode")
for key, value in expected.items():
    if values.get(key) != value:
        raise SystemExit(mode + " safety mismatch: " + key)
    print(mode + " " + key + "=" + value)
PY
)
python3 -c "$check_delete" env </dev/null
docker compose config --quiet </dev/null
docker compose config --format json </dev/null | python3 -c "$check_delete" compose

# 服务更新完成后执行运行时核对。
docker compose exec -T alertmanager-feishu-bridge python -c "$check_delete" runtime </dev/null
```

- 有备份计划时，在明确批准的私有根目录内创建本次备份目录。以下片段用于阶段脚本，先进入部署目录并将 `backup_root` 设为执行用户拥有的 0700 目录。归档生产卷时，按已核验的卷与一致性方案，在同一个私有权限范围内写入；保留归档元数据，检查归档可读及校验和。按实际变更选择配置、状态或数据卷。

<!-- release-example: backup -->
```bash
stage=private-backup
[[ -n "$backup_root" && -d "$backup_root" && -O "$backup_root" ]]
test "$(stat -c '%a' -- "$backup_root")" = 700
(
  umask 077
  backup_dir=$(mktemp -d -- "$backup_root/release.XXXXXXXX")
  install -m 0600 -- .env "$backup_dir/.env"
  cd "$backup_dir"
  sha256sum -- .env >SHA256SUMS
  sha256sum --check --status SHA256SUMS
  test "$(stat -c '%a' -- .)" = 700
  test "$(stat -c '%a' -- .env)" = 600
  test "$(stat -c '%a' -- SHA256SUMS)" = 600
  printf 'Private backup %s\n' "$backup_dir"
)
```

## 4. 精确快进与定向更新

准备工作通过后，在阶段脚本里使用步骤 3 的 `cd`、`before`、`target` 三个赋值。下面片段再次核对工作区及取回的目标。若期间运行过其他 Git fetch，先重新执行步骤 3。只允许快进，禁止盲目 `git pull`、自动 stash、`reset --hard`、强制推送及清理文件或卷。Git 写入工作树时使用局部 `umask 022`。

<!-- release-example: merge -->
```bash
stage=git-merge
test "$(git branch --show-current)" = main
test "$(git rev-parse HEAD)" = "$before"
test "$(git rev-parse --verify 'FETCH_HEAD^{commit}')" = "$target"
git diff --quiet
git diff --cached --quiet
git merge-base --is-ancestor "$before" "$target"
(
  umask 022
  git -c merge.autoStash=false merge --ff-only --no-overwrite-ignore "$target" </dev/null
)
test "$(git rev-parse HEAD)" = "$target"
printf 'Before=%s Target=%s\n' "$before" "$target"
```

- 在 `librenms+grafana` 复核快进后的 Compose 渲染值，再执行批准的服务更新。例：Bigscreen 静态更新且依赖健康、生产镜像已就绪，可用 `docker compose up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 90 bigscreen </dev/null`。若有其他服务、配置生成或一次性任务，按本次变更单确定顺序和退出状态。使用新 Platform API 容器时传入经过核对的 `PLATFORM_GIT_COMMIT` 并核验运行时值和镜像 ID。共享镜像的其他运行容器不会因镜像预构建自动切换。
- 如历史权限异常，先定位文件和实际 serving worker 的有效 UID/GID。普通新增非执行静态文件通常是 0644，新目录通常是 0755，实际 worker 需要能读取文件并遍历各层目录。经批准仅修复明确列出的静态路径和必要目录，然后重复校验。严禁递归 `chmod 777`。保留 `.env`、凭据、平台状态和备份的严格权限。

## 5. 验收和失败处置

- 检查受影响服务的容器、健康状态及挂载来源，运行 `./deploy-check.sh configured </dev/null`。核对目标 Git SHA 与已跟踪工作树、暂存区，记录 configured 摘要与任何 SKIP 的原因。
- 对变更清单中的静态文件逐一核对 Git 工作树源码、容器实际绑定挂载、启动时复制目录及 HTTP 响应的 SHA256。通过 `docker compose exec -T bigscreen nginx -T </dev/null` 核对实际 Nginx `root`、`alias` 与路由。当前普通静态文件由 `/app` 提供，复制目录是 `/usr/share/nginx/html`。`/config.js` 由入口生成，通过 alias 提供，比较生成文件与 HTTP；拓扑数据走独立挂载，按对应 alias 验证。删除路径按实际路由验收。
- 通过 `docker compose exec -T bigscreen ps -o pid,user,args </dev/null` 找到 worker，读取容器内该进程的 `/proc/<PID>/status`，核对有效 UID/GID 和文件系统 UID/GID，必要时核对补充组。使用该身份执行哈希检查。root 的 `test -r` 无法证明 worker 能读。若身份或挂载映射无法核实，停止并记录 UNKNOWN。
- 从 `docker compose port bigscreen 80 </dev/null` 核对实际本机端口和 IPv4 监听地址。HTTP 验收显式绕过代理，核对缓存头、页面实际引用的资源版本及浏览器强刷。按变更单验证页面和功能，至少确认受影响入口可用。只做批准的验收动作，不执行真实设备 DELETE 或发送测试飞书。

普通静态文件示例片段在部署目录执行。先把 `asset`、实际 `route`、`port`、`target`、`worker_uid`、`worker_gid` 设为已核对的值。此处 worker 的文件系统身份与有效 UID/GID 一致。读取文件会同时验证路径上的目录可遍历。

```bash
stage=static-verification
[[ "$worker_uid" =~ ^[0-9]+$ && "$worker_uid" != 0 && "$worker_gid" =~ ^[0-9]+$ ]]
[[ "$port" =~ ^[0-9]+$ ]]
sha_source=$(sha256sum -- "bigscreen/$asset" | awk '{print $1}')
sha_mount=$(docker compose exec -T --user "$worker_uid:$worker_gid" bigscreen \
  sha256sum "/app/$asset" </dev/null | awk '{print $1}')
sha_copy=$(docker compose exec -T --user "$worker_uid:$worker_gid" bigscreen \
  sha256sum "/usr/share/nginx/html/$asset" </dev/null | awk '{print $1}')
http_body=$(mktemp -- "${TMPDIR:-/tmp}/company-http.XXXXXXXX")
http_status=$(curl --noproxy '*' --fail --silent --show-error --max-time 20 \
  --output "$http_body" --write-out '%{http_code}' "http://127.0.0.1:$port$route?verify=$target")
test "$http_status" = 200
sha_http=$(sha256sum -- "$http_body" | awk '{print $1}')
test "$sha_source" = "$sha_mount"
test "$sha_source" = "$sha_copy"
test "$sha_source" = "$sha_http"
rm -- "$http_body"
printf 'SHA OK %s %s\n' "$asset" "$sha_source"
```

全部必需命令和网页检查完成后，才在对应 Issue/PR 记录最终 PASS。失败记为 FAIL，明确未执行的可选验证记为 SKIP，证据不足记为 UNKNOWN。失败时停止后续发布，保留 SHA、阶段、行号、退出状态、容器状态、备份和原卷，避免回贴敏感配置。代码回滚与数据恢复分别授权，先确认回退版本可读当前数据格式；禁止自动回滚、覆盖卷或清理现场。
