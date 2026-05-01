# monitor-autoconfig

Docker Compose 一键部署的赛事网络监控栈，**Zabbix + LibreNMS + Prometheus + Grafana** 自动发现 + 模板化告警，专为短期出差赛事设计：clone → 改 IP → 起服务 → 自检 → 用一阵子 → 拆机回收。

## 服务

| 服务 | 默认端口 | 用户 / 密码 | 用途 |
|---|---|---|---|
| Grafana | 3000 | admin / root | 大屏 dashboard，比赛中实时看 |
| Zabbix | 8001 | Admin / zabbix | 防火墙告警 + 飞书推送 |
| LibreNMS | 8002 | admin / admin | 交换机自动发现 + 拓扑图 |

## Dashboard 列表（Grafana → Network 文件夹）

| Dashboard | 用什么时候看 |
|---|---|
| Event Infrastructure | 赛前 / 平时——设备 ping + ISP 上下行流量 + 丢包热力图 |
| Match 5v5 | 2 队 × 5 人对战（5v5、王者荣耀类） |
| Tournament 6 队 | 6 队比赛（3 人/队 / 4 人/队，三角洲、CS 类） |
| Tournament 64 (2 层) | 16 队 × 4 人，舞台 2 层 4-4/4-4 |
| Tournament 64 (3 层 233) | 16 队 × 4 人，舞台 3 层下窄上宽 |
| Tournament 64 (3 层 332) | 16 队 × 4 人，舞台 3 层下宽上窄 |

## 一、装 Docker

### Ubuntu

```bash
curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh
sudo apt update && sudo apt install -y docker-compose-plugin git
sudo usermod -aG docker $USER && newgrp docker
```

### CentOS

```bash
sudo yum install -y yum-utils git
sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker
```

## 二、部署 checklist（每次新机都跑一遍）

### 1. 拉代码

```bash
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git
cd monitor-autoconfig/zabbix+librenms+grafana
```

### 2. 写 `.env`

```bash
cp .env.example .env
vi .env
```

**必改项**：

```bash
SERVER_IP=                 # 监控服务器自身 IP
LIBRENMS_BASE_URL=http://${SERVER_IP}:8002

# 基础设施 ping（Name:IP 格式，逗号分隔，支持 1-10 范围）
CORE_SWITCH_PING=Core:192.168.10.254
DIST_SWITCH_PING=SW1:192.168.10.11,SW2:192.168.10.12
FIREWALL_PING=FW1:192.168.1.1,FW2:192.168.1.2
SERVER_PING=Server:192.168.10.10

# 防火墙 SNMP（HA 两台都写）
FIREWALL_SNMP_TARGETS=192.168.1.1,192.168.1.2

# LibreNMS 自动发现
LIBRENMS_DISCOVERY_TARGETS=192.168.10.1-100,192.168.10.254
LIBRENMS_CORE_IP=192.168.10.254
SWITCH_DISCOVERY_RANGE=192.168.10.1-100,192.168.10.254
```

**赛事监控用**（不接选手就留空）：

```bash
TOURNAMENT_SWITCHES=192.168.10.11,192.168.10.12   # 选手接入交换机 IP
PLAYER_SUBNETS=192.168.11.0/24                    # 选手有线网段
WIRELESS_SUBNETS=192.168.12.0/24                  # 选手无线（备用，不用就留空）
```

### 3. 起服务

```bash
docker compose up -d
docker compose ps
```

首次 5-8 分钟（拉镜像 + DB 初始化 + 自动配置）。

### 4. 跑赛前自检

```bash
./pre-match-check.sh
```

输出每条监控链路的状态：容器、Prometheus 抓取目标、设备 ping、选手 targets 注册情况、ISP 链路检测、Grafana 加载情况。绿色=OK，红色=要解决。

### 5. 浏览器访问

`http://${SERVER_IP}:3000` Grafana → Network 文件夹 → 挑对应比赛形态的 dashboard。

## 三、选手监控接线约定

`generate-player-targets.py` 通过 SNMP walk 读交换机端口 `ifAlias`（端口描述），按 `team\d+[-_]\d+` 正则解析 `team` 和 `seat` 标签。

**约定**：交换机端口描述写成 `teamNN-MM`，编号**从下往上、从左往右**递增。

例：64 人 2 层布局，舞台左下第 1 队的 4 个选手插在交换机的 4 个端口：
```
端口 1 ifAlias = team01-01
端口 2 ifAlias = team01-02
端口 3 ifAlias = team01-03
端口 4 ifAlias = team01-04
```

3 层布局的 team 编号映射：

| 布局 | 顶层 | 中层 | 底层 |
|---|---|---|---|
| 2 层 (4-4/4-4) | T9-12 ︱ T13-16 | — | T1-4 ︱ T5-8 |
| 3 层 233 (下→上 2-3-3) | T11-13 ︱ T14-16 | T5-7 ︱ T8-10 | T1-2 ︱ T3-4 |
| 3 层 332 (下→上 3-3-2) | T13-14 ︱ T15-16 | T7-9 ︱ T10-12 | T1-3 ︱ T4-6 |

无线选手（备用）：把无线 AP 接入网段写到 `.env` 的 `WIRELESS_SUBNETS`。dashboard 顶部"网络"选择器切到"无线"或"全部"看。

## 四、防火墙 ISP 接口约定

防火墙 SNMP 抓 `ifHCInOctets` / `ifHCOutOctets` 算上下行流量。Dashboard 自动按 `ifAlias` 包含 `telecom|telcom|unicom|isp|wan` 关键词（不区分大小写）筛选 WAN 口。

**约定**：在防火墙上把 WAN 口的 description / ifAlias 改成包含 ISP 名字的字符串：

| 例子 | 匹配关键词 |
|---|---|
| `ISP-Telecom-100M` | telecom + isp |
| `Unicom-WAN-Backup` | unicom + wan |
| `WAN1-Telcom` | wan + telcom |

每条 ISP 一个独立 panel 显示上下行，最多自动平铺 4 条。

## 五、网段变更（赛后换场）

新场地换网段，改 `.env`：

```bash
SERVER_IP=10.10.20.10
CORE_SWITCH_PING=Core:10.10.20.254
DIST_SWITCH_PING=SW1:10.10.20.11,SW2:10.10.20.12
FIREWALL_PING=FW1:10.10.20.1
LIBRENMS_DISCOVERY_TARGETS=10.10.20.1-100,10.10.20.254
LIBRENMS_CORE_IP=10.10.20.254
SWITCH_DISCOVERY_RANGE=10.10.20.1-100,10.10.20.254
```

应用：

```bash
docker compose up -d --force-recreate prometheus librenms librenms-dispatcher librenms-config zabbix-config player-targets
```

## 六、自动配置说明

### Zabbix
`zabbix-config` 容器自动：修复 Agent localhost 连接、创建主机和 SNMP 接口、导入 WatchGuard / Hillstone 防火墙模板、配置飞书告警机器人（`feishu-robot.py`，token 在 Zabbix 媒介里设）。

### LibreNMS
`librenms-config` 容器自动：启 dispatcher / rrdcached、创建 admin 用户和 API Token、按 `LIBRENMS_DISCOVERY_TARGETS` 自动发现设备、配置 2 条告警规则（**设备离线告警** critical + **高丢包告警** warning，丢包 > 10%）。**不接通知渠道**——告警只在 LibreNMS UI 显示，飞书告警走 Zabbix 那条线。

### Prometheus / Grafana
Prometheus 抓基础设施 ping + 防火墙 SNMP（64-bit 计数器）+ 选手 ping（file_sd 自动同步）。Grafana 通过 file provisioning 自动加载 dashboard，每 30 秒重载。

## 七、常见问题

**服务起不来 / 一直重启**
```bash
docker compose ps
docker compose logs --tail=100 <service-name>
```

**Zabbix Web 502 / 连不上**
等 mysql + zabbix-server healthy。首次 2-3 分钟。

**LibreNMS 显示发现 0 个设备**
```bash
docker exec librenms snmpwalk -v2c -c global 192.168.10.254 sysName.0
```
不通 = 防火墙策略 / community 错 / 设备没开 SNMP。

**选手 dashboard 全是 No data**
1. `./pre-match-check.sh` 看选手 targets 注册了多少
2. 0 个 = 检查 `TOURNAMENT_SWITCHES` 配了没 + 交换机端口 ifAlias 是否按约定命名
3. 注册了但都离线 = 选手电脑 / 手机没接好

**改了 .env 后某些数据不更新**
- 改 IP / community / 选手网段 → 重启 prometheus 和 player-targets
- 改 dashboard JSON → Grafana 30 秒自动 reload，或 `docker compose restart grafana`

**重置所有数据从头开始**
```bash
docker compose down -v
rm -rf mysql-data zabbix-server-data grafana-data librenms-db-data librenms-data librenms-rrdcached-journal prometheus-data
docker compose up -d
```

## 八、赛后清理

赛事结束、服务器要回收：
```bash
docker compose down -v        # 停服务并删数据卷
docker system prune -a        # 清镜像（如果服务器还要给别人用）
```

数据要带走的话，先备份：
```bash
tar czf monitor-backup-$(date +%Y%m%d).tar.gz \
  mysql-data/ grafana-data/ librenms-data/ librenms-db-data/ prometheus-data/ .env
```

下次新场子直接 `git clone` + `cp .env.example .env` 重头来。
