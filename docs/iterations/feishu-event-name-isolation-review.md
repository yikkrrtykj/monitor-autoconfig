# Feishu 隔离实现提交独立审计

日期：2026-09-07。被审提交：`7ad540cc75bd955a51656c618a2511163dc8271f`，父提交 `e44ca0e84449b2ea822e67147fb15a99d1c2751a`。
状态：**隔离逻辑无阻断；R1～R4 已确认解决；R5 已修正待复核，未放行前不 push**。

## 范围与基线

按用户最新状态核实，修复已经实施，不能再按旧方案状态重做。开始时本地 main 为 7ad540c、工作区及暂存区干净；实时 GitHub main 为 e44ca0e，本地领先一个实现提交。
本次审阅该提交的运行代码、测试、使用说明和部署手册；只记录发现，不修改实现/测试/部署命令，不连接生产服务器，不发送消息或执行 DELETE。
Batch 5.2 保持正式收口，P3 `/incident` 刷新延后，ISP/Golden 不审查。

## 代码与验证结论

- 空/空白 EVENT_NAME 默认返回 None；群历史入口没有开启 allow_unscoped，即使历史条目误标 p2p 也不能绕过。
- 长连接仅明确 p2p 可保留空名称兼容；非空 EVENT_NAME 仍须匹配前缀，allow_unscoped 不会跳过这一约束。
- 两个入口在 None 时均于启动命令任务前退出；原轮询 ready 抑制、去重、baseline 与卡片路径未被修改。
- 既有多实例测试分别清空实例去重状态，不会因共用去重缓存产生虚假隔离通过。
- 业务 diff 未发现本次范围内的阻断。此结论不是全仓审计，也不解除方案已经记录的共享名称唯一性/前缀歧义限制。

本轮独立验证：Windows、Python 3.12.14（可用的 bundled runtime），标准库 importlib + unittest.mock 装载被审模块，模拟线程并禁止网络、Bridge、回复；共 **188 个组合通过**：

1. 空串、空格、制表/换行名称 × 仅 @、帮助、无前缀命令、其它赛事命令 × group、unknown、None、字段缺失、p2p。
2. 群历史全部拒绝；长连接在 CHAT_TARGET 为空/有值时只允许明确 p2p；两种入口分别验证 Singapore、Shanghai、空名称的同组消息隔离。
3. 轮询 ready 时私聊继续被原入口抑制；allow_unscoped 不绕过非空名称。

Python 编译检查与 `git diff 7ad540c^ 7ad540c --check` 通过。未重跑 pytest：当前执行环境未发现上一实施轮使用的 Python 3.14，已知 bundled Python 未安装 pytest。
实施轮的 20 passed、contracts 3 passed/68 deselected 作为既有证据保留，不冒充本轮复跑。生产、Docker 实际执行及真实飞书投递未测。

## 必须修正的交付问题

以下行号均对应 **7ad540c** 的 `docs/runbooks/feishu-event-name-isolation-deployment.md`。

### R1 / P1 — 配置上下文输出不保证凭据隔离（第 36 行）

`docker compose config ... | grep -A2 -B2 EVENT_NAME` 输出的是完整渲染配置中的相邻字段，不能限定为 EVENT_NAME。服务环境包含 FEISHU_APP_SECRET 等凭据，邻近输出可能进入复制的验收日志；隐藏 stderr 不会隐藏 stdout 凭据。
修正：解析 Compose JSON，仅选定服务及非凭据字段并输出。不得打印原始 JSON 或相邻 YAML，不运行现有命令获取生产样例来验证泄漏。

### R2 / P2 — 运行文件路径错误，SHA 与隔离验证必然失败（第 67～68、81 行）

Compose 第 572 行实际挂载 `/feishu-ws-client.py`，第 582 行用该路径启动。手册 SHA 主命令、fallback 和 import 都指向不存在的 `/app/feishu-ws-client.py`；fallback 重复同一个错误路径，无法完成验收。
修正：统一为真实挂载/入口路径，固定 Compose 工作目录与服务名，移除按容器名重试的无效 fallback；不让用户临时猜路径。

### R3 / P1 — 没有验证实际执行删除逻辑的四个运行时开关（第 53～56 行及部署前检查）

四开关读取的是 feishu-ws，而该服务只注入 DEVICE_PENDING_DELETE_ENABLED；其余三个开关属于 alertmanager-feishu-bridge。grep 只匹配到一个值也会成功，不能证明删除安全。部署前只展示 .env，未断言 Compose 实际值；Bridge ready 也只 grep 历史日志，不核对当前 health payload。
修正：部署前断言 .env 和 Compose 中 Bridge 的四开关，部署后在 Bridge 容器断言四值并请求本机 `/health` 检查 `ready is True`；feishu-ws 单独核对 EVENT_NAME。缺失或错误即退出，不输出完整 env。

### R4 / P2 — 同步与部署缺少固定版本及失败停止保护（第 12～18 行，后续分段部署块）

手册直接 merge origin/main，没有锁定已审计的完整 SHA；main 后续追加提交时可能部署未经本轮审计的内容。git status 只打印，不执行分支/已跟踪差异断言；分段命令也没有统一的失败即停机制，前序失败后可能继续部署旧代码。
修正：单个 Bash 块使用失败停止语义，断言 main、已跟踪工作区/暂存区干净；fetch 后验证固定目标在远端历史中并可从 HEAD 快进，只合并该目标，部署前后核对 HEAD。保留已有 untracked 文件，冲突立即停止。比较容器实际 StartedAt，不能把 ps 中 Up 时长当成未经计算的本轮启动时间证据。

## 手册修正（2026-09-07，追加提交）

R1～R4 已全部修正，运行代码、测试与隔离实现提交 7ad540c 均未改动：

- R1：删 chasing `grep -A2 -B2 EVENT_NAME`；改为解析 `docker compose config --format json`，仅断言并输出 Bridge 四开关与 feishu-ws EVENT_NAME，不打印原始 JSON/相邻 YAML/凭据。
- R2：容器内路径统一为实际挂载/入口 `/feishu-ws-client.py`（Compose 572/582 行）；移除按容器名重试的 fallback 与“先 ls /app 猜路径”指令，固定工作目录与服务名。
- R3：部署前 `grep -qx` 精确断言 .env 四开关；Compose 渲染后断言 Bridge 四开关；部署后在 Bridge 容器断言四个运行时开关并请求本机 `/health` 断言 `ready is True`；feishu-ws 只单独核对 EVENT_NAME；用 `docker inspect StartedAt` 取实际启动时间。
- R4：固定目标 SHA `7ad540cc75bd955a51656c618a2511163dc8271f`；断言 main 分支、已跟踪工作区/暂存区干净、目标在远端历史内且可从 HEAD 快进；每个代码块 `set -euo pipefail`，失败即停；部署前后均断言 HEAD。

本地验证：内嵌 Python（JSON 解析、运行时开关断言、/health 断言、隔离纯函数检查）逐段语法检查通过；Bash 块未在本机执行（Windows 环境），未在生产执行，标记未测。

## R5 修正（2026-09-07，追加提交）

按复核最小修正要求重写手册第 2、3 节的 EVENT_NAME 校验，运行代码、测试与隔离实现提交 7ad540c 仍无改动：

- 部署前由执行人明确确认预期 EVENT_NAME 并写入独立快照 `/tmp/feishu-expected-event-name.txt`；**允许明确预期为空**，不为通过验收修改该值或生产配置。
- Compose 校验与部署后 feishu-ws 运行时校验均与同一份快照比较，均按与 `route_event_command` 相同的空白规范化规则处理；一致且非空时报告名称，一致且为空时报告“按预期禁用群命令”，不一致立即停止。
- 保留字段存在性校验（Compose 缺 EVENT_NAME 字段仍拒绝）；删除两处仅检查非空的断言，未只删断言而不比较名称。
- 快照不一致时的处理指引：核对 event-config.yml 与 .env 实际来源，确属配置错误按正常流程修正后重跑手册，不临时改预期绕过。

本地验证（Windows）：8 个 Bash 块逐段静态检查、5 段内嵌 Python 逐段 compile、5 个改动文档本地链接、`git diff --check` 均通过；另用 mock subprocess/os.environ 实际执行手册两段校验逻辑，5 个场景全部符合预期：预期空（两段均 PASS，旧断言会拒绝——回归确认已消除）、预期一致 PASS、运行时漂移 FAIL、Compose 漂移 FAIL。未执行生产部署，仍标记未测。

## 下一步与发布门槛

仅修正以上部署手册问题并同步 STATUS/本记录，**不重做隔离实现**。保留 7ad540c，另加普通 commit，不 amend/rebase/reset。
9c78f1b 复核结果见上节；R5 已按最小修正要求完成（见“R5 修正”节）。唯一下一步：独立复核确认 R5 已解决；通过后普通 push（含 7ad540c、a34f2c2、9c78f1b、d90dcf2 与本轮提交），此后用户按修订手册部署验收。验收后返回 pre-refactor 只读审计主线，不扩大 correctness 修复范围。

## 9c78f1b 独立复核（2026-09-07）

被审提交 `9c78f1b0adf9a32bfbb187879060d114afd8bc9f`。开始时工作区和暂存区干净，本地 main 领先三个提交；实时远端 main 仍为 e44ca0e。运行代码与测试自 7ad540c 起无改动，本轮不重复审计隔离实现。

原发现核对：R1 已限定 JSON 字段输出，R2 路径已统一为真实挂载，R3 的三层四开关与当前 Bridge health 已正确改用权威服务，R4 已固定 SHA 并检查 main/干净状态/快进关系/部署前后 HEAD。以上修正不再要求重做。
SHA 一致性和 StartedAt 与部署开始时间仍按手册文字要求人工比较；它们不是脚本自动断言，执行人必须完成比较才可报告通过，不将此再扩大为新修复范围。

### R5 / P2 — 用非空校验替代预期 EVENT_NAME 一致性

位置：9c78f1b 手册第 83～87 行，以及第 135 行。
Compose 校验 `assert event.strip()`、运行时校验 `assert v is not None and v.strip()` 存在同一个契约错误：

- 预期 EVENT_NAME 为空、按隔离策略静默拒绝群命令的部署被强制停止，并被引导修改 .env；这不是本轮修复要求，且与手册“空名称属于预期拒绝状态”的说明冲突。
- 两处只检查非空，不保存或比较预期名称。模拟部署前 Singapore、运行时 Shanghai，两处均 PASS，不能证明部署后的实例身份仍正确。deploy.sh 会从 event-config.yml 同步 .env，不能假设部署前后配置必然相同。

最小修正：保留字段存在性校验；在部署前明确确认预期 EVENT_NAME，保存为独立快照；Compose 和运行时分别与同一个预期值比较，按既定名称规范化规则处理，**允许明确预期为空**。不符时停止，符合且为空时报告“按预期禁用群命令”，不要要求为通过验收修改生产配置。不得只删除非空断言而仍不比较名称。

验证证据（Windows、Git Bash、Python 3.12.14）：

- 8 个 Bash 块逐段 `bash -n` 通过；仅解析，没有执行 shell 中的部署命令。
- 5 段内嵌 Python 逐段 compile 通过。
- 用 mock subprocess 返回 Compose JSON、mock os.environ 模拟运行时，实际执行手册两个名称校验片段：预期空名称被拒绝；Singapore → Shanghai 两段均通过。没有 Docker/服务器/网络调用，也没有真实配置内容。
- `git diff 9c78f1b^ 9c78f1b --check` 通过。原 NOT RUN 中“Bash 未在生产执行”继续成立；本轮补齐的是语法检查，不是生产验证。

本轮仅追加复核结论和状态，不修改部署手册或实现，不 push。剩余一步：只修 R5，复核通过后由用户推送和部署；不再扩展其他 correctness 问题。

R5 已于同日追加提交修正（见“R5 修正”节）；等待独立复核放行后推送。

审计记录仅本地提交，不 push；首次审计检查了 62 个本地文档链接，本次复核检查了 5 个改动文档中的 29 个本地链接，均通过；Markdown 围栏及 `git diff --check` 也通过。自身提交用 `git log -1 --format="%H %s" -- docs/iterations/feishu-event-name-isolation-review.md` 定位。
交接已持久化；无可调用的客户端压缩工具，未执行上下文压缩。
