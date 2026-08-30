"""Interconnect aggregate watcher for the Feishu alert bridge.

The bridge owns environment parsing, shared transports, card presentation,
cross-domain Syslog correlation, health state, and thread supervision.  This
module owns Interconnect Prometheus mapping, active alert persistence, and the
watcher state machine.
"""

import re
import time

from lag_ownership import resolve_lag_ownership


def classify_interconnect(lag_up, member_ups):
    """Classify an aggregate without manufacturing state from missing members."""
    if len(member_ups) < 2 or any(state is None for state in member_ups):
        return "unknown"
    if not lag_up:
        return "down"
    any_down = not all(member_ups)
    any_up = any(member_ups)
    if not any_down:
        return "healthy"
    if not any_up:
        return "down"
    return "degraded"


def _port_label(metric):
    for field in ("ifName", "ifDescr", "ifAlias"):
        value = (metric.get(field) or "").strip()
        if value:
            return value
    return metric.get("ifIndex") or "?"


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


def _if_admin_is_up(metric, value):
    status_label = (
        metric.get("ifAdminStatus")
        or metric.get("ifAdminStatus_label")
        or metric.get("ifAdminStatus_state")
    )
    if status_label:
        if value < 0.5:
            return None
        return str(status_label).lower() == "up"
    return int(value) == 1


def load_interconnect_alert_states(state_file, load_json_dict, as_float):
    loaded = {}
    for key, value in load_json_dict(state_file).items():
        if not key or not isinstance(value, dict) or not value.get("alerting"):
            continue
        down_members = value.get("down_members")
        last_port = value.get("last_port")
        loaded[str(key)] = {
            "alerting": True,
            "down_since": as_float(value.get("down_since")),
            "down_members": [str(item) for item in down_members]
            if isinstance(down_members, list) else [],
            "peer_switch": str(value.get("peer_switch") or ""),
            "last_port": dict(last_port) if isinstance(last_port, dict) else {},
            "handoff_logged": False,
            "missing_since": None,
        }
    return loaded


def save_interconnect_alert_states(state_file, states, save_json_dict):
    active = {}
    for key, state in states.items():
        if not state.get("alerting"):
            continue
        last_port = state.get("last_port")
        active[str(key)] = {
            "alerting": True,
            "down_since": state.get("down_since"),
            "down_members": list(state.get("down_members") or []),
            "peer_switch": state.get("peer_switch") or "",
            "last_port": dict(last_port) if isinstance(last_port, dict) else {},
        }
    return save_json_dict(state_file, active)


def build_peer_map(edges, audit_port_key):
    """Build scored physical/aggregate peer evidence from LLDP topology edges."""
    peers = {}

    def add(ip, ports, peer, weight):
        for port in ports:
            key = (ip, audit_port_key(port))
            if not key[0] or not key[1] or not peer:
                continue
            scores = peers.setdefault(key, {})
            scores[peer] = scores.get(peer, 0) + weight

    for edge in edges or []:
        ip = str(edge.get("from_ip") or "").strip()
        port = str(edge.get("from_port") or "").strip()
        peer = str(edge.get("to_sysname") or edge.get("to_ip") or "").strip()
        weight = 1 if edge.get("stale") else 2
        from_ports = [port]
        from_ports.extend(edge.get("from_member_ports") or [])
        from_ports.append(edge.get("from_aggregate_port") or "")
        add(ip, from_ports, peer, weight)
        reverse_ip = str(edge.get("to_ip") or "").strip()
        reverse_port = str(edge.get("to_port") or "").strip()
        reverse_peer = str(edge.get("from_sysname") or edge.get("from_ip") or "").strip()
        reverse_ports = [reverse_port]
        reverse_ports.extend(edge.get("to_member_ports") or [])
        reverse_ports.append(edge.get("to_aggregate_port") or "")
        add(reverse_ip, reverse_ports, reverse_peer, weight)
    return peers


def resolve_peer_switch(peer_map, ip, physical_ports, audit_port_key, aggregate_port=""):
    """Resolve physical peers first; use an aggregate only as a true fallback."""

    def resolve_one(port):
        scores = peer_map.get((ip, audit_port_key(port))) or {}
        if isinstance(scores, str):
            scores = {scores: 1}
        if not scores:
            return "missing", ""
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0].casefold()))
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            return "ambiguous", ""
        return "resolved", ranked[0][0]

    resolved_physical = set()
    for port in physical_ports or []:
        status, peer = resolve_one(port)
        if status == "ambiguous":
            return ""
        if status == "resolved":
            resolved_physical.add(peer)
    if len(resolved_physical) > 1:
        return ""
    if resolved_physical:
        return next(iter(resolved_physical))
    if aggregate_port:
        status, peer = resolve_one(aggregate_port)
        return peer if status == "resolved" else ""
    return ""


class InterconnectWatcher:
    """Own the Interconnect polling, alert, recovery, and restart state machine."""

    def __init__(
        self,
        *,
        enabled,
        alert_for_seconds,
        poll_interval,
        jobs,
        port_filter,
        state_file,
        prometheus_query,
        fetch_name_cache,
        load_topology_edges,
        load_json_dict,
        save_json_dict,
        as_float,
        normalize_label,
        audit_port_key,
        build_card,
        send,
        find_merge_candidate,
        complete_merge,
        mark_watcher_health,
        log,
        sleep=None,
        now=None,
    ):
        self.enabled = enabled
        self.alert_for_seconds = alert_for_seconds
        self.poll_interval = poll_interval
        self.jobs = jobs
        self.port_filter = port_filter
        self.state_file = state_file
        self.prometheus_query = prometheus_query
        self.fetch_name_cache = fetch_name_cache
        self.load_topology_edges = load_topology_edges
        self.load_json_dict = load_json_dict
        self.save_json_dict = save_json_dict
        self.as_float = as_float
        self.normalize_label = normalize_label
        self.audit_port_key = audit_port_key
        self.build_card = build_card
        self.send = send
        self.find_merge_candidate = find_merge_candidate
        self.complete_merge = complete_merge
        self.mark_watcher_health = mark_watcher_health
        self.log = log
        self.sleep = sleep or time.sleep
        self.now = now or time.time
        # The old function attribute survived watcher-supervisor restarts. Keep
        # the same log-deduplication lifetime on the singleton watcher instance.
        self._last_conflicts = None

    def _keywords(self):
        return [part.strip().lower() for part in self.port_filter.split(",") if part.strip()]

    def _is_interconnect_port(self, metric):
        fields = [
            metric.get("ifName") or "",
            metric.get("ifDescr") or "",
            metric.get("ifAlias") or "",
        ]
        joined = " ".join(fields).lower()
        norm = self.normalize_label(joined)
        for keyword in self._keywords():
            knorm = self.normalize_label(keyword)
            if not knorm:
                continue
            if len(knorm) <= 3:
                if norm.startswith(knorm):
                    return True
                continue
            if keyword in joined or norm.startswith(knorm):
                return True
        return False

    def load_alert_states(self):
        return load_interconnect_alert_states(
            self.state_file, self.load_json_dict, self.as_float,
        )

    def save_alert_states(self, states):
        return save_interconnect_alert_states(
            self.state_file, states, self.save_json_dict,
        )

    def fetch_interconnect_members(self, jobs_regex):
        """Resolve authoritative LAG ownership from current SNMP metrics."""

        def fetch(metric_name):
            try:
                return self.prometheus_query(f'{metric_name}{{job=~"{jobs_regex}"}}')
            except Exception as exc:
                self.log(f"[LINK] {metric_name} lookup failed: {exc}")
                return []

        def metric_ip(metric):
            return metric.get("target_ip") or metric.get("instance") or ""

        ifstack_by_ip = {}
        for item in fetch("ifStackStatus"):
            raw_value = (item.get("value") or [None, None])[-1]
            try:
                if float(raw_value) != 1.0:
                    continue
            except (TypeError, ValueError):
                continue
            metric = item.get("metric") or {}
            higher = metric.get("ifStackHigherLayer") or metric.get("ifStackHigherLayerIndex")
            lower = metric.get("ifStackLowerLayer") or metric.get("ifStackLowerLayerIndex")
            ip = metric_ip(metric)
            if not higher or not lower or higher == "0" or lower == "0":
                continue
            bucket = ifstack_by_ip.setdefault(ip, {}).setdefault(higher, [])
            if lower not in bucket:
                bucket.append(lower)

        def indexed_values(metric_name, index_label):
            values = {}
            for item in fetch(metric_name):
                metric = item.get("metric") or {}
                ip = metric_ip(metric)
                index = metric.get(index_label)
                try:
                    value = int(float((item.get("value") or [None, "nan"])[-1]))
                except (TypeError, ValueError):
                    continue
                if ip and index not in (None, ""):
                    values.setdefault(ip, {})[index] = value
            return values

        pagp_by_ip = indexed_values("pagpGroupIfIndex", "physicalIfIndex")
        attached_by_ip = indexed_values("dot3adAggPortAttachedAggID", "physicalIfIndex")
        aggregate_keys_by_ip = indexed_values("dot3adAggActorAdminKey", "aggregateIfIndex")
        physical_keys_by_ip = indexed_values("dot3adAggPortActorAdminKey", "physicalIfIndex")

        resolved_members = {}
        conflicts = {}
        device_ips = (
            set(ifstack_by_ip) | set(pagp_by_ip) | set(attached_by_ip)
            | set(aggregate_keys_by_ip) | set(physical_keys_by_ip)
        )
        for ip in sorted(device_ips):
            resolution = resolve_lag_ownership(
                ifstack_claims=ifstack_by_ip.get(ip),
                pagp_group_ifindex=pagp_by_ip.get(ip),
                attached_aggregate_id=attached_by_ip.get(ip),
                aggregate_admin_keys=aggregate_keys_by_ip.get(ip),
                physical_admin_keys=physical_keys_by_ip.get(ip),
            )
            for aggregate, member_indexes in resolution["members_by_aggregate"].items():
                resolved_members[(ip, str(aggregate))] = [str(value) for value in member_indexes]
            if resolution["conflicts"]:
                conflicts[ip] = resolution["conflicts"]

        if conflicts != self._last_conflicts:
            if conflicts:
                self.log(f"[LINK] isolated ambiguous LAG ownership: {conflicts}")
            self._last_conflicts = conflicts
        return resolved_members

    def fetch_interconnect_ports(self, jobs_regex):
        results = self.prometheus_query(f'ifOperStatus{{job=~"{jobs_regex}"}}')
        try:
            admin_results = self.prometheus_query(f'ifAdminStatus{{job=~"{jobs_regex}"}}')
        except Exception as exc:
            self.log(f"[LINK] ifAdminStatus lookup failed; skipping admin-state filter: {exc}")
            admin_results = []
        admin_up = {}
        for item in admin_results:
            metric = item.get("metric") or {}
            ip = metric.get("target_ip") or metric.get("instance") or ""
            ifindex = metric.get("ifIndex")
            if not (ip and ifindex):
                continue
            try:
                value = float((item.get("value") or [None, "nan"])[1])
            except (TypeError, ValueError):
                continue
            state = _if_admin_is_up(metric, value)
            if state is not None:
                admin_up[(ip, ifindex)] = state

        index_meta = {}
        index_up = {}
        for item in results:
            metric = item.get("metric") or {}
            ip = metric.get("target_ip") or metric.get("instance") or ""
            ifindex = metric.get("ifIndex")
            if not (ip and ifindex):
                continue
            index_meta[(ip, ifindex)] = {
                "name": _port_label(metric),
                "alias": metric.get("ifAlias") or "",
                "descr": metric.get("ifDescr") or "",
            }
            try:
                value = float((item.get("value") or [None, "nan"])[1])
            except (TypeError, ValueError):
                continue
            member_up = _if_oper_is_up(metric, value)
            if member_up is not None:
                index_up[(ip, ifindex)] = bool(member_up)
        members_map = self.fetch_interconnect_members(jobs_regex)

        ports = []
        for item in results:
            metric = item.get("metric") or {}
            if not self._is_interconnect_port(metric):
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
            if admin_up.get((ip, ifindex)) is False:
                continue
            members = []
            for member_idx in members_map.get((ip, ifindex), []):
                member_meta = index_meta.get((ip, member_idx)) or {}
                name = member_meta.get("name")
                if not name or name == port:
                    continue
                members.append({
                    "name": name,
                    "ifindex": member_idx,
                    "up": index_up.get((ip, member_idx)),
                    "alias": member_meta.get("alias") or "",
                    "descr": member_meta.get("descr") or "",
                })
            ports.append({
                "key": "|".join([metric.get("job", ""), ip, ifindex or port]),
                "device": metric.get("display_name") or metric.get("instance") or ip or "?",
                "ip": ip,
                "port": port,
                "ifindex": ifindex,
                "alias": metric.get("ifAlias") or "",
                "lag_up": bool(up),
                "members": members,
            })
        return ports

    def run(self):
        if not self.enabled:
            self.log("[LINK] interconnect watcher disabled")
            return
        jobs = [job.strip() for job in self.jobs.split(",") if job.strip()]
        safe_jobs = [job for job in jobs if re.match(r"^[A-Za-z0-9_:.-]+$", job)]
        if not safe_jobs:
            self.log("[LINK] no valid SNMP jobs configured, watcher disabled")
            return

        jobs_regex = "|".join(safe_jobs)
        # A supervisor restart must discard unpersisted runtime state and reload
        # the last successfully delivered active alerts before the original wait.
        states = self.load_alert_states()
        last_status_log = 0.0
        last_name_refresh = 0.0
        librenms_names = {}
        peer_map = {}
        self.sleep(25)
        self.log(
            "[LINK] interconnect watcher enabled "
            f"(jobs={','.join(safe_jobs)}, for={self.alert_for_seconds}s, "
            f"poll={self.poll_interval}s, filter={self.port_filter!r})"
        )

        while True:
            now = self.now()
            if now - last_name_refresh >= 60:
                try:
                    librenms_names = self.fetch_name_cache()
                except Exception as exc:
                    self.log(f"[LINK] LibreNMS name refresh failed: {exc}")
                try:
                    peer_map = build_peer_map(self.load_topology_edges(), self.audit_port_key)
                except Exception as exc:
                    self.log(f"[LINK] topology peer refresh failed: {exc}")
                last_name_refresh = now

            try:
                ports = self.fetch_interconnect_ports(jobs_regex)
            except Exception as exc:
                self.mark_watcher_health("interconnect", False, exc)
                self.log(f"[LINK] poll failed: {exc}")
                self.sleep(self.poll_interval)
                continue
            self.mark_watcher_health("interconnect", True)

            if now - last_status_log >= 60:
                degraded = sum(
                    1 for port in ports
                    if classify_interconnect(
                        port["lag_up"], [member["up"] for member in port["members"]],
                    ) == "degraded"
                )
                self.log(f"[LINK] watched aggregates total={len(ports)} degraded={degraded}")
                last_status_log = now

            seen_keys = set()
            for port in ports:
                seen_keys.add(port["key"])
                ip = port.get("ip") or ""
                if ip in librenms_names:
                    port["device"] = librenms_names[ip]
                member_ups = [member["up"] for member in port["members"]]
                status = classify_interconnect(port["lag_up"], member_ups)
                state = states.setdefault(port["key"], {
                    "down_since": None,
                    "alerting": False,
                    "down_members": [],
                    "handoff_logged": False,
                    "missing_since": None,
                })
                state["last_port"] = dict(port)
                state["missing_since"] = None

                if status in ("degraded", "down"):
                    down_member_details = [
                        dict(member) for member in port["members"]
                        if member.get("up") is False
                    ]
                    down_members = [member["name"] for member in down_member_details]
                    if state["down_since"] is None:
                        state["down_since"] = now
                    duration = max(0, now - state["down_since"])
                    state["down_members"] = down_members
                    if not state["alerting"] and duration >= self.alert_for_seconds:
                        peer = resolve_peer_switch(
                            peer_map,
                            port["ip"],
                            down_members,
                            self.audit_port_key,
                            aggregate_port=port["port"],
                        )
                        state["peer_switch"] = peer
                        event = dict(port)
                        event["down_members"] = down_members
                        event["down_member_details"] = down_member_details
                        event["up_members"] = [
                            member["name"] for member in port["members"]
                            if member.get("up") is True
                        ]
                        event["peer_switch"] = peer
                        event["duration"] = duration
                        event["status"] = status
                        cause = self.find_merge_candidate(event, now)
                        if cause:
                            event["syslog_cause"] = cause
                        self.log(
                            f"[LINK] ALERT {event['device']} {event['port']} "
                            f"status={status}, down member(s)={down_members} peer={peer or '-'}"
                        )
                        if self.send(self.build_card(event, recovered=False)):
                            self.complete_merge(event, cause, now)
                            state["alerting"] = True
                            state["handoff_logged"] = False
                            self.save_alert_states(states)
                else:
                    if state["alerting"]:
                        if status == "healthy":
                            event = dict(port)
                            event["down_members"] = state.get("down_members") or []
                            event["up_members"] = [member["name"] for member in port["members"]]
                            event["peer_switch"] = state.get("peer_switch") or ""
                            event["duration"] = max(0, now - (state["down_since"] or now))
                            event["status"] = "healthy"
                            self.log(
                                f"[LINK] RECOVER {event['device']} {event['port']} members back up"
                            )
                            if self.send(self.build_card(event, recovered=True)):
                                state["alerting"] = False
                                state["down_since"] = None
                                state["down_members"] = []
                                state["handoff_logged"] = False
                                self.save_alert_states(states)
                    else:
                        state["down_since"] = None
                        state["down_members"] = []
                        state["handoff_logged"] = False

            for key, state in list(states.items()):
                if key in seen_keys:
                    continue
                if not state.get("alerting"):
                    states.pop(key, None)
                    continue
                if state.get("missing_since") is None:
                    state["missing_since"] = now
                if now - state["missing_since"] < self.alert_for_seconds:
                    continue
                event = dict(state.get("last_port") or {})
                event["down_members"] = state.get("down_members") or []
                event["up_members"] = []
                event["peer_switch"] = state.get("peer_switch") or ""
                event["duration"] = max(0, now - (state.get("down_since") or now))
                event["status"] = "healthy"
                self.log(
                    f"[LINK] RECOVER vanished aggregate "
                    f"{event.get('device', '?')} {event.get('port', '?')}"
                )
                if self.send(self.build_card(event, recovered=True)):
                    states.pop(key, None)
                    self.save_alert_states(states)

            self.sleep(self.poll_interval)
