# monitor-autoconfig
## 拉取文件后修改Prometheus.yml的监控IP

|    服务    |        访问地址      | 默认用户名 | 默认密码 |
|------------|-----------------------|-----------|-----------|
|   Grafana  | http://localhost:3000 |   admin   |    root   |
| Zabbix Web | http://localhost:8001 |   Admin   |   zabbix  |
| Prometheus | http://localhost:9090 |           |           |
|    ipam    | http://localhost:8002 |           |           |
# Ubuntu安装

```
sudo curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh && sudo apt update && sudo apt upgrade -y && sudo apt install -y docker-compose-plugin git && sudo usermod -aG docker $USER && mkdir -p mysql-data zabbix-server-data grafana-data prometheus-data && sudo chown -R 999:999 mysql-data && sudo chown -R 1997:1997 zabbix-server-data && sudo chown -R 472:472 grafana-data && sudo chown -R 65534:65534 prometheus-data && newgrp docker && sudo usermod -aG docker $USER
```


# centos安装

```
sudo yum install -y yum-utils && sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo && sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin git && sudo systemctl start docker && sudo systemctl enable docker && sudo usermod -aG docker $USER
```


# 安装地址

```
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git
```

# docker部署
```
docker compose up -d
```
