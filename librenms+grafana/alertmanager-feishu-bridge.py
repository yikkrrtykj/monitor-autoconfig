#!/usr/bin/env python3
"""
LibreNMS webhook -> Feishu bot bridge + device-online watcher + syslog watcher.

Stdlib only (http.server + urllib + json + threading + re) so the container
runs on python:3-slim with no requirements.txt.

Env:
  FEISHU_BRIDGE_PORT      listen port (default 5005)
  FEISHU_ROBOT_TOKEN      Feishu bot webhook token
  FEISHU_BRIDGE_DRY_RUN   true = log payloads, never POST to Feishu
  LIBRENMS_URL            LibreNMS internal URL (e.g. http://librenms:8000)
  LIBRENMS_API_TOKEN      LibreNMS API token (falls back to token file)
  LIBRENMS_TOKEN_FILE     path to token file written by librenms-config
                          (default /librenms-data/librenms-api-token)
  SWITCH_WATCH_INTERVAL   seconds between device-list polls (default 120)
  PROMETHEUS_URL          Prometheus internal URL (default http://prometheus:9090)
  ISP_ALERT_ENABLED       true = watch firewall WAN bandwidth (default true)
  ISP_ALERT_FOR_SECONDS   seconds above threshold before alerting (default 10)
  ISP_ALERT_POLL_INTERVAL seconds between checks (default 5)
  ISP_ALERT_RATE_WINDOW   Prometheus rate() window (default 1m)
  ISP_ALERT_STATUS_INTERVAL seconds between status logs (default 30)
  ISP_ALERT_RESOLVE_SECONDS seconds below threshold before recovery (default 30)
  FIREWALL_WAN_IF_FILTER  WAN interface label keywords
  BIGSCREEN_ISP_MAX_BANDWIDTH ISP bandwidth Mbps config
  BIGSCREEN_ISP_IPS     optional ISP display names, NAME:IP comma list
  ISP_PING              ISP ping targets, NAME:IP comma list
  ISP_SATURATION_PERCENT  alert threshold percent of configured bandwidth
  SYSLOG_WATCH_ENABLED    true = watch syslog file for security events (default true)
  SYSLOG_FILE             path to syslog file from rsyslog (default /var/log/remote/syslog.log)
  DEVICE_DOWN_ENABLED     true = watch infra ping targets for down (default true)
  DEVICE_DOWN_FOR_SECONDS seconds unreachable before alerting (default 10)
  ISP_DOWN_FOR_SECONDS    seconds unreachable before ISP ping alerting (default 0)
  DEVICE_DOWN_REQUIRE_SEEN_UP true = alert only after target was discovered/up once
  DEVICE_DOWN_POLL_INTERVAL seconds between probe_success polls (default 1)
  DEVICE_DOWN_JOBS        comma list of Prometheus ping jobs to watch for down
  DEVICE_DOWN_STATE_FILE  persisted active down alerts (default /bridge-state/device-down-alerts.json)
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
from http.server import BaseHTTPRequestHandler, HTTPServer
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

LIBRENMS_URL = os.environ.get("LIBRENMS_URL", "").rstrip("/")
LIBRENMS_API_TOKEN = os.environ.get("LIBRENMS_API_TOKEN", "")
LIBRENMS_TOKEN_FILE = os.environ.get("LIBRENMS_TOKEN_FILE", "/librenms-data/librenms-api-token")
SWITCH_WATCH_INTERVAL = int(os.environ.get("SWITCH_WATCH_INTERVAL", "30"))
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
ISP_ALERT_ENABLED = os.environ.get("ISP_ALERT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
ISP_ALERT_FOR_SECONDS = int(os.environ.get("ISP_ALERT_FOR_SECONDS", "10"))
ISP_ALERT_POLL_INTERVAL = int(os.environ.get("ISP_ALERT_POLL_INTERVAL", "5"))
ISP_ALERT_RATE_WINDOW = os.environ.get("ISP_ALERT_RATE_WINDOW", "1m")
ISP_ALERT_RESOLVE_SECONDS = int(os.environ.get("ISP_ALERT_RESOLVE_SECONDS", "30"))
ISP_ALERT_STATUS_INTERVAL = int(os.environ.get("ISP_ALERT_STATUS_INTERVAL", "30"))
FIREWALL_WAN_IF_FILTER = os.environ.get("FIREWALL_WAN_IF_FILTER", "telecom,telcom,unicom,isp,WAN")
BIGSCREEN_ISP_MAX_BANDWIDTH = os.environ.get("BIGSCREEN_ISP_MAX_BANDWIDTH", "1000")
BIGSCREEN_ISP_IPS = os.environ.get("BIGSCREEN_ISP_IPS", "")
ISP_PING = os.environ.get("ISP_PING", "")
ISP_SATURATION_PERCENT = float(os.environ.get("ISP_SATURATION_PERCENT", "80") or "80")
SYSLOG_WATCH_ENABLED = os.environ.get("SYSLOG_WATCH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SYSLOG_FILE = os.environ.get("SYSLOG_FILE", "/var/log/remote/syslog.log")
DEVICE_DOWN_ENABLED = os.environ.get("DEVICE_DOWN_ENABLED", "true").lower() in ("1", "true", "yes", "on")
DEVICE_DOWN_FOR_SECONDS = int(os.environ.get("DEVICE_DOWN_FOR_SECONDS", "10"))
ISP_DOWN_FOR_SECONDS = int(os.environ.get("ISP_DOWN_FOR_SECONDS", "0"))
DEVICE_DOWN_REQUIRE_SEEN_UP = os.environ.get("DEVICE_DOWN_REQUIRE_SEEN_UP", "true").lower() in ("1", "true", "yes", "on")
DEVICE_DOWN_POLL_INTERVAL = int(os.environ.get("DEVICE_DOWN_POLL_INTERVAL", "1"))
DEVICE_DOWN_JOBS = os.environ.get(
    "DEVICE_DOWN_JOBS",
    "infra-core-ping,infra-dist-ping,infra-fw-ping,infra-isp-ping,infra-srv-ping",
)
DEVICE_ONLINE_FROM_PING = os.environ.get("DEVICE_ONLINE_FROM_PING", "false").lower() in ("1", "true", "yes", "on")
DEVICE_AUTO_ADD_FROM_PING = os.environ.get("DEVICE_AUTO_ADD_FROM_PING", "true").lower() in ("1", "true", "yes", "on")
DEVICE_AUTO_ADD_SNMP_JOBS = os.environ.get("DEVICE_AUTO_ADD_SNMP_JOBS", "infra-core-ping,infra-dist-ping")
SNMP_COMMUNITY = os.environ.get("SNMP_COMMUNITY", "public")
UNIFI_AP_SNMP_AUTO_ADD = os.environ.get("UNIFI_AP_SNMP_AUTO_ADD", "true").lower() in ("1", "true", "yes", "on")
UNIFI_AP_SNMP_COMMUNITY = os.environ.get("UNIFI_AP_SNMP_COMMUNITY", SNMP_COMMUNITY)
UNIFI_AP_SNMP_ADD_RETRY_SECONDS = int(os.environ.get("UNIFI_AP_SNMP_ADD_RETRY_SECONDS", "300"))
UNIFI_CONTROLLER_URL = os.environ.get("UNIFI_CONTROLLER_URL", "").rstrip("/")
UNIFI_CONTROLLER_USER = os.environ.get("UNIFI_CONTROLLER_USER", "")
UNIFI_CONTROLLER_PASS = os.environ.get("UNIFI_CONTROLLER_PASS", "")
UNIFI_CONTROLLER_SITES = os.environ.get("UNIFI_CONTROLLER_SITES", "all")
UNIFI_CONTROLLER_VERIFY_SSL = os.environ.get("UNIFI_CONTROLLER_VERIFY_SSL", "false").lower() in ("1", "true", "yes", "on")
UNIFI_CONTROLLER_REFRESH_SECONDS = int(os.environ.get("UNIFI_CONTROLLER_REFRESH_SECONDS", "60"))
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
# UniFi AP 掉线告警：从 UniFi Poller(unpoller) 在 Prometheus 里的 controller 数据
# 判断 AP 在线/掉线。没配 UniFi 时该查询为空、watcher 自动静默。
UNIFI_AP_ALERT_ENABLED = os.environ.get("UNIFI_AP_ALERT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
UNIFI_AP_DOWN_FOR_SECONDS = int(os.environ.get("UNIFI_AP_DOWN_FOR_SECONDS", "90"))
UNIFI_AP_POLL_INTERVAL = int(os.environ.get("UNIFI_AP_POLL_INTERVAL", "15"))

_DHCP_SNOOP_RE = re.compile(r"DHCP_SNOOPING", re.IGNORECASE)
_MAC_RE = r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}|[0-9A-Fa-f]{4}(?:\.[0-9A-Fa-f]{4}){2}|[0-9A-Fa-f]{12}"


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
        return
    with DEVICE_ONLINE_STATE_LOCK:
        items = _load_json_set(DEVICE_ONLINE_STATE_FILE)
        if clean.issubset(items):
            return
        items.update(clean)
        _save_json_set(DEVICE_ONLINE_STATE_FILE, items)


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
            "alerting": bool(value.get("alerting", True)),
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
    active = {}
    for key, state in states.items():
        if not state.get("alerting"):
            continue
        active[str(key)] = {
            "down_since": state.get("down_since"),
            "last_up_at": state.get("last_up_at"),
            "seen_up": bool(state.get("seen_up", True)),
            "online_sent": bool(state.get("online_sent", False)),
            "name": state.get("name") or "",
            "ip": state.get("ip") or "",
            "job": state.get("job") or "",
            "alerting": True,
        }
    with DEVICE_DOWN_STATE_LOCK:
        _save_json_dict(DEVICE_DOWN_STATE_FILE, active)


EVENT_ID = max(
    int(os.environ.get("FEISHU_BRIDGE_EVENT_ID_START", "0") or "0"),
    _read_int_file(EVENT_ID_FILE, 0),
)


def log(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", file=sys.stderr, flush=True)


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


def update_librenms_device_display(ip, name, log_prefix="[WATCHER]"):
    token = _librenms_token()
    name = str(name or "").strip()
    if not token or not LIBRENMS_URL or not ip or not name or _looks_like_ip(name):
        return False
    payload = {"field": "display", "data": name}
    encoded_ip = parse.quote(str(ip), safe="")
    req = request.Request(
        f"{LIBRENMS_URL}/api/v0/devices/{encoded_ip}",
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


def add_librenms_snmp_device(ip, name="", community=None, log_prefix="[WATCHER]"):
    token = _librenms_token()
    if not ip:
        return False
    if not LIBRENMS_URL:
        log(f"{log_prefix} SNMP auto-add postponed for {name or ip} ({ip}): LIBRENMS_URL not set")
        return False
    if not token:
        log(f"{log_prefix} SNMP auto-add postponed for {name or ip} ({ip}): LibreNMS API token not ready")
        return False
    snmp_community = (community or SNMP_COMMUNITY or "public").strip()
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
            return True
        if "already" in str(message).lower():
            log(f"{log_prefix} SNMP auto-add skipped for {name or ip} ({ip}): already exists")
            update_librenms_device_display(ip, name, log_prefix=log_prefix)
            return True
        log(f"{log_prefix} SNMP auto-add failed for {name or ip} ({ip}): {str(message)[:160]}")
        return False
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
        if "already" in body.lower():
            log(f"{log_prefix} SNMP auto-add skipped for {name or ip} ({ip}): already exists")
            update_librenms_device_display(ip, name, log_prefix=log_prefix)
            return True
        log(f"{log_prefix} SNMP auto-add HTTP {exc.code} for {name or ip} ({ip}): {body[:160]}")
        return False
    except Exception as exc:
        log(f"{log_prefix} SNMP auto-add failed for {name or ip} ({ip}): {exc}")
        return False


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


def _dhcp_message_type_text(value):
    value = str(value or "").strip().upper()
    labels = {
        "DHCPDISCOVER": "发现",
        "DHCPOFFER": "提供",
        "DHCPREQUEST": "请求",
        "DHCPDECLINE": "拒绝",
        "DHCPACK": "确认",
        "DHCPNAK": "拒绝确认",
        "DHCPRELEASE": "释放地址",
        "DHCPINFORM": "信息请求",
    }
    return labels.get(value, value)


def parse_dhcp_snooping_message(message):
    text = str(message or "")
    event = {
        "message_type": "",
        "chaddr": "",
        "chaddr_hex": "",
        "mac_sa": "",
        "mac_sa_hex": "",
        "reason": "",
    }

    match = re.search(r"message type:\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
    if match:
        event["message_type"] = match.group(1).upper()

    match = re.search(rf"\bchaddr:\s*({_MAC_RE})", text, re.IGNORECASE)
    if match:
        event["chaddr"] = _format_mac(match.group(1))
        event["chaddr_hex"] = _normalize_mac_hex(match.group(1))

    match = re.search(rf"\bMAC\s+sa:\s*({_MAC_RE})", text, re.IGNORECASE)
    if match:
        event["mac_sa"] = _format_mac(match.group(1))
        event["mac_sa_hex"] = _normalize_mac_hex(match.group(1))

    if "MATCH_MAC_FAIL" in text or "chaddr doesn't match source mac" in text.lower():
        event["reason"] = "chaddr 与源 MAC 不一致"
    else:
        match = re.search(r"%[^:]+:\s*(.+?)(?:,\s*message type:|$)", text)
        if match:
            event["reason"] = match.group(1).strip()
    return event


def _port_label_from_fdb(entry):
    if not entry:
        return ""
    port = str(entry.get("ifName") or entry.get("ifDescr") or entry.get("ifAlias") or "").strip()
    if not port:
        return ""

    def comparable(value):
        text = re.sub(r"[\s_-]+", "", str(value or "").lower())
        replacements = {
            "tengigabitethernet": "te",
            "twentyfivegigabitethernet": "twe",
            "fortygigabitethernet": "fo",
            "hundredgigabitethernet": "hu",
            "gigabitethernet": "gi",
            "fastethernet": "fa",
            "portchannel": "po",
            "ethernet": "eth",
        }
        for long_name, short_name in replacements.items():
            text = text.replace(long_name, short_name)
        return text

    for extra in (entry.get("ifAlias"), entry.get("ifDescr")):
        extra = str(extra or "").strip()
        if not extra or extra == port or comparable(extra) == comparable(port):
            continue
        return f"{port} / {extra}"
    return port


def lookup_librenms_fdb_port(mac, host=""):
    token = _librenms_token()
    mac_hex = _normalize_mac_hex(mac)
    if not token or not LIBRENMS_URL or not mac_hex:
        return None

    url = f"{LIBRENMS_URL}/api/v0/resources/fdb/{mac_hex}/detail"
    req = request.Request(url, headers={"X-Auth-Token": token})
    try:
        with request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except error.HTTPError as exc:
        if exc.code != 404:
            body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            log(f"[SYSLOG] FDB lookup HTTP {exc.code} for {_format_mac(mac_hex)}: {body[:160]}")
        return None
    except Exception as exc:
        log(f"[SYSLOG] FDB lookup failed for {_format_mac(mac_hex)}: {exc}")
        return None

    entries = data.get("ports_fdb") or []
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list) or not entries:
        return None

    host_l = str(host or "").strip().lower()
    for entry in entries:
        candidates = [
            str(entry.get("hostname") or "").strip().lower(),
            str(entry.get("sysName") or "").strip().lower(),
        ]
        if host_l and host_l in candidates:
            return entry
    return entries[0]


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


def build_device_online_card(device):
    name = _best_device_name(device) or "?"
    ip = device.get("ip") or device.get("hostname") or "?"
    hw = device.get("hardware") or ""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [f"🖥 设备：{name}", f"🌐 IP：{ip}"]
    if hw:
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


def build_device_down_card(name, ip, recovered, offline_seconds=0, job=""):
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
        f"⏰ 时间：{ts}",
    ]
    return _make_card(next_event_title(), f"{header_emoji} {subtitle}", color, "\n".join(lines))


def build_interconnect_card(event, recovered=False):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = "green" if recovered else "red"
    status_emoji = "✅" if recovered else "❌"
    header_emoji = "🟢" if recovered else "🔴"
    state_text = "UP" if recovered else "DOWN"
    duration_label = "恢复耗时" if recovered else "断线时间"
    device = event.get("device") or event.get("ip") or "?"
    ip = event.get("ip") or ""
    device_text = f"{device} ({ip})" if ip and ip != device else device
    port = event.get("port") or "?"
    alias = event.get("alias") or ""
    port_text = f"{port} / {alias}" if alias and alias != port else port
    lines = [
        f"🖥 设备：{device_text}",
        f"🔌 接口：{port_text}",
        f"{status_emoji} 状态：{state_text}",
        f"⏳ {duration_label}：{format_alert_duration(event.get('duration'), recovered)}",
        f"⏰ 时间：{ts}",
    ]
    return _make_card(next_event_title(), f"{header_emoji} 互联口断链告警", color, "\n".join(lines))


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
                "title": {"tag": "plain_text", "content": title},
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


def send_feishu(card):
    if DRY_RUN:
        log(f"[DRY] would POST card: {card['card']['header']['title']['content']}")
        return True
    if not TOKEN:
        log("[WARN] FEISHU_ROBOT_TOKEN empty, dropping alert (set token or enable DRY_RUN)")
        return False
    url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}"
    data = json.dumps(card).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=5) as resp:
            response_text = resp.read().decode("utf-8", errors="replace")
            log(f"feishu response: {response_text[:200]}")
        return True
    except error.URLError as exc:
        log(f"[ERR] feishu request failed: {exc}")
        return False
    except Exception as exc:
        log(f"[ERR] unexpected: {exc}")
        return False


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
    lower = label.lower()
    return any(keyword in lower for keyword in _wan_keywords())


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


def _bandwidth_for_label(label, direction, cfg):
    lower = label.lower()
    norm = _norm_label(label)
    for entry in cfg["per"]:
        if (entry["label"] and entry["label"] in lower) or (entry["norm"] and entry["norm"] in norm):
            return entry["down"] if direction == "in" else entry["up"]
    default = cfg["default"] or {"down": 1000.0, "up": 1000.0}
    return default["down"] if direction == "in" else default["up"]


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
                "target_ip": metric_labels.get("target_ip") or metric_labels.get("instance") or "",
            })
    return results


def log_isp_status(rates, bandwidth_cfg):
    if not rates:
        log(
            "[ISP] no WAN traffic series matched "
            f"FIREWALL_WAN_IF_FILTER={FIREWALL_WAN_IF_FILTER!r}; "
            "check Prometheus job=firewall-snmp labels ifAlias/ifName/ifDescr"
        )
        return

    rows = []
    for sample in sorted(rates, key=lambda item: item["value_bps"], reverse=True)[:6]:
        capacity_mbps = _bandwidth_for_label(sample["label"], sample["direction"], bandwidth_cfg)
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
    log(
        "[ISP] realtime bandwidth watcher enabled "
        f"(threshold={ISP_SATURATION_PERCENT:g}%, for={ISP_ALERT_FOR_SECONDS}s, "
        f"poll={ISP_ALERT_POLL_INTERVAL}s, rate_window={ISP_ALERT_RATE_WINDOW}, prometheus={PROMETHEUS_URL})"
    )

    while True:
        now = time.time()
        try:
            rates = fetch_wan_rates()
        except Exception as exc:
            log(f"[ISP] poll failed: {exc}")
            time.sleep(ISP_ALERT_POLL_INTERVAL)
            continue

        if now - last_status_log >= ISP_ALERT_STATUS_INTERVAL:
            log_isp_status(rates, bandwidth_cfg)
            last_status_log = now

        seen = set()
        for sample in rates:
            seen.add(sample["key"])
            capacity_mbps = _bandwidth_for_label(sample["label"], sample["direction"], bandwidth_cfg)
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
                    state["alerting"] = True
                    state["alert_started"] = state["active_since"]
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
                    send_feishu(build_isp_bandwidth_card(event, recovered=False))
            else:
                state["active_since"] = None
                if state["alerting"]:
                    if state["clear_since"] is None:
                        state["clear_since"] = now
                    clear_duration = now - state["clear_since"]
                    if clear_duration >= ISP_ALERT_RESOLVE_SECONDS:
                        state["alerting"] = False
                        # 持续 = 整段饱和时长（首次越过阈值→恢复），不是 30 秒恢复防抖
                        _start = state["alert_started"] if state["alert_started"] is not None else state["clear_since"]
                        saturated_duration = now - _start
                        state["alert_started"] = None
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
                        send_feishu(build_isp_bandwidth_card(event, recovered=True))
                else:
                    state["clear_since"] = None

        for key, state in list(states.items()):
            if key in seen:
                continue
            if state.get("alerting") and state.get("clear_since") is None:
                state["clear_since"] = now
            elif state.get("clear_since") and now - state["clear_since"] >= ISP_ALERT_RESOLVE_SECONDS:
                states.pop(key, None)

        time.sleep(ISP_ALERT_POLL_INTERVAL)


def fetch_interconnect_ports(jobs_regex):
    query = f'ifOperStatus{{job=~"{jobs_regex}"}}'
    ports = []
    for item in prometheus_query(query):
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
        ports.append({
            "key": "|".join([
                metric.get("job", ""),
                ip,
                metric.get("ifIndex") or port,
            ]),
            "device": metric.get("display_name") or metric.get("instance") or ip or "?",
            "ip": ip,
            "port": port,
            "alias": metric.get("ifAlias") or "",
            "up": up,
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
            last_name_refresh = now

        try:
            ports = fetch_interconnect_ports(jobs_regex)
        except Exception as exc:
            log(f"[LINK] poll failed: {exc}")
            time.sleep(INTERCONNECT_ALERT_POLL_INTERVAL)
            continue

        if now - last_status_log >= 60:
            up_count = sum(1 for port in ports if port["up"])
            down_count = len(ports) - up_count
            log(f"[LINK] watched port-channels total={len(ports)} up={up_count} down={down_count}")
            last_status_log = now

        for port in ports:
            ip = port.get("ip") or ""
            if ip in librenms_names:
                port["device"] = librenms_names[ip]
            state = states.setdefault(port["key"], {
                "down_since": None,
                "alerting": False,
                "last_up_at": None,
            })

            if not port["up"]:
                if state["down_since"] is None:
                    state["down_since"] = state.get("last_up_at") or now
                duration = max(0, now - state["down_since"])
                if not state["alerting"] and duration >= INTERCONNECT_ALERT_FOR_SECONDS:
                    state["alerting"] = True
                    event = dict(port)
                    event["duration"] = duration
                    log(f"[LINK] ALERT {event['device']} {event['port']} DOWN")
                    send_feishu(build_interconnect_card(event, recovered=False))
            else:
                previous_down_since = state.get("down_since")
                state["last_up_at"] = now
                if state["alerting"]:
                    duration = max(0, now - (previous_down_since or now))
                    event = dict(port)
                    event["duration"] = duration
                    log(f"[LINK] RECOVER {event['device']} {event['port']} offline={int(duration)}s")
                    send_feishu(build_interconnect_card(event, recovered=True))
                state["alerting"] = False
                state["down_since"] = None

        time.sleep(INTERCONNECT_ALERT_POLL_INTERVAL)


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
    query = 'probe_success{job=~"%s"}' % "|".join(safe_jobs)
    time.sleep(20)  # let Prometheus/blackbox settle after a (re)start
    states = load_device_down_states()
    last_status_log = 0.0
    last_name_refresh = 0.0
    librenms_names = {}
    isp_names = _isp_target_names()
    auto_add_jobs = {j.strip() for j in DEVICE_AUTO_ADD_SNMP_JOBS.split(",") if j.strip()}
    auto_add_attempted = set()
    log(
        "[DOWN] device-down watcher enabled "
        f"(jobs={','.join(jobs)}, for={DEVICE_DOWN_FOR_SECONDS}s, "
        f"isp_for={ISP_DOWN_FOR_SECONDS}s, poll={DEVICE_DOWN_POLL_INTERVAL}s, "
        f"require_seen_up={DEVICE_DOWN_REQUIRE_SEEN_UP}, active_loaded={len(states)})"
    )

    while True:
        now = time.time()
        if now - last_name_refresh >= 60:
            try:
                librenms_names = fetch_librenms_name_cache()
            except Exception as exc:
                log(f"[DOWN] LibreNMS name refresh failed: {exc}")
            last_name_refresh = now

        try:
            results = prometheus_query(query)
        except Exception as exc:
            log(f"[DOWN] poll failed: {exc}")
            time.sleep(DEVICE_DOWN_POLL_INTERVAL)
            continue

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
                "seen_up": False,
                "ignored_initial_down": False,
                "last_up_at": None,
                "online_sent": False,
                "name": "",
                "ip": "",
                "job": "",
            }
            state = states.setdefault(key, default_state.copy())
            for field, value in default_state.items():
                state.setdefault(field, value)
            state["name"] = name
            state["ip"] = ip
            state["job"] = job
            if not up:
                if DEVICE_DOWN_REQUIRE_SEEN_UP and not state["seen_up"] and not state["alerting"]:
                    if not state["ignored_initial_down"]:
                        log(f"[DOWN] waiting for first UP before alerting {job} {prom_name} ({ip})")
                        state["ignored_initial_down"] = True
                    state["down_since"] = None
                    continue
                if state["down_since"] is None:
                    state["down_since"] = sample_ts or now
                if not state["alerting"] and now - state["down_since"] >= down_for_seconds:
                    state["alerting"] = True
                    state["seen_up"] = True
                    offline = max(0, now - state["down_since"])
                    log(f"[DOWN] ALERT {job} {name} ({ip}) DOWN")
                    send_feishu(build_device_down_card(name, ip, recovered=False, offline_seconds=offline, job=job))
                    save_device_down_states(states)
            else:
                previous_down_since = state.get("down_since")
                state["last_up_at"] = sample_ts or now
                first_up_after_candidate_down = (not state["seen_up"] and state.get("ignored_initial_down"))
                if not state["seen_up"]:
                    state["seen_up"] = True
                    state["ignored_initial_down"] = False
                    log(f"[DOWN] armed {job} {name} ({ip}) after first UP")
                    if (
                        DEVICE_AUTO_ADD_FROM_PING
                        and job in auto_add_jobs
                        and ip
                        and not known_by_librenms
                        and ip not in auto_add_attempted
                    ):
                        auto_add_attempted.add(ip)
                        add_librenms_snmp_device(ip)
                    if first_up_after_candidate_down and DEVICE_ONLINE_FROM_PING and not state["online_sent"]:
                        state["online_sent"] = True
                        log(f"[DOWN] online detected from ping: {job} {name} ({ip})")
                        send_feishu(build_device_online_card({
                            "display": name,
                            "ip": ip,
                            "os": "Ping only",
                        }))
                if state["alerting"]:
                    offline = max(0, (sample_ts or now) - (previous_down_since or sample_ts or now))
                    log(f"[DOWN] RECOVER {job} {name} ({ip}) offline={int(offline)}s")
                    send_feishu(build_device_down_card(name, ip, recovered=True, offline_seconds=offline, job=job))
                    state["alerting"] = False
                    state["down_since"] = None
                    save_device_down_states(states)
                else:
                    state["down_since"] = None
                state["alerting"] = False

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
    name_sync_attempted = {}
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
            log(f"[AP] poll failed: {exc}")
            time.sleep(UNIFI_AP_POLL_INTERVAL)
            continue

        metric_online = _ap_online_metric_map()
        controller_aps = fetch_unifi_controller_aps_cached()
        current = {}
        known = {}
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
            if controller_info:
                is_online = bool(controller_info.get("online"))
            elif is_online is None:
                is_online = True if label_online is None else label_online
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
            if controller_info.get("online"):
                current[key] = merged

        # Seen APs: refresh metadata, arm on first sight, recover if was down.
        for key, info in current.items():
            name = info.get("name") or key
            sync_name = name if _should_sync_ap_name(info) else ""
            ip = info.get("ip") or ""
            add_attempted = False
            if UNIFI_AP_SNMP_AUTO_ADD and ip:
                last_attempt = snmp_add_attempted.get(ip, 0)
                if now - last_attempt >= UNIFI_AP_SNMP_ADD_RETRY_SECONDS:
                    snmp_add_attempted[ip] = now
                    add_attempted = True
                    if add_librenms_snmp_device(
                        ip,
                        name=sync_name,
                        community=UNIFI_AP_SNMP_COMMUNITY,
                        log_prefix="[AP]",
                    ):
                        mark_device_online_notified(name, ip)
            if ip and sync_name and not add_attempted:
                last_sync = name_sync_attempted.get(ip, 0)
                if now - last_sync >= UNIFI_AP_NAME_SYNC_SECONDS:
                    name_sync_attempted[ip] = now
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
                send_feishu(build_ap_down_card(name, state["ip"], state["model"],
                                               recovered=True, offline_seconds=offline))
            state["alerting"] = False
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
                state["alerting"] = True
                offline = max(0, now - state["down_since"])
                name = state.get("name") or key
                log(f"[AP] ALERT {name} ({state['ip']}) DOWN")
                send_feishu(build_ap_down_card(name, state["ip"], state["model"],
                                               recovered=False, offline_seconds=offline))

        if now - last_status_log >= 60:
            down = sum(1 for s in states.values() if s.get("alerting"))
            missing_ip = sum(1 for info in current.values() if not info.get("ip"))
            log(
                f"[AP] {len(current)} online / {len(known)} listed / {len(states)} known / "
                f"{down} down / {missing_ip} online without IP"
            )
            last_status_log = now

        time.sleep(UNIFI_AP_POLL_INTERVAL)


def build_dhcp_snooping_card(host, message, parsed=None):
    parsed = parsed or parse_dhcp_snooping_message(message)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fdb_entry = lookup_librenms_fdb_port(parsed.get("mac_sa_hex"), host)
    if not fdb_entry and parsed.get("chaddr_hex"):
        fdb_entry = lookup_librenms_fdb_port(parsed.get("chaddr_hex"), host)

    device = _host_display_name(host, fdb_entry)
    dev_text = f"{device} ({host})" if host and host != device else device
    port = _port_label_from_fdb(fdb_entry)

    lines = [f"🖥 设备：{dev_text}"]
    if port:
        lines.append(f"🔌 接口：{port}")
    else:
        lines.append("🔌 接口：FDB 未查到该 MAC 所在接口，不猜接口")

    if parsed.get("reason"):
        lines.append(f"📋 异常：{parsed['reason']}")
    if parsed.get("message_type"):
        lines.append(f"📨 DHCP：{_dhcp_message_type_text(parsed['message_type'])}")
    if parsed.get("mac_sa"):
        lines.append(f"🔗 实际源 MAC：{parsed['mac_sa']}")
    if parsed.get("chaddr"):
        lines.append(f"🧾 报文客户端 MAC：{parsed['chaddr']}")
    lines.append(f"⏰ 时间：{ts}")
    return _make_card(next_event_title(), "⚠️ DHCP Snooping 违规", "orange", "\n".join(lines))


def syslog_watcher():
    log(f"[SYSLOG] watching {SYSLOG_FILE} for DHCP snooping violations")
    _last_sent = {}
    RATE_LIMIT = 60

    while not os.path.exists(SYSLOG_FILE):
        time.sleep(5)

    try:
        f = open(SYSLOG_FILE)
        f.seek(0, 2)
        current_ino = os.fstat(f.fileno()).st_ino
    except OSError as exc:
        log(f"[SYSLOG] cannot open {SYSLOG_FILE}: {exc}")
        return

    while True:
        line = f.readline()
        if not line:
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

        if _DHCP_SNOOP_RE.search(message):
            parsed = parse_dhcp_snooping_message(message)
            dedupe_key = "|".join([
                host,
                parsed.get("message_type") or "",
                parsed.get("mac_sa_hex") or "",
                parsed.get("chaddr_hex") or "",
                message[:120] if not (parsed.get("mac_sa_hex") or parsed.get("chaddr_hex")) else "",
            ])
            now = time.time()
            if now - _last_sent.get(dedupe_key, 0) >= RATE_LIMIT:
                _last_sent[dedupe_key] = now
                log(
                    f"[SYSLOG] DHCP snooping violation from {host} "
                    f"type={parsed.get('message_type') or '-'} "
                    f"mac_sa={parsed.get('mac_sa') or '-'} chaddr={parsed.get('chaddr') or '-'}"
                )
                send_feishu(build_dhcp_snooping_card(host, message, parsed))


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
    while True:
        try:
            token = _librenms_token()
            if not token:
                log("[WATCHER] token lost, skipping poll")
                time.sleep(SWITCH_WATCH_INTERVAL)
                continue
            devices = fetch_librenms_devices(token)
        except Exception as exc:
            log(f"[WATCHER] poll failed: {exc}")
            time.sleep(SWITCH_WATCH_INTERVAL)
            continue

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
                log(f"[WATCHER] new SNMP device detected: {_best_device_name(online_dev)} ({ip})")
                send_feishu(build_device_online_card(online_dev))
                notified.update(keys)
                changed = True

        if changed:
            with DEVICE_ONLINE_STATE_LOCK:
                current = _load_json_set(DEVICE_ONLINE_STATE_FILE)
                current.update(notified)
                _save_json_set(DEVICE_ONLINE_STATE_FILE, current)
                notified = current
        time.sleep(SWITCH_WATCH_INTERVAL)


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
        return self._send(404, b"not found")

    def _handle_librenms(self):
        payload = self._read_json()
        card = build_librenms_card(payload)
        rule_name = payload.get("name") or payload.get("rule") or "LibreNMS 告警"
        log(f"librenms alert: {rule_name} state={payload.get('state')}")
        send_feishu(card)
        return self._send(200, b"OK")

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"OK")
        return self._send(404, b"not found")

    def log_message(self, fmt, *args):
        pass


def main():
    log(f"listening on 0.0.0.0:{PORT}  dry_run={DRY_RUN}  token_set={bool(TOKEN)}")
    if not TOKEN and not DRY_RUN:
        log("[WARN] no FEISHU_ROBOT_TOKEN set; LibreNMS alerts will not be forwarded")

    if LIBRENMS_URL:
        log(f"[WATCHER] device watcher enabled (librenms_url={LIBRENMS_URL})")
        threading.Thread(target=device_watcher, daemon=True).start()
    else:
        log("[WATCHER] LIBRENMS_URL not set, device watcher disabled")

    if PROMETHEUS_URL:
        threading.Thread(target=isp_bandwidth_watcher, daemon=True).start()
        threading.Thread(target=interconnect_watcher, daemon=True).start()
        threading.Thread(target=device_down_watcher, daemon=True).start()
        threading.Thread(target=unifi_ap_watcher, daemon=True).start()

    if SYSLOG_WATCH_ENABLED:
        log(f"[SYSLOG] DHCP snooping watcher enabled (file={SYSLOG_FILE})")
        threading.Thread(target=syslog_watcher, daemon=True).start()
    else:
        log("[SYSLOG] DHCP snooping watcher disabled")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
