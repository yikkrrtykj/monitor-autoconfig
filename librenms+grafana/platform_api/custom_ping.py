"""浏览器鉴权、地址策略以及自定义 Ping HTTP API。"""
from __future__ import annotations

import hmac
from http.client import HTTPException
from ipaddress import IPv4Address, IPv4Network, ip_address
import logging
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

from . import auth
from .custom_ping_store import (
    GROUPS, JOB, PingError, PingStore, decode_json, timestamp, valid_id, valid_name, validate_ip,
)

BASE = "/ping/targets"
MAX_BODY = 4096
MAX_INVENTORY_BYTES = 8 * 1024 * 1024
LOG = logging.getLogger("platform_api.custom_ping")


def header(handler, name: str) -> str:
    values = handler.headers.get_all(name, [])
    if len(values) > 1:
        raise PingError(400, "custom_ping_duplicate_header")
    return values[0] if values else ""


def origin_tuple(value: str) -> tuple[str, str, int] | None:
    if not value or len(value) > 512 or re.search(r"[\s,\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.path or parsed.query or parsed.fragment
            or "?" in value or "#" in value
        ):
            return None
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def read_body(handler) -> dict:
    length = header(handler, "Content-Length")
    if header(handler, "Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,10}", length):
        raise PingError(400, "custom_ping_invalid_length")
    if int(length) > MAX_BODY:
        raise PingError(413, "custom_ping_body_too_large")
    if header(handler, "Content-Type").partition(";")[0].strip().lower() != "application/json":
        raise PingError(415, "custom_ping_json_required")
    deadline = time.monotonic() + 5
    raw = bytearray()
    remaining = int(length)
    try:
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError
            handler.connection.settimeout(timeout)
            chunk = handler.rfile.read1(remaining)
            if not chunk:
                raise ValueError("incomplete request")
            raw.extend(chunk)
            remaining -= len(chunk)
        data = decode_json(raw)
    except OSError as exc:
        raise PingError(408, "custom_ping_read_timeout") from exc
    except (ValueError, RecursionError) as exc:
        raise PingError(400, "custom_ping_invalid_json") from exc
    if not isinstance(data, dict):
        raise PingError(400, "custom_ping_invalid_json")
    return data


def validate_changes(data: dict, create: bool) -> dict:
    fields = {"name", "ip", "group"}
    if create and data.keys() != fields:
        raise PingError(400, "custom_ping_invalid_fields")
    if not create and (not data or data.keys() - (fields | {"enabled"})):
        raise PingError(400, "custom_ping_invalid_fields")
    if "name" in data and not valid_name(data["name"]):
        raise PingError(400, "custom_ping_invalid_name")
    if "group" in data and (not isinstance(data["group"], str) or data["group"] not in GROUPS):
        raise PingError(400, "custom_ping_invalid_group")
    if "enabled" in data and type(data["enabled"]) is not bool:
        raise PingError(400, "custom_ping_invalid_enabled")
    if "ip" in data:
        try:
            data["ip"] = validate_ip(data["ip"])
        except ValueError as exc:
            raise PingError(400, "custom_ping_invalid_ipv4") from exc
    return data


class TargetInventory:
    def __init__(self, prom_url: str, allowed: str, player_subnets: str, timeout: float):
        self.prom_url = prom_url.rstrip("/")
        self.allowed = allowed
        self.player_subnets = player_subnets
        self.timeout = min(timeout, 4)

    def active(self) -> list[dict]:
        try:
            with urlopen(f"{self.prom_url}/api/v1/targets?state=active", timeout=self.timeout) as response:
                # read1 lets a slow sender consume at most one socket timeout beyond the deadline.
                deadline = time.monotonic() + self.timeout
                raw = bytearray()
                while time.monotonic() < deadline:
                    chunk = response.read1(min(65536, MAX_INVENTORY_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > MAX_INVENTORY_BYTES:
                        raise ValueError("oversize inventory")
                else:
                    raise ValueError("inventory deadline")
            payload = decode_json(bytes(raw))
            active = payload["data"]["activeTargets"]
            if (
                payload["status"] != "success" or not isinstance(active, list)
                or any(not isinstance(row["scrapePool"], str) for row in active)
                or not any(row["scrapePool"] == "prometheus" for row in active)
            ):
                raise ValueError("invalid or uninitialized inventory")
            return active
        except (OSError, HTTPException, TypeError, ValueError, KeyError, RecursionError) as exc:
            raise PingError(503, "custom_ping_inventory_unavailable") from exc

    def check(self, ip: str) -> None:
        address = IPv4Address(ip)
        try:
            allowed = [IPv4Network(item.strip()) for item in self.allowed.split(",") if item.strip()]
            reserved = [IPv4Network(item.strip()) for item in self.player_subnets.split(",") if item.strip()]
        except ValueError as exc:
            raise PingError(503, "custom_ping_address_policy_invalid") from exc
        if any(address in network for network in reserved):
            raise PingError(409, "custom_ping_duplicate_ip", source="player-network")
        permitted = any(
            address in network and (network.prefixlen >= 31 or address not in (network.network_address, network.broadcast_address))
            for network in allowed
        )
        if not permitted:
            raise PingError(403, "custom_ping_ip_outside_allowed_cidrs")
        for row in self.active():
            source = row["scrapePool"]
            if source not in {"player-ping", "firewall-snmp"} and not source.startswith("infra-"):
                continue
            try:
                address_label = row.get("labels", {}).get("target_ip") or row["discoveredLabels"]["__address__"]
                if not isinstance(address_label, str):
                    raise ValueError("missing IP label")
                if re.fullmatch(r"[0-9.]+:[0-9]+", address_label):
                    address_label = address_label.rsplit(":", 1)[0]
                existing = ip_address(address_label)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise PingError(503, "custom_ping_inventory_unavailable") from exc
            if address == existing:
                raise PingError(409, "custom_ping_duplicate_ip", source=source)

    def observed(self, snapshot: dict) -> dict:
        current = {row["id"]: row for row in snapshot["targets"] if row["enabled"]}
        active = [row for row in self.active() if row["scrapePool"] == JOB]
        observed = []
        for row in active:
            try:
                labels, discovered = row["labels"], row["discoveredLabels"]
                target_id, ip = labels["custom_id"], labels["target_ip"]
                revision = discovered["__meta_custom_ping_revision"]
                if not valid_id(target_id) or validate_ip(ip) != ip or not re.fullmatch(r"[1-9][0-9]{0,14}", revision):
                    raise ValueError("invalid discovery labels")
                observed.append({
                    "id": target_id, "ip": ip, "revision": int(revision),
                    "scrapeHealth": row["health"] if row["health"] in ("up", "down") else "unknown",
                })
            except (KeyError, TypeError, ValueError) as exc:
                raise PingError(503, "custom_ping_inventory_unavailable") from exc
        matched = (
            snapshot["publication"] == "published" and current and len(observed) == len(current)
            and {(row["id"], row["ip"], row["revision"]) for row in observed}
            == {(row["id"], row["ip"], row["revision"]) for row in current.values()}
        )
        return {"ok": True, "revision": snapshot["revision"], "publishedRevision": snapshot["publishedRevision"],
                "discoveredRevision": snapshot["revision"] if matched else None, "activeTargets": observed}


class CustomPingAPI:
    def __init__(
        self, store: PingStore, auth_context: auth.AuthContext, write_enabled: bool,
        inventory: TargetInventory, public_origin: str = "",
    ):
        self.store = store
        self.auth_context = auth_context
        self.write_enabled = write_enabled
        self.inventory = inventory
        self.public_origin = public_origin
        self.csrf_key = secrets.token_bytes(32)

    def start_reconciliation(self) -> None:
        def loop():
            last_error = None
            wait = threading.Event()
            while True:
                try:
                    self.store.reconcile()
                    last_error = None
                except PingError as exc:
                    if exc.code != last_error:
                        LOG.warning("custom_ping reconciliation_failed code=%s", exc.code)
                    last_error = exc.code
                wait.wait(10)

        threading.Thread(target=loop, name="custom-ping-discovery", daemon=True).start()

    def _credentials(self, handler) -> tuple[str, str]:
        context = self.auth_context
        if not context.enabled:
            raise PingError(503, "custom_ping_auth_disabled")
        token = auth.parse_cookies(header(handler, "Cookie")).get(context.cookie_name, "")
        session = context.sessions.get(token)
        if not session or session["expires"] <= time.time():
            raise PingError(401, "custom_ping_auth_required")
        actor = session["username"]
        if not context.auth_path.is_file():
            raise PingError(503, "custom_ping_auth_unavailable")
        if actor != auth.read_auth_store(context)["username"]:
            raise PingError(403, "custom_ping_admin_required")
        if not self.write_enabled:
            raise PingError(403, "custom_ping_write_disabled")
        return actor, hmac.digest(self.csrf_key, token.encode("ascii"), "sha256").hex()

    def _check_csrf(self, handler, expected: str) -> None:
        expected_origin = origin_tuple(self.public_origin or header(handler, "X-Platform-Public-Origin"))
        origin = origin_tuple(header(handler, "Origin"))
        token = header(handler, "X-CSRF-Token")
        if (
            origin is None or origin != expected_origin
            or header(handler, "Sec-Fetch-Site") not in ("", "same-origin")
            or not re.fullmatch(r"[0-9a-f]{64}", token)
            or not hmac.compare_digest(token, expected)
        ):
            raise PingError(403, "custom_ping_csrf_rejected")

    def dispatch(self, handler, path: str) -> bool:
        if path != BASE and not path.startswith(BASE + "/"):
            return False
        action = handler.command
        actor = None
        target_id = None
        changes = {}
        try:
            if action == "GET":
                if path == BASE:
                    handler._send_json(self.store.list_targets())
                elif path == BASE + "/discovery":
                    handler._send_json(self.inventory.observed(self.store.list_targets()))
                elif path == BASE + "/csrf":
                    actor, csrf = self._credentials(handler)
                    handler._send_json({"ok": True, "csrfToken": csrf})
                else:
                    raise PingError(404, "custom_ping_not_found")
                return True

            actor, csrf = self._credentials(handler)
            self._check_csrf(handler, csrf)
            key = header(handler, "Idempotency-Key")
            if not valid_id(key):
                raise PingError(400, "custom_ping_idempotency_key_required")
            expected = None
            if action == "POST" and path == BASE:
                if header(handler, "If-Match"):
                    raise PingError(400, "custom_ping_unexpected_revision")
                changes = validate_changes(read_body(handler), create=True)
            elif action in ("PATCH", "DELETE") and valid_id(path.removeprefix(BASE + "/")):
                target_id = path.removeprefix(BASE + "/")
                match = re.fullmatch(r'"([1-9][0-9]{0,14})"', header(handler, "If-Match"))
                if not match:
                    raise PingError(428, "custom_ping_revision_required")
                expected = int(match[1])
                if action == "PATCH":
                    changes = validate_changes(read_body(handler), create=False)
                else:
                    if header(handler, "Transfer-Encoding") or header(handler, "Content-Length") not in ("", "0"):
                        raise PingError(400, "custom_ping_unexpected_body")
                    changes = {}
            else:
                raise PingError(404, "custom_ping_not_found")
            status, result = self.store.change(action, target_id, expected, changes, actor, key, self.inventory.check)
            headers = {"ETag": f'"{result["target"]["revision"]}"'} if result["target"] else None
            handler._send_json(result, status, headers)
        except auth.AuthError as exc:
            self._reject(handler, action, actor, target_id, None, PingError(exc.status, "custom_ping_auth_unavailable"))
        except PingError as exc:
            self._reject(handler, action, actor, target_id, changes.get("ip"), exc)
        except (BrokenPipeError, ConnectionResetError):
            return True
        except Exception as exc:
            LOG.error("custom_ping internal_error type=%s", type(exc).__name__)
            self._reject(handler, action, actor, target_id, changes.get("ip"), PingError(500, "custom_ping_internal_error"))
        return True

    @staticmethod
    def _reject(handler, method: str, actor: str | None, target_id: str | None, ip: str | None, exc: PingError) -> None:
        if method != "GET":
            LOG.warning("custom_ping rejected time=%s action=%s actor=%r id=%s ip=%s reason=%s",
                        timestamp(), method, actor, target_id, ip, exc.code)
        handler._send_json({"ok": False, "error": exc.code, **exc.detail}, exc.status)
