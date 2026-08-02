"""Pure Cisco-style network syslog parsing and MAC-flap correlation.

This module intentionally has no delivery, file-system, or Prometheus side
effects.  The alert bridge owns those concerns and can unit-test this parser
without starting any watcher threads.
"""

from __future__ import annotations

import re
import time


_MAC_RE = (
    r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}|"
    r"[0-9A-Fa-f]{4}(?:\.[0-9A-Fa-f]{4}){2}|[0-9A-Fa-f]{12}"
)
_MACFLAP_RE = re.compile(
    rf"MACFLAP_NOTIF:\s+Host\s+({_MAC_RE})\s+in\s+vlan\s+(\d+)\s+is\s+"
    r"flapping\s+between\s+port\s+(\S+)\s+and\s+port\s+(\S+)",
    re.IGNORECASE,
)
_NATIVE_VLAN_RE = re.compile(
    r"NATIVE_VLAN_MISMATCH:\s+Native VLAN mismatch discovered on\s+(\S+)\s+"
    r"\((\d+)\),\s+with\s+(.+?)\s+(\S+)\s+\((\d+)\)",
    re.IGNORECASE,
)
_ERRDISABLE_RE = re.compile(
    r"ERR_?DISABLE:\s+(.+?)\s+error detected on\s+(\S+),\s+putting\s+\S+\s+"
    r"in err-disable state",
    re.IGNORECASE,
)
_BPDUGUARD_RE = re.compile(
    r"(?:BPDUGUARD|BPDU Guard).*?(?:port|interface)\s+(\S+)", re.IGNORECASE,
)
_STORM_RE = re.compile(
    r"(?:STORM_CONTROL|storm-control|storm control).*?(?:on|interface)\s+(\S+)",
    re.IGNORECASE,
)
_LOOPBACK_RE = re.compile(
    r"LOOP_BACK_DETECTED.*?(?:on|interface)\s+(\S+)", re.IGNORECASE,
)
_LINK_STATE_RE = re.compile(
    r"(?:LINK-\d+-\w+|LINEPROTO-\d+-UPDOWN):\s+"
    r"(?:Line protocol on\s+)?Interface\s+(\S+),\s+changed state to\s+"
    r"(administratively down|up|down)",
    re.IGNORECASE,
)


def normalize_mac_hex(value) -> str:
    mac = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).lower()
    return mac if len(mac) == 12 else ""


def format_mac(value) -> str:
    mac = normalize_mac_hex(value)
    if not mac:
        return str(value or "").strip()
    return ":".join(mac[index:index + 2] for index in range(0, 12, 2))


def clean_iface_token(value) -> str:
    return str(value or "").strip().rstrip(".,;:")


def normalize_iface_key(value) -> str:
    token = clean_iface_token(value).lower().replace(" ", "")
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


def network_event_port(event) -> str:
    if not event:
        return ""
    if event.get("kind") == "native_vlan_mismatch":
        return event.get("local_port") or ""
    return event.get("port") or ""


def network_event_priority(kind) -> int:
    return {
        "native_vlan_mismatch": 3,
        "loopback": 2,
        "errdisable": 1,
        "bpduguard": 1,
    }.get(str(kind or ""), 0)


def is_bpdu_event(event) -> bool:
    kind = str((event or {}).get("kind") or "").lower()
    reason = str((event or {}).get("reason") or "").lower()
    return kind == "bpduguard" or (kind == "errdisable" and "bpdu" in reason)


def parse_link_state_event(message):
    match = _LINK_STATE_RE.search(str(message or ""))
    if not match:
        return None
    port, state = match.groups()
    return {"port": clean_iface_token(port), "state": state.lower()}


def parse_network_syslog_event(message):
    text = str(message or "").strip()

    match = _NATIVE_VLAN_RE.search(text)
    if match:
        local_port, local_vlan, peer_device, peer_port, peer_vlan = match.groups()
        local_port = clean_iface_token(local_port)
        peer_port = clean_iface_token(peer_port)
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
        port_a = clean_iface_token(port_a)
        port_b = clean_iface_token(port_b)
        return {
            "kind": "mac_flap",
            "title": "🚨 MAC 地址漂移",
            "color": "red",
            "mac": format_mac(mac),
            "vlan": vlan,
            "port_a": port_a,
            "port_b": port_b,
            "dedupe": f"macflap|{normalize_mac_hex(mac)}|{vlan}|{port_a}|{port_b}",
            "hint": "同一个 MAC 在两个端口之间反复学习，通常是二层环路、无线桥接、AP Mesh/第二网口或错误跳线。",
        }

    match = _ERRDISABLE_RE.search(text)
    if match:
        reason, port = match.groups()
        reason = reason.strip()
        port = clean_iface_token(port)
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
        port = clean_iface_token(match.group(1)) if match else ""
        return {
            "kind": "bpduguard",
            "title": "⛔ BPDU blocked: Has worsened",
            "color": "red",
            "port": port,
            "dedupe": f"bpduguard|{port}|{text[:100]}",
            "hint": "普通终端/AP 接入口不应该收到 BPDU；后面可能接了交换机、桥接设备或形成环路。",
        }

    if any(token in text.lower() for token in ("storm_control", "storm-control", "storm control")):
        match = _STORM_RE.search(text)
        port = clean_iface_token(match.group(1)) if match else ""
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
        port = clean_iface_token(match.group(1)) if match else ""
        return {
            "kind": "loopback",
            "title": "🛑 端口检测到回环",
            "color": "red",
            "port": port,
            "dedupe": f"loopback|{port}|{text[:100]}",
            "hint": "接口检测到二层回环，优先查该口后面的跳线、AP 第二网口或小交换机。",
        }
    return None


class MacFlapTracker:
    """Turn noisy MACFLAP lines into rate-aware, actionable events."""

    def __init__(self, gateway_macs=None, gateway_uplink_ports=None,
                 window_seconds=60, threshold=3):
        self.gateway_macs = {
            normalize_mac_hex(mac) for mac in (gateway_macs or [])
            if normalize_mac_hex(mac)
        }
        self.gateway_uplink_ports = {
            normalize_iface_key(port) for port in (gateway_uplink_ports or [])
            if normalize_iface_key(port)
        }
        self.window_seconds = max(1, int(window_seconds))
        self.threshold = max(1, int(threshold))
        self._moves = {}
        self._last_prune = 0.0

    def observe(self, host, event, now=None):
        if not event or event.get("kind") != "mac_flap":
            return event
        now = time.time() if now is None else float(now)
        mac = normalize_mac_hex(event.get("mac"))
        vlan = str(event.get("vlan") or "")
        key = (str(host or "").lower(), mac, vlan)
        cutoff = now - self.window_seconds
        if now - self._last_prune >= self.window_seconds:
            retained = {}
            for move_key, stamps in self._moves.items():
                active = [stamp for stamp in stamps if stamp >= cutoff]
                if active:
                    retained[move_key] = active
            self._moves = retained
            self._last_prune = now
        moves = [stamp for stamp in self._moves.get(key, []) if stamp >= cutoff]
        moves.append(now)
        self._moves[key] = moves

        port_a = event.get("port_a") or ""
        port_b = event.get("port_b") or ""
        normalized_ports = {
            port_a: normalize_iface_key(port_a),
            port_b: normalize_iface_key(port_b),
        }
        expected = [
            port for port, normalized in normalized_ports.items()
            if normalized and normalized in self.gateway_uplink_ports
        ]
        unexpected = [
            port for port, normalized in normalized_ports.items()
            if normalized and normalized not in self.gateway_uplink_ports
        ]
        is_gateway = bool(mac and mac in self.gateway_macs)
        uplink_to_unexpected = bool(is_gateway and len(expected) == 1 and len(unexpected) == 1)
        if not uplink_to_unexpected and len(moves) < self.threshold:
            return None

        enriched = dict(event)
        enriched.update({
            "gateway_mac": is_gateway,
            "move_count": len(moves),
            "window_seconds": self.window_seconds,
        })
        if is_gateway:
            enriched.update({
                "title": "🔴 网关 MAC 异常移动",
                "color": "red",
                "dedupe": f"gateway-mac-flap|{mac}|{vlan}",
            })
            if uplink_to_unexpected:
                enriched.update({
                    "normal_port": expected[0],
                    "abnormal_port": unexpected[0],
                    "hint": "关键网关 MAC 在正常上联和其他接口之间移动，可能存在二层环路、错误跳线或网关双主。",
                })
            else:
                enriched["hint"] = "关键网关 MAC 在统计窗口内频繁移动；若两端都是正常 HA 上联，先核对主备切换，否则检查二层环路、错误跳线或网关双主。"
        else:
            enriched.update({
                "title": "🟠 普通 MAC 频繁漂移",
                "color": "orange",
                "dedupe": f"mac-flap|{mac}|{vlan}",
                "hint": "同一个 MAC 在两个接口之间频繁学习，常见于二层环路、无线桥接、AP Mesh/第二网口或错误跳线。",
            })
        return enriched
