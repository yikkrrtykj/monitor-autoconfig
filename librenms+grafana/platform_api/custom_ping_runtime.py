"""DNS 运行状态和 IP 生效时段。调用方持有 PingStore 的配置/发布锁。"""
from __future__ import annotations

from copy import deepcopy
from ipaddress import IPv4Address, IPv4Network
import math
import logging
import os
import time

from .custom_ping_dns import PRIVATE, signature
from .custom_ping_store import (
    PingError, decode_json, encode_json, staged_file, sync_directory, valid_id, validate_ip,
)

LOG = logging.getLogger("platform_api.custom_ping")
MAX_BYTES = 32 * 1024 * 1024
MAX_SEGMENTS = 100000
RETENTION = 7 * 86400
FIELDS = {"address", "endpointRevision", "status", "error", "ips", "ip", "expiresAt", "resolvedAt",
          "nextRefreshAt", "failures", "generation", "activatedAt"}
STATUSES = {"DNS_OK", "DNS_ERROR", "DNS_POLICY_BLOCKED", "DNS_STALE"}
ERRORS = {"", "timeout", "nxdomain", "servfail", "no_a", "cname_limit", "answer_limit",
          "invalid_response", "resolver_error", "policy", "inventory", "ttl_zero", "history_capacity", "paused"}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate(state):
    if (not isinstance(state, dict) or state.keys() != {"schema", "generation", "rows", "segments"}
            or type(state["schema"]) is not int or state["schema"] != 1
            or type(state["generation"]) is not int or not 0 <= state["generation"] < 10**15
            or not isinstance(state["rows"], dict) or len(state["rows"]) > 300
            or not isinstance(state["segments"], list) or len(state["segments"]) > MAX_SEGMENTS):
        raise ValueError("invalid DNS runtime")
    for identity, row in state["rows"].items():
        if (not valid_id(identity) or not isinstance(row, dict) or row.keys() != FIELDS
                or not isinstance(row["address"], str) or len(row["address"]) > 253
                or row["status"] not in STATUSES or row["error"] not in ERRORS
                or type(row["endpointRevision"]) is not int or row["endpointRevision"] < 1
                or type(row["generation"]) is not int or not 0 < row["generation"] <= state["generation"]
                or type(row["failures"]) is not int or not 0 <= row["failures"] <= 4
                or not isinstance(row["ips"], list) or len(row["ips"]) > 64
                or any(str(IPv4Address(ip)) != ip for ip in row["ips"])
                or row["ip"] is not None and validate_ip(row["ip"]) != row["ip"]
                or not all(finite(row[k]) for k in ("expiresAt", "resolvedAt", "nextRefreshAt", "activatedAt"))):
            raise ValueError("invalid DNS runtime row")
    seen = set()
    for segment in state["segments"]:
        if (not isinstance(segment, dict)
                or segment.keys() != {"id", "address", "ip", "generation", "start", "end"}
                or not valid_id(segment["id"]) or not isinstance(segment["address"], str)
                or len(segment["address"]) > 253 or validate_ip(segment["ip"]) != segment["ip"]
                or type(segment["generation"]) is not int
                or not 0 < segment["generation"] <= state["generation"]
                or segment["generation"] in seen or not finite(segment["start"])
                or segment["end"] is not None and (
                    not finite(segment["end"]) or segment["end"] < segment["start"])):
            raise ValueError("invalid DNS history segment")
        seen.add(segment["generation"])


class DNSState:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock

    def read(self, required=False):
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
        except FileNotFoundError as exc:
            if required:
                raise PingError(503, "custom_ping_dns_state_unavailable") from exc
            return {"schema": 1, "generation": 0, "rows": {}, "segments": []}
        except OSError as exc:
            raise PingError(503, "custom_ping_dns_state_unavailable") from exc
        try:
            if len(raw) > MAX_BYTES:
                raise ValueError("DNS state too large")
            state = decode_json(raw)
            validate(state)
            return state
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise PingError(503, "custom_ping_dns_state_unavailable") from exc

    def write(self, state):
        validate(state)
        encoded = encode_json(state)
        if len(encoded) > MAX_BYTES:
            raise PingError(409, "custom_ping_dns_storage_limit")
        try:
            with staged_file(self.path, encoded, 0o600) as temporary:
                os.replace(temporary, self.path)
            sync_directory(self.path.parent)
        except OSError as exc:
            raise PingError(503, "custom_ping_dns_commit_failed") from exc

    def view(self, row, state, now):
        saved = state["rows"].get(row["id"])
        if not saved or (saved["address"], saved["endpointRevision"]) != (
                row["address"], row["endpointRevision"]):
            return {"dnsStatus": "DNS_PENDING", "resolvedIp": None, "aCount": 0,
                    "resolutionGeneration": None, "sampleNotBefore": None,
                    "resolvedAt": None, "expiresAt": None, "dnsError": ""}
        status = saved["status"]
        if status == "DNS_OK" and (saved["expiresAt"] <= now or saved["resolvedAt"] > now):
            status = "DNS_STALE"
        return {"dnsStatus": status, "resolvedIp": saved["ip"], "aCount": len(saved["ips"]),
                "resolutionGeneration": saved["generation"], "sampleNotBefore": saved["activatedAt"],
                "resolvedAt": saved["resolvedAt"], "expiresAt": saved["expiresAt"], "dnsError": saved["error"]}

    def maintain(self, manifest, state, now):
        """过期/删除/编辑关闭时段，保留七天内来源。仅 reconciliation/写入口落盘。"""
        result = deepcopy(state)
        rows = {r["id"]: r for r in manifest["targets"] if r.get("kind") == "dns" and r["enabled"]}
        for segment in result["segments"]:
            saved = result["rows"].get(segment["id"])
            row = rows.get(segment["id"])
            if segment["end"] is not None or saved is None:
                continue
            if saved["generation"] != segment["generation"]:
                continue
            end = None
            if not row or signature(row) != (row["id"], saved["address"], saved["endpointRevision"]):
                end = now
                for receipt in manifest["receipts"]:
                    before, after = receipt["before"], receipt["after"]
                    if (before and before["id"] == segment["id"] and before.get("kind") == "dns"
                            and before["endpointRevision"] == saved["endpointRevision"]
                            and (after is None or after.get("endpointRevision") != before["endpointRevision"])):
                        from datetime import datetime
                        end = datetime.fromisoformat(receipt["time"].replace("Z", "+00:00")).timestamp()
                        break
            if saved["expiresAt"] <= now:
                end = min(end if end is not None else now, saved["expiresAt"])
            if end is not None:
                segment["end"] = max(segment["start"], min(now, end))
        result["segments"] = [s for s in result["segments"] if s["end"] is None or s["end"] >= now - RETENTION]
        result["rows"] = {identity: value for identity, value in result["rows"].items() if identity in rows}
        return result

    def apply(self, manifest, row, answer, check_address):
        now = self.clock()
        state = self.maintain(manifest, self.read(required=True), now)
        old = state["rows"].get(row["id"])
        same = old and (old["address"], old["endpointRevision"]) == (row["address"], row["endpointRevision"])
        previous_ip = old["ip"] if same else None
        failures = min(4, (old["failures"] if same else 0) + 1)
        ips = []
        error = answer.error
        allowed = []
        if not error:
            try:
                if len(answer.ips) > 64 or type(answer.ttl) is not int or not 0 <= answer.ttl <= 2**31 - 1:
                    raise ValueError("invalid DNS result")
                ips = sorted({str(IPv4Address(ip)) for ip in answer.ips}, key=IPv4Address)
            except (ValueError, TypeError):
                error = "policy"
            used = {r["ip"] for r in manifest["targets"] if r.get("kind") != "dns"}
            for other in manifest["targets"]:
                if other.get("kind") == "dns" and other["id"] != row["id"]:
                    view = self.view(other, state, now)
                    if other["enabled"] and view["dnsStatus"] == "DNS_OK":
                        used.add(view["resolvedIp"])
            if not error:
                for ip in ips:
                    address = IPv4Address(ip)
                    if any(address in n for n in PRIVATE) and not any(
                            address in IPv4Network(n) for n in row["privateCidrs"]):
                        continue
                    if ip in used:
                        continue
                    try:
                        check_address(ip)
                    except PingError as exc:
                        if exc.status >= 500:
                            error = "inventory"
                            break
                        continue
                    allowed.append(ip)
                if not error and not allowed:
                    error = "policy" if ips else "no_a"
                if not error and answer.ttl == 0:
                    error = "ttl_zero"
        selected = (previous_ip if previous_ip in allowed else next(iter(allowed), None)) if not error else None
        active_old = same and old["status"] == "DNS_OK" and old["expiresAt"] > now
        changed = not (active_old and selected and selected == previous_ip)
        if selected and changed and len(state["segments"]) >= MAX_SEGMENTS:
            error, selected = "history_capacity", None
        if changed:
            state["generation"] += 1
            for segment in state["segments"]:
                if segment["id"] == row["id"] and segment["end"] is None:
                    segment["end"] = max(segment["start"], min(now, old["expiresAt"] if old else now))
        generation = state["generation"] if changed else old["generation"]
        activated = now if changed else old["activatedAt"]
        if selected and changed:
            state["segments"].append({"id": row["id"], "address": row["address"], "ip": selected,
                                      "generation": generation, "start": now, "end": None})
        status = "DNS_OK" if selected else "DNS_POLICY_BLOCKED" if error == "policy" else (
            "DNS_STALE" if error == "ttl_zero" else "DNS_ERROR")
        state["rows"][row["id"]] = {
            "address": row["address"], "endpointRevision": row["endpointRevision"],
            "status": status, "error": error, "ips": ips,
            "ip": selected or previous_ip, "expiresAt": now + answer.ttl if selected else now,
            "resolvedAt": now if selected else (old["resolvedAt"] if same else 0),
            "nextRefreshAt": now + (max(30, min(300, int(answer.ttl * 0.8))) if selected else (30, 60, 120, 300)[failures - 1]),
            "failures": 0 if selected else failures, "generation": generation, "activatedAt": activated,
        }
        self.write(state)
        if changed and (not old or (old["status"], old["ip"], old["error"]) != (status, selected or previous_ip, error)):
            LOG.info("custom_ping dns_transition id=%s generation=%d status=%s error=%s",
                     row["id"], generation, status, error)
        return state

