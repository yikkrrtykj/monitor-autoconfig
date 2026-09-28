# 项目长期上下文

维护日期：2026-09-06。来源：用户项目交接文本、后续服务器执行输出，以及本地 Git/目录核对。
下述生产结论来自用户交接或执行反馈；Agent 未连接公司服务器，也未独立重新做生产验收。

## 环境与协作

- 仓库：`https://github.com/yikkrrtykj/monitor-autoconfig`。
- 本地主检出：`C:\Users\31313\Documents\monitor-autoconfig-main`。
- 公司服务器仓库：`/root/monitor-autoconfig`；部署目录：`/root/monitor-autoconfig/librenms+grafana`。
- 平台组合：LibreNMS、Prometheus、Grafana、8088 大屏/控制台、Python API、飞书 Bridge、Docker Compose。
- Agent 分析、实施限定修复、审查提交，提供完整部署验收命令；服务器由用户复制命令执行。默认中文。
- 当前代码更新使用 Issue + `codex/` 分支 + PR，详见 [Git 工作流](GIT_WORKFLOW.md)；历史直接 push main 的记录见 [原文归档 #44](https://github.com/yikkrrtykj/monitor-autoconfig/issues/44)。禁止 amend、rebase 和 force push。
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

## 稳定的事故存储约束

读取最多 `16 MiB + 1`；1000 条事故、每条 200 events、单条 64 KiB、文件 16 MiB；UTF-8 计算与实际序列化一致。损坏 JSON、UTF-8、非列表根节点和不安全历史明确失败；PATCH 状态与 event 一起成功或失败。旧超限数据不裁剪/删除，允许对应维度不增长或缩小；容量错误 HTTP 409，损坏存储 HTTP 500。mkdir/write/replace 的 OSError 转为安全错误，保留 `require_write()` 权限语义。

## 历史证据与活跃任务

迁移前本文件原文见 [归档 Issue #44](https://github.com/yikkrrtykj/monitor-autoconfig/issues/44)。历史 Batch 5.1、5.2 的验收记录分别见 [#21](https://github.com/yikkrrtykj/monitor-autoconfig/issues/21)、[#22](https://github.com/yikkrrtykj/monitor-autoconfig/issues/22)；其余迭代原文见 #18–#24。当前任务、TODO 和生产状态以 [开放 Issues](https://github.com/yikkrrtykj/monitor-autoconfig/issues) 为准。A-02.1 尚待生产验收，见 [#13](https://github.com/yikkrrtykj/monitor-autoconfig/issues/13)。
