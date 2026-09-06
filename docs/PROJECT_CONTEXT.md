# 项目长期上下文

维护日期：2026-09-06。来源：用户项目交接文本、后续服务器执行输出，以及本地 Git/目录核对。
下述生产结论来自用户交接或执行反馈；Agent 未连接公司服务器，也未独立重新做生产验收。

## 环境与协作

- 仓库：`https://github.com/yikkrrtykj/monitor-autoconfig`。
- 本地主检出：`C:\Users\31313\Documents\monitor-autoconfig-main`。
- 公司服务器仓库：`/root/monitor-autoconfig`；部署目录：`/root/monitor-autoconfig/librenms+grafana`。
- 平台组合：LibreNMS、Prometheus、Grafana、8088 大屏/控制台、Python API、飞书 Bridge、Docker Compose。
- Agent 分析、实施限定修复、审查提交，提供完整部署验收命令；服务器由用户复制命令执行。默认中文。
- 本地 `main` 串行普通提交和 push；无 feature branch、worktree、detached HEAD、PR、amend、rebase、force push。
- 不制作 Linux 验证包或单独测试镜像；本地缺 Linux Python 3.13、Node 20 或 ShellCheck 只记录未测，不单独阻止 push。
- 不删除、移动或提交服务器既有 untracked `.env` 备份、日志、`VERSION` 和运行目录。
- Golden appliance 已冻结且 `.git` 已故意删除；禁止访问或修改 Golden。

## ISP 与 Hillstone HA 冻结模型

ISP 已多轮生产验证，除真实生产故障外不重新审查或重构。

| 地址 | 职责 |
|---|---|
| `192.168.9.1` | HA logical VIP；ISP/WAN authoritative inventory source；LibreNMS ping-only |
| `192.168.9.11`、`192.168.9.12` | 物理 HA 单元；full SNMP / health / uptime |

对应生产非凭据配置：

```ini
FIREWALL_PING=192.168.9.1
FIREWALL_SNMP_TARGETS=192.168.9.1
FIREWALL_UNIT_SNMP_TARGETS=192.168.9.11,192.168.9.12
```

`FIREWALL_SNMP_COMMUNITY` 的实际值保留在服务器配置，不把交接中的真实采集凭据复制进 Git。

- 历史生产：自动发现/普通大屏 5 个 ISP；Tournament 为 2、2、1，共 3 页。
- Canonical telemetry identity 为 `metric_target + metric_ifindex`；`display_name` 仅显示；`metric_name` 用于原生标签、诊断和旧数据兼容。
- `targets[]` 是可选 availability target；无 gateway 的 WAN 也必须保留 inventory。
- Canonical AUTO flag 为 `ISP_GATEWAY_AUTO_DISCOVER`；`BIGSCREEN_ISP_AUTO_DISCOVER` 为兼容 alias，值冲突必须失败。
- AUTO=true 时，`BIGSCREEN_ISP_NAMES` 和 `BIGSCREEN_ISP_IPS` 仅作 metadata。

## 公司设备删除安全

```ini
DEVICE_PENDING_DELETE_ENABLED=true
DEVICE_AUTO_DELETE_ENABLED=true
DEVICE_AUTO_DELETE_DRY_RUN=true
DEVICE_AUTO_DELETE_DRY_RUN_NOTIFY=false
```

未获用户明确批准，禁止设置 `DEVICE_AUTO_DELETE_DRY_RUN=false`。不执行真实生产设备 DELETE，不为测试主动发送飞书消息。
部署前核对 `.env` 与 Compose 渲染值，部署后核对 Bridge 运行时值；不能只看示例配置。

## 已完成并由用户报告生产验收的批次

| 批次 | 提交 | 已确认行为与证据摘要 |
|---|---|---|
| Batch 2 | `e5363d72fbb843397319041f6f921e077ee2df71` | Pending Delete ONLINE/OFFLINE/UNKNOWN，UNKNOWN 保留 pending 并拒删、在线证据优先；凭据脱敏；精确忽略实际 event-config 及指定备份。部署、Bridge SHA、ready、四开关、configured 通过。 |
| Batch 3 | `32d91bfe7601ed97c1c5e5e7e4d9e5d47779edd8` | deploy/apply 只接受本轮新任务容器 exited + ExitCode=0；Docker 调用共享绝对期限/隔离 stdin，镜像准备后启动运行时预算；Bridge、watcher、Feishu capability、token availability 纳入验收；禁用 profile 时只清理本 Compose 的 sidecar；legacy rule 删除真实成功才报告。网页 Apply、Bootstrap 33/33、Configured 39/39 通过。 |
| Batch 4.1 | `32763ef1ec91b591ab5edb9ea0e31cec4e6c2f97` | CIDR 分配前计算 host 上限，最后 host 使用地址算术，排除地址后惰性读取至 max_hosts + 1，额外地址只判断溢出。源码 SHA、目标生成、Configured 39/39、网页通过；当时无在线选手，生成 0 target 正常。 |
| Batch 4.2 | `665baf4e6d1ba9023d4715c88cf0a955e9d44251`、`1d972601f3778de6694a974c18709ff372b5ef9f` | 事故存储有界、事务候选和安全异常，见下文。事故文件部署前后 missing/0 条；Configured 39/39、5 个 ISP、网页正常。 |

Batch 4.2 持久化约束：读取最多 `16 MiB + 1`；1000 条事故、每条 200 events、单条 64 KiB、文件 16 MiB；UTF-8 计算与实际序列化一致。
损坏 JSON、UTF-8、非列表根节点和不安全历史明确失败；PATCH 状态与 event 一起成功或失败。
旧超限数据不裁剪/删除，允许对应维度不增长或缩小；容量错误 HTTP 409，损坏存储 HTTP 500。
mkdir/write/replace 的 OSError 转为安全错误，保留 `require_write()` 权限语义。

## Batch 5.1 验收补充

[Batch 5.1](iterations/batch-5.1.md) **已收口**。2026-09-06 收到用户输出：实现提交 `675409458a02ac21d478aa01634d71b84f981dd5` 部署成功，Bootstrap 33/33、Configured 39/39、5 个新鲜 ISP、三个 JS 及缓存检查、三层四开关均 PASS。
随后服务器 main 快进至测试补丁 `8135c8542bc7c98076961959aae00ff586fb2ca3`，没有运行代码变化，无需再部署；已有未跟踪生产文件保留。
同日用户明确确认网页登录等待、退出等待后重登、页面切换、配置草稿、事故区域和五个 ISP 的检查全部通过，满足本批次收口条件。后续没有新证据不重新审计该批次。

## Batch 5.2 收口补充

[Batch 5.2](iterations/batch-5.2-proposal.md) 已完成本地实现：事故分析面板使用页面生命周期代次和面板内请求序号共同控制 UI 提交权，同页只允许最新请求的成功或失败更新视图。旧请求不强制取消，可自然完成但不能进入分析、覆盖页面或展示错误。

本地行为测试覆盖旧成功/失败与新成功/失败的乱序组合、最新请求等待状态、单个慢请求和原 stop/restart 契约。用户于 2026-09-06 明确确认请求乱序修复、GitHub CI、生产部署、服务器 39/39、从首页进入实际页面均 PASS，Blocking finding NONE，Batch 5.2 正式收口。已知 P3 `/incident` 直接刷新问题 DEFERRED，不继续处理。

## 当前主线

按用户明确恢复的顺序：Feishu EVENT_NAME shared-group isolation → pre-refactor 只读审计 → 无剩余 P0/P1 blocker 后停止扩大 correctness 修复 → behavior-preserving refactor。
在 ce36e96 上只读审计确认空 EVENT_NAME 仍会放行群命令；[最小隔离方案](iterations/feishu-event-name-isolation.md) 已形成，尚未实施。群轮询和长连接群消息需统一拒绝空名称，明确 p2p 路径保留既有兼容；不修改冻结 ISP、已收口批次、P3 刷新或设备删除逻辑。
