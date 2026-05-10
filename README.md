# monitor-autoconfig

Docker Compose 一键部署的赛事网络监控栈，**Zabbix + LibreNMS + Prometheus + Grafana** 自动发现 + 模板化告警，专为短期出差赛事设计：clone → 改 IP → 起服务 → 自检 → 用一阵子 → 拆机回收。

## 服务

| 服务 | 默认端口 | 用户 / 密码 | 用途 |
|---|---|---|---|
| Grafana | 3000 | admin / root | 大屏 dashboard，比赛中实时看 |
| Zabbix | 8001 | Admin / zabbix | 防火墙告警 + 飞书推送 |
| LibreNMS | 8002 | admin / admin | 交换机自动发现 + 拓扑图 |

## ⚠️ 安全提醒

上表里的默认账户密码**只适合内网临时使用**。任何对外网络可达、需要存活超过一场赛事、或共享给他人的部署，都必须改：

| 服务 | 默认 | 改在哪 |
|---|---|---|
| Grafana 管理员 | `admin / root` | `.env` 里 `GRAFANA_PASSWORD`（首次起服务前改） |
| LibreNMS 管理员 | `admin / admin` | `.env` 里 `LIBRENMS_ADMIN_PASSWORD` |
| Zabbix 管理员 | `Admin / zabbix` | Zabbix UI 首次登录后立即改（`.env` 不管 Zabbix 默认账户） |
| SNMP community | `global` | `.env` 里 `SNMP_COMMUNITY` + 交换机 / 防火墙 SNMP 配置同步改 |

MariaDB 内部账户（`mysql root` 等）只在容器网络内可达，不暴露到宿主机端口，可以维持默认。

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

## 二、部署 checklist

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

# 防火墙 SNMP
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
chmod +x deploy.sh
./deploy.sh
```

首次 5-8 分钟（拉镜像 + DB 初始化 + 自动配置）。`deploy.sh` 会先串行拉镜像并自动重试，避免 Docker Hub / CDN 偶发 502 导致整次部署中断。

### 4. 跑赛前自检

```bash
./pre-match-check.sh
```

输出每条监控链路的状态：容器、Prometheus 抓取目标、设备 ping、选手 targets 注册情况、ISP 链路检测、Grafana 加载情况。绿色=OK，红色=要解决。

## 三、常见问题

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
./deploy.sh
```

## 四、赛后清理

赛事结束、服务器要回收（或换给别人用）。Docker 的两种数据存法要分开清：

**容器 + 镜像 + 命名卷**（grafana-data、prometheus-data、player-targets-data）：

```bash
docker compose down -v        # 停容器、删 monitor 网络、删命名卷
docker system prune -a        # 删镜像（占空间最多）
```

**bind mount 目录**（mysql、zabbix、librenms 数据落在仓库目录里）——`down -v` 不会删，要手动：

```bash
sudo rm -rf mysql-data zabbix-server-data \
            librenms-data librenms-db-data \
            librenms-rrdcached-journal
```

要 `sudo` 是因为 `init-permissions` 容器把这些目录 chown 给了容器内 UID（grafana=472、zabbix=1997 等），普通用户删不掉。

**仓库本身也删**：

```bash
cd .. && rm -rf monitor-autoconfig
```

到这一步服务器上完全没痕迹，可以放心给别人。

数据要带走的话先备份再清：

```bash
tar czf monitor-backup-$(date +%Y%m%d).tar.gz \
  mysql-data/ zabbix-server-data/ \
  librenms-data/ librenms-db-data/ \
  .env grafana-provisioning/
```

注意 `prometheus-data/` 和 `grafana-data/` 是命名卷，不在仓库目录里——Prometheus 数据短期赛事不值得带（默认 15d 保留），Grafana dashboard 都在 git 里随 `grafana-provisioning/` 走。

下次新场子直接 `git clone` + `cp .env.example .env` 重头来。
