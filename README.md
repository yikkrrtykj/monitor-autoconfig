# 赛事网络监控

用来查看现场交换机、防火墙、AP 和选手网络的状态，也可以收飞书告警、查历史流量和记录事故。

第一次安装按下面做；已经装好的服务器看 [更新说明](docs/INSTALLATION.md#更新)。只想找网页入口，可以直接看 [打开网页](#打开网页并填写现场配置)。

## 安装前准备

- 一台 Linux x86_64 服务器，建议至少 4 核、8 GB 内存、100 GB 磁盘。
- 以下命令在 Debian/Ubuntu 的 **root Bash 终端**执行，例如 Ubuntu 24.04。其它系统看 [Docker 安装说明](https://docs.docker.com/engine/install/)。
- 服务器能访问交换机管理地址；交换机已启用 SNMP，团体字与平台配置一致。只开了 ping，不能读取名字、接口和接线。

**能直接下载软件，选第一种；需要 Clash，选第二种。每一步成功后再继续。** 已有 Docker 不必重装；已有项目不要重新 clone，也不要覆盖 `.env` 和 `event-config.yml`。

## 第一种：服务器能直接访问外网

安装 Docker、Compose、git 和 Python：

```bash
curl -fL --connect-timeout 10 --max-time 120 \
  https://get.docker.com -o /tmp/get-docker.sh &&
test -s /tmp/get-docker.sh &&
bash /tmp/get-docker.sh &&
systemctl daemon-reload &&
systemctl enable --now docker &&
docker --version &&
docker compose version &&
apt-get -o APT::Update::Error-Mode=any update &&
apt-get install -y git python3
```

第一次下载项目并启动：

```bash
cd /root &&
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git &&
cd monitor-autoconfig/librenms+grafana &&
cp .env.example .env &&
cp event-config.example.yml event-config.yml &&
chmod +x *.sh &&
./deploy.sh
```

看到 `Platform bootstrap completed successfully` 后，继续 [打开网页](#打开网页并填写现场配置)。如果下载被重置或超时，改用下一种方式。

## 第二种：通过 Clash 下载

电脑和服务器要能互相访问。Clash 开启 **Allow LAN / 允许局域网连接**，找到 HTTP 或 Mixed 端口。下面的 `192.168.1.100:7890` 是例子，所有出现这个地址的地方都换成你的电脑 IP 和端口。

### 1. 设置终端代理，再安装软件

```bash
export http_proxy="http://192.168.1.100:7890"
export https_proxy="$http_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$http_proxy"
export no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="$no_proxy"

curl -fL --connect-timeout 10 --max-time 120 \
  https://get.docker.com -o /tmp/get-docker.sh &&
test -s /tmp/get-docker.sh &&
bash /tmp/get-docker.sh &&
systemctl daemon-reload &&
systemctl enable --now docker &&
docker --version &&
docker compose version &&
apt-get -o APT::Update::Error-Mode=any update &&
apt-get install -y git python3
```

这里 `https_proxy` 也用 `http://`。不要填电脑的 `127.0.0.1`，也不要用 Clash 的控制端口或纯 SOCKS 端口。换终端后需要重新设置代理。

### 2. 给 Docker 设置下载代理

Docker 不会自动使用上面的终端代理，还要单独设置。这一步会重启 Docker；已有服务时，选允许重启的时间操作。如果这个配置文件已经存在，先备份并修改原文件，不要直接覆盖。[Docker 代理说明](https://docs.docker.com/engine/daemon/proxy/)

```bash
mkdir -p /etc/systemd/system/docker.service.d &&
cat > /etc/systemd/system/docker.service.d/http-proxy.conf <<'EOF'
[Service]
Environment="HTTP_PROXY=http://192.168.1.100:7890"
Environment="HTTPS_PROXY=http://192.168.1.100:7890"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
EOF
systemctl daemon-reload &&
systemctl restart docker &&
docker pull hello-world
```

### 3. 下载项目并构建

```bash
cd /root &&
git clone https://github.com/yikkrrtykj/monitor-autoconfig.git &&
cd monitor-autoconfig/librenms+grafana &&
cp .env.example .env &&
cp event-config.example.yml event-config.yml &&
chmod +x *.sh &&
docker compose --progress plain build \
  --build-arg HTTP_PROXY=http://192.168.1.100:7890 \
  --build-arg HTTPS_PROXY=http://192.168.1.100:7890 \
  --build-arg http_proxy=http://192.168.1.100:7890 \
  --build-arg https_proxy=http://192.168.1.100:7890
```

构建时下载软件也需要代理，否则 `apk`、`apt`、`pip` 可能反复重试。不要加 `--no-cache`，已完成的部分可以复用。[Docker 构建代理说明](https://docs.docker.com/build/building/variables/#proxy-arguments)

### 4. 清掉终端代理，再启动

构建成功后执行。这样本机检查不会绕到 Clash，也不会取消 Docker 的下载代理。

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="$no_proxy"
./deploy.sh
```

看到 `Platform bootstrap completed successfully` 后，继续下面的网页配置。

## 打开网页并填写现场配置

| 页面 | 地址 | 用来做什么 |
|---|---|---|
| 控制台 | `http://服务器IP:8088/control` | 填现场配置、查问题、记事故 |
| 大屏 | `http://服务器IP:8088` | 看网络状态和拓扑 |
| Grafana | `http://服务器IP:3000` | 看流量和历史图表 |
| LibreNMS | `http://服务器IP:8002` | 看设备、接口和采集情况 |

控制台、Grafana 和 LibreNMS 的默认账号都是 `admin`，默认密码都是 `global123!@#`。需要修改时见 [账号说明](docs/REFERENCE.md#账号和访问)。这些页面供现场内网使用，不要直接开放到公网。

在控制台的“基础配置”中填写：

1. 赛事名称、核心 IP、交换机管理网段和 SNMP 团体字。
2. 赛事交换机及选手网段；赛事交换机只填承载选手电脑的设备。
3. 按需填写防火墙、UniFi 和飞书；暂时不用的项目可以留空。
4. 点击 **应用配置**。只点“保存”不会让服务读取新配置。

首次安装的示例没有现场设备，先不显示设备是正常的。填好并应用后，在服务器检查：

```bash
cd /root/monitor-autoconfig/librenms+grafana &&
./deploy-check.sh configured &&
./pre-match-check.sh
```

没有配置 UniFi 或飞书时，对应的 `SKIP` 正常。具体字段、DHCP 页面和测速工具看 [功能与配置参考](docs/REFERENCE.md)。

## 常见问题

**Docker 提示 `docker.service does not exist`**

前面的安装没成功。先处理下载或安装报错，反复启动服务解决不了。阿里云源报 `File has unexpected size` 时，看 [软件源恢复](docs/INSTALLATION.md#阿里云软件源报-file-has-unexpected-size)。

**容器都在运行，最后检查却全部超时**

先清掉当前终端代理，再检查，不用重装：

```bash
cd /root/monitor-autoconfig/librenms+grafana || exit 1
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="$no_proxy"
./deploy-check.sh bootstrap
```

仍失败时，按 [本机服务检查](docs/INSTALLATION.md#容器都启动了但最后检查全部超时) 核对网页和服务。

**交换机在线，却没有名字或接线**

能 ping 通不等于能读取 SNMP。先在交换机执行 `show snmp`；如果提示 `SNMP agent not enabled`，需要启用 SNMP。再核对团体字和访问控制，以及它是否已经进入 LibreNMS。自动添加曾失败时，不能只等 ping 恢复就认定已补加。

**UniFi 的 AP 数量和拓扑数量不一样**

“AP 已定位数/总数”表示找到了接入位置的数量，不是在线数量。没有可信的接入记录、位置有歧义或记录过期时，AP 不会连线。

**更新前提示本地文件有修改**

先看 `git status --short`。不要直接覆盖、清空或删除文件；只有权限变化和有内容变化需要分别处理。已有配置、备份、日志和数据目录都要保留，也不要用 `docker compose down -v`。

## 需要更多说明时

- [完整安装、更新和排障](docs/INSTALLATION.md)：代理、本机健康检查、软件源恢复、端口和配置升级。
- [功能与配置参考](docs/REFERENCE.md)：控制台、DHCP、测速、飞书、多赛事部署和详细采集行为。
- [飞书应用与群排障](librenms+grafana/docs/feishu-app-chat-troubleshooting.md)。
- [开发文档](docs/README.md)与 [AGENTS.md](AGENTS.md)：参与代码维护时再看。

问题和进度记录在 [Issues](https://github.com/yikkrrtykj/monitor-autoconfig/issues)，改动和测试结果在 [PR](https://github.com/yikkrrtykj/monitor-autoconfig/pulls)。
