# 赛事网络监控

一套面向电竞现场的网络监控平台：LibreNMS 负责设备和端口，Prometheus/Grafana 负责采集和图表，8088 大屏负责现场看板、基础配置、问题清单、事故记录和交换机配置巡检。

## 入口

| 服务 | 默认地址 | 用途 |
|---|---|---|
| 大屏 / 控制台 | `http://服务器IP:8088` | 现场展示，`/control` 做配置、问题清单和事故记录 |
| Grafana | `http://服务器IP:3000` | 查图、查历史、临时排障 |
| LibreNMS | `http://服务器IP:8002` | 设备发现、端口流量、告警规则 |

默认账号：

| 服务 | 默认账号 |
|---|---|
| Grafana | `admin / root` |
| LibreNMS | `admin / librenms123` |
| 赛事控制台 | `admin / global`，首次登录后必须改密码 |

正式使用前请修改 `.env` 里的 `GRAFANA_PASSWORD`、`LIBRENMS_ADMIN_PASSWORD`、`SNMP_COMMUNITY`。8088 控制台有登录，但仍建议只在内网或 VPN 后访问。

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
| 需要关注 | 只列出当前异常或缺失项，不再显示分数 |
| 基础配置 | 直接修改赛制、VLAN、SNMP、设备、ISP、UniFi、飞书等配置 |
| 配置中心 | 当前赛制、网络、平台 API、选手探测目标等只读状态 |
| 拓扑诊断 | 检查核心、防火墙、接入交换机、LLDP 边 |
| 事故流转 / 事故库 | 记录事故、恢复时间和复盘线索 |
| 离线部署包 | 查看离线镜像、文件和安装脚本清单 |
| 交换机配置巡检 | 粘贴 Cisco `show run` 片段，检查现场风险 |

基础配置按钮：

| 按钮 | 作用 |
|---|---|
| 验证 | 只检查配置，不写文件 |
| 保存 | 写入 `event-config.yml` |
| 应用配置 | 写入 `event-config.yml` 并根据基础配置生成 `.env` |
| 回滚 | 恢复上一次配置 |
| 导入 | 导入赛事配置包里的 YAML/JSON |
| 导出包 | 下载当前配置、事故和部署清单 |

点 `应用配置` 后，配置文件已经写好。当前版本如果要让已启动容器立刻读取新的 `.env`，在服务器执行：

```bash
docker compose up -d --force-recreate platform-api bigscreen prometheus blackbox-exporter snmp-exporter alertmanager-feishu-bridge librenms-config
```

## 基础配置怎么填

| 字段 | 建议 |
|---|---|
| 默认赛制 | 控制 `/control` 里默认座位布局 |
| SNMP Community | 交换机、防火墙、LibreNMS/Prometheus 采集共用，默认 `global` |
| 选手 VLAN / 无线 VLAN | 默认 `40 / 41` |
| 选手网关 | 留空时复用核心 IP |
| 交换机管理网段 | 给 LibreNMS 自动发现用，例如 `192.168.10.0/24` 或 `192.168.10.1-100,192.168.10.254` |
| 防火墙管理网段 | 默认 `192.168.9.0/24`；需要发现其它防火墙管理地址时再改 |
| 核心 IP | 三层核心或网关交换机管理 IP |
| 防火墙 IP | 同时用于判断防火墙在线和 WAN 流量 SNMP；多个 IP 用逗号或换行，不用 `/` |
| 物理防火墙 SNMP IP | HA 两台物理机分别采集时填写，多个 IP 用逗号或换行 |
| 舞台交换机 | 默认 `192.168.10.11-14`，用于选手识别和大屏选手监控 |
| 其它接入交换机 | 不参与选手识别，只用于在线、拓扑和发现 |
| 服务器 | 默认空；需要监控游戏服务器时再添加名称和 IP |
| ISP 自动发现 | 通过防火墙 SNMP 的 WAN 口名称/描述识别运营商链路 |
| WAN 口识别关键词 | 自动发现 WAN 口时匹配接口名/描述，默认 `telecom,telcom,unicom,isp,WAN` |
| 外网网关探测地址 | 建议填运营商外网网关，用于 ISP 丢包/掉线告警 |
| 运营商公网 IP | 可选；用于拓扑展示，也会作为 ping-only 设备加入 LibreNMS |
| 未填带宽时按 Mbps | 链路没有单独带宽时用于饱和判断；可留空，内部默认 1000；饱和阈值默认 90% |
| 单链路带宽 | 优先级高于默认带宽 |
| UniFi | 使用 UniFi AP 时填控制器地址和只读账号 |
| 飞书机器人 Token | 留空则不推飞书；多台监控可以复用同一个 token，但会推到同一个群且可能重复告警 |

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
