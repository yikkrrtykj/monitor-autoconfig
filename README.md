# 赛事网络监控

一套面向电竞现场的网络监控平台：LibreNMS 负责设备和端口，Prometheus/Grafana 负责采集和图表，8088 大屏负责现场看板、赛前检查、基础配置、事故记录和交换机配置巡检。

## 入口

| 服务 | 默认地址 | 用途 |
|---|---|---|
| 大屏 / 控制台 | `http://服务器IP:8088` | 现场展示，`/control` 做配置和赛前检查 |
| Grafana | `http://服务器IP:3000` | 查图、查历史、临时排障 |
| LibreNMS | `http://服务器IP:8002` | 设备发现、端口流量、告警规则 |

默认账号：

| 服务 | 默认账号 |
|---|---|
| Grafana | `admin / root` |
| LibreNMS | `admin / librenms123` |

正式使用前请修改 `.env` 里的 `GRAFANA_PASSWORD`、`LIBRENMS_ADMIN_PASSWORD`、`SNMP_COMMUNITY`。8088 默认无登录，不建议直接暴露公网。

## 部署

```bash
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git
cd monitor-autoconfig/librenms+grafana
cp .env.example .env
cp event-config.example.yml event-config.yml
chmod +x *.sh
./deploy.sh
```

启动后检查：

```bash
docker compose ps
./pre-match-check.sh
```

更新测试分支示例：

```bash
git fetch origin
git checkout -B codex/remove-quality-heatmap origin/codex/remove-quality-heatmap
cd librenms+grafana
docker compose up -d
```

国内服务器拉镜像超时，需要给 Docker daemon 配代理：

```bash
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'
[Service]
Environment="HTTP_PROXY=http://代理IP:端口"
Environment="HTTPS_PROXY=http://代理IP:端口"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
EOF
systemctl daemon-reload
systemctl restart docker
```

## 控制台

打开：

```text
http://服务器IP:8088/control
```

主要区域：

| 区域 | 用途 |
|---|---|
| 就绪度 | 把关键检查项汇总成现场分数 |
| 赛前检查 | 看座位、设备、Prometheus targets 是否正常 |
| 基础配置 | 直接修改赛事、VLAN、设备、ISP、UniFi、飞书等配置 |
| 配置中心 | 当前赛制、网络、平台 API、目标文件等只读状态 |
| 拓扑诊断 | 检查核心、防火墙、接入交换机、LLDP 边 |
| 事故流转 / 事故库 | 记录事故、恢复时间和复盘线索 |
| 交付包 | 导出当前配置包或离线部署包线索 |
| 交换机配置巡检 | 粘贴 Cisco `show run` 片段，检查现场风险 |

基础配置按钮：

| 按钮 | 作用 |
|---|---|
| 验证 | 只检查配置，不写文件 |
| 保存 | 写入 `event-config.yml` |
| 应用 | 根据基础配置生成 `.env` |
| 回滚 | 恢复上一次配置 |
| 导入 | 导入赛事配置包里的 YAML/JSON |
| 导出包 | 下载当前配置、事故和交付清单 |

点 `应用` 后，让容器读取新 `.env`：

```bash
./apply-env.sh
```

或者：

```bash
docker compose up -d
```

## 基础配置怎么填

| 字段 | 建议 |
|---|---|
| 赛事名称 | 可留空，正式项目再填 |
| 默认赛制 | 控制 `/control` 里默认座位布局 |
| 公网地址 | 只有做公网反代时才填 |
| 选手 VLAN / 无线 VLAN | 默认 `40 / 41` |
| 选手网关 | 留空时复用核心 IP |
| 核心 IP | 三层核心或网关交换机管理 IP |
| 防火墙管理 IP | 防火墙管理地址或 HA 管理/VIP |
| 防火墙 SNMP 目标 IP | SNMP 采集目标，不是服务器 IP |
| 接入交换机 | 默认 `stage-1` 到 `stage-4`，`192.168.10.11-14` |
| 服务器 | 默认 `game server`，IP 可留空 |
| ISP 自动发现 | 通过防火墙 SNMP/WAN 口关键词发现 ISP 链路 |
| 兜底带宽 Mbps | 单条 ISP 没填带宽时使用这个值 |
| 单链路带宽 | 优先级高于兜底带宽 |
| UniFi | 使用 UniFi AP 时填控制器地址和只读账号 |
| 飞书机器人 Token | 留空则不推飞书 |

## 交换机侧配置

至少要让交换机把 syslog 发到监控服务器：

```text
logging host <监控服务器IP>
logging trap informational
service timestamps log datetime msec
```

接入口建议：

```text
spanning-tree portfast edge
spanning-tree bpduguard enable
storm-control broadcast level 1.00 0.50
storm-control action shutdown
```

核心/上联口不要乱开 BPDU Guard。DHCP Snooping 的 trust 只放 DHCP 服务器方向或上联方向，普通终端/AP 口不要 trust。

## 离线交付

有网络时在一台服务器打包：

```bash
cd librenms+grafana
./offline-package.sh
```

现场离线服务器：

```bash
tar -xf monitor-offline-*.tar.gz
cd monitor-offline-*
./install-offline.sh
docker compose up -d
```

## 常用排障

查看服务：

```bash
docker compose ps
docker compose logs -f platform-api bigscreen prometheus alertmanager-feishu-bridge
```

只重启控制台：

```bash
docker compose up -d --force-recreate platform-api bigscreen
```

旧容器名冲突时，只删容器，不删数据：

```bash
docker rm -f librenms grafana prometheus bigscreen rsyslog blackbox-exporter snmp-exporter loki promtail-syslog alertmanager-feishu-bridge player-targets topology-collector platform-api grafana-setup init-permissions 2>/dev/null || true
docker compose up -d
```

数据目录如 `librenms-data`、`grafana-data`、`librenms-db-data` 不要随便删除。
