#!/usr/bin/env python3
"""
LibreNMS webhook -> Feishu bot bridge + device-online watcher + syslog watcher.

Stdlib only (http.server + urllib + json + threading + re) so the container
runs on python:3-slim with no requirements.txt.

Env:
  FEISHU_BRIDGE_PORT      listen port (default 5005)
  FEISHU_ROBOT_TOKEN      Feishu bot webhook token
  FEISHU_BRIDGE_DRY_RUN   true = log payloads, never POST to Feishu
  FEISHU_SEND_MAX_ATTEMPTS retry attempts for each Feishu delivery (default 3)
  FEISHU_SEND_RETRY_BASE_SECONDS exponential retry base delay (default 1)
  FEISHU_FAILED_EVENT_RETRY_SECONDS retry delay for queued syslog events (default 30)
  LIBRENMS_URL            LibreNMS internal URL (e.g. http://librenms:8000)
  LIBRENMS_API_TOKEN      LibreNMS API token (falls back to token file)
  LIBRENMS_TOKEN_FILE     path to token file written by librenms-config
                          (default /librenms-data/librenms-api-token)
  SWITCH_WATCH_INTERVAL   seconds between device-list polls (default 120)
  DEVICE_MODEL_WAIT_SECONDS seconds to wait for LibreNMS discovery/inventory
                          before sending a new-device card (default 300)
  PROMETHEUS_URL          Prometheus internal URL (default http://prometheus:9090)
  ISP_ALERT_ENABLED       true = watch firewall WAN bandwidth (default true)
  ISP_ALERT_FOR_SECONDS   seconds above threshold before alerting (default 10)
  ISP_ALERT_POLL_INTERVAL seconds between checks (default 5)
  ISP_ALERT_RATE_WINDOW   Prometheus rate() window (default 1m)
  ISP_ALERT_STATUS_INTERVAL seconds between status logs (default 30)
  ISP_ALERT_RESOLVE_SECONDS seconds below threshold before recovery (default 30)
  ISP_ALERT_SPIKE_IGNORE_FACTOR drop rate samples above capacity x this factor
                          as SNMP counter glitches (default 5, 0 = keep all)
  ISP_DATA_MISSING_ALERT_SECONDS alert after WAN traffic series have been gone
                          this long (default 120, 0 = disable)
  FIREWALL_WAN_IF_FILTER  WAN interface label keywords
  BIGSCREEN_ISP_MAX_BANDWIDTH ISP bandwidth Mbps config
  BIGSCREEN_ISP_IPS     optional ISP display names, NAME:IP comma list
  ISP_PING              ISP ping targets, NAME:IP comma list
  ISP_SATURATION_PERCENT  alert threshold percent of configured bandwidth
  SYSLOG_WATCH_ENABLED    true = watch syslog file for security events (default true)
  SYSLOG_FILE             path to syslog file from rsyslog (default /var/log/remote/syslog.log)
  SYSLOG_EVENT_RATE_LIMIT seconds to suppress duplicate syslog event cards (default 60)
  SYSLOG_ALERT_TYPES      comma list of syslog cards to push
                          (default native_vlan_mismatch,errdisable,bpduguard,loopback)
  SYSLOG_CORRELATION_SECONDS seconds to collapse native-vlan + errdisable on same port
  SYSLOG_RECOVERY_ENABLED true = send recovery cards when an alerted port comes back up
  DEVICE_DOWN_ENABLED     true = watch infra ping targets for down (default true)
  DEVICE_DOWN_FOR_SECONDS seconds unreachable before alerting (default 10)
  ISP_DOWN_FOR_SECONDS    seconds unreachable before ISP ping alerting (default 10)
  DEVICE_DOWN_REQUIRE_SEEN_UP true = alert only after target was discovered/up once
  DEVICE_DOWN_POLL_INTERVAL seconds between probe_success polls (default 1)
  DEVICE_DOWN_SAMPLE_WINDOW_SECONDS recent probe window to catch short flaps (default 5)
  DEVICE_DOWN_JOBS        comma list of Prometheus ping jobs to watch for down
  DEVICE_DOWN_STATE_FILE  persisted active down alerts (default /bridge-state/device-down-alerts.json)
  DEVICE_REENROLL_AFTER_SECONDS offline age at which selected devices auto-retire (default 172800)
  DEVICE_REENROLL_JOBS    comma list of temporary-device jobs (default infra-dist-ping)
  DEVICE_LIBRENMS_SYNC_RETRY_SECONDS retry interval for retire/delete/re-add API calls (default 60)
  DEVICE_ONLINE_FROM_PING true = send online card when a candidate first comes up
                            (default false; SNMP/LibreNMS online cards are preferred)
  DEVICE_AUTO_ADD_FROM_PING true = add newly reachable switch candidates to LibreNMS by SNMP
  DEVICE_AUTO_ADD_SNMP_JOBS comma list of ping jobs that may be SNMP devices
  SNMP_COMMUNITY           default SNMP v2c community for auto-add
  UNIFI_AP_SNMP_AUTO_ADD   true = add online UniFi APs to LibreNMS by SNMP
  UNIFI_AP_SNMP_COMMUNITY  optional AP SNMP community (defaults to SNMP_COMMUNITY)
  UNIFI_AP_SNMP_ADD_RETRY_SECONDS seconds before retrying a failed AP add
  UNIFI_CONTROLLER_URL/USER/PASS optional direct UniFi API fallback for AP name/IP
  UNIFI_CONTROLLER_SITES  comma sites or all (default all)
  UNIFI_AP_NAME_SYNC_SECONDS seconds between LibreNMS display-name sync attempts
  INTERCONNECT_ALERT_ENABLED true = watch Port-channel/LAG ifOperStatus
  INTERCONNECT_ALERT_FOR_SECONDS seconds a link must be down before alerting
  INTERCONNECT_ALERT_POLL_INTERVAL seconds between interconnect checks
  INTERCONNECT_ALERT_JOBS comma list of SNMP jobs to watch
  INTERCONNECT_PORT_FILTER comma list of interface keywords/prefixes
"""
from datetime import datetime
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import ssl
import sys
import threading
import time
from urllib import error, parse, request

PORT = int(os.environ.get("FEISHU_BRIDGE_PORT", "5005"))
DRY_RUN = os.environ.get("FEISHU_BRIDGE_DRY_RUN", "").lower() in ("1", "true", "yes", "on")
FEISHU_SEND_MAX_ATTEMPTS = max(1, int(os.environ.get("FEISHU_SEND_MAX_ATTEMPTS", "3")))
FEISHU_SEND_RETRY_BASE_SECONDS = max(0.0, float(os.environ.get("FEISHU_SEND_RETRY_BASE_SECONDS", "1")))
FEISHU_FAILED_EVENT_RETRY_SECONDS = max(1, int(os.environ.get("FEISHU_FAILED_EVENT_RETRY_SECONDS", "30")))

LIBRENMS_URL = os.environ.get("LIBRENMS_URL", "").rstrip("/")
LIBRENMS_API_TOKEN = os.environ.get("LIBRENMS_API_TOKEN", "")
LIBRENMS_TOKEN_FILE = os.environ.get("LIBRENMS_TOKEN_FILE", "/librenms-data/librenms-api-token")
SWITCH_WATCH_INTERVAL = int(os.environ.get("SWITCH_WATCH_INTERVAL", "30"))
DEVICE_MODEL_WAIT_SECONDS = int(os.environ.get("DEVICE_MODEL_WAIT_SECONDS", "300"))
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
ISP_ALERT_ENABLED = os.environ.get("ISP_ALERT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
ISP_ALERT_FOR_SECONDS = int(os.environ.get("ISP_ALERT_FOR_SECONDS", "10"))
ISP_ALERT_POLL_INTERVAL = int(os.environ.get("ISP_ALERT_POLL_INTERVAL", "5"))
ISP_ALERT_RATE_WINDOW = os.environ.get("ISP_ALERT_RATE_WINDOW", "1m")
ISP_ALERT_RESOLVE_SECONDS = int(os.environ.get("ISP_ALERT_RESOLVE_SECONDS", "30"))
ISP_ALERT_STATUS_INTERVAL = int(os.environ.get("ISP_ALERT_STATUS_INTERVAL", "30"))
# 计数器跳变防护：换防火墙/HA 切换/重启会让同一采集 IP 背后的 SNMP 计数器突变，
# rate() 会在整个窗口内算出几十 Gbps 的假速率。超过"配置带宽 x 该倍数"的样本
# 判定为物理上不可能，直接丢弃不参与告警。0 = 关闭防护。
ISP_ALERT_SPIKE_IGNORE_FACTOR = float(os.environ.get("ISP_ALERT_SPIKE_IGNORE_FACTOR", "5") or "0")
# WAN 流量序列整体消失（SNMP 认证不通、换防火墙后接口名不再匹配
# FIREWALL_WAN_IF_FILTER 等）持续该秒数后推数据中断告警。0 = 关闭。
ISP_DATA_MISSING_ALERT_SECONDS = int(os.environ.get("ISP_DATA_MISSING_ALERT_SECONDS", "120"))
FIREWALL_WAN_IF_FILTER = os.environ.get("FIREWALL_WAN_IF_FILTER", "telecom,telcom,unicom,isp,WAN")
BIGSCREEN_ISP_MAX_BANDWIDTH = os.environ.get("BIGSCREEN_ISP_MAX_BANDWIDTH", "1000")
BIGSCREEN_ISP_IPS = os.environ.get("BIGSCREEN_ISP_IPS", "")
ISP_PING = os.environ.get("ISP_PING", "")
ISP_SATURATION_PERCENT = float(os.environ.get("ISP_SATURATION_PERCENT", "90") or "90")
SYSLOG_WATCH_ENABLED = os.environ.get("SYSLOG_WATCH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SYSLOG_FILE = os.environ.get("SYSLOG_FILE", "/var/log/remote/syslog.log")
SYSLOG_EVENT_RATE_LIMIT = int(os.environ.get("SYSLOG_EVENT_RATE_LIMIT", "60"))
# A recurring problem on the same port re-alerts at most once per this window, so
# a flapping BPDU/errdisable/loopback port doesn't spam a card every minute.
SYSLOG_REALERT_SECONDS = int(os.environ.get("SYSLOG_REALERT_SECONDS", "600"))
# A port must stay clear this long before its recovery card is sent; a re-fire
# cancels it. Collapses improved/worsened flapping into one alert + one recovery.
SYSLOG_RECOVER_STABLE_SECONDS = int(os.environ.get("SYSLOG_RECOVER_STABLE_SECONDS", "120"))
SYSLOG_ALERT_TYPES = {
    part.strip().lower()
    for part in os.environ.get(
        "SYSLOG_ALERT_TYPES",
        "native_vlan_mismatch,errdisable,bpduguard,loopback",
    ).split(",")
    if part.strip()
}
SYSLOG_CORRELATION_SECONDS = int(os.environ.get("SYSLOG_CORRELATION_SECONDS", "10"))
SYSLOG_RECOVERY_ENABLED = os.environ.get("SYSLOG_RECOVERY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
DEVICE_DOWN_ENABLED = os.environ.get("DEVICE_DOWN_ENABLED", "true").lower() in ("1", "true", "yes", "on")
DEVICE_DOWN_FOR_SECONDS = int(os.environ.get("DEVICE_DOWN_FOR_SECONDS", "10"))
ISP_DOWN_FOR_SECONDS = int(os.environ.get("ISP_DOWN_FOR_SECONDS", "10"))
# Recovery debounce: only declare a target recovered after it has stayed UP this
# long. A flapping ISP (up/down/up/down) then yields one DOWN card plus a single
# recovery once it is genuinely stable, instead of a card per transition.
# 0 = recover immediately on the first UP (legacy behaviour).
DEVICE_RECOVER_STABLE_SECONDS = int(os.environ.get("DEVICE_RECOVER_STABLE_SECONDS", "0"))
ISP_RECOVER_STABLE_SECONDS = int(os.environ.get("ISP_RECOVER_STABLE_SECONDS", "10"))
DEVICE_DOWN_REQUIRE_SEEN_UP = os.environ.get("DEVICE_DOWN_REQUIRE_SEEN_UP", "true").lower() in ("1", "true", "yes", "on")
# Root-cause suppression: when an upstream switch is down, the devices/APs behind
# it are unreachable too. Instead of a card per victim, alert only the highest
# device whose own uplink is still reachable (real-time, no batching) and fold a
# "下游 N 台同时离线" line into it. Needs the LLDP topology (edges.json) + core IP;
# with neither, nothing is suppressed (fail open -> current behaviour).
DEVICE_DOWN_ROOT_CAUSE_ENABLED = os.environ.get("DEVICE_DOWN_ROOT_CAUSE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
TOPOLOGY_EDGES_FILE = os.environ.get("TOPOLOGY_EDGES_FILE", "/etc/prometheus/targets/topology/edges.json")
LIBRENMS_CORE_IP = os.environ.get("LIBRENMS_CORE_IP", "").strip()
DEVICE_DOWN_POLL_INTERVAL = int(os.environ.get("DEVICE_DOWN_POLL_INTERVAL", "1"))
DEVICE_DOWN_SAMPLE_WINDOW_SECONDS = int(os.environ.get("DEVICE_DOWN_SAMPLE_WINDOW_SECONDS", "5"))
DEVICE_DOWN_JOBS = os.environ.get(
    "DEVICE_DOWN_JOBS",
    "infra-core-ping,infra-dist-ping,infra-fw-ping,infra-fw-unit-ping,infra-isp-ping,infra-srv-ping",
)
# Event access/distribution switches are often deployed for only a few days.
# At 48 hours offline, retire its old outage automatically. If it returns later,
# start a fresh lifecycle and send a new-device card instead of a recovery card.
DEVICE_REENROLL_AFTER_SECONDS = int(os.environ.get("DEVICE_REENROLL_AFTER_SECONDS", "172800"))
DEVICE_REENROLL_JOBS = os.environ.get("DEVICE_REENROLL_JOBS", "infra-dist-ping")
DEVICE_LIBRENMS_SYNC_RETRY_SECONDS = int(os.environ.get("DEVICE_LIBRENMS_SYNC_RETRY_SECONDS", "60"))
DEVICE_ONLINE_FROM_PING = os.environ.get("DEVICE_ONLINE_FROM_PING", "false").lower() in ("1", "true", "yes", "on")
DEVICE_AUTO_ADD_FROM_PING = os.environ.get("DEVICE_AUTO_ADD_FROM_PING", "true").lower() in ("1", "true", "yes", "on")
DEVICE_AUTO_ADD_SNMP_JOBS = os.environ.get("DEVICE_AUTO_ADD_SNMP_JOBS", "infra-core-ping,infra-dist-ping")
SNMP_COMMUNITY = os.environ.get("SNMP_COMMUNITY", "global")
UNIFI_AP_SNMP_AUTO_ADD = os.environ.get("UNIFI_AP_SNMP_AUTO_ADD", "true").lower() in ("1", "true", "yes", "on")
UNIFI_AP_SNMP_COMMUNITY = os.environ.get("UNIFI_AP_SNMP_COMMUNITY", SNMP_COMMUNITY)
UNIFI_AP_SNMP_ADD_RETRY_SECONDS = int(os.environ.get("UNIFI_AP_SNMP_ADD_RETRY_SECONDS", "60"))
UNIFI_CONTROLLER_URL = os.environ.get("UNIFI_CONTROLLER_URL", "").rstrip("/")
UNIFI_CONTROLLER_USER = os.environ.get("UNIFI_CONTROLLER_USER", "")
UNIFI_CONTROLLER_PASS = os.environ.get("UNIFI_CONTROLLER_PASS", "")
UNIFI_CONTROLLER_SITES = os.environ.get("UNIFI_CONTROLLER_SITES", "all")
UNIFI_CONTROLLER_VERIFY_SSL = os.environ.get("UNIFI_CONTROLLER_VERIFY_SSL", "false").lower() in ("1", "true", "yes", "on")
UNIFI_CONTROLLER_REFRESH_SECONDS = int(os.environ.get("UNIFI_CONTROLLER_REFRESH_SECONDS", "10"))
UNIFI_AP_NAME_SYNC_SECONDS = int(os.environ.get("UNIFI_AP_NAME_SYNC_SECONDS", "300"))
INTERCONNECT_ALERT_ENABLED = os.environ.get("INTERCONNECT_ALERT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
INTERCONNECT_ALERT_FOR_SECONDS = int(os.environ.get("INTERCONNECT_ALERT_FOR_SECONDS", "5"))
INTERCONNECT_ALERT_POLL_INTERVAL = int(os.environ.get("INTERCONNECT_ALERT_POLL_INTERVAL", "5"))
INTERCONNECT_ALERT_JOBS = os.environ.get("INTERCONNECT_ALERT_JOBS", "infra-switch-ifmib")
INTERCONNECT_PORT_FILTER = os.environ.get(
    "INTERCONNECT_PORT_FILTER",
    "port-channel,portchannel,po,eth-trunk,bridge-aggregation,bundle-ether,lag,ae,be,trk",
)
BRIDGE_STATE_DIR = os.environ.get("FEISHU_BRIDGE_STATE_DIR", "/bridge-state")
EVENT_ID_FILE = os.environ.get("FEISHU_BRIDGE_EVENT_ID_FILE", os.path.join(BRIDGE_STATE_DIR, "event-id"))
DEVICE_DOWN_STATE_FILE = os.environ.get(
    "DEVICE_DOWN_STATE_FILE",
    os.path.join(BRIDGE_STATE_DIR, "device-down-alerts.json"),
)
DEVICE_ONLINE_STATE_FILE = os.environ.get(
    "DEVICE_ONLINE_STATE_FILE",
    os.path.join(BRIDGE_STATE_DIR, "notified-devices.json"),
)
SYSNAME_STATE_FILE = os.environ.get(
    "SYSNAME_STATE_FILE",
    os.path.join(BRIDGE_STATE_DIR, "device-sysnames.json"),
)
# UniFi AP 掉线告警：从 UniFi Poller(unpoller) 在 Prometheus 里的 controller 数据
# 判断 AP 在线/掉线。没配 UniFi 时该查询为空、watcher 自动静默。
UNIFI_AP_ALERT_ENABLED = os.environ.get("UNIFI_AP_ALERT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
UNIFI_AP_DOWN_FOR_SECONDS = int(os.environ.get("UNIFI_AP_DOWN_FOR_SECONDS", "180"))
UNIFI_AP_POLL_INTERVAL = int(os.environ.get("UNIFI_AP_POLL_INTERVAL", "5"))
# sysName 变更告警：bridge 自己轮询 LibreNMS 设备列表，对比每台设备的 sysName，
# 变化时推送 旧→新 飞书卡片（LibreNMS 没有可靠的 "changed" 告警算子，webhook 也只带当前值）。
SYSNAME_CHANGE_ALERT_ENABLED = os.environ.get("SYSNAME_CHANGE_ALERT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SYSNAME_CHANGE_POLL_INTERVAL = int(os.environ.get("SYSNAME_CHANGE_POLL_INTERVAL", "60"))

_MAC_RE = r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}|[0-9A-Fa-f]{4}(?:\.[0-9A-Fa-f]{4}){2}|[0-9A-Fa-f]{12}"
_MACFLAP_RE = re.compile(
    rf"MACFLAP_NOTIF:\s+Host\s+({_MAC_RE})\s+in\s+vlan\s+(\d+)\s+is\s+flapping\s+between\s+port\s+(\S+)\s+and\s+port\s+(\S+)",
    re.IGNORECASE,
)
_NATIVE_VLAN_RE = re.compile(
    r"NATIVE_VLAN_MISMATCH:\s+Native VLAN mismatch discovered on\s+(\S+)\s+\((\d+)\),\s+with\s+(.+?)\s+(\S+)\s+\((\d+)\)",
    re.IGNORECASE,
)
_ERRDISABLE_RE = re.compile(
    r"ERR_?DISABLE:\s+(.+?)\s+error detected on\s+(\S+),\s+putting\s+\S+\s+in err-disable state",
    re.IGNORECASE,
)
_BPDUGUARD_RE = re.compile(
    r"(?:BPDUGUARD|BPDU Guard).*?(?:port|interface)\s+(\S+)",
    re.IGNORECASE,
)
_STORM_RE = re.compile(
    r"(?:STORM_CONTROL|storm-control|storm control).*?(?:on|interface)\s+(\S+)",
    re.IGNORECASE,
)
_LOOPBACK_RE = re.compile(
    r"LOOP_BACK_DETECTED.*?(?:on|interface)\s+(\S+)",
    re.IGNORECASE,
)
_LINK_STATE_RE = re.compile(
    r"(?:LINK-\d+-\w+|LINEPROTO-\d+-UPDOWN):\s+"
    r"(?:Line protocol on\s+)?Interface\s+(\S+),\s+changed state to\s+"
    r"(administratively down|up|down)",
    re.IGNORECASE,
)
def _clean_token(raw):
    t = raw.strip()
    if "/hook/" in t:
        t = t.rsplit("/hook/", 1)[-1]
    return t.strip().strip("/")


TOKEN = _clean_token(os.environ.get("FEISHU_ROBOT_TOKEN", ""))

SEVERITY_COLOR = {
    "critical": "red",
    "high": "orange",
    "warning": "yellow",
    "info": "blue",
    "average": "orange",
    "disaster": "purple",
}

EVENT_ID_LOCK = threading.Lock()
DEVICE_ONLINE_STATE_LOCK = threading.Lock()
DEVICE_DOWN_STATE_LOCK = threading.Lock()
HEALTH_LOCK = threading.Lock()
WATCHER_THREADS = {}
WATCHER_HEALTH = {}
DELIVERY_HEALTH = {"lastSuccessAt": None, "lastFailureAt": None, "lastError": ""}


def _read_int_file(path, default=0):
    try:
        with open(path, encoding="utf-8") as f:
            return int((f.read() or "").strip() or default)
    except (OSError, TypeError, ValueError):
        return default


def _atomic_write_text(path, text):
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _load_json_set(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(item) for item in data if item}
        if isinstance(data, dict):
            return {str(item) for item in data.get("items", []) if item}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return set()


def _save_json_set(path, values):
    payload = json.dumps(sorted(values), ensure_ascii=False)
    return _atomic_write_text(path, payload)


def _load_json_dict(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_json_dict(path, values):
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True)
    return _atomic_write_text(path, payload)


def mark_device_online_notified(*values):
    clean = {str(value).strip() for value in values if str(value or "").strip()}
    if not clean:
        return False
    with DEVICE_ONLINE_STATE_LOCK:
        items = _load_json_set(DEVICE_ONLINE_STATE_FILE)
        if clean.issubset(items):
            return False
        items.update(clean)
        return _save_json_set(DEVICE_ONLINE_STATE_FILE, items)


def _as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_device_down_states():
    loaded = {}
    with DEVICE_DOWN_STATE_LOCK:
        raw = _load_json_dict(DEVICE_DOWN_STATE_FILE)
    for key, value in raw.items():
        if not key or not isinstance(value, dict):
            continue
        loaded[str(key)] = {
            "down_since": _as_float(value.get("down_since")),
            "alerting": bool(value.get("alerting", not value.get("retired"))),
            "retired": bool(value.get("retired", False)),
            "retired_at": _as_float(value.get("retired_at")),
            # Do not migrate the old disabled marker: a retired device from the
            # previous version still needs one permanent DELETE after upgrade.
            "librenms_deleted": bool(value.get("librenms_deleted", False)),
            "librenms_readded": bool(value.get("librenms_readded", False)),
            "librenms_sync_last_attempt": _as_float(value.get("librenms_sync_last_attempt"), 0),
            "seen_up": bool(value.get("seen_up", True)),
            "ignored_initial_down": False,
            "last_up_at": _as_float(value.get("last_up_at")),
            "online_sent": bool(value.get("online_sent", False)),
            "name": str(value.get("name") or ""),
            "ip": str(value.get("ip") or ""),
            "job": str(value.get("job") or ""),
        }
    return loaded


def save_device_down_states(states):
    active_or_retired = {}
    for key, state in states.items():
        if not state.get("alerting") and not state.get("retired"):
            continue
        active_or_retired[str(key)] = {
            "down_since": state.get("down_since"),
            "retired_at": state.get("retired_at"),
            "librenms_deleted": bool(state.get("librenms_deleted", False)),
            "librenms_readded": bool(state.get("librenms_readded", False)),
            "librenms_sync_last_attempt": state.get("librenms_sync_last_attempt"),
            "last_up_at": state.get("last_up_at"),
            "seen_up": bool(state.get("seen_up", True)),
            "online_sent": bool(state.get("online_sent", False)),
            "name": state.get("name") or "",
            "ip": state.get("ip") or "",
            "job": state.get("job") or "",
            "alerting": bool(state.get("alerting")),
            "retired": bool(state.get("retired")),
        }
    with DEVICE_DOWN_STATE_LOCK:
        _save_json_dict(DEVICE_DOWN_STATE_FILE, active_or_retired)


EVENT_ID = max(
    int(os.environ.get("FEISHU_BRIDGE_EVENT_ID_START", "0") or "0"),
    _read_int_file(EVENT_ID_FILE, 0),
)


def log(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", file=sys.stderr, flush=True)


def mark_watcher_health(name, ok=True, error_message=""):
    with HEALTH_LOCK:
        state = WATCHER_HEALTH.setdefault(name, {})
        state["lastPollAt"] = int(time.time())
        if ok:
            state["lastSuccessAt"] = state["lastPollAt"]
            state["lastError"] = ""
        else:
            state["lastFailureAt"] = state["lastPollAt"]
            state["lastError"] = str(error_message or "unknown error")[:300]


def mark_delivery_health(ok, error_message=""):
    with HEALTH_LOCK:
        if ok:
            DELIVERY_HEALTH["lastSuccessAt"] = int(time.time())
            DELIVERY_HEALTH["lastError"] = ""
        else:
            DELIVERY_HEALTH["lastFailureAt"] = int(time.time())
            DELIVERY_HEALTH["lastError"] = str(error_message or "delivery failed")[:300]


def bridge_health_payload():
    with HEALTH_LOCK:
        watchers = {}
        for name, thread in WATCHER_THREADS.items():
            state = dict(WATCHER_HEALTH.get(name) or {})
            state["alive"] = bool(thread.is_alive())
            watchers[name] = state
        delivery = dict(DELIVERY_HEALTH)
    dead = sorted(name for name, state in watchers.items() if not state.get("alive"))
    token_ready = bool(TOKEN) or DRY_RUN
    ready = token_ready and not dead
    return {
        "ok": True,
        "ready": ready,
        "dryRun": DRY_RUN,
        "tokenConfigured": bool(TOKEN),
        "deadWatchers": dead,
        "watchers": watchers,
        "delivery": delivery,
        "time": int(time.time()),
    }


def start_watcher(name, target):
    def guarded():
        mark_watcher_health(name, True)
        try:
            target()
        except Exception as exc:
            mark_watcher_health(name, False, exc)
            log(f"[FATAL] watcher {name} stopped: {exc}")
            raise
    thread = threading.Thread(target=guarded, name=f"watcher-{name}", daemon=True)
    with HEALTH_LOCK:
        WATCHER_THREADS[name] = thread
    thread.start()
    return thread


def next_event_title():
    global EVENT_ID
    with EVENT_ID_LOCK:
        EVENT_ID += 1
        _atomic_write_text(EVENT_ID_FILE, str(EVENT_ID))
        return f"#{EVENT_ID}"


def _librenms_token():
    try:
        with open(LIBRENMS_TOKEN_FILE) as f:
            token = f.read().strip()
            if token:
                return token
    except OSError:
        pass
    return LIBRENMS_API_TOKEN


def fetch_librenms_devices(token):
    req = request.Request(
        f"{LIBRENMS_URL}/api/v0/devices",
        headers={"X-Auth-Token": token},
    )
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8")).get("devices", [])


def _find_librenms_device_by_ip(token, ip):
    for device in fetch_librenms_devices(token):
        candidates = {
            str(device.get("ip") or "").strip(),
            str(device.get("hostname") or "").strip(),
        }
        if str(ip) in candidates:
            return device
    return None


def _librenms_device_ref_for_ip(token, ip):
    """Resolve an IP to the device id accepted by LibreNMS PATCH routes."""
    device = _find_librenms_device_by_ip(token, ip)
    if device:
        return device.get("device_id") or device.get("hostname") or ip
    return ip


def _confirm_librenms_device_exists(token, ip, message, log_prefix):
    """Never trust a generic 'already exists' error without finding the device."""
    try:
        exists = _find_librenms_device_by_ip(token, ip) is not None
    except Exception as exc:
        log(f"{log_prefix} could not verify existing LibreNMS device {ip}: {exc}")
        return False
    if not exists:
        log(
            f"{log_prefix} LibreNMS reported 'already exists' for {ip}, but no matching "
            f"device is returned by the API; will retry. Response: {str(message)[:240]}"
        )
    return exists


def update_librenms_device_display(ip, name, log_prefix="[WATCHER]"):
    token = _librenms_token()
    name = str(name or "").strip()
    if not token or not LIBRENMS_URL or not ip or not name or _looks_like_ip(name):
        return False
    payload = {"field": "display", "data": name}
    try:
        device_ref = _librenms_device_ref_for_ip(token, ip)
    except Exception as exc:
        log(f"{log_prefix} LibreNMS device-id lookup failed for {ip}: {exc}")
        device_ref = ip
    encoded_ref = parse.quote(str(device_ref), safe="")
    req = request.Request(
        f"{LIBRENMS_URL}/api/v0/devices/{encoded_ref}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-Auth-Token": token, "Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
        status = str(data.get("status") or "").lower()
        if status == "ok":
            log(f"{log_prefix} LibreNMS display synced for {ip}: {name}")
            return True
        message = data.get("message") or raw
        log(f"{log_prefix} LibreNMS display sync failed for {ip}: {str(message)[:160]}")
        return False
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        log(f"{log_prefix} LibreNMS display sync HTTP {exc.code} for {ip}: {body[:160]}")
        return False
    except Exception as exc:
        log(f"{log_prefix} LibreNMS display sync failed for {ip}: {exc}")
        return False


def delete_librenms_device(ip, log_prefix="[DOWN]"):
    """Permanently delete a retired LibreNMS device.

    Returns ``deleted`` when DELETE succeeds, ``missing`` when LibreNMS has no
    matching device, and an empty string for retryable API/auth failures.
    """
    token = _librenms_token()
    if not token or not LIBRENMS_URL or not ip:
        return ""
    try:
        device = _find_librenms_device_by_ip(token, ip)
    except Exception as exc:
        log(f"{log_prefix} LibreNMS lookup failed for {ip}: {exc}")
        return ""
    if not device:
        log(f"{log_prefix} LibreNMS has no device record for {ip}; deletion already complete")
        return "missing"
    device_ref = device.get("device_id") or device.get("hostname") or ip
    encoded_ref = parse.quote(str(device_ref), safe="")
    req = request.Request(
        f"{LIBRENMS_URL}/api/v0/devices/{encoded_ref}",
        headers={"X-Auth-Token": token},
        method="DELETE",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
        entries = data if isinstance(data, list) else [data]
        if any(str(item.get("status") or "").lower() == "ok" for item in entries if isinstance(item, dict)):
            log(f"{log_prefix} LibreNMS retired device deleted permanently: {ip}")
            return "deleted"
        log(f"{log_prefix} LibreNMS device deletion failed for {ip}: {str(raw)[:160]}")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        log(f"{log_prefix} LibreNMS device deletion HTTP {exc.code} for {ip}: {body[:160]}")
    except Exception as exc:
        log(f"{log_prefix} LibreNMS device deletion failed for {ip}: {exc}")
    return ""


def add_librenms_snmp_device(ip, name="", community=None, log_prefix="[WATCHER]"):
    """Ensure an SNMP device exists in LibreNMS.

    Returns "added" for a new add request, "exists" when LibreNMS already has
    the device, and "" on failure. Existing callers only need truthiness.
    """
    token = _librenms_token()
    if not ip:
        return ""
    if not LIBRENMS_URL:
        log(f"{log_prefix} SNMP auto-add postponed for {name or ip} ({ip}): LIBRENMS_URL not set")
        return ""
    if not token:
        log(f"{log_prefix} SNMP auto-add postponed for {name or ip} ({ip}): LibreNMS API token not ready")
        return ""
    snmp_community = (community or SNMP_COMMUNITY or "global").strip()
    payload = {
        "hostname": ip,
        "version": "v2c",
        "community": snmp_community,
        "port": 161,
        "transport": "udp",
    }
    if name:
        payload["display_name"] = name
    req = request.Request(
        f"{LIBRENMS_URL}/api/v0/devices",
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-Auth-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
        status = str(data.get("status") or "").lower()
        message = data.get("message") or raw
        if status == "ok":
            log(f"{log_prefix} SNMP auto-add requested for {name or ip} ({ip})")
            update_librenms_device_display(ip, name, log_prefix=log_prefix)
            return "added"
        if "already" in str(message).lower() and _confirm_librenms_device_exists(
            token, ip, message, log_prefix
        ):
            log(f"{log_prefix} SNMP auto-add skipped for {name or ip} ({ip}): already exists")
            update_librenms_device_display(ip, name, log_prefix=log_prefix)
            return "exists"
        log(f"{log_prefix} SNMP auto-add failed for {name or ip} ({ip}): {str(message)[:160]}")
        return ""
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        if "already" in body.lower() and _confirm_librenms_device_exists(
            token, ip, body, log_prefix
        ):
            log(f"{log_prefix} SNMP auto-add skipped for {name or ip} ({ip}): already exists")
            update_librenms_device_display(ip, name, log_prefix=log_prefix)
            return "exists"
        log(f"{log_prefix} SNMP auto-add HTTP {exc.code} for {name or ip} ({ip}): {body[:160]}")
        return ""
    except Exception as exc:
        log(f"{log_prefix} SNMP auto-add failed for {name or ip} ({ip}): {exc}")
        return ""


def _looks_like_ip(value):
    return bool(re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", str(value or "")))


def _first_non_ip(*values):
    for value in values:
        value = str(value or "").strip()
        if value and not _looks_like_ip(value) and not re.fullmatch(r"\d+", value):
            return value
    return ""


def _best_device_name(dev):
    return (
        _first_non_ip(dev.get("display"), dev.get("sysName"), dev.get("hostname"))
        or dev.get("ip")
        or dev.get("hostname")
        or ""
    )


def _has_meaningful_device_name(dev):
    return bool(_first_non_ip(dev.get("display"), dev.get("sysName"), dev.get("hostname")))


def _is_ping_only_device(dev):
    values = [
        dev.get("os"),
        dev.get("type"),
        dev.get("hardware"),
        dev.get("platform"),
        dev.get("device_os"),
    ]
    text = " ".join(str(value or "").lower() for value in values)
    return "ping only" in text or re.search(r"(^|\W)ping($|\W)", text) is not None or "icmp" in text


def _find_unifi_ap_by_ip(ip):
    if not ip or not _unifi_controller_enabled():
        return None
    for ap in fetch_unifi_controller_aps_cached().values():
        if ap.get("ip") == ip:
            return ap
    return None


def _enrich_device_with_unifi(device):
    ip = device.get("ip") or device.get("hostname") or ""
    ap = _find_unifi_ap_by_ip(ip)
    if not ap:
        return device
    enriched = dict(device)
    if ap.get("name"):
        enriched["display"] = ap["name"]
        enriched["sysName"] = ap["name"]
    if ap.get("model") and not enriched.get("hardware"):
        enriched["hardware"] = ap["model"]
    return enriched


_GENERIC_DEVICE_MODEL_RE = re.compile(
    r"^(?:c\d+xx\s+stacking|cisco\s+ios|generic|unknown|n/?a|none|not\s+available)$",
    re.IGNORECASE,
)


def _clean_device_model(value):
    model = re.sub(r"\s+", " ", str(value or "")).strip()
    if not model or _GENERIC_DEVICE_MODEL_RE.fullmatch(model):
        return ""
    if re.fullmatch(r"(?:0x[0-9a-f]+|zeroDotZero|\d+)", model, re.IGNORECASE):
        return ""
    return model


def _inventory_device_model(inventory):
    """Return concrete chassis model(s), ignoring ports/PSUs/generic labels."""
    rows = [row for row in (inventory or []) if isinstance(row, dict)]
    groups = [
        [row for row in rows if str(row.get("entPhysicalClass") or "").lower() in ("chassis", "3")],
        [row for row in rows if str(row.get("entPhysicalContainedIn") or "") in ("", "0")],
    ]
    for group in groups:
        models = []
        for row in group:
            model = _clean_device_model(row.get("entPhysicalModelName"))
            if model and model not in models:
                models.append(model)
        if models:
            return " / ".join(models)
    return ""


def _best_device_model(device):
    for field in ("inventory_model", "hardware", "model"):
        model = _clean_device_model(device.get(field))
        if model:
            return model
    return ""


def fetch_librenms_inventory(token, device):
    device_ref = device.get("device_id") or device.get("hostname") or device.get("ip")
    if not token or not LIBRENMS_URL or not device_ref:
        return []
    encoded_ref = parse.quote(str(device_ref), safe="")
    req = request.Request(
        f"{LIBRENMS_URL}/api/v0/inventory/{encoded_ref}/all",
        headers={"X-Auth-Token": token},
    )
    with request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("inventory", []) if isinstance(payload, dict) else []


def _enrich_device_with_inventory(device, token):
    enriched = dict(device)
    try:
        inventory_model = _inventory_device_model(fetch_librenms_inventory(token, device))
    except Exception as exc:
        log(f"[WATCHER] inventory lookup failed for {device.get('hostname') or device.get('ip')}: {exc}")
        inventory_model = ""
    if inventory_model:
        enriched["inventory_model"] = inventory_model
    return enriched


def _device_name(dev):
    return _best_device_name(dev)


def fetch_librenms_name_cache():
    token = _librenms_token()
    if not token or not LIBRENMS_URL:
        return {}
    devices = fetch_librenms_devices(token)
    names = {}
    for dev in devices:
        name = _device_name(dev)
        if not name:
            continue
        for field in ("ip", "hostname"):
            value = dev.get(field)
            if _looks_like_ip(value):
                names[value] = name
    return names


def _normalize_mac_hex(value):
    mac = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).lower()
    return mac if len(mac) == 12 else ""


def _format_mac(value):
    mac = _normalize_mac_hex(value)
    if not mac:
        return str(value or "").strip()
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))


def _host_display_name(host, fdb_entry=None):
    if fdb_entry:
        name = _first_non_ip(fdb_entry.get("sysName"), fdb_entry.get("hostname"))
        if name:
            return name
    try:
        cache = fetch_librenms_name_cache()
        return cache.get(host) or host
    except Exception as exc:
        log(f"[SYSLOG] device name lookup failed for {host}: {exc}")
        return host


def build_librenms_card(payload):
    state = str(payload.get("state", "1"))
    rule_name = payload.get("name") or payload.get("rule") or "告警"
    severity = (payload.get("severity") or "warning").lower()

    sys_name = payload.get("sysName") or ""
    hostname = payload.get("hostname") or ""
    ip = payload.get("ip") or ""
    if not sys_name and not hostname and not ip:
        devices = payload.get("devices") or []
        if devices:
            first = devices[0]
            sys_name = first.get("sysName") or ""
            hostname = first.get("hostname") or ""
            ip = first.get("ip") or ""

    elapsed = str(payload.get("elapsed") or "").strip()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    recovered = state == "0"
    if recovered:
        color = "green"
        emoji = "✅"
        state_text = "UP"
    else:
        color = SEVERITY_COLOR.get(severity, "yellow")
        emoji = "❌" if severity in ("critical", "disaster") else "🔴"
        state_text = "DOWN"

    title = next_event_title()

    # 设备名优先 sysName（交换机名）/ 非 IP 的 hostname；避免出现 "IP (IP)"。
    dev_name = sys_name or hostname or ip or "?"
    ip_str = f" ({ip})" if ip and ip != dev_name else ""
    lines = [
        f"🖥 设备：{dev_name}{ip_str}",
        f"{emoji} 状态：{state_text}",
    ]
    if elapsed and elapsed not in ("0s",):
        label = "恢复耗时" if recovered else "断线时间"
        lines.append(f"⏳ {label}：{elapsed}")
    lines.append(f"⏰ 时间：{ts}")

    return _make_card(title, f"{emoji} {rule_name}", color, "\n".join(lines))


def build_sysname_change_card(old_name, new_name, ip="", hostname=""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dev = new_name or hostname or ip or "?"
    ip_str = f" ({ip})" if ip and ip != dev else ""
    old_name = old_name or "?"
    new_name = new_name or "?"
    lines = [
        f"🖥 设备：{dev}{ip_str}",
        f"✏️ sysName：{old_name} → {new_name}",
        f"⏰ 时间：{ts}",
    ]
    return _make_card(next_event_title(), "✏️ sysName 变更告警", "yellow", "\n".join(lines))


def build_test_card():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "✅ 飞书告警链路正常",
        "📡 这是一条测试告警，收到即代表机器人配置无误。",
        f"⏰ 时间：{ts}",
    ]
    return _make_card(next_event_title(), "🔔 测试告警", "blue", "\n".join(lines))


def build_device_online_card(device):
    name = _best_device_name(device) or "?"
    ip = device.get("ip") or device.get("hostname") or "?"
    hw = _best_device_model(device) or "暂未识别"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [f"🖥 设备：{name}", f"🌐 IP：{ip}"]
    lines.append(f"🔧 型号：{hw}")
    lines.append(f"⏰ 时间：{ts}")

    return _make_card(next_event_title(), "🔵 新设备部署", "blue", "\n".join(lines))


def format_bps(value):
    value = float(value or 0)
    units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]
    idx = 0
    while abs(value) >= 1000 and idx < len(units) - 1:
        value /= 1000.0
        idx += 1
    decimals = 1 if idx >= 2 else 0
    return f"{value:.{decimals}f} {units[idx]}"


def format_duration(seconds):
    try:
        seconds_float = float(seconds or 0)
    except (TypeError, ValueError):
        seconds_float = 0
    seconds = max(0, int(seconds_float + 0.999)) if seconds_float > 0 else 0
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒" if sec else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"


def format_alert_duration(seconds, recovered=False):
    if not recovered:
        try:
            if float(seconds or 0) <= 0:
                return "1 秒"
        except (TypeError, ValueError):
            return "1 秒"
    return format_duration(seconds)


def build_isp_bandwidth_card(event, recovered=False):
    color = "green" if recovered else "red"
    direction_text = "下载" if event["direction"] == "in" else "上传"
    state_text = "已恢复" if recovered else "带宽超限"
    status_emoji = "✅" if recovered else "🔴"
    header_emoji = "🟢" if recovered else "🔴"
    duration_label = "恢复耗时" if recovered else "持续时间"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"🌐 ISP：{event['label']}",
        f"📶 方向：{direction_text}",
        f"{status_emoji} 状态：{state_text}",
        f"📈 当前：{format_bps(event['value_bps'])}",
        f"⏳ {duration_label}：{format_alert_duration(event['duration'], recovered)}",
        f"⏰ 时间：{ts}",
    ]
    return _make_card(next_event_title(), f"{header_emoji} 外网 ISP 告警", color, "\n".join(lines))


def build_isp_data_missing_card(missing_seconds, recovered=False):
    color = "green" if recovered else "red"
    status_emoji = "✅" if recovered else "❌"
    header_emoji = "🟢" if recovered else "🔴"
    state_text = "已恢复" if recovered else "数据中断"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "🌐 对象：防火墙 WAN 流量采集",
        f"{status_emoji} 状态：{state_text}",
        f"⏳ 中断时长：{format_duration(missing_seconds)}",
        f"⏰ 时间：{ts}",
    ]
    if not recovered:
        lines.append("💡 请检查防火墙 SNMP 是否可达、FIREWALL_WAN_IF_FILTER 是否匹配新接口名")
    return _make_card(next_event_title(), f"{header_emoji} 外网流量采集告警", color, "\n".join(lines))


def build_device_down_card(name, ip, recovered, offline_seconds=0, job="", downstream=0):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dev = f"{name} ({ip})" if ip and ip != name else (name or ip or "?")
    is_isp = job == "infra-isp-ping"
    if recovered:
        color, state_text = "green", "UP"
        status_emoji = "✅"
        header_emoji = "🟢"
    else:
        color, state_text = "red", "DOWN"
        status_emoji = "❌"
        header_emoji = "🔴"
    label = "ISP" if is_isp else "设备"
    label_emoji = "🌐" if is_isp else "🖥"
    subtitle = "外网 ISP 告警" if is_isp else "设备离线告警"
    duration_label = "恢复耗时" if recovered else "断线时间"
    lines = [
        f"{label_emoji} {label}：{dev}",
        f"{status_emoji} 状态：{state_text}",
        f"⏳ {duration_label}：{format_alert_duration(offline_seconds, recovered)}",
    ]
    if not recovered and downstream:
        # This device is the root cause: its downstream victims are folded in here
        # instead of each firing its own card.
        lines.append(f"📉 下游 {downstream} 台设备同时不可达（疑似受其影响，已折叠）")
    lines.append(f"⏰ 时间：{ts}")
    return _make_card(next_event_title(), f"{header_emoji} {subtitle}", color, "\n".join(lines))


def _join_ports(names):
    return "、".join(n for n in (names or []) if n) or "?"


def build_interconnect_card(event, recovered=False):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = "green" if recovered else "red"
    header_emoji = "🟢" if recovered else "🔴"
    device = event.get("device") or event.get("ip") or "?"
    ip = event.get("ip") or ""
    device_text = f"{device} ({ip})" if ip and ip != device else device
    # Prefer the real peer switch from LLDP; fall back to the port alias/name.
    peer = event.get("peer_switch") or event.get("alias") or event.get("port") or "?"
    down_members = event.get("down_members") or []
    up_members = event.get("up_members") or []
    aggregate_down = event.get("status") == "down"
    if recovered:
        subtitle = "链路聚合恢复"
        status_emoji = "✅"
        lines = [
            f"🖥 设备：{device_text}",
            f"🔌 物理口：{_join_ports(down_members) if down_members else event.get('port', '?')} 已恢复",
            f"🔗 对端交换机：{peer}",
            f"{status_emoji} 状态：链路冗余已恢复",
            f"⏳ 恢复耗时：{format_alert_duration(event.get('duration'), True)}",
            f"⏰ 时间：{ts}",
        ]
    else:
        subtitle = "链路聚合告警"
        port_text = _join_ports(down_members) if down_members else event.get("port", "?")
        state_text = (
            f"聚合链路 DOWN；在线成员：{_join_ports(up_members)}"
            if aggregate_down else
            f"冗余降低；剩 {_join_ports(up_members)} 在线"
        )
        lines = [
            f"🖥 设备：{device_text}",
            f"🔌 异常接口：{port_text}",
            f"🔗 对端交换机：{peer}",
            f"⚠️ 状态：{state_text}",
            f"⏳ 持续时间：{format_alert_duration(event.get('duration'), False)}",
            f"⏰ 时间：{ts}",
        ]
    return _make_card(next_event_title(), f"{header_emoji} {subtitle}", color, "\n".join(lines))


def build_ap_down_card(name, ip, model, recovered, offline_seconds=0):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tail = f" · {model}" if model else ""
    dev = f"{name} ({ip}{tail})" if ip else (name or "?")
    if recovered:
        color, state_text, status_emoji, header_emoji = "green", "UP", "✅", "🟢"
    else:
        color, state_text, status_emoji, header_emoji = "red", "DOWN", "❌", "🔴"
    duration_label = "恢复耗时" if recovered else "断线时长"
    lines = [
        f"📶 AP：{dev}",
        f"{status_emoji} 状态：{state_text}",
        f"⏳ {duration_label}：{format_alert_duration(offline_seconds, recovered)}",
        f"⏰ 时间：{ts}",
    ]
    return _make_card(next_event_title(), f"{header_emoji} AP 掉线告警", color, "\n".join(lines))


def _card_preview_title(title, subtitle):
    preview = re.sub(r"\s+", " ", f"{title} {subtitle}".strip())
    return preview[:120]


def _make_card(title, subtitle, color, body_md):
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {
                "style": {
                    "text_size": {
                        "normal_v2": {
                            "default": "normal",
                            "pc": "normal",
                            "mobile": "heading",
                        }
                    }
                }
            },
            "header": {
                "title": {"tag": "plain_text", "content": _card_preview_title(title, subtitle)},
                "subtitle": {"tag": "plain_text", "content": subtitle},
                "template": color,
                "padding": "12px 12px 12px 12px",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": body_md,
                        "text_align": "left",
                        "text_size": "normal_v2",
                        "margin": "0px 0px 0px 0px",
                    }
                ],
            },
        },
    }


def _feishu_response_result(response_text):
    """Return (ok, detail) for a Feishu webhook JSON response.

    Feishu reports token/permission/rate-limit failures in a JSON business code,
    often while the HTTP status itself is 200.  Treating every HTTP 200 as a
    success permanently loses alerts, so a recognizable zero code is required.
    """
    try:
        payload = json.loads(str(response_text or "").strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, "response is not valid JSON"
    if not isinstance(payload, dict):
        return False, "response JSON is not an object"
    code = payload.get("code")
    if code is None:
        code = payload.get("StatusCode", payload.get("status_code"))
    try:
        ok = int(code) == 0
    except (TypeError, ValueError):
        return False, "response has no recognizable business code"
    detail = str(payload.get("msg") or payload.get("StatusMessage") or payload.get("message") or "")
    return ok, detail or f"code={code}"


def send_feishu(card):
    if DRY_RUN:
        log(f"[DRY] would POST card: {card['card']['header']['title']['content']}")
        mark_delivery_health(True)
        return True
    if not TOKEN:
        log("[WARN] FEISHU_ROBOT_TOKEN empty, dropping alert (set token or enable DRY_RUN)")
        mark_delivery_health(False, "FEISHU_ROBOT_TOKEN is empty")
        return False
    url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}"
    data = json.dumps(card).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    last_error = "delivery failed"
    for attempt in range(1, FEISHU_SEND_MAX_ATTEMPTS + 1):
        try:
            with request.urlopen(req, timeout=5) as resp:
                response_text = resp.read().decode("utf-8", errors="replace")
            ok, detail = _feishu_response_result(response_text)
            if ok:
                log(f"feishu response: {response_text[:200]}")
                mark_delivery_health(True)
                return True
            last_error = detail
            log(
                f"[ERR] feishu rejected alert attempt "
                f"{attempt}/{FEISHU_SEND_MAX_ATTEMPTS}: {detail}; response={response_text[:200]}"
            )
        except error.URLError as exc:
            last_error = str(exc)
            log(f"[ERR] feishu request attempt {attempt}/{FEISHU_SEND_MAX_ATTEMPTS} failed: {exc}")
        except Exception as exc:
            last_error = str(exc)
            log(f"[ERR] unexpected Feishu error attempt {attempt}/{FEISHU_SEND_MAX_ATTEMPTS}: {exc}")
        if attempt < FEISHU_SEND_MAX_ATTEMPTS:
            delay = FEISHU_SEND_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            if delay > 0:
                time.sleep(delay)
    mark_delivery_health(False, last_error)
    return False


def send_device_online_once(card, *identity_values):
    """Ensure an online card was delivered once; persist keys only after success."""
    clean = {str(value).strip() for value in identity_values if str(value or "").strip()}
    if not clean:
        return False
    # Serialize check -> send -> commit so the AP and LibreNMS watcher cannot
    # both deliver the same device while still avoiding a false persisted mark.
    with DEVICE_ONLINE_STATE_LOCK:
        items = _load_json_set(DEVICE_ONLINE_STATE_FILE)
        if clean & items:
            if not clean.issubset(items):
                items.update(clean)
                _save_json_set(DEVICE_ONLINE_STATE_FILE, items)
            return True
        if not send_feishu(card):
            return False
        items.update(clean)
        return _save_json_set(DEVICE_ONLINE_STATE_FILE, items)


def send_device_online_new_lifecycle(card, *identity_values):
    """Deliver a new-device card even when this IP/name was notified before.

    Re-enrollment is an explicit new lifecycle, so the normal lifetime de-dupe
    must not suppress it. Keep the identities recorded after delivery so the
    regular LibreNMS watcher does not send a duplicate card.
    """
    if not send_feishu(card):
        return False
    mark_device_online_notified(*identity_values)
    return True


def _norm_label(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _parse_bandwidth_config(raw):
    raw = str(raw or "").strip()
    cfg = {"default": None, "per": []}
    if not raw:
        return cfg
    try:
        mbps = float(raw)
        cfg["default"] = {"down": mbps, "up": mbps}
        return cfg
    except ValueError:
        pass

    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        label, bandwidth = [part.strip() for part in item.split(":", 1)]
        parts = [part.strip() for part in bandwidth.split("/", 1)]
        try:
            down = float(parts[0])
        except (TypeError, ValueError):
            continue
        try:
            up = float(parts[1]) if len(parts) > 1 else down
        except (TypeError, ValueError):
            up = down
        if label == "*":
            cfg["default"] = {"down": down, "up": up}
            continue
        cfg["per"].append({
            "label": label.lower(),
            "norm": _norm_label(label),
            "down": down,
            "up": up,
        })
    return cfg


def _parse_named_targets(raw):
    names = {}
    for item in str(raw or "").split(","):
        item = item.strip().replace(" ", "")
        if not item or ":" not in item:
            continue
        name, target = item.split(":", 1)
        if not name or not target:
            continue
        if "-" not in target:
            names[target] = name
            continue

        start_ip, end_part = target.rsplit("-", 1)
        try:
            prefix, start_octet = start_ip.rsplit(".", 1)
            end_octet = end_part.rsplit(".", 1)[-1]
            start = int(start_octet)
            end = int(end_octet)
        except ValueError:
            continue
        if start > end:
            continue
        for idx, octet in enumerate(range(start, end + 1), start=1):
            names[f"{prefix}.{octet}"] = f"{name}{idx}"
    return names


def _isp_target_names():
    names = _parse_named_targets(BIGSCREEN_ISP_IPS)
    names.update(_parse_named_targets(ISP_PING))
    return names


def _wan_keywords():
    return [part.strip().lower() for part in FIREWALL_WAN_IF_FILTER.split(",") if part.strip()]


def _wan_label(metric):
    return (metric.get("ifAlias") or metric.get("ifName") or metric.get("ifDescr") or "").strip()


def _is_wan_port(label):
    # 以数字结尾的关键词按边界匹配：WatchGuard 等防火墙 SNMP 只报 eth0/eth1
    # 物理名，填 eth1 不能顺带命中 eth10~eth15；其它关键词维持包含匹配。
    lower = label.lower()
    for keyword in _wan_keywords():
        if keyword[-1:].isdigit():
            if re.search(re.escape(keyword) + r"(?:\D|$)", lower):
                return True
        elif keyword in lower:
            return True
    return False


def _interconnect_keywords():
    return [part.strip().lower() for part in INTERCONNECT_PORT_FILTER.split(",") if part.strip()]


def _port_label(metric):
    for field in ("ifName", "ifDescr", "ifAlias"):
        value = (metric.get(field) or "").strip()
        if value:
            return value
    return metric.get("ifIndex") or "?"


def _is_interconnect_port(metric):
    fields = [
        metric.get("ifName") or "",
        metric.get("ifDescr") or "",
        metric.get("ifAlias") or "",
    ]
    joined = " ".join(fields).lower()
    norm = _norm_label(joined)
    for keyword in _interconnect_keywords():
        knorm = _norm_label(keyword)
        if not knorm:
            continue
        if len(knorm) <= 3:
            if norm.startswith(knorm):
                return True
            continue
        if keyword in joined or norm.startswith(knorm):
            return True
    return False


def _if_oper_is_up(metric, value):
    status_label = (
        metric.get("ifOperStatus")
        or metric.get("ifOperStatus_label")
        or metric.get("ifOperStatus_state")
    )
    if status_label:
        if value < 0.5:
            return None
        return str(status_label).lower() == "up"
    return int(value) == 1


def _bandwidth_for_label(label, direction, cfg, index=None):
    lower = label.lower()
    norm = _norm_label(label)
    # 取最具体（名字最长）的匹配：双线场景配了 "电信:500,电信2:200" 时，
    # 端口 电信2 必须拿到自己的 200，而不是被兜底的 "电信" 抢先命中。
    best = None
    for entry in cfg["per"]:
        if (entry["label"] and entry["label"] in lower) or (entry["norm"] and entry["norm"] in norm):
            if best is None or len(entry["norm"]) > len(best["norm"]):
                best = entry
    if best is not None:
        return best["down"] if direction == "in" else best["up"]
    if isinstance(index, int) and 0 <= index < len(cfg["per"]):
        entry = cfg["per"][index]
        return entry["down"] if direction == "in" else entry["up"]
    default = cfg["default"] or {"down": 1000.0, "up": 1000.0}
    return default["down"] if direction == "in" else default["up"]


def _counter_glitch_limit_bps(capacity_mbps, factor=None):
    """Bps ceiling above which a rate sample is a counter glitch, or None if off.

    A WAN port cannot legitimately carry many times its configured capacity;
    values far beyond it only appear when rate() spans an SNMP counter jump
    (firewall replaced, HA member switch, snmpd restart).
    """
    factor = ISP_ALERT_SPIKE_IGNORE_FACTOR if factor is None else factor
    if not factor or factor <= 0:
        return None
    try:
        capacity = float(capacity_mbps)
    except (TypeError, ValueError):
        return None
    if capacity <= 0:
        return None
    return capacity * 1000000 * factor


def _dedupe_wan_labels(results):
    """同名 WAN 口（双电信/双联通）按 ifIndex 排位补 -1/-2 后缀。

    不加后缀时两条线会共用同一个告警状态键和卡片名，互相覆盖。后缀与
    ISP 网关自动发现的重名编号规则一致（同样按 ifIndex 升序），带宽配置
    写 电信-1/电信-2（或 电信1，匹配时忽略符号）即可分线绑定。单线不受影响。
    """
    indexes = {}
    for sample in results:
        try:
            ifi = int(sample.get("if_index"))
        except (TypeError, ValueError):
            ifi = 2**31
        sample["_ifindex"] = ifi
        indexes.setdefault(sample["label"], set()).add(ifi)
    ranks = {
        label: {ifi: pos + 1 for pos, ifi in enumerate(sorted(values))}
        for label, values in indexes.items()
        if len(values) > 1
    }
    for sample in results:
        label = sample["label"]
        if label in ranks:
            sample["label"] = f"{label}-{ranks[label][sample['_ifindex']]}"
        sample["key"] = f"{sample['label']}|{sample['direction']}"
        sample.pop("_ifindex", None)
    return results


def _bandwidth_indexes(rates):
    ports = {}
    for sample in rates:
        label = sample["label"]
        try:
            if_index = int(sample.get("if_index"))
        except (TypeError, ValueError):
            if_index = 2**31
        ports[label] = min(if_index, ports.get(label, 2**31))
    ordered = sorted(ports, key=lambda label: (ports[label], label.lower()))
    return {label: index for index, label in enumerate(ordered)}


def prometheus_query(query):
    url = f"{PROMETHEUS_URL}/api/v1/query?{parse.urlencode({'query': query})}"
    with request.urlopen(url, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error") or "Prometheus query failed")
    return payload.get("data", {}).get("result", [])


def fetch_wan_rates():
    results = []
    for direction, metric in (("in", "ifHCInOctets"), ("out", "ifHCOutOctets")):
        query = f'rate({metric}{{job="firewall-snmp"}}[{ISP_ALERT_RATE_WINDOW}]) * 8'
        for item in prometheus_query(query):
            metric_labels = item.get("metric") or {}
            label = _wan_label(metric_labels)
            if not label or not _is_wan_port(label):
                continue
            try:
                value_bps = float((item.get("value") or [None, "nan"])[1])
            except (TypeError, ValueError):
                continue
            if value_bps < 0:
                continue
            results.append({
                "key": f"{label}|{direction}",
                "label": label,
                "direction": direction,
                "value_bps": value_bps,
                "if_index": metric_labels.get("ifIndex"),
                "target_ip": metric_labels.get("target_ip") or metric_labels.get("instance") or "",
            })
    return _dedupe_wan_labels(results)


def log_isp_status(rates, bandwidth_cfg):
    if not rates:
        log(
            "[ISP] no WAN traffic series matched "
            f"FIREWALL_WAN_IF_FILTER={FIREWALL_WAN_IF_FILTER!r}; "
            "check Prometheus job=firewall-snmp labels ifAlias/ifName/ifDescr"
        )
        return

    rows = []
    indexes = _bandwidth_indexes(rates)
    for sample in sorted(rates, key=lambda item: item["value_bps"], reverse=True)[:6]:
        capacity_mbps = _bandwidth_for_label(
            sample["label"], sample["direction"], bandwidth_cfg, indexes.get(sample["label"])
        )
        threshold_bps = capacity_mbps * 1000000 * (ISP_SATURATION_PERCENT / 100.0)
        rows.append(
            f"{sample['label']} {sample['direction']}="
            f"{format_bps(sample['value_bps'])}/{format_bps(threshold_bps)}"
        )
    log("[ISP] rates " + "; ".join(rows))


def isp_bandwidth_watcher():
    if not ISP_ALERT_ENABLED:
        log("[ISP] realtime bandwidth watcher disabled")
        return
    time.sleep(30)
    bandwidth_cfg = _parse_bandwidth_config(BIGSCREEN_ISP_MAX_BANDWIDTH)
    states = {}
    last_status_log = 0.0
    data_seen = False
    data_missing_since = None
    data_missing_alerting = False
    log(
        "[ISP] realtime bandwidth watcher enabled "
        f"(threshold={ISP_SATURATION_PERCENT:g}%, for={ISP_ALERT_FOR_SECONDS}s, "
        f"poll={ISP_ALERT_POLL_INTERVAL}s, rate_window={ISP_ALERT_RATE_WINDOW}, "
        f"spike_ignore_factor={ISP_ALERT_SPIKE_IGNORE_FACTOR:g}, "
        f"data_missing_after={ISP_DATA_MISSING_ALERT_SECONDS}s, prometheus={PROMETHEUS_URL})"
    )

    while True:
        now = time.time()
        try:
            rates = fetch_wan_rates()
        except Exception as exc:
            mark_watcher_health("isp-bandwidth", False, exc)
            log(f"[ISP] poll failed: {exc}")
            time.sleep(ISP_ALERT_POLL_INTERVAL)
            continue
        mark_watcher_health("isp-bandwidth", True)

        # 数据中断守护：以前有 WAN 序列、现在整体消失（换防火墙后 SNMP 认证
        # 不通 / 接口名不再匹配 WAN 关键词 / 采集挂了），静默丢监控比误报更危险。
        if rates:
            if data_missing_alerting:
                missing = now - data_missing_since if data_missing_since else 0
                log(f"[ISP] WAN traffic series recovered after {int(missing)}s gap")
                send_feishu(build_isp_data_missing_card(missing, recovered=True))
            data_seen = True
            data_missing_since = None
            data_missing_alerting = False
        elif data_seen and ISP_DATA_MISSING_ALERT_SECONDS > 0:
            if data_missing_since is None:
                data_missing_since = now
            elif not data_missing_alerting and now - data_missing_since >= ISP_DATA_MISSING_ALERT_SECONDS:
                data_missing_alerting = True
                log(
                    "[ISP] ALERT WAN traffic series missing for "
                    f"{int(now - data_missing_since)}s; check firewall SNMP and FIREWALL_WAN_IF_FILTER"
                )
                send_feishu(build_isp_data_missing_card(now - data_missing_since, recovered=False))

        if now - last_status_log >= ISP_ALERT_STATUS_INTERVAL:
            log_isp_status(rates, bandwidth_cfg)
            last_status_log = now

        seen = set()
        indexes = _bandwidth_indexes(rates)
        for sample in rates:
            seen.add(sample["key"])
            capacity_mbps = _bandwidth_for_label(
                sample["label"], sample["direction"], bandwidth_cfg, indexes.get(sample["label"])
            )
            glitch_limit = _counter_glitch_limit_bps(capacity_mbps)
            if glitch_limit is not None and sample["value_bps"] >= glitch_limit:
                log(
                    f"[ISP] ignore counter glitch {sample['label']} {sample['direction']} "
                    f"{format_bps(sample['value_bps'])} (> {format_bps(glitch_limit)}, "
                    f"capacity {capacity_mbps:g} Mbps)"
                )
                continue
            threshold_bps = capacity_mbps * 1000000 * (ISP_SATURATION_PERCENT / 100.0)
            state = states.setdefault(sample["key"], {
                "active_since": None,
                "clear_since": None,
                "alerting": False,
                "alert_started": None,
                "last_value": 0.0,
            })
            state["last_value"] = sample["value_bps"]

            if sample["value_bps"] >= threshold_bps:
                if state["active_since"] is None:
                    state["active_since"] = now
                state["clear_since"] = None
                duration = now - state["active_since"]
                if not state["alerting"] and duration >= ISP_ALERT_FOR_SECONDS:
                    event = {
                        **sample,
                        "threshold_bps": threshold_bps,
                        "capacity_mbps": capacity_mbps,
                        "percent": ISP_SATURATION_PERCENT,
                        "duration": duration,
                    }
                    log(
                        f"[ISP] ALERT {sample['label']} {sample['direction']} "
                        f"{format_bps(sample['value_bps'])} >= {format_bps(threshold_bps)}"
                    )
                    if send_feishu(build_isp_bandwidth_card(event, recovered=False)):
                        state["alerting"] = True
                        state["alert_started"] = state["active_since"]
            else:
                state["active_since"] = None
                if state["alerting"]:
                    if state["clear_since"] is None:
                        state["clear_since"] = now
                    clear_duration = now - state["clear_since"]
                    if clear_duration >= ISP_ALERT_RESOLVE_SECONDS:
                        # 持续 = 整段饱和时长（首次越过阈值→恢复），不是 30 秒恢复防抖
                        _start = state["alert_started"] if state["alert_started"] is not None else state["clear_since"]
                        saturated_duration = now - _start
                        event = {
                            **sample,
                            "threshold_bps": threshold_bps,
                            "capacity_mbps": capacity_mbps,
                            "percent": ISP_SATURATION_PERCENT,
                            "duration": saturated_duration,
                        }
                        log(
                            f"[ISP] RECOVER {sample['label']} {sample['direction']} "
                            f"{format_bps(sample['value_bps'])} < {format_bps(threshold_bps)}"
                        )
                        if send_feishu(build_isp_bandwidth_card(event, recovered=True)):
                            state["alerting"] = False
                            state["alert_started"] = None
                else:
                    state["clear_since"] = None

        for key, state in list(states.items()):
            if key in seen:
                continue
            if state.get("alerting"):
                # A vanished Prometheus series is not proof of recovery. Keep the
                # delivered alert active until the series returns with a clear value.
                state["clear_since"] = None
            elif state.get("clear_since") and now - state["clear_since"] >= ISP_ALERT_RESOLVE_SECONDS:
                states.pop(key, None)

        time.sleep(ISP_ALERT_POLL_INTERVAL)


def classify_interconnect(lag_up, member_ups):
    """Decide what an interconnect LAG's member states mean for alerting:

      healthy  - every member up (nothing to report)
      degraded - some members down but the bundle is still up (redundancy lost,
                 traffic still flows -> this is the case worth alerting)
      down     - aggregate protocol/oper state is down, or every member is down
      unknown  - no member visibility (switch lacks ifStackTable); nothing to say
    """
    if not member_ups:
        return "down" if lag_up is False else "unknown"
    if not lag_up:
        return "down"
    any_down = not all(member_ups)
    any_up = any(member_ups)
    if not any_down:
        return "healthy"
    if not any_up:
        return "down"
    return "degraded"


def fetch_interconnect_members(jobs_regex):
    """Map (device_ip, aggregate ifIndex) -> [member ifIndex] via ifStackTable.
    Returns {} when the switch does not expose it."""
    try:
        results = prometheus_query(f'ifStackStatus{{job=~"{jobs_regex}"}}')
    except Exception as exc:
        log(f"[LINK] ifStack lookup failed (member ports unavailable): {exc}")
        return {}
    members = {}
    for item in results:
        metric = item.get("metric") or {}
        higher = metric.get("ifStackHigherLayer") or metric.get("ifStackHigherLayerIndex")
        lower = metric.get("ifStackLowerLayer") or metric.get("ifStackLowerLayerIndex")
        ip = metric.get("target_ip") or metric.get("instance") or ""
        # 0 marks the top/bottom sentinels of a stack; only real higher->lower rows pair an aggregate with a member.
        if not higher or not lower or higher == "0" or lower == "0":
            continue
        bucket = members.setdefault((ip, higher), [])
        if lower not in bucket:
            bucket.append(lower)
    return members


def fetch_interconnect_ports(jobs_regex):
    results = prometheus_query(f'ifOperStatus{{job=~"{jobs_regex}"}}')
    # Per-interface name + up/down for every interface (physical members too), so
    # an aggregate's member ifIndexes resolve to real port names and states.
    index_names = {}
    index_up = {}
    for item in results:
        metric = item.get("metric") or {}
        ip = metric.get("target_ip") or metric.get("instance") or ""
        ifindex = metric.get("ifIndex")
        if not (ip and ifindex):
            continue
        index_names[(ip, ifindex)] = _port_label(metric)
        try:
            value = float((item.get("value") or [None, "nan"])[1])
        except (TypeError, ValueError):
            continue
        member_up = _if_oper_is_up(metric, value)
        # Unknown/stale status counts as up so a missing sample never fakes a degrade.
        index_up[(ip, ifindex)] = True if member_up is None else bool(member_up)
    members_map = fetch_interconnect_members(jobs_regex)

    ports = []
    for item in results:
        metric = item.get("metric") or {}
        if not _is_interconnect_port(metric):
            continue
        try:
            value = float((item.get("value") or [None, "nan"])[1])
        except (TypeError, ValueError):
            continue
        up = _if_oper_is_up(metric, value)
        if up is None:
            continue
        ip = metric.get("target_ip") or metric.get("instance") or ""
        port = _port_label(metric)
        ifindex = metric.get("ifIndex")
        members = []
        for member_idx in members_map.get((ip, ifindex), []):
            name = index_names.get((ip, member_idx))
            if not name or name == port:
                continue
            members.append({"name": name, "up": index_up.get((ip, member_idx), True)})
        ports.append({
            "key": "|".join([metric.get("job", ""), ip, ifindex or port]),
            "device": metric.get("display_name") or metric.get("instance") or ip or "?",
            "ip": ip,
            "port": port,
            "alias": metric.get("ifAlias") or "",
            "lag_up": bool(up),
            "members": members,
        })
    return ports


def interconnect_watcher():
    if not INTERCONNECT_ALERT_ENABLED:
        log("[LINK] interconnect watcher disabled")
        return
    jobs = [j.strip() for j in INTERCONNECT_ALERT_JOBS.split(",") if j.strip()]
    safe_jobs = [j for j in jobs if re.match(r"^[A-Za-z0-9_:.-]+$", j)]
    if not safe_jobs:
        log("[LINK] no valid SNMP jobs configured, watcher disabled")
        return

    jobs_regex = "|".join(safe_jobs)
    states = {}
    last_status_log = 0.0
    last_name_refresh = 0.0
    librenms_names = {}
    peer_map = {}
    time.sleep(25)
    log(
        "[LINK] interconnect watcher enabled "
        f"(jobs={','.join(safe_jobs)}, for={INTERCONNECT_ALERT_FOR_SECONDS}s, "
        f"poll={INTERCONNECT_ALERT_POLL_INTERVAL}s, filter={INTERCONNECT_PORT_FILTER!r})"
    )

    while True:
        now = time.time()
        if now - last_name_refresh >= 60:
            try:
                librenms_names = fetch_librenms_name_cache()
            except Exception as exc:
                log(f"[LINK] LibreNMS name refresh failed: {exc}")
            try:
                peer_map = build_peer_map(load_topology_edges())
            except Exception as exc:
                log(f"[LINK] topology peer refresh failed: {exc}")
            last_name_refresh = now

        try:
            ports = fetch_interconnect_ports(jobs_regex)
        except Exception as exc:
            mark_watcher_health("interconnect", False, exc)
            log(f"[LINK] poll failed: {exc}")
            time.sleep(INTERCONNECT_ALERT_POLL_INTERVAL)
            continue
        mark_watcher_health("interconnect", True)

        if now - last_status_log >= 60:
            degraded = sum(1 for p in ports if classify_interconnect(p["lag_up"], [m["up"] for m in p["members"]]) == "degraded")
            log(f"[LINK] watched aggregates total={len(ports)} degraded={degraded}")
            last_status_log = now

        for port in ports:
            ip = port.get("ip") or ""
            if ip in librenms_names:
                port["device"] = librenms_names[ip]
            member_ups = [m["up"] for m in port["members"]]
            status = classify_interconnect(port["lag_up"], member_ups)
            state = states.setdefault(port["key"], {
                "down_since": None,
                "alerting": False,
                "down_members": [],
                "handoff_logged": False,
            })

            # Alert both reduced redundancy and a fully/protocol-down aggregate.
            # The peer may remain reachable through another path, so handing the
            # latter off to device-down can otherwise miss the failure entirely.
            if status in ("degraded", "down"):
                down_members = [m["name"] for m in port["members"] if not m["up"]]
                if state["down_since"] is None:
                    state["down_since"] = now
                duration = max(0, now - state["down_since"])
                state["down_members"] = down_members
                if not state["alerting"] and duration >= INTERCONNECT_ALERT_FOR_SECONDS:
                    # Peer switch from LLDP: look up the down member ports first,
                    # then the aggregate; save it so recovery names it too.
                    peer = resolve_peer_switch(peer_map, port["ip"], down_members + [port["port"]])
                    state["peer_switch"] = peer
                    event = dict(port)
                    event["down_members"] = down_members
                    event["up_members"] = [m["name"] for m in port["members"] if m["up"]]
                    event["peer_switch"] = peer
                    event["duration"] = duration
                    event["status"] = status
                    log(f"[LINK] ALERT {event['device']} {event['port']} status={status}, down member(s)={down_members} peer={peer or '-'}")
                    if send_feishu(build_interconnect_card(event, recovered=False)):
                        state["alerting"] = True
                        state["handoff_logged"] = False
            else:
                if state["alerting"]:
                    if status == "healthy":
                        event = dict(port)
                        event["down_members"] = state.get("down_members") or []
                        event["up_members"] = [m["name"] for m in port["members"]]
                        event["peer_switch"] = state.get("peer_switch") or ""
                        event["duration"] = max(0, now - (state["down_since"] or now))
                        event["status"] = "healthy"
                        log(f"[LINK] RECOVER {event['device']} {event['port']} members back up")
                        if send_feishu(build_interconnect_card(event, recovered=True)):
                            state["alerting"] = False
                            state["down_since"] = None
                            state["down_members"] = []
                            state["handoff_logged"] = False
                else:
                    state["down_since"] = None
                    state["down_members"] = []
                    state["handoff_logged"] = False

        time.sleep(INTERCONNECT_ALERT_POLL_INTERVAL)


def build_topology_parents(edges, root_ip):
    """BFS from the core over LLDP adjacency -> {device_ip: uplink_parent_ip}.

    The root (core) has no parent. Devices not reachable from the core in the
    graph are simply omitted, so callers treat 'unknown parent' as a root cause
    (alert it) -- the suppression only ever fires on confidently-mapped victims.
    """
    adjacency = {}
    for edge in edges or []:
        a = str(edge.get("from_ip") or "").strip()
        b = str(edge.get("to_ip") or "").strip()
        if not a or not b or a == b:
            continue
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    root_ip = str(root_ip or "").strip()
    parents = {}
    if not root_ip or root_ip not in adjacency:
        return parents
    seen = {root_ip}
    queue = [root_ip]
    while queue:
        current = queue.pop(0)
        for neighbor in sorted(adjacency.get(current, ())):
            if neighbor in seen:
                continue
            seen.add(neighbor)
            parents[neighbor] = current
            queue.append(neighbor)
    return parents


def build_peer_map(edges):
    """(device_ip, local_port) -> peer switch name, from the LLDP topology, so a
    link alert can name the switch on the far end (not just the port alias)."""
    peers = {}
    for edge in edges or []:
        ip = str(edge.get("from_ip") or "").strip()
        port = str(edge.get("from_port") or "").strip()
        peer = str(edge.get("to_sysname") or edge.get("to_ip") or "").strip()
        if ip and port and peer:
            peers[(ip, port)] = peer
        reverse_ip = str(edge.get("to_ip") or "").strip()
        reverse_port = str(edge.get("to_port") or "").strip()
        reverse_peer = str(edge.get("from_sysname") or edge.get("from_ip") or "").strip()
        if reverse_ip and reverse_port and reverse_peer:
            peers[(reverse_ip, reverse_port)] = reverse_peer
    return peers


def resolve_peer_switch(peer_map, ip, ports):
    """First peer switch found among the given local ports (down members, then
    the aggregate). "" when the LLDP topology doesn't know the far end."""
    for port in ports:
        peer = peer_map.get((ip, str(port or "").strip()))
        if peer:
            return peer
    return ""


def is_down_symptom(ip, parents, unreachable):
    """True when ip sits below a currently-unreachable device in the tree, i.e.
    it is a downstream victim of an upstream outage rather than the root cause."""
    seen = set()
    current = parents.get(ip)
    while current and current not in seen:
        if current in unreachable:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


def count_down_descendants(ip, parents, unreachable):
    """How many currently-unreachable devices sit below ip in the tree (for the
    root-cause card's 'N downstream devices also unreachable' line)."""
    children = {}
    for node, parent in parents.items():
        children.setdefault(parent, []).append(node)
    count = 0
    seen = set()
    stack = list(children.get(ip, []))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        if node in unreachable:
            count += 1
        stack.extend(children.get(node, []))
    return count


def _topology_core_ip():
    """The tree root: explicit LIBRENMS_CORE_IP, else the first CORE_SWITCH_PING
    entry (stripping any 'name:' prefix and '-range' suffix)."""
    if LIBRENMS_CORE_IP:
        return LIBRENMS_CORE_IP
    first = os.environ.get("CORE_SWITCH_PING", "").split(",")[0].strip()
    if ":" in first:
        first = first.split(":", 1)[1].strip()
    return first.split("-")[0].strip()


def load_topology_edges():
    """Load the LLDP adjacency the topology collector writes; [] if absent."""
    try:
        with open(TOPOLOGY_EDGES_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def recovery_ready(state, now, sample_ts, recover_stable):
    """Flap-debounce for recoveries: an alerting target that is now UP must stay
    UP for `recover_stable` seconds before it counts as recovered. Tracks the
    continuous-up start in state['up_since']; a dip elsewhere clears it so the
    window restarts. Returns True once it has been stable long enough."""
    if state.get("up_since") is None:
        state["up_since"] = sample_ts if sample_ts is not None else now
    return (now - state["up_since"]) >= recover_stable


def device_retirement_due(state, job, now):
    """Return True when a temporary device's old outage must be retired."""
    jobs = {item.strip() for item in DEVICE_REENROLL_JOBS.split(",") if item.strip()}
    down_since = _as_float(state.get("down_since"))
    return bool(
        job in jobs
        and DEVICE_REENROLL_AFTER_SECONDS > 0
        and down_since is not None
        and (state.get("alerting") or state.get("seen_up"))
        and now - down_since >= DEVICE_REENROLL_AFTER_SECONDS
    )


def retire_expired_device_states(states, now):
    """End 48-hour outages even while their targets are absent from Prometheus."""
    retired_keys = []
    for key, state in states.items():
        if not device_retirement_due(state, state.get("job", ""), now):
            continue
        state["alerting"] = False
        state["retired"] = True
        state["retired_at"] = now
        state["down_since"] = None
        state["up_since"] = None
        state["seen_up"] = False
        state["online_sent"] = False
        state["online_pending"] = False
        state["librenms_deleted"] = False
        state["librenms_readded"] = False
        state["librenms_sync_last_attempt"] = 0
        retired_keys.append(key)
    return retired_keys


def sync_retired_librenms_deletions(states, now):
    """Delete newly retired LibreNMS devices, with bounded retries."""
    changed = False
    retry_after = max(1, DEVICE_LIBRENMS_SYNC_RETRY_SECONDS)
    for state in states.values():
        if not state.get("retired") or state.get("librenms_deleted"):
            continue
        last_attempt = _as_float(state.get("librenms_sync_last_attempt"), 0) or 0
        if now - last_attempt < retry_after:
            continue
        state["librenms_sync_last_attempt"] = now
        changed = True
        result = delete_librenms_device(state.get("ip"))
        if result in ("deleted", "missing"):
            state["librenms_deleted"] = True
            state["librenms_sync_last_attempt"] = 0
    return changed


def prepare_reenrolled_librenms_device(state, name, ip, now):
    """Create the fresh LibreNMS record before announcing a new lifecycle."""
    if not state.get("librenms_deleted"):
        return False
    if state.get("librenms_readded"):
        return True
    retry_after = max(1, DEVICE_LIBRENMS_SYNC_RETRY_SECONDS)
    last_attempt = _as_float(state.get("librenms_sync_last_attempt"), 0) or 0
    if now - last_attempt < retry_after:
        return False
    state["librenms_sync_last_attempt"] = now
    result = add_librenms_snmp_device(ip, name=name, log_prefix="[DOWN]")
    if result in ("added", "exists"):
        state["librenms_readded"] = True
        return True
    return False


def notify_device_reenrolled(state, name, ip):
    """Send the fresh online card and close the old outage only after delivery."""
    card = build_device_online_card({"display": name, "ip": ip})
    if not send_device_online_new_lifecycle(card, name, ip):
        return False
    state["alerting"] = False
    state["retired"] = False
    state["retired_at"] = None
    state["down_since"] = None
    state["up_since"] = None
    state["seen_up"] = True
    state["online_sent"] = True
    state["online_pending"] = False
    state["librenms_deleted"] = False
    state["librenms_readded"] = False
    state["librenms_sync_last_attempt"] = None
    return True


def device_down_watcher():
    """Fast device-down alerts off Prometheus blackbox ICMP (probe_success).

    Detects within ~DEVICE_DOWN_FOR_SECONDS instead of LibreNMS's minute-grained
    poll. name/IP come from the target's instance/target_ip labels.
    """
    if not DEVICE_DOWN_ENABLED:
        log("[DOWN] device-down watcher disabled")
        return
    jobs = [j.strip() for j in DEVICE_DOWN_JOBS.split(",") if j.strip()]
    if not jobs:
        log("[DOWN] no jobs configured, watcher disabled")
        return
    safe_jobs = [j for j in jobs if re.match(r"^[A-Za-z0-9_:.-]+$", j)]
    if not safe_jobs:
        log("[DOWN] no valid jobs configured, watcher disabled")
        return
    sample_window = max(1, DEVICE_DOWN_SAMPLE_WINDOW_SECONDS)
    query = 'min_over_time(probe_success{job=~"%s"}[%ss])' % ("|".join(safe_jobs), sample_window)
    time.sleep(20)  # let Prometheus/blackbox settle after a (re)start
    states = load_device_down_states()
    last_status_log = 0.0
    last_name_refresh = 0.0
    librenms_names = {}
    isp_names = _isp_target_names()
    auto_add_jobs = {j.strip() for j in DEVICE_AUTO_ADD_SNMP_JOBS.split(",") if j.strip()}
    auto_add_attempted = set()
    parents = {}
    core_ip = _topology_core_ip()
    log(
        "[DOWN] device-down watcher enabled "
        f"(jobs={','.join(jobs)}, for={DEVICE_DOWN_FOR_SECONDS}s, "
        f"isp_for={ISP_DOWN_FOR_SECONDS}s, poll={DEVICE_DOWN_POLL_INTERVAL}s, "
        f"sample_window={sample_window}s, "
        f"require_seen_up={DEVICE_DOWN_REQUIRE_SEEN_UP}, active_loaded={len(states)})"
    )

    while True:
        now = time.time()
        retired_keys = retire_expired_device_states(states, now)
        if retired_keys:
            for retired_key in retired_keys:
                retired_state = states[retired_key]
                log(
                    f"[DOWN] RETIRED {retired_state.get('job')} "
                    f"{retired_state.get('name')} ({retired_state.get('ip')}) after 48h offline"
                )
        librenms_state_changed = sync_retired_librenms_deletions(states, now)
        if retired_keys or librenms_state_changed:
            save_device_down_states(states)
        if now - last_name_refresh >= 60:
            try:
                librenms_names = fetch_librenms_name_cache()
            except Exception as exc:
                log(f"[DOWN] LibreNMS name refresh failed: {exc}")
            if DEVICE_DOWN_ROOT_CAUSE_ENABLED:
                try:
                    parents = build_topology_parents(load_topology_edges(), core_ip)
                except Exception as exc:
                    log(f"[DOWN] topology refresh failed: {exc}")
            last_name_refresh = now

        try:
            results = prometheus_query(query)
        except Exception as exc:
            mark_watcher_health("device-down", False, exc)
            log(f"[DOWN] poll failed: {exc}")
            time.sleep(DEVICE_DOWN_POLL_INTERVAL)
            continue
        mark_watcher_health("device-down", True)

        # Which targets are unreachable right now (raw, immediate) -- used to tell
        # a root-cause outage from its downstream victims.
        unreachable = set()
        for item in results:
            metric = item.get("metric") or {}
            target_ip = metric.get("target_ip") or ""
            try:
                if target_ip and float((item.get("value") or [None, "1"])[1]) < 1:
                    unreachable.add(target_ip)
            except (TypeError, ValueError):
                pass

        if now - last_status_log >= 60:
            counts = {}
            for item in results:
                job = (item.get("metric") or {}).get("job", "")
                counts[job] = counts.get(job, 0) + 1
            if "infra-isp-ping" in jobs and counts.get("infra-isp-ping", 0) == 0:
                log("[DOWN] no infra-isp-ping targets found; set ISP_PING and run ./apply-env.sh")
            else:
                summary = ", ".join(f"{job}={counts.get(job, 0)}" for job in jobs)
                log(f"[DOWN] targets {summary}")
            last_status_log = now

        for item in results:
            metric = item.get("metric") or {}
            job = metric.get("job", "")
            ip = metric.get("target_ip") or ""
            prom_name = metric.get("display_name") or metric.get("instance") or ip or "?"
            env_name = isp_names.get(ip) or isp_names.get(prom_name)
            if job == "infra-isp-ping":
                name = env_name or (prom_name if prom_name != ip else "") or ip or "?"
            else:
                name = librenms_names.get(ip) or env_name or prom_name
            key = f"{job}|{ip or prom_name}"
            try:
                sample = item.get("value") or [None, "1"]
                sample_ts = float(sample[0]) if sample[0] is not None else now
                up = float(sample[1]) >= 1
            except (TypeError, ValueError):
                continue

            down_for_seconds = ISP_DOWN_FOR_SECONDS if job == "infra-isp-ping" else DEVICE_DOWN_FOR_SECONDS
            known_by_librenms = bool(ip and ip in librenms_names)
            default_state = {
                "down_since": None,
                "alerting": False,
                "retired": False,
                "retired_at": None,
                "librenms_deleted": False,
                "librenms_readded": False,
                "librenms_sync_last_attempt": None,
                "seen_up": False,
                "ignored_initial_down": False,
                "last_up_at": None,
                "up_since": None,
                "online_sent": False,
                "online_pending": False,
                "name": "",
                "ip": "",
                "job": "",
            }
            recover_stable = ISP_RECOVER_STABLE_SECONDS if job == "infra-isp-ping" else DEVICE_RECOVER_STABLE_SECONDS
            state = states.setdefault(key, default_state.copy())
            for field, value in default_state.items():
                state.setdefault(field, value)
            state["name"] = name
            state["ip"] = ip
            state["job"] = job
            if not up:
                # A dip cancels any in-progress recovery debounce: the link must
                # restart its stable-up window before it counts as recovered.
                state["up_since"] = None
                if DEVICE_DOWN_REQUIRE_SEEN_UP and not state["seen_up"] and not state["alerting"]:
                    if not state["ignored_initial_down"]:
                        log(f"[DOWN] waiting for first UP before alerting {job} {prom_name} ({ip})")
                        state["ignored_initial_down"] = True
                    state["down_since"] = None
                    continue
                if state["down_since"] is None:
                    state["down_since"] = sample_ts or now
                if not state["alerting"] and now - state["down_since"] >= down_for_seconds:
                    # Root-cause suppression: if an upstream device is also down,
                    # this one is a downstream victim -- stay quiet (no alerting
                    # flag), keep down_since so it alerts later if the upstream
                    # recovers while it's still down.
                    if DEVICE_DOWN_ROOT_CAUSE_ENABLED and is_down_symptom(ip, parents, unreachable):
                        if not state.get("suppressed_logged"):
                            log(f"[DOWN] suppress downstream symptom {job} {name} ({ip}) behind a down upstream")
                            state["suppressed_logged"] = True
                    else:
                        state["suppressed_logged"] = False
                        offline = max(0, now - state["down_since"])
                        downstream = count_down_descendants(ip, parents, unreachable) if DEVICE_DOWN_ROOT_CAUSE_ENABLED else 0
                        log(f"[DOWN] ALERT {job} {name} ({ip}) DOWN downstream={downstream}")
                        if send_feishu(build_device_down_card(name, ip, recovered=False, offline_seconds=offline, job=job, downstream=downstream)):
                            state["alerting"] = True
                            state["retired"] = False
                            state["retired_at"] = None
                            state["seen_up"] = True
                            save_device_down_states(states)
            else:
                previous_down_since = state.get("down_since")
                was_retired = bool(state.get("retired"))
                state["last_up_at"] = sample_ts or now
                first_up_after_candidate_down = (not state["seen_up"] and state.get("ignored_initial_down"))
                if not state["seen_up"]:
                    state["seen_up"] = True
                    state["ignored_initial_down"] = False
                    log(f"[DOWN] armed {job} {name} ({ip}) after first UP")
                    if (
                        not was_retired
                        and DEVICE_AUTO_ADD_FROM_PING
                        and job in auto_add_jobs
                        and ip
                        and not known_by_librenms
                        and ip not in auto_add_attempted
                    ):
                        auto_add_attempted.add(ip)
                        add_librenms_snmp_device(ip)
                    if first_up_after_candidate_down and DEVICE_ONLINE_FROM_PING and not state["online_sent"]:
                        state["online_pending"] = True
                        log(f"[DOWN] online detected from ping: {job} {name} ({ip})")
                if was_retired:
                    log(f"[DOWN] REENROLL {job} {name} ({ip}) as a new device")
                    if not prepare_reenrolled_librenms_device(state, name, ip, now):
                        log(f"[DOWN] REENROLL LibreNMS delete/re-add pending {job} {name} ({ip})")
                        save_device_down_states(states)
                        continue
                    if notify_device_reenrolled(state, name, ip):
                        log(f"[DOWN] NEW LIFECYCLE {job} {name} ({ip}) online card delivered")
                    else:
                        log(f"[DOWN] REENROLL delivery pending {job} {name} ({ip})")
                    save_device_down_states(states)
                    continue
                if DEVICE_ONLINE_FROM_PING and state.get("online_pending") and not state["online_sent"]:
                    if send_device_online_once(build_device_online_card({
                            "display": name,
                            "ip": ip,
                            "os": "Ping only",
                        }), name, ip):
                        state["online_sent"] = True
                        state["online_pending"] = False
                if state["alerting"]:
                    # Debounce recovery: wait until the target has been continuously
                    # UP for recover_stable seconds before sending the recovery card,
                    # so a flapping link doesn't spam up/down messages.
                    if not recovery_ready(state, now, sample_ts, recover_stable):
                        # Still settling -- stay in the alerting state, send nothing.
                        save_device_down_states(states)
                        continue
                    recovered_at = state["up_since"]
                    offline = max(0, recovered_at - (previous_down_since or recovered_at))
                    stable_for = now - recovered_at
                    log(f"[DOWN] RECOVER {job} {name} ({ip}) offline={int(offline)}s stable={int(stable_for)}s")
                    if send_feishu(build_device_down_card(name, ip, recovered=True, offline_seconds=offline, job=job)):
                        state["alerting"] = False
                        state["down_since"] = None
                        state["up_since"] = None
                    save_device_down_states(states)
                else:
                    state["down_since"] = None
                    state["up_since"] = None

        time.sleep(DEVICE_DOWN_POLL_INTERVAL)


def _ap_online_from_labels(metric):
    for field in ("state", "status", "stat", "connected", "up", "disabled"):
        raw = str(metric.get(field) or "").strip().lower()
        if not raw:
            continue
        if field == "disabled" and raw in ("1", "true", "yes", "on", "disabled"):
            return False
        if re.search(r"offline|disconnect|disconnected|down|unknown|false|^0$", raw):
            return False
        if re.search(r"online|connected|active|adopted|true|^1$", raw):
            return True
    return None


def _optional_prometheus_query(query):
    try:
        return prometheus_query(query)
    except Exception:
        return []


UNIFI_CONTROLLER_AP_CACHE = {"ts": 0.0, "items": {}}
UNIFI_CONTROLLER_WARN_TS = 0.0


def _unifi_controller_enabled():
    return bool(UNIFI_CONTROLLER_URL and UNIFI_CONTROLLER_USER and UNIFI_CONTROLLER_PASS)


def _unifi_request_json(opener, path, payload=None, method=None):
    url = UNIFI_CONTROLLER_URL + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method or ("POST" if payload is not None else "GET"))
    with opener.open(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw or "{}")


def _unifi_login():
    jar = CookieJar()
    handlers = [request.HTTPCookieProcessor(jar)]
    if UNIFI_CONTROLLER_URL.startswith("https://") and not UNIFI_CONTROLLER_VERIFY_SSL:
        handlers.append(request.HTTPSHandler(context=ssl._create_unverified_context()))
    opener = request.build_opener(*handlers)
    payload = {"username": UNIFI_CONTROLLER_USER, "password": UNIFI_CONTROLLER_PASS, "remember": True}
    last_error = None
    for path in ("/api/auth/login", "/api/login"):
        try:
            _unifi_request_json(opener, path, payload=payload, method="POST")
            return opener
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"login failed: {last_error}")


def _unifi_sites(opener):
    configured = [part.strip() for part in UNIFI_CONTROLLER_SITES.split(",") if part.strip()]
    if configured and configured != ["all"]:
        return configured
    for path in ("/proxy/network/api/self/sites", "/api/self/sites"):
        try:
            data = _unifi_request_json(opener, path)
            sites = [
                str(site.get("name") or "").strip()
                for site in (data.get("data") or [])
                if str(site.get("name") or "").strip()
            ]
            if sites:
                return sites
        except Exception:
            continue
    return ["default"]


def _unifi_ap_online(device):
    raw_state = device.get("state")
    if raw_state is not None:
        return str(raw_state).strip() == "1"
    for field in ("connected", "up", "adopted"):
        if field in device:
            return bool(device.get(field))
    raw_status = str(device.get("status") or "").strip().lower()
    if raw_status:
        if re.search(r"offline|disconnect|disconnected|down|unknown|false|^0$", raw_status):
            return False
        if re.search(r"online|connected|active|adopted|true|^1$", raw_status):
            return True
    return True


def _best_unifi_ap_name(device):
    for field in ("name", "display_name", "hostname", "ap_name", "mac"):
        value = str(device.get(field) or "").strip()
        if value and not _looks_like_ip(value) and not re.fullmatch(r"\d+", value):
            return value
    return ""


def _fetch_unifi_controller_aps():
    opener = _unifi_login()
    sites = _unifi_sites(opener)
    aps = {}
    prefixes = ("/proxy/network/api", "/api")
    for site in sites:
        site_enc = parse.quote(site, safe="")
        site_devices = None
        for prefix in prefixes:
            try:
                site_devices = _unifi_request_json(opener, f"{prefix}/s/{site_enc}/stat/device")
                break
            except Exception:
                continue
        if not site_devices:
            continue
        for device in site_devices.get("data") or []:
            dev_type = str(device.get("type") or "").lower()
            if dev_type and not dev_type.startswith("uap"):
                continue
            ip = str(device.get("ip") or device.get("last_ip") or device.get("fixed_ip") or "").strip()
            name = _best_unifi_ap_name(device)
            key = str(device.get("mac") or ip or name).strip()
            if not key or not name:
                continue
            aps[key] = {
                "key": key,
                "name": name,
                "ip": ip,
                "model": str(
                    device.get("model_display")
                    or device.get("model_name")
                    or device.get("display_model")
                    or device.get("model")
                    or device.get("board_rev")
                    or ""
                ).strip(),
                "online": _unifi_ap_online(device),
                "source": "controller",
            }
    return aps


def fetch_unifi_controller_aps_cached():
    global UNIFI_CONTROLLER_WARN_TS
    if not _unifi_controller_enabled():
        return {}
    now = time.time()
    if now - UNIFI_CONTROLLER_AP_CACHE["ts"] < UNIFI_CONTROLLER_REFRESH_SECONDS:
        return UNIFI_CONTROLLER_AP_CACHE["items"]
    try:
        items = _fetch_unifi_controller_aps()
        UNIFI_CONTROLLER_AP_CACHE["ts"] = now
        UNIFI_CONTROLLER_AP_CACHE["items"] = items
        return items
    except Exception as exc:
        if now - UNIFI_CONTROLLER_WARN_TS >= 300:
            log(f"[AP] UniFi controller API fetch failed: {exc}")
            UNIFI_CONTROLLER_WARN_TS = now
        return UNIFI_CONTROLLER_AP_CACHE["items"]


def _ap_display_name(metric):
    for field in ("display_name", "name", "hostname", "mac"):
        value = str(metric.get(field) or "").strip()
        if value:
            return value
    return ""


def _norm_ap_name(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _should_sync_ap_name(info):
    name = str(info.get("name") or "").strip()
    if not name or _looks_like_ip(name):
        return False
    if info.get("source") == "controller":
        return True
    model = str(info.get("model") or "").strip()
    return not model or _norm_ap_name(name) != _norm_ap_name(model)


def _ap_online_metric_map():
    online = {}
    queries = [
        'max by (name, mac) (unpoller_device_up{type="uap"})',
        'max by (name, mac) (unpoller_device_connected{type="uap"})',
        'max by (name, mac) (unpoller_device_state{type="uap"})',
        'max by (name, mac) (unpoller_device_status{type="uap"})',
        'max by (name, mac) (unpoller_device_uptime_seconds{type="uap"} > bool 0)',
        'max by (name, mac) (unpoller_device_uptime{type="uap"} > bool 0)',
    ]
    for query in queries:
        for item in _optional_prometheus_query(query):
            metric = item.get("metric") or {}
            key = metric.get("mac") or metric.get("name") or ""
            if not key:
                continue
            try:
                value = float((item.get("value") or [None, "0"])[1])
            except (TypeError, ValueError):
                continue
            online[key] = value > 0
    return online


def unifi_ap_watcher():
    """UniFi AP up/down alerts off UniFi Poller (unpoller) metrics in Prometheus.

    unpoller_device_info is identity metadata and can still exist for offline
    APs, so the watcher first uses explicit up/connected/state/status/uptime
    metrics when the exporter exposes them. If only info exists, it falls back
    to the older behaviour where presence means online.
    No UniFi configured => the query is empty => the watcher idles silently
    (safe for events that don't use UniFi). APs already down at startup are not
    alerted (never seen up), matching the infra device-down watcher's behaviour.
    """
    if not UNIFI_AP_ALERT_ENABLED:
        log("[AP] UniFi AP watcher disabled")
        return
    query = 'unpoller_device_info{type="uap"}'
    time.sleep(20)  # let Prometheus/unpoller settle after a (re)start
    states = {}
    snmp_add_attempted = {}
    snmp_confirmed_exists = set()  # IPs confirmed in LibreNMS — skip future add retries
    name_sync_attempted = {}
    name_last_synced = {}  # last display name successfully synced per IP
    last_status_log = 0.0
    log(
        "[AP] UniFi AP watcher enabled "
        f"(for={UNIFI_AP_DOWN_FOR_SECONDS}s, poll={UNIFI_AP_POLL_INTERVAL}s, "
        f"snmp_auto_add={UNIFI_AP_SNMP_AUTO_ADD}, "
        f"controller_api={_unifi_controller_enabled()})"
    )

    while True:
        now = time.time()
        try:
            results = prometheus_query(query)
        except Exception as exc:
            mark_watcher_health("unifi-ap", False, exc)
            log(f"[AP] poll failed: {exc}")
            time.sleep(UNIFI_AP_POLL_INTERVAL)
            continue
        mark_watcher_health("unifi-ap", True)

        metric_online = _ap_online_metric_map()
        controller_aps = fetch_unifi_controller_aps_cached()
        current = {}
        known = {}
        explicit_online = {}
        for item in results:
            metric = item.get("metric") or {}
            key = metric.get("mac") or metric.get("name") or ""
            name = _ap_display_name(metric)
            if not key or not name:
                continue
            metric_ip = (
                metric.get("ip")
                or metric.get("ip_address")
                or metric.get("address")
                or metric.get("host")
                or ""
            )
            controller_info = controller_aps.get(key) or {}
            if not controller_info and controller_aps:
                for ap_info in controller_aps.values():
                    if (metric_ip and ap_info.get("ip") == metric_ip) or (name and ap_info.get("name") == name):
                        controller_info = ap_info
                        key = ap_info.get("key") or key
                        break
            if controller_aps and not controller_info:
                continue
            info = {
                "name": controller_info.get("name") or name,
                "ip": controller_info.get("ip") or metric_ip,
                "model": controller_info.get("model") or metric.get("model") or "",
                "source": controller_info.get("source") or "prometheus",
            }
            known[key] = info
            label_online = _ap_online_from_labels(metric)
            is_online = metric_online.get(key)
            if is_online is None and label_online is not None:
                is_online = label_online
            if is_online is not None:
                explicit_online[key] = is_online
            elif controller_info:
                is_online = bool(controller_info.get("online"))
            else:
                is_online = True
            if is_online:
                current[key] = info

        for key, controller_info in controller_aps.items():
            info = known.get(key, {})
            merged = {
                "name": controller_info.get("name") or info.get("name") or key,
                "ip": controller_info.get("ip") or info.get("ip") or "",
                "model": controller_info.get("model") or info.get("model") or "",
                "source": controller_info.get("source") or info.get("source") or "controller",
            }
            known[key] = merged
            if key in explicit_online:
                if explicit_online[key]:
                    current[key] = merged
            elif controller_info.get("online"):
                current[key] = merged

        # Seen APs: refresh metadata, arm on first sight, recover if was down.
        for key, info in current.items():
            name = info.get("name") or key
            sync_name = name if _should_sync_ap_name(info) else ""
            ip = info.get("ip") or ""
            add_attempted = False
            if UNIFI_AP_SNMP_AUTO_ADD and ip and ip not in snmp_confirmed_exists:
                last_attempt = snmp_add_attempted.get(ip, 0)
                if now - last_attempt >= UNIFI_AP_SNMP_ADD_RETRY_SECONDS:
                    snmp_add_attempted[ip] = now
                    add_attempted = True
                    add_result = add_librenms_snmp_device(
                        ip,
                        name=sync_name,
                        community=UNIFI_AP_SNMP_COMMUNITY,
                        log_prefix="[AP]",
                    )
                    if add_result == "exists":
                        snmp_confirmed_exists.add(ip)
                    if add_result in ("added", "exists"):
                        action = "added" if add_result == "added" else "first seen (already in LibreNMS)"
                        card = build_device_online_card({
                            "display": name,
                            "ip": ip,
                            "hardware": info.get("model") or "",
                        })
                        if send_device_online_once(card, name, ip):
                            log(f"[AP] AP deployment notification confirmed: {name} ({ip}), {action}")
            if ip and sync_name and not add_attempted:
                last_sync = name_sync_attempted.get(ip, 0)
                if now - last_sync >= UNIFI_AP_NAME_SYNC_SECONDS:
                    name_sync_attempted[ip] = now
                    if name_last_synced.get(ip) != sync_name:
                        name_last_synced[ip] = sync_name
                        update_librenms_device_display(ip, sync_name, log_prefix="[AP]")
            state = states.setdefault(key, {
                "alerting": False, "down_since": None, "seen_up": False,
                "last_seen": now, "name": name, "ip": "", "model": "",
            })
            state["name"] = name
            state["ip"] = info["ip"] or state["ip"]
            state["model"] = info["model"] or state["model"]
            state["last_seen"] = now
            if not state["seen_up"]:
                state["seen_up"] = True
                log(f"[AP] armed {name} ({state['ip']}) after first seen")
            if state["alerting"]:
                offline = max(0, now - (state.get("down_since") or now))
                log(f"[AP] RECOVER {name} ({state['ip']}) offline={int(offline)}s")
                if send_feishu(build_ap_down_card(name, state["ip"], state["model"],
                                                  recovered=True, offline_seconds=offline)):
                    state["alerting"] = False
                    state["down_since"] = None
            else:
                state["down_since"] = None

        # Previously-seen APs now missing => down after debounce.
        for key, state in list(states.items()):
            if key in current or not state.get("seen_up"):
                continue
            if controller_aps and key not in known:
                log(f"[AP] retired {state.get('name') or key}: removed from UniFi controller, no down alert")
                states.pop(key, None)
                continue
            if key in known:
                state["name"] = known[key].get("name") or state.get("name") or key
                state["ip"] = known[key].get("ip") or state.get("ip") or ""
                state["model"] = known[key].get("model") or state.get("model") or ""
            if state.get("down_since") is None:
                state["down_since"] = state.get("last_seen") or now
            if not state["alerting"] and now - state["down_since"] >= UNIFI_AP_DOWN_FOR_SECONDS:
                offline = max(0, now - state["down_since"])
                name = state.get("name") or key
                log(f"[AP] ALERT {name} ({state['ip']}) DOWN")
                if send_feishu(build_ap_down_card(name, state["ip"], state["model"],
                                                  recovered=False, offline_seconds=offline)):
                    state["alerting"] = True

        if now - last_status_log >= 60:
            down = sum(1 for s in states.values() if s.get("alerting"))
            missing_ip = sum(1 for info in current.values() if not info.get("ip"))
            log(
                f"[AP] {len(current)} online / {len(known)} listed / {len(states)} known / "
                f"{down} down / {missing_ip} online without IP"
            )
            last_status_log = now

        time.sleep(UNIFI_AP_POLL_INTERVAL)


def _clean_iface_token(value):
    return str(value or "").strip().rstrip(".,;:")


def _normalize_iface_key(value):
    token = _clean_iface_token(value).lower().replace(" ", "")
    replacements = [
        ("hundredgigabitethernet", "hu"),
        ("twentyfivegigabitethernet", "twe"),
        ("fortygigabitethernet", "fo"),
        ("tengigabitethernet", "te"),
        ("gigabitethernet", "gi"),
        ("fastethernet", "fa"),
        ("port-channel", "po"),
        ("portchannel", "po"),
    ]
    for full, short in replacements:
        if token.startswith(full):
            return short + token[len(full):]
    return token


def _syslog_event_enabled(kind):
    return "all" in SYSLOG_ALERT_TYPES or str(kind or "").lower() in SYSLOG_ALERT_TYPES


def _network_event_port(event):
    if not event:
        return ""
    if event.get("kind") == "native_vlan_mismatch":
        return event.get("local_port") or ""
    return event.get("port") or ""


def _network_event_priority(kind):
    return {
        "native_vlan_mismatch": 3,
        "loopback": 2,
        "errdisable": 1,
        "bpduguard": 1,
    }.get(str(kind or ""), 0)


def parse_link_state_event(message):
    match = _LINK_STATE_RE.search(str(message or ""))
    if not match:
        return None
    port, state = match.groups()
    return {
        "port": _clean_iface_token(port),
        "state": state.lower(),
    }


def _is_bpdu_event(event):
    kind = str((event or {}).get("kind") or "").lower()
    reason = str((event or {}).get("reason") or "").lower()
    return kind == "bpduguard" or (kind == "errdisable" and "bpdu" in reason)


def parse_network_syslog_event(message):
    text = str(message or "").strip()

    match = _NATIVE_VLAN_RE.search(text)
    if match:
        local_port, local_vlan, peer_device, peer_port, peer_vlan = match.groups()
        local_port = _clean_iface_token(local_port)
        peer_port = _clean_iface_token(peer_port)
        return {
            "kind": "native_vlan_mismatch",
            "title": "🚨 接入口疑似串线",
            "color": "red",
            "local_port": local_port,
            "local_vlan": local_vlan,
            "peer_device": peer_device.strip(),
            "peer_port": peer_port,
            "peer_vlan": peer_vlan,
            "dedupe": f"native|{local_port}|{local_vlan}|{peer_device.strip()}|{peer_port}|{peer_vlan}",
            "hint": "两个 access/native VLAN 不一致的端口互相收到了 CDP，常见于跳线、小交换机、AP 第二网口把两个口桥在一起。",
        }

    match = _MACFLAP_RE.search(text)
    if match:
        mac, vlan, port_a, port_b = match.groups()
        port_a = _clean_iface_token(port_a)
        port_b = _clean_iface_token(port_b)
        return {
            "kind": "mac_flap",
            "title": "🚨 MAC 地址漂移",
            "color": "red",
            "mac": _format_mac(mac),
            "vlan": vlan,
            "port_a": port_a,
            "port_b": port_b,
            "dedupe": f"macflap|{_normalize_mac_hex(mac)}|{vlan}|{port_a}|{port_b}",
            "hint": "同一个 MAC 在两个端口之间反复学习，通常是二层环路、无线桥接、AP Mesh/第二网口或错误跳线。",
        }

    match = _ERRDISABLE_RE.search(text)
    if match:
        reason, port = match.groups()
        reason = reason.strip()
        port = _clean_iface_token(port)
        return {
            "kind": "errdisable",
            "title": "🛑 接口被保护关闭",
            "color": "orange",
            "port": port,
            "reason": reason,
            "dedupe": f"errdisable|{port}|{reason.lower()}",
            "hint": "交换机已把接口放入 err-disabled；按原因检查 BPDU、风暴、环路或链路抖动。",
        }

    if "BPDUGUARD" in text.upper() or "BPDU Guard" in text:
        match = _BPDUGUARD_RE.search(text)
        port = _clean_iface_token(match.group(1)) if match else ""
        return {
            "kind": "bpduguard",
            "title": "⛔ BPDU blocked: Has worsened",
            "color": "red",
            "port": port,
            "dedupe": f"bpduguard|{port}|{text[:100]}",
            "hint": "普通终端/AP 接入口不应该收到 BPDU；后面可能接了交换机、桥接设备或形成环路。",
        }

    if "STORM_CONTROL" in text.upper() or "storm-control" in text.lower() or "storm control" in text.lower():
        match = _STORM_RE.search(text)
        port = _clean_iface_token(match.group(1)) if match else ""
        return {
            "kind": "storm_control",
            "title": "🛑 广播/组播风暴",
            "color": "orange",
            "port": port,
            "dedupe": f"storm|{port}|{text[:100]}",
            "hint": "广播或组播流量超过阈值，端口可能已被 storm-control 关闭。",
        }

    if "LOOP_BACK_DETECTED" in text.upper():
        match = _LOOPBACK_RE.search(text)
        port = _clean_iface_token(match.group(1)) if match else ""
        return {
            "kind": "loopback",
            "title": "🛑 端口检测到回环",
            "color": "red",
            "port": port,
            "dedupe": f"loopback|{port}|{text[:100]}",
            "hint": "接口检测到二层回环，优先查该口后面的跳线、AP 第二网口或小交换机。",
        }

    return None


def build_network_syslog_card(host, message, event, recovered=False, duration=0):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    device = _host_display_name(host)
    dev_text = f"{device} ({host})" if host and host != device else device

    lines = [f"🖥 设备：{dev_text}"]
    kind = event.get("kind")

    if _is_bpdu_event(event):
        lines.extend([
            f"🔌 接口：{event.get('port') or '未解析到'}",
        ])
        if event.get("reason"):
            lines.append(f"📋 原因：{event.get('reason')}")
        if recovered:
            lines.append("✅ 状态：已恢复")
            lines.append(f"⏳ 恢复耗时：{format_alert_duration(duration, recovered=True)}")
            lines.append(f"⏰ 时间：{ts}")
            return _make_card(next_event_title(), "🟢 BPDU 保护恢复", "green", "\n".join(lines))
        lines.append(f"⏰ 时间：{ts}")
        return _make_card(next_event_title(), "⛔ BPDU 保护触发", "red", "\n".join(lines))

    if kind == "native_vlan_mismatch":
        lines.extend([
            f"🔌 本地接口：{event.get('local_port')} / VLAN {event.get('local_vlan')}",
            f"🔁 对端：{event.get('peer_device')} {event.get('peer_port')} / VLAN {event.get('peer_vlan')}",
        ])
    elif kind == "mac_flap":
        lines.extend([
            f"🔗 MAC：{event.get('mac')}",
            f"🏷 VLAN：{event.get('vlan')}",
            f"🔌 端口：{event.get('port_a')} ↔ {event.get('port_b')}",
        ])
    elif kind == "errdisable":
        lines.extend([
            f"🔌 接口：{event.get('port') or '未知'}",
            f"📋 原因：{event.get('reason') or '未知'}",
        ])
    else:
        lines.extend([
            f"🔌 接口：{event.get('port') or '未解析到'}",
        ])

    if recovered:
        lines.append("✅ 状态：已恢复")
        lines.append(f"⏳ 恢复耗时：{format_alert_duration(duration, recovered=True)}")
    lines.append(f"⏰ 时间：{ts}")

    if recovered:
        recovery_titles = {
            "native_vlan_mismatch": "🟢 接入口疑似串线恢复",
            "loopback": "🟢 端口回环恢复",
            "errdisable": "🟢 接口保护恢复",
        }
        return _make_card(
            next_event_title(),
            recovery_titles.get(kind, "🟢 网络安全事件恢复"),
            "green",
            "\n".join(lines),
        )

    return _make_card(next_event_title(), event.get("title") or "⚠️ 网络安全事件", event.get("color") or "orange", "\n".join(lines))


def syslog_watcher():
    log(f"[SYSLOG] watching {SYSLOG_FILE} for network security events")
    _last_sent = {}
    _recent_priority_ports = {}
    _pending_events = {}
    _active_events = {}
    _failed_events = {}
    rate_limit = max(1, SYSLOG_EVENT_RATE_LIMIT)
    network_realert = max(rate_limit, SYSLOG_REALERT_SECONDS)
    recover_stable = max(0, SYSLOG_RECOVER_STABLE_SECONDS)
    correlation_window = max(0, SYSLOG_CORRELATION_SECONDS)

    def _port_key(host, port):
        return "|".join([str(host or ""), _normalize_iface_key(port)])

    def _purge_recent_priority(now):
        if correlation_window <= 0:
            _recent_priority_ports.clear()
            return
        cutoff = now - correlation_window
        for key, entry in list(_recent_priority_ports.items()):
            if entry.get("ts", 0) < cutoff:
                _recent_priority_ports.pop(key, None)

    def _has_recent_higher_priority(host, port, kind, now):
        if not port or correlation_window <= 0:
            return False
        _purge_recent_priority(now)
        entry = _recent_priority_ports.get(_port_key(host, port))
        if not entry:
            return False
        return entry.get("priority", 0) > _network_event_priority(kind)

    def _remember_recent_event(host, event, now):
        priority = _network_event_priority(event.get("kind"))
        if priority <= 0 or correlation_window <= 0:
            return
        port = _network_event_port(event)
        if not port:
            return
        _recent_priority_ports[_port_key(host, port)] = {
            "kind": event.get("kind"),
            "priority": priority,
            "ts": now,
        }

    def _send_network_event(host, message, event, now):
        port = _network_event_port(event)
        # A re-fire means the problem is still ongoing: cancel any in-progress
        # recovery so a flapping port doesn't emit improved/worsened churn.
        if port:
            active = _active_events.get(_port_key(host, port))
            if active:
                active.pop("recovering_since", None)
        dedupe_key = "|".join([host, event.get("dedupe") or event.get("kind") or message[:120]])
        if now - _last_sent.get(dedupe_key, 0) >= network_realert:
            log(
                f"[SYSLOG] {event.get('kind')} from {host} "
                f"port={event.get('local_port') or event.get('port') or '-'}"
            )
            sent = send_feishu(build_network_syslog_card(host, message, event))
            if sent:
                _last_sent[dedupe_key] = now
                _failed_events.pop(dedupe_key, None)
                if SYSLOG_RECOVERY_ENABLED and _network_event_priority(event.get("kind")) > 0 and port:
                    _active_events[_port_key(host, port)] = {
                        "host": host,
                        "message": message,
                        "event": event,
                        "started": now,
                    }
            else:
                previous = _failed_events.get(dedupe_key, {})
                _failed_events[dedupe_key] = {
                    "host": host,
                    "message": message,
                    "event": event,
                    "retry_at": now + FEISHU_FAILED_EVENT_RETRY_SECONDS,
                    "attempts": int(previous.get("attempts", 0)) + 1,
                }
        _remember_recent_event(host, event, now)

    def _flush_failed_events(now):
        for item in list(_failed_events.values()):
            if now < item.get("retry_at", 0):
                continue
            _send_network_event(item["host"], item["message"], item["event"], now)

    def _drop_pending_lower_for(host, port, priority):
        key = _port_key(host, port)
        pending = _pending_events.get(key)
        if pending and _network_event_priority(pending["event"].get("kind")) < priority:
            _pending_events.pop(key, None)

    def _queue_pending_event(host, message, event, now):
        port = _network_event_port(event)
        key = _port_key(host, port)
        priority = _network_event_priority(event.get("kind"))
        pending = _pending_events.get(key)
        if pending and _network_event_priority(pending["event"].get("kind")) >= priority:
            log(
                f"[SYSLOG] suppressed {event.get('kind')} from {host} "
                f"port={port or '-'} after pending higher/equal priority event"
            )
            return
        _pending_events[key] = {
            "host": host,
            "message": message,
            "event": event,
            "due": now + correlation_window,
        }

    def _flush_pending_events(now):
        for key, pending in list(_pending_events.items()):
            if now < pending["due"]:
                continue
            _pending_events.pop(key, None)
            if _has_recent_higher_priority(
                pending["host"],
                _network_event_port(pending["event"]),
                pending["event"].get("kind"),
                now,
            ):
                log(
                    f"[SYSLOG] suppressed {pending['event'].get('kind')} from {pending['host']} "
                    f"port={_network_event_port(pending['event']) or '-'} after higher priority event"
                )
                continue
            _send_network_event(pending["host"], pending["message"], pending["event"], now)

    def _emit_recovery(active, now):
        event = active["event"]
        duration = max(0, now - active.get("started", now))
        log(
            f"[SYSLOG] RECOVER {event.get('kind')} from {active['host']} "
            f"port={_network_event_port(event) or '-'} duration={int(duration)}s"
        )
        return send_feishu(build_network_syslog_card(
            active["host"], active["message"], event, recovered=True, duration=duration,
        ))

    def _send_recovery_if_active(host, port, now):
        if not SYSLOG_RECOVERY_ENABLED:
            return False
        key = _port_key(host, port)
        _pending_events.pop(key, None)
        active = _active_events.get(key)
        if not active:
            return False
        if recover_stable <= 0:
            active["recovering_since"] = now
            if _emit_recovery(active, now):
                _active_events.pop(key, None)
            else:
                active["recovery_retry_at"] = now + FEISHU_FAILED_EVENT_RETRY_SECONDS
            return True
        # Debounce: the port must stay clear for recover_stable before we call it
        # recovered; a re-fire (in _send_network_event) clears this timestamp.
        active.setdefault("recovering_since", now)
        return True

    def _flush_recoveries(now):
        for key, active in list(_active_events.items()):
            started = active.get("recovering_since")
            retry_at = active.get("recovery_retry_at", 0)
            if started is not None and now - started >= recover_stable and now >= retry_at:
                if _emit_recovery(active, now):
                    _active_events.pop(key, None)
                else:
                    active["recovery_retry_at"] = now + FEISHU_FAILED_EVENT_RETRY_SECONDS

    while not os.path.exists(SYSLOG_FILE):
        mark_watcher_health("syslog", False, f"waiting for {SYSLOG_FILE}")
        time.sleep(5)

    try:
        f = open(SYSLOG_FILE)
        f.seek(0, 2)
        current_ino = os.fstat(f.fileno()).st_ino
    except OSError as exc:
        mark_watcher_health("syslog", False, exc)
        log(f"[SYSLOG] cannot open {SYSLOG_FILE}: {exc}")
        return

    while True:
        mark_watcher_health("syslog", True)
        _flush_failed_events(time.time())
        _flush_pending_events(time.time())
        _flush_recoveries(time.time())
        line = f.readline()
        if not line:
            _flush_failed_events(time.time())
            _flush_pending_events(time.time())
            _flush_recoveries(time.time())
            time.sleep(0.5)
            try:
                if os.stat(SYSLOG_FILE).st_ino != current_ino:
                    f.close()
                    f = open(SYSLOG_FILE)
                    current_ino = os.fstat(f.fileno()).st_ino
                    log("[SYSLOG] log rotated, reopened file")
            except OSError:
                pass
            continue

        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        host, _severity, message = parts
        now = time.time()

        link_state = parse_link_state_event(message)
        if link_state and link_state.get("state") == "up":
            _send_recovery_if_active(host, link_state.get("port"), now)
            continue

        event = parse_network_syslog_event(message)
        if event:
            if not _syslog_event_enabled(event.get("kind")):
                continue
            kind = event.get("kind")
            if kind == "native_vlan_mismatch":
                _drop_pending_lower_for(host, _network_event_port(event), _network_event_priority(kind))
                _send_network_event(host, message, event, now)
                continue
            if kind == "loopback":
                if _has_recent_higher_priority(host, _network_event_port(event), kind, now):
                    log(
                        f"[SYSLOG] suppressed loopback from {host} "
                        f"port={_network_event_port(event) or '-'} after higher priority event"
                    )
                    continue
                _drop_pending_lower_for(host, _network_event_port(event), _network_event_priority(kind))
                if correlation_window > 0:
                    _queue_pending_event(host, message, event, now)
                else:
                    _send_network_event(host, message, event, now)
                continue
            if kind == "errdisable" and correlation_window > 0:
                if _has_recent_higher_priority(host, event.get("port"), kind, now):
                    log(
                        f"[SYSLOG] suppressed errdisable from {host} "
                        f"port={event.get('port') or '-'} after higher priority event"
                    )
                    continue
                _queue_pending_event(host, message, event, now)
                continue
            _send_network_event(host, message, event, now)


def device_watcher():
    log(f"[WATCHER] starting, interval={SWITCH_WATCH_INTERVAL}s, url={LIBRENMS_URL}")
    time.sleep(10)  # Give LibreNMS/API token a short moment after container start.

    token = _librenms_token()
    if not token:
        log("[WATCHER] no API token available, retrying in 60s...")
        time.sleep(60)
        token = _librenms_token()
        if not token:
            log("[WATCHER] still no token, watcher disabled")
            return

    with DEVICE_ONLINE_STATE_LOCK:
        notified = _load_json_set(DEVICE_ONLINE_STATE_FILE)
    log(f"[WATCHER] loaded {len(notified)} notified devices")
    first_successful_poll = True
    model_wait_started = {}
    while True:
        try:
            token = _librenms_token()
            if not token:
                mark_watcher_health("device-online", False, "LibreNMS token unavailable")
                log("[WATCHER] token lost, skipping poll")
                time.sleep(SWITCH_WATCH_INTERVAL)
                continue
            devices = fetch_librenms_devices(token)
        except Exception as exc:
            mark_watcher_health("device-online", False, exc)
            log(f"[WATCHER] poll failed: {exc}")
            time.sleep(SWITCH_WATCH_INTERVAL)
            continue
        mark_watcher_health("device-online", True)

        with DEVICE_ONLINE_STATE_LOCK:
            persisted_notified = _load_json_set(DEVICE_ONLINE_STATE_FILE)
        if persisted_notified:
            notified.update(persisted_notified)

        changed = False
        if first_successful_poll and not notified:
            seeded = 0
            for dev in devices:
                if _is_ping_only_device(dev):
                    continue
                online_dev = _enrich_device_with_unifi(dev)
                if not _has_meaningful_device_name(online_dev):
                    continue
                key = online_dev.get("hostname") or online_dev.get("ip")
                ip = online_dev.get("ip") or online_dev.get("hostname")
                keys = {value for value in (key, ip) if value}
                notified.update(keys)
                seeded += 1
            if seeded:
                with DEVICE_ONLINE_STATE_LOCK:
                    current = _load_json_set(DEVICE_ONLINE_STATE_FILE)
                    current.update(notified)
                    _save_json_set(DEVICE_ONLINE_STATE_FILE, current)
                    notified = current
            log(f"[WATCHER] initialized baseline with {seeded} existing SNMP devices")
            first_successful_poll = False
            time.sleep(SWITCH_WATCH_INTERVAL)
            continue

        first_successful_poll = False
        for dev in devices:
            if _is_ping_only_device(dev):
                continue
            online_dev = _enrich_device_with_unifi(dev)
            if not _has_meaningful_device_name(online_dev):
                key = online_dev.get("hostname") or online_dev.get("ip") or "?"
                log(f"[WATCHER] waiting for SNMP name before online alert: {key}")
                continue
            key = online_dev.get("hostname") or online_dev.get("ip")
            ip = online_dev.get("ip") or online_dev.get("hostname")
            keys = {value for value in (key, ip) if value}
            if keys and not (keys & notified):
                online_dev = _enrich_device_with_inventory(online_dev, token)
                model = _best_device_model(online_dev)
                pending_key = str(ip or key)
                if not model:
                    started_at = model_wait_started.setdefault(pending_key, time.time())
                    waited = time.time() - started_at
                    if waited < DEVICE_MODEL_WAIT_SECONDS:
                        log(
                            f"[WATCHER] waiting for device model before online alert: "
                            f"{_best_device_name(online_dev)} ({ip}), waited={int(waited)}s"
                        )
                        continue
                else:
                    online_dev["hardware"] = model
                log(f"[WATCHER] new SNMP device detected: {_best_device_name(online_dev)} ({ip})")
                if send_device_online_once(build_device_online_card(online_dev), *keys):
                    notified.update(keys)
                    model_wait_started.pop(pending_key, None)
                    changed = True

        if changed:
            with DEVICE_ONLINE_STATE_LOCK:
                current = _load_json_set(DEVICE_ONLINE_STATE_FILE)
                current.update(notified)
                _save_json_set(DEVICE_ONLINE_STATE_FILE, current)
                notified = current
        time.sleep(SWITCH_WATCH_INTERVAL)


def sysname_change_watcher():
    """Alert on switch sysName (hostname) changes, with old -> new.

    LibreNMS alert rules have no reliable "changed" operator, and the alert
    webhook only carries the current sysName, so neither can show old -> new.
    Instead the bridge tracks each device's sysName itself by polling
    /api/v0/devices and persisting a snapshot; when a device's sysName differs
    from the stored value it pushes a Feishu card showing old -> new.

    A fresh deploy (no snapshot) seeds the baseline silently to avoid an alert
    storm on first run. The snapshot is replaced each poll so a removed/re-added
    device (new device_id) does not false-alert.
    """
    if not SYSNAME_CHANGE_ALERT_ENABLED:
        log("[SYSNAME] sysName change watcher disabled")
        return
    if not LIBRENMS_URL:
        log("[SYSNAME] LIBRENMS_URL not set, sysName change watcher disabled")
        return

    time.sleep(30)  # let LibreNMS/API token settle after a (re)start
    snapshot = _load_json_dict(SYSNAME_STATE_FILE)
    seeded = bool(snapshot)
    log(
        "[SYSNAME] sysName change watcher enabled "
        f"(poll={SYSNAME_CHANGE_POLL_INTERVAL}s, tracked={len(snapshot)})"
    )

    while True:
        token = _librenms_token()
        if not token:
            mark_watcher_health("sysname-change", False, "LibreNMS token unavailable")
            log("[SYSNAME] no API token yet, retrying...")
            time.sleep(SYSNAME_CHANGE_POLL_INTERVAL)
            continue
        try:
            devices = fetch_librenms_devices(token)
        except Exception as exc:
            mark_watcher_health("sysname-change", False, exc)
            log(f"[SYSNAME] poll failed: {exc}")
            time.sleep(SYSNAME_CHANGE_POLL_INTERVAL)
            continue
        mark_watcher_health("sysname-change", True)

        current = {}
        for dev in devices:
            device_id = str(dev.get("device_id") or "")
            sys_name = str(dev.get("sysName") or "").strip()
            if not device_id or not sys_name:
                continue
            ip = str(dev.get("ip") or dev.get("hostname") or "").strip()
            hostname = str(dev.get("hostname") or "").strip()
            current[device_id] = {"sysName": sys_name, "ip": ip, "hostname": hostname}

            prev_name = str((snapshot.get(device_id) or {}).get("sysName") or "").strip()
            if seeded and prev_name and prev_name != sys_name:
                log(f"[SYSNAME] CHANGE device_id={device_id} {prev_name} -> {sys_name} ({ip})")
                if not send_feishu(build_sysname_change_card(prev_name, sys_name, ip=ip, hostname=hostname)):
                    # Retain the previous snapshot so the same change is retried
                    # on the next successful LibreNMS poll.
                    current[device_id] = snapshot[device_id]

        snapshot = current
        _save_json_dict(SYSNAME_STATE_FILE, snapshot)
        if not seeded:
            seeded = True
            log(f"[SYSNAME] baseline recorded for {len(snapshot)} device(s)")

        time.sleep(SYSNAME_CHANGE_POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    server_version = "feishu-bridge/1.0"

    def _send(self, status, body=b"OK", content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except (ValueError, json.JSONDecodeError):
            pass

        form = parse.parse_qs(text, keep_blank_values=True) if ("=" in text or "&" in text) else {}
        if form:
            payload = {key: values[-1] if values else "" for key, values in form.items()}
            body = payload.get("body")
            if body:
                try:
                    nested = json.loads(body)
                    if isinstance(nested, dict):
                        payload.update(nested)
                except (ValueError, json.JSONDecodeError):
                    pass
            return payload

        return {"name": "LibreNMS transport test", "raw": text}

    def do_POST(self):
        if self.path == "/librenms":
            return self._handle_librenms()
        if self.path == "/test-alert":
            return self._handle_test_alert()
        return self._send(404, b"not found")

    def _handle_librenms(self):
        payload = self._read_json()
        card = build_librenms_card(payload)
        rule_name = payload.get("name") or payload.get("rule") or "LibreNMS 告警"
        log(f"librenms alert: {rule_name} state={payload.get('state')}")
        if send_feishu(card):
            return self._send(200, b"OK")
        # A non-2xx response tells LibreNMS that transport delivery failed and
        # preserves its opportunity to retry instead of acknowledging a lost alert.
        return self._send(502, b"Feishu delivery failed")

    def _handle_test_alert(self):
        self._read_json()  # drain body
        if not TOKEN and not DRY_RUN:
            result = {"ok": False, "error": "未配置飞书机器人 Token（FEISHU_ROBOT_TOKEN）"}
        else:
            sent = send_feishu(build_test_card())
            result = {"ok": bool(sent), "dryRun": DRY_RUN}
            if not sent and not DRY_RUN:
                result["error"] = "飞书返回失败，请检查 Token / 群机器人是否有效"
        log(f"[TEST] test alert requested -> {result}")
        return self._send(200, json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps(bridge_health_payload(), ensure_ascii=False).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        return self._send(404, b"not found")

    def log_message(self, fmt, *args):
        pass


def main():
    log(f"listening on 0.0.0.0:{PORT}  dry_run={DRY_RUN}  token_set={bool(TOKEN)}")
    if not TOKEN and not DRY_RUN:
        log("[WARN] no FEISHU_ROBOT_TOKEN set; LibreNMS alerts will not be forwarded")

    if LIBRENMS_URL:
        log(f"[WATCHER] device watcher enabled (librenms_url={LIBRENMS_URL})")
        start_watcher("device-online", device_watcher)
        if SYSNAME_CHANGE_ALERT_ENABLED:
            start_watcher("sysname-change", sysname_change_watcher)
    else:
        log("[WATCHER] LIBRENMS_URL not set, device watcher disabled")

    if PROMETHEUS_URL:
        if ISP_ALERT_ENABLED:
            start_watcher("isp-bandwidth", isp_bandwidth_watcher)
        if INTERCONNECT_ALERT_ENABLED:
            start_watcher("interconnect", interconnect_watcher)
        if DEVICE_DOWN_ENABLED:
            start_watcher("device-down", device_down_watcher)
        if UNIFI_AP_ALERT_ENABLED:
            start_watcher("unifi-ap", unifi_ap_watcher)

    if SYSLOG_WATCH_ENABLED:
        log(
            f"[SYSLOG] security watcher enabled "
            f"(file={SYSLOG_FILE}, rate_limit={SYSLOG_EVENT_RATE_LIMIT}s, "
            f"types={','.join(sorted(SYSLOG_ALERT_TYPES)) or '-'}, "
            f"correlation={SYSLOG_CORRELATION_SECONDS}s, "
            f"recovery={SYSLOG_RECOVERY_ENABLED})"
        )
        start_watcher("syslog", syslog_watcher)
    else:
        log("[SYSLOG] security watcher disabled")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
