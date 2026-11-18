"""DNS 运行状态和 IP 生效时段。调用方持有 PingStore 的配置/发布锁。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Address, IPv4Network
import logging
import math
import os
import time

from .custom_ping_dns import Answer, PRIVATE, signature
from .custom_ping_store import (
    PingError, decode_json, encode_json, staged_file, sync_directory, valid_id, validate_ip,
)

LOG = logging.getLogger("platform_api.custom_ping")
MAX_BYTES = 32 * 1024 * 1024
MAX_SEGMENTS = 100000
# 300 rows at <2 KiB each, plus the generation and JSON envelope.
RUNTIME_RESERVE_BYTES = 1024 * 1024
# Each open segment can gain two finite JSON timestamps in place of null.
OPEN_SEGMENT_RESERVE_BYTES = 64
RETENTION = 7 * 86400
FIELDS = {"address", "endpointRevision", "status", "error", "ips", "ip", "expiresAt", "resolvedAt",
          "nextRefreshAt", "failures", "generation", "activatedAt"}
STATUSES = {"DNS_OK", "DNS_ERROR", "DNS_POLICY_BLOCKED", "DNS_STALE"}
ERRORS = {"", "timeout", "nxdomain", "servfail", "no_a", "cname_limit", "answer_limit",
          "invalid_response", "resolver_error", "policy", "inventory", "ttl_zero", "ttl_expired", "history_capacity", "paused"}


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
                or segment.keys() - {"sampleNotBefore"} != {"id", "address", "ip", "generation", "start", "end"}
                or not valid_id(segment["id"]) or not isinstance(segment["address"], str)
                or len(segment["address"]) > 253 or validate_ip(segment["ip"]) != segment["ip"]
                or type(segment["generation"]) is not int
                or not 0 < segment["generation"] <= state["generation"]
                or segment["generation"] in seen or not finite(segment["start"])
                or segment.get("sampleNotBefore") is not None and (
                    not finite(segment["sampleNotBefore"]) or segment["sampleNotBefore"] < segment["start"])
                or segment["end"] is not None and (
                    not finite(segment["end"]) or segment["end"] < segment["start"])):
            raise ValueError("invalid DNS history segment")
        seen.add(segment["generation"])


@dataclass
class Lease:
    generation: int
    deadline: float
    refresh_at: float
    sample_not_before: float | None = None


@dataclass(frozen=True)
class WriteFailure:
    endpoint: tuple
    ended_at: float
    retry_at: float


class DNSState:
    def __init__(self, path, clock=time.time, monotonic=time.monotonic):
        self.path = path
        self.clock = clock
        self.monotonic = monotonic
        # The disk state keeps history across restarts. Only DNS answers verified
        # by this process may publish or attest to a current endpoint.
        self.leases: dict[str, Lease] = {}
        self.failures: dict[str, WriteFailure] = {}

    def failure(self, row):
        failure = self.failures.get(row["id"])
        return failure if failure and failure.endpoint == signature(row) else None

    def invalidate(self, row, *, ended_at=None, retry_at=None):
        previous = self.failure(row)
        self.leases.pop(row["id"], None)
        if ended_at is None:
            ended_at = self.clock()
        if previous:
            ended_at = min(ended_at, previous.ended_at)
        if retry_at is None:
            retry_at = self.monotonic() + 30
        self.failures[row["id"]] = WriteFailure(signature(row), ended_at, retry_at)

    def prune(self, manifest):
        active = {row["id"]: row for row in manifest["targets"] if row.get("kind") == "dns" and row["enabled"]}
        self.leases = {identity: lease for identity, lease in self.leases.items() if identity in active}
        self.failures = {identity: failure for identity, failure in self.failures.items()
                         if identity in active and failure.endpoint == signature(active[identity])}

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

    def write(self, state, *, still_valid=None) -> bool:
        validate(state)
        encoded = encode_json(state)
        if len(encoded) > MAX_BYTES:
            raise PingError(409, "custom_ping_dns_storage_limit")
        try:
            with staged_file(self.path, encoded, 0o600) as temporary:
                if still_valid is not None and not still_valid():
                    return False
                os.replace(temporary, self.path)
            sync_directory(self.path.parent)
        except OSError as exc:
            raise PingError(503, "custom_ping_dns_commit_failed") from exc
        return True

    def view(self, row, state, now):
        identity = row["id"]
        saved = state["rows"].get(identity)
        failure = self.failure(row)
        empty = {"resolvedIp": None, "aCount": 0, "resolutionGeneration": None,
                 "activatedAt": None, "sampleNotBefore": None, "resolvedAt": None, "expiresAt": None}
        if not saved or signature(row) != (identity, saved["address"], saved["endpointRevision"]):
            return {**empty, "dnsStatus": "DNS_ERROR" if failure else "DNS_PENDING",
                    "dnsError": "storage" if failure else ""}
        status, error = saved["status"], saved["error"]
        lease = self.leases.get(identity)
        if failure:
            status, error = "DNS_ERROR", "storage"
        elif status == "DNS_OK":
            if not lease or lease.generation != saved["generation"]:
                status, error = "DNS_STALE", "restart_pending"
            elif (lease.deadline <= self.monotonic() or saved["expiresAt"] <= now
                  or saved["resolvedAt"] > now):
                # A wall clock correction cannot revive a withdrawn lease.
                lease.deadline = 0
                status, error = "DNS_STALE", "ttl_expired"
        return {"dnsStatus": status, "resolvedIp": saved["ip"], "aCount": len(saved["ips"]),
                "resolutionGeneration": saved["generation"], "activatedAt": saved["activatedAt"],
                "sampleNotBefore": lease.sample_not_before if status == "DNS_OK" else None,
                "resolvedAt": saved["resolvedAt"], "expiresAt": saved["expiresAt"], "dnsError": error}

    def confirm(self, state, candidates, now):
        """Persist the first observed scrape boundary for each live generation."""
        pending = {identity: lease for identity, lease in candidates.items() if lease.sample_not_before is None}
        if not pending:
            return
        for segment in state["segments"]:
            lease = pending.get(segment["id"])
            if lease and lease.generation == segment["generation"] and segment["end"] is None:
                segment["sampleNotBefore"] = max(segment["start"], now)
        self.write(state)
        for segment in state["segments"]:
            lease = pending.get(segment["id"])
            if lease and lease.generation == segment["generation"] and segment["end"] is None:
                lease.sample_not_before = segment["sampleNotBefore"]

    @staticmethod
    def history_fits(state):
        segments = state["segments"]
        if len(segments) > MAX_SEGMENTS:
            return False
        reserved = sum(segment["end"] is None for segment in segments) * OPEN_SEGMENT_RESERVE_BYTES
        return len(encode_json({**state, "rows": {}})) + reserved + RUNTIME_RESERVE_BYTES <= MAX_BYTES

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
                        end = datetime.fromisoformat(receipt["time"].replace("Z", "+00:00")).timestamp()
                        break
            if row:
                failure = self.failure(row)
                if failure:
                    end = min(end if end is not None else now, failure.ended_at)
                elif self.view(row, state, now)["dnsStatus"] != "DNS_OK":
                    end = min(end if end is not None else now, saved["expiresAt"], now)
            if saved["expiresAt"] <= now:
                end = min(end if end is not None else now, saved["expiresAt"])
            if end is not None:
                segment["end"] = max(segment["start"], min(now, end))
        result["segments"] = [s for s in result["segments"] if s["end"] is None or s["end"] >= now - RETENTION]
        result["rows"] = {identity: value for identity, value in result["rows"].items() if identity in rows}
        return result

    def apply(self, manifest, previous, row, answer, check_address):
        now = self.clock()
        state = self.maintain(manifest, previous, now)
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
                private = tuple(map(IPv4Network, row["privateCidrs"]))
                for ip in ips:
                    address = IPv4Address(ip)
                    if any(address in n for n in PRIVATE):
                        network = max((n for n in private if address in n),
                                      key=lambda n: n.prefixlen, default=None)
                        if network is None or (network.prefixlen < 31 and address in (
                                network.network_address, network.broadcast_address)):
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
        now = self.clock()
        monotonic_now = self.monotonic()
        deadline = expires_at = now
        if not error:
            deadline = answer.started_monotonic + answer.ttl
            expires_at = min(answer.started_at + answer.ttl, now + (deadline - monotonic_now))
            if deadline <= monotonic_now or expires_at <= now or now < answer.started_at:
                error = "ttl_expired"
        selected = (previous_ip if previous_ip in allowed else next(iter(allowed), None)) if not error else None
        active_old = same and self.view(row, state, now)["dnsStatus"] == "DNS_OK"
        changed = not (active_old and selected == previous_ip and selected is not None)
        if changed:
            state["generation"] += 1
            for segment in state["segments"]:
                if segment["id"] == row["id"] and segment["end"] is None:
                    segment["end"] = max(segment["start"], min(now, old["expiresAt"] if old else now))
        generation = state["generation"] if changed else old["generation"]
        activated = now if changed else old["activatedAt"]
        if selected and changed:
            state["segments"].append({"id": row["id"], "address": row["address"], "ip": selected,
                                      "generation": generation, "start": now, "end": None,
                                      "sampleNotBefore": None})
            if not self.history_fits(state):
                state["segments"].pop()
                error, selected = "history_capacity", None
        status = "DNS_OK" if selected else "DNS_POLICY_BLOCKED" if error == "policy" else (
            "DNS_STALE" if error in ("ttl_zero", "ttl_expired") else "DNS_ERROR")
        refresh_interval = (max(30, min(300, int(answer.ttl * 0.8))) if selected
                            else (30, 60, 120, 300)[failures - 1])
        state["rows"][row["id"]] = {
            "address": row["address"], "endpointRevision": row["endpointRevision"],
            "status": status, "error": error, "ips": ips,
            "ip": selected or previous_ip, "expiresAt": expires_at if selected else now,
            "resolvedAt": now if selected else (old["resolvedAt"] if same else 0),
            "nextRefreshAt": now + refresh_interval,
            "failures": 0 if selected else failures, "generation": generation, "activatedAt": activated,
        }

        def still_valid():
            wall = self.clock()
            return answer.started_at <= wall < expires_at and self.monotonic() < deadline

        if not self.write(state, still_valid=still_valid if selected else None):
            # The valid answer expired while staging. No proposed row or segment
            # reached disk; commit the same transition as an expired result.
            return self.apply(manifest, previous, row, Answer(error="ttl_expired"), check_address)
        self.failures.pop(row["id"], None)
        if selected:
            identity = row["id"]
            refresh_at = monotonic_now + refresh_interval
            if changed:
                self.leases[identity] = Lease(generation, deadline, refresh_at)
            else:
                self.leases[identity].deadline = deadline
                self.leases[identity].refresh_at = refresh_at
        else:
            self.leases.pop(row["id"], None)
        if changed and (not old or (old["status"], old["ip"], old["error"]) != (status, selected or previous_ip, error)):
            LOG.info("custom_ping dns_transition id=%s generation=%d status=%s error=%s",
                     row["id"], generation, status, error)
        return state

