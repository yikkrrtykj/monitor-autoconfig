## monitor-autoconfig
安装后修改Prometheus.yml的外网IP

# Ubuntu安装

```
sudo curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh && sudo apt update && sudo apt install -y docker-compose-plugin git && sudo usermod -aG docker $USER && mkdir -p mysql-data zabbix-server-data grafana-data prometheus-data && sudo chown -R 999:999 mysql-data && sudo chown -R 1997:1997 zabbix-server-data && sudo chown -R 472:472 grafana-data && sudo chown -R 65534:65534 prometheus-data && newgrp docker && sudo usermod -aG docker $USER
```


# centos安装

```
sudo yum install -y yum-utils && sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo && sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin git && sudo systemctl start docker && sudo systemctl enable docker && sudo usermod -aG docker $USER
```


# 安装地址

```
wget https://github.com/yikkrrtykj/monitor-autoconfig/releases/download/v1.0/zabbix+prometheus+grafana.tar.gz
```

# docker部署
```
docker compose up -d
```
