# monitor-autoconfig

Docker Compose 一键部署的网络监控栈，包含 Zabbix、Grafana、LibreNMS、Uptime Kuma 等服务。

## 服务访问

| 服务 | 访问地址 | 默认用户名 | 默认密码 |
|------|----------|------------|----------|
| Grafana | http://localhost:3000 | admin | root |
| Zabbix Web | http://localhost:8001 | Admin | zabbix |
| LibreNMS | http://localhost:8002 | admin | admin |
| Uptime Kuma | http://localhost:3001 | 需自己创建 | - |

## 安装

### Ubuntu

```bash
sudo curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker-compose-plugin git
sudo usermod -aG docker $USER
newgrp docker
```

### CentOS

```bash
sudo yum install -y yum-utils
sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin git
sudo systemctl start docker && sudo systemctl enable docker
sudo usermod -aG docker $USER
```

## 部署
```bash
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git
cd monitor-autoconfig/zabbix+prometheus+grafana
cp .env.example .env
docker compose up -d
```

如果以后内网换网段，修改 `zabbix+prometheus+grafana/.env`：

```env
SNMP_COMMUNITY=global
LIBRENMS_BASE_URL=http://192.168.10.10:8002
LIBRENMS_DISCOVERY_TARGETS=192.168.10.1-100,192.168.10.254
LIBRENMS_CORE_IP=192.168.10.254
SWITCH_DISCOVERY_RANGE=192.168.10.1-100,192.168.10.254
PROMETHEUS_SNMP_TARGETS=192.168.10.254,192.168.10.11-16
PROMETHEUS_PING_TARGETS=192.168.10.254,192.168.10.11-16
```

例如改成 `10.10.20.0/24`，可以写成：

```env
LIBRENMS_BASE_URL=http://10.10.20.10:8002
LIBRENMS_DISCOVERY_TARGETS=10.10.20.1-100,10.10.20.254
LIBRENMS_CORE_IP=10.10.20.254
SWITCH_DISCOVERY_RANGE=10.10.20.1-100,10.10.20.254
PROMETHEUS_SNMP_TARGETS=10.10.20.254,10.10.20.11-16
PROMETHEUS_PING_TARGETS=10.10.20.254,10.10.20.11-16
```

改完执行 `docker compose up -d --force-recreate librenms librenms-dispatcher librenms-config zabbix-config` 重新应用自动发现配置。
如果 Grafana 的 SNMP / ICMP 面板也要跟着变，修改 `PROMETHEUS_SNMP_TARGETS` 和 `PROMETHEUS_PING_TARGETS`，再执行 `docker compose up -d --force-recreate prometheus`。

## 自动配置

启动后以下配置会自动完成：

- **Zabbix**：添加主机、模板、SNMP 监控、飞书告警
- **LibreNMS**：启动 Web、Redis、dispatcher、rrdcached，创建默认管理员，自动发现 SNMP 设备（默认范围：`192.168.10.1-100,192.168.10.254`），配置告警规则
- **Prometheus**：轻量模式，只为 Grafana 采集核心/舞台交换机 SNMP 和 ICMP 数据

查看配置日志：
```bash
docker compose logs zabbix-config
docker compose logs librenms-config
docker compose logs librenms-dispatcher
```

LibreNMS 首次启动后需要等一个 poller 周期，通常 3-5 分钟。`Mail skipped` 和 Docker 镜像的更新提示可以先不处理；如果 Validate 里还提示 Web Server 地址不正确，检查 `.env` 里的 `LIBRENMS_BASE_URL` 是否就是浏览器访问 LibreNMS 的地址。

## 监控的网络设备

| 设备 | IP | 类型 |
|------|-----|------|
| Core | 192.168.10.254 | 核心交换机 |
| Stage1-6 | 192.168.10.11-16 | 舞台交换机 |
