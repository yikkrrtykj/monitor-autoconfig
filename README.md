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
docker compose up -d
```

## 自动配置

启动后以下配置会自动完成：

- **Zabbix**：添加主机、模板、SNMP 监控、飞书告警
- **LibreNMS**：创建默认管理员、自动发现 SNMP 设备（默认范围：`192.168.10.1-100,192.168.10.254`）、告警规则

查看配置日志：
```bash
docker compose logs zabbix-config
docker compose logs librenms-config
```

## 监控的网络设备

| 设备 | IP | 类型 |
|------|-----|------|
| Core | 192.168.10.254 | 核心交换机 |
| Stage1-6 | 192.168.10.11-16 | 舞台交换机 |
