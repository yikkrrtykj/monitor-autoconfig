"""自定义 Ping 权威清单和独立的 Prometheus file_sd 发布。"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
from http import HTTPStatus
from ipaddress import IPv4Address, IPv4Network
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Callable
import unicodedata
from uuid import uuid4

from .custom_ping_dns import hostname, private_cidrs

if os.name != "nt":
    import fcntl

JOB = "custom-ping"
DISCOVERY_PATH = Path("/etc/prometheus/targets/custom/custom_targets.json")
GROUPS = frozenset({"Remote", "Server", "Other"})
MAX_ACTIVE = 100
MAX_TARGETS = 300
MAX_RECEIPTS = 512
MAX_BYTES = 1024 * 1024
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
BLOCKED = tuple(map(IPv4Network, ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/3")))
TARGET_KEYS = {"id", "name", "ip", "group", "enabled", "createdAt", "updatedAt", "revision"}
DNS_TARGET_KEYS = (TARGET_KEYS - {"ip"}) | {"kind", "address", "privateCidrs", "endpointRevision"}
LOG = logging.getLogger("platform_api.custom_ping")


class PingError(Exception):
    def __init__(self, status: int, code: str, **detail):
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail


def decode_json(raw: bytes):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON key")
        return result

    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject_constant)


def encode_json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def validate_ip(value: str) -> str:
    if not isinstance(value, str) or len(value) > 15:
        raise ValueError("invalid IPv4")
    ip = IPv4Address(value)
    if any(ip in network for network in BLOCKED):
        raise ValueError("reserved IPv4")
    return str(ip)


def valid_name(value) -> bool:
    return (
        isinstance(value, str) and 1 <= len(value) <= 80 and value == value.strip()
        and len(value.encode("utf-8", errors="surrogatepass")) <= 240
        and all(unicodedata.category(ch)[0] != "C" for ch in value)
    )


def valid_id(value) -> bool:
    return isinstance(value, str) and UUID4.fullmatch(value) is not None


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def valid_time(value) -> bool:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value,
    ):
        return False
    datetime.fromisoformat(value)
    return True


def target_identity(row: dict) -> tuple[str, str]:
    return ("dns", row["address"]) if row.get("kind") == "dns" else ("ipv4", row["ip"])


def validate_target(target: dict, revision: int) -> None:
    dns = isinstance(target, dict) and target.get("kind") == "dns"
    if dns:
        if (target.keys() != DNS_TARGET_KEYS or hostname(target["address"]) != target["address"]
                or private_cidrs(target["privateCidrs"]) != target["privateCidrs"]
                or type(target["endpointRevision"]) is not int
                or not 1 <= target["endpointRevision"] <= target["revision"]):
            raise ValueError("invalid DNS target")
    if (
        not isinstance(target, dict) or target.keys() != (DNS_TARGET_KEYS if dns else TARGET_KEYS)
        or not valid_id(target["id"]) or not valid_name(target["name"])
        or not dns and validate_ip(target["ip"]) != target["ip"]
        or not isinstance(target["group"], str) or target["group"] not in GROUPS
        or type(target["enabled"]) is not bool
        or not valid_time(target["createdAt"]) or not valid_time(target["updatedAt"])
        or type(target["revision"]) is not int or not 1 <= target["revision"] <= revision
    ):
        raise ValueError("invalid custom Ping target")


def validate_receipt(entry: dict, revision: int) -> None:
    if (
        not isinstance(entry, dict)
        or entry.keys() != {"key", "digest", "actor", "action", "time", "before", "after", "revision"}
        or not valid_id(entry["key"]) or not valid_time(entry["time"])
        or not isinstance(entry["actor"], str) or not 1 <= len(entry["actor"]) <= 256
        or entry["action"] not in ("POST", "PATCH", "DELETE")
        or not isinstance(entry["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["digest"])
        or type(entry["revision"]) is not int or not 1 <= entry["revision"] <= revision
    ):
        raise ValueError("invalid custom Ping receipt")
    before, after = entry["before"], entry["after"]
    if (before is None) != (entry["action"] == "POST") or (after is None) != (entry["action"] == "DELETE"):
        raise ValueError("invalid custom Ping receipt action")
    if before is not None:
        validate_target(before, entry["revision"] - 1)
    if after is not None:
        validate_target(after, entry["revision"])
        if after["revision"] != entry["revision"] or (before and after["id"] != before["id"]):
            raise ValueError("invalid custom Ping receipt target")


def validate_manifest(manifest: dict) -> None:
    if (
        not isinstance(manifest, dict) or manifest.keys() != {"schema", "revision", "targets", "receipts"}
        or type(manifest["schema"]) is not int or manifest["schema"] not in (1, 2)
        or type(manifest["revision"]) is not int or not 0 <= manifest["revision"] < 10**15
        or not isinstance(manifest["targets"], list) or len(manifest["targets"]) > MAX_TARGETS
        or not isinstance(manifest["receipts"], list) or len(manifest["receipts"]) > MAX_RECEIPTS
    ):
        raise ValueError("invalid custom Ping manifest")
    targets, receipts, revision = manifest["targets"], manifest["receipts"], manifest["revision"]
    for target in targets:
        validate_target(target, revision)
        if manifest["schema"] == 1 and target.get("kind") == "dns":
            raise ValueError("DNS target requires Schema 2")
    if (
        len({row["id"] for row in targets}) != len(targets)
        or len({target_identity(row) for row in targets}) != len(targets)
        or sum(row["enabled"] for row in targets) > MAX_ACTIVE
    ):
        raise ValueError("invalid custom Ping inventory")
    if len(receipts) != min(revision, MAX_RECEIPTS):
        raise ValueError("incomplete custom Ping journal")
    for expected, entry in enumerate(receipts, revision - len(receipts) + 1):
        validate_receipt(entry, revision)
        if entry["revision"] != expected:
            raise ValueError("invalid custom Ping journal order")
    if len({(entry["actor"], entry["key"]) for entry in receipts}) != len(receipts):
        raise ValueError("duplicate custom Ping receipt")


def discovery_entry(row: dict) -> dict:
    labels = {"custom_id": row["id"], "display_name": row["name"], "custom_group": row["group"],
              "__meta_custom_ping_revision": str(row["revision"])}
    if "resolutionGeneration" in row:
        labels["__meta_custom_ping_resolution_generation"] = str(row["resolutionGeneration"])
    return {"targets": [row["ip"]], "labels": labels}


def discovery_bytes(manifest: dict) -> bytes:
    return encode_json([discovery_entry(row) for row in manifest["targets"] if row["enabled"]])


def read_bounded(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_BYTES:
        raise ValueError("custom Ping file too large")
    return raw


@contextmanager
def staged_file(path: Path, data: bytes, mode: int):
    with tempfile.NamedTemporaryFile(prefix=".custom-ping-", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            if os.name != "nt":
                os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            yield temporary
        finally:
            handle.close()
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                LOG.warning("custom_ping staging_cleanup_failed")


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class PingStore:
    def __init__(self, state_dir: Path, discovery_path: Path = DISCOVERY_PATH):
        self.manifest = state_dir / "custom-ping.json"
        self.backup = state_dir / "custom-ping.previous.json"
        self.lockfile = state_dir / "custom-ping.lock"
        self.discovery = discovery_path
        self.lock = threading.Lock()
        # A refresh unable to enter the store must still signal loss of evidence.
        # This lock protects only the mailbox. It is never held during store I/O.
        self._failed_dns_lock = threading.Lock()
        self._failed_dns: dict[tuple, tuple[float, float]] = {}
        from .custom_ping_runtime import DNSState
        self.dns = DNSState(state_dir / "custom-ping-dns.json")
        self.dns_enabled = True

    @contextmanager
    def _locked(self):
        if not self.lock.acquire(timeout=6):
            raise PingError(503, "custom_ping_busy")
        try:
            if os.name == "nt":
                # Windows is supported for local tests. Compose writers run on Linux.
                yield
                return
            fd = os.open(self.lockfile, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise PingError(503, "custom_ping_busy") from exc
                yield
            finally:
                os.close(fd)
        except OSError as exc:
            raise PingError(503, "custom_ping_storage_unavailable") from exc
        finally:
            self.lock.release()

    def _read(self) -> tuple[dict, bytes | None]:
        try:
            raw = read_bounded(self.manifest)
            if raw is None:
                if self.backup.exists() or read_bounded(self.discovery) not in (None, b"[]\n"):
                    raise ValueError("missing custom Ping manifest with previous data")
                return {"schema": 1, "revision": 0, "targets": [], "receipts": []}, None
            manifest = decode_json(raw)
            validate_manifest(manifest)
            return manifest, raw
        except (OSError, ValueError, RecursionError) as exc:
            raise PingError(503, "custom_ping_manifest_unavailable") from exc

    def _publication_matches(self, data: bytes) -> bool:
        try:
            return read_bounded(self.discovery) == data
        except (ValueError, OSError):
            return False

    def _projection(self, manifest: dict, state: dict | None = None) -> dict:
        if manifest["schema"] == 1:
            return manifest
        if state is None:
            state = self._dns_state(manifest)
        rows = []
        now = self.dns.clock()
        for row in manifest["targets"]:
            if row.get("kind") != "dns":
                rows.append(row)
                continue
            view = self.dns.view(row, state, now)
            if self.dns_enabled and row["enabled"] and view["dnsStatus"] == "DNS_OK":
                rows.append({**row, "ip": view["resolvedIp"],
                             "resolutionGeneration": view["resolutionGeneration"]})
        return {**manifest, "targets": rows}

    def _status(self, manifest: dict, state: dict | None = None) -> dict:
        matched = self._publication_matches(discovery_bytes(self._projection(manifest, state)))
        return {
            "revision": manifest["revision"],
            "publishedRevision": manifest["revision"] if matched else None,
            "publication": "published" if matched else "pending",
        }

    def list_targets(self) -> dict:
        with self._locked():
            manifest, _ = self._read()
            targets = manifest["targets"]
            state = None
            if manifest["schema"] == 2:
                state = self._dns_state(manifest)
                now = self.dns.clock()
                targets = [{**row, **self.dns.view(row, state, now)} if row.get("kind") == "dns" else row
                           for row in targets]
            result = {"ok": True, "targets": targets, **self._status(manifest, state)}
            if manifest["schema"] == 2:
                result["manifestRevision"] = manifest["revision"]
                result["dnsEnabled"] = self.dns_enabled
            return result

    def _publish(self, manifest: dict, state: dict | None = None) -> None:
        if state is None and manifest["schema"] == 2:
            state = self._dns_state(manifest)
        expected = discovery_bytes(self._projection(manifest, state))
        try:
            self.discovery.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            while not self._publication_matches(expected):
                with staged_file(self.discovery, expected, 0o644) as temporary:
                    # fsync and staging can outlive a short lease. Under the lock
                    # only expiration can change this projection between checks.
                    current = discovery_bytes(self._projection(manifest, state))
                    if current == expected:
                        os.replace(temporary, self.discovery)
                        break
                    expected = current
            sync_directory(self.discovery.parent)
        except OSError as exc:
            raise PingError(503, "custom_ping_publication_failed") from exc

    def _withdraw_dns(self, manifest, state):
        """During a state-volume fault, only remove invalid DNS entries from file_sd."""
        try:
            existing = decode_json(read_bounded(self.discovery) or b"[]")
            if not isinstance(existing, list):
                raise ValueError("invalid custom Ping discovery")
            live = {(row["id"], str(row["resolutionGeneration"])): [row["ip"]]
                    for row in self._projection(manifest, state)["targets"] if row.get("kind") == "dns"}
            kept = []
            for entry in existing:
                labels = entry["labels"]
                generation = labels.get("__meta_custom_ping_resolution_generation")
                if generation is None or live.get((labels["custom_id"], generation)) == entry["targets"]:
                    kept.append(entry)
            if len(kept) != len(existing):
                with staged_file(self.discovery, encode_json(kept), 0o644) as temporary:
                    os.replace(temporary, self.discovery)
                sync_directory(self.discovery.parent)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise PingError(503, "custom_ping_publication_failed") from exc

    def reconcile(self) -> None:
        with self._locked():
            manifest, _ = self._read()
            state = None
            if manifest["schema"] == 2:
                self.dns.prune(manifest)
                state = self._dns_state(manifest)
            # Keep the manifest durable before publishing its projection, then
            # withdraw before any runtime history maintenance writes.
            try:
                sync_directory(self.manifest.parent)
            except OSError as exc:
                if state is not None:
                    self._withdraw_dns(manifest, state)
                raise PingError(503, "custom_ping_publication_failed") from exc
            self._publish(manifest, state)
            if state is not None:
                now = self.dns.clock()
                updated = self.dns.maintain(manifest, state, now)
                if not self.dns_enabled:
                    self.dns.leases.clear()
                    for saved in updated["rows"].values():
                        if saved["status"] == "DNS_OK":
                            updated["generation"] += 1
                            saved.update(status="DNS_STALE", error="paused", expiresAt=now,
                                         nextRefreshAt=now, generation=updated["generation"])
                    for segment in updated["segments"]:
                        if segment["end"] is None:
                            segment["end"] = max(segment["start"], now)
                if updated != state:
                    self.dns.write(updated)

    def _commit(self, manifest: dict, previous: bytes | None) -> bool:
        # Prepare all writes on their own filesystems before committing the manifest.
        # Its revision and receipts are the delivery intent, even across a crash.
        if manifest["schema"] == 2 and not self.dns.path.exists():
            # Bootstrap state before the first Schema 2 intent becomes durable.
            self.dns.write(self.dns.read())
        encoded = encode_json(manifest)
        if len(encoded) > MAX_BYTES:
            raise PingError(HTTPStatus.CONFLICT, "custom_ping_storage_limit")
        with ExitStack() as staged:
            try:
                self.discovery.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                # A candidate manifest may still fail to commit. Only consume
                # lock failures against the version that actually reached disk.
                state = self.dns.read(required=True) if manifest["schema"] == 2 else None
                projection = discovery_bytes(self._projection(manifest, state))
                discovery = staged.enter_context(staged_file(self.discovery, projection, 0o644))
                current = staged.enter_context(staged_file(self.manifest, encoded, 0o600))
                if previous is not None:
                    backup = staged.enter_context(staged_file(self.backup, previous, 0o600))
                    os.replace(backup, self.backup)
                    sync_directory(self.backup.parent)
                os.replace(current, self.manifest)
            except OSError as exc:
                raise PingError(503, "custom_ping_commit_failed") from exc
            try:
                sync_directory(self.manifest.parent)
                if projection == discovery_bytes(self._projection(manifest)):
                    os.replace(discovery, self.discovery)
                    sync_directory(self.discovery.parent)
                else:
                    self._publish(manifest)
            except (OSError, PingError):
                LOG.warning("custom_ping publication_pending revision=%d", manifest["revision"])
                return False
        return True

    def change(
        self, action: str, target_id: str | None, expected: int | None, changes: dict,
        actor: str, key: str, check_address: Callable[[str], None],
    ) -> tuple[int, dict]:
        digest = hashlib.sha256(encode_json([action, target_id, expected, changes])).hexdigest()
        with self._locked():
            manifest, previous = self._read()
            for receipt in reversed(manifest["receipts"]):
                if (receipt["actor"], receipt["key"]) == (actor, key):
                    if receipt["digest"] != digest:
                        raise PingError(409, "custom_ping_idempotency_conflict")
                    return self._write_result(receipt, self._status(manifest), replayed=True)

            if manifest["schema"] == 2:
                self._dns_state(manifest)
            targets = manifest["targets"]
            before = next((row for row in targets if row["id"] == target_id), None)
            if action != "POST":
                if before is None:
                    raise PingError(404, "custom_ping_not_found")
                if before["revision"] != expected:
                    raise PingError(412, "custom_ping_revision_conflict", currentRevision=before["revision"])
            revision = manifest["revision"] + 1
            now = timestamp()
            after = None
            if action != "DELETE":
                base = before or {"id": str(uuid4()), "createdAt": now, "enabled": True}
                after = {**base, **changes, "updatedAt": now, "revision": revision}
                if changes.get("kind") == "dns":
                    after.pop("ip", None)
                    if not before or before.get("address") != after["address"]:
                        after["privateCidrs"] = changes.get("privateCidrs", [])
                    after.setdefault("privateCidrs", [])
                elif "ip" in changes:
                    after = {k: v for k, v in after.items() if k in TARGET_KEYS}
                if "privateCidrs" in changes and after.get("kind") != "dns":
                    raise PingError(400, "custom_ping_invalid_fields")
                if after.get("kind") == "dns":
                    if (not before or before.get("kind") != "dns"
                            or any(after[k] != before[k] for k in ("address", "privateCidrs", "enabled"))):
                        after["endpointRevision"] = revision
            remaining = [row for row in targets if row is not before]
            if after:
                if any(target_identity(row) == target_identity(after) for row in remaining):
                    raise PingError(409, "custom_ping_duplicate_ip", source=JOB)
                if len(remaining) >= MAX_TARGETS or sum(row["enabled"] for row in remaining) + after["enabled"] > MAX_ACTIVE:
                    raise PingError(409, "custom_ping_capacity")
                if after.get("kind") != "dns" and (
                    not before or after["ip"] != before.get("ip") or (after["enabled"] and not before["enabled"])
                ):
                    check_address(after["ip"])
                    if manifest["schema"] == 2:
                        projection = self._projection(manifest)
                        if any(r["id"] != after["id"] and r.get("kind") == "dns" and r["ip"] == after["ip"]
                               for r in projection["targets"]):
                            raise PingError(409, "custom_ping_duplicate_ip", source=JOB)
            candidate_targets = [
                after if row is before else row for row in targets if row is not before or after is not None
            ]
            if before is None and after is not None:
                candidate_targets.append(after)
            receipt = {
                "key": key, "digest": digest, "actor": actor, "action": action, "time": now,
                "before": before, "after": after, "revision": revision,
            }
            candidate = {
                "schema": 2 if manifest["schema"] == 2 or any(r.get("kind") == "dns" for r in candidate_targets)
                else 1, "revision": revision, "targets": candidate_targets,
                "receipts": (manifest["receipts"] + [receipt])[-MAX_RECEIPTS:],
            }
            validate_manifest(candidate)
            published = self._commit(candidate, previous)
            self.dns.prune(candidate)
            LOG.info("custom_ping committed %s", encode_json({
                "actor": actor, "action": action, "time": now, "id": (after or before)["id"],
                "before_ip": before.get("ip", before.get("address")) if before else None,
                "after_ip": after.get("ip", after.get("address")) if after else None,
                "revision": revision, "published": published,
            }).decode("utf-8").rstrip())
            return self._write_result(receipt, {
                "revision": revision, "publishedRevision": revision if published else None,
                "publication": "published" if published else "pending",
            })

    @staticmethod
    def _dns_ticket_key(row, digest):
        return (row["id"], row["address"], row["endpointRevision"], row["revision"], digest)

    def _record_dns_lock_failure(self, ticket):
        key = self._dns_ticket_key(ticket, ticket["_runtimeDigest"])
        with self._failed_dns_lock:
            # Refresh has at most eight in-flight workers and drains on each
            # successful scheduler/read tick; the active-target cap is a hard limit.
            if len(self._failed_dns) < MAX_ACTIVE:
                self._failed_dns.setdefault(key, (self.dns.clock(), self.dns.monotonic()))

    def _dns_state(self, manifest):
        state = self.dns.read(required=True)
        with self._failed_dns_lock:
            failures, self._failed_dns = self._failed_dns, {}
        if failures:
            for row in manifest["targets"]:
                if row.get("kind") != "dns" or not row["enabled"]:
                    continue
                digest = self._dns_digest(row, state["rows"].get(row["id"]))
                failed_at = failures.get(self._dns_ticket_key(row, digest))
                if failed_at is not None:
                    wall, monotonic = failed_at
                    self.dns.invalidate(row, ended_at=wall, retry_at=monotonic + 30)
        return state

    def _dns_digest(self, row, saved) -> str:
        failure = self.dns.failure(row)
        payload = [saved, failure.retry_at] if failure else saved
        return hashlib.sha256(encode_json(payload)).hexdigest()

    def dns_due(self) -> list[dict]:
        if not self.dns_enabled:
            return []
        with self._locked():
            manifest, _ = self._read()
            if manifest["schema"] == 1:
                return []
            state = self._dns_state(manifest)
            now = self.dns.clock()
            due = []
            for row in manifest["targets"]:
                if row.get("kind") != "dns" or not row["enabled"]:
                    continue
                identity = row["id"]
                saved = state["rows"].get(identity)
                failure = self.dns.failure(row)
                if failure:
                    if self.dns.monotonic() < failure.retry_at:
                        continue
                elif saved and (saved["address"], saved["endpointRevision"]) == (
                        row["address"], row["endpointRevision"]):
                    lease = self.dns.leases.get(identity)
                    if saved["status"] == "DNS_OK":
                        if (lease and lease.generation == saved["generation"]
                                and saved["nextRefreshAt"] > now and lease.refresh_at > self.dns.monotonic()):
                            continue
                    elif saved["nextRefreshAt"] > now >= saved["expiresAt"]:
                        continue
                due.append({**row, "_runtimeDigest": self._dns_digest(row, saved)})
            return due

    def resolve_dns(self, ticket, answer, check_address) -> bool:
        started = self.dns.monotonic()
        answer = answer.measured_at(self.dns.clock(), started)
        with ExitStack() as stack:
            try:
                stack.enter_context(self._locked())
            except PingError:
                self._record_dns_lock_failure(ticket)
                raise
            manifest, _ = self._read()
            row = next((r for r in manifest["targets"] if r["id"] == ticket["id"]), None)
            intent = {k: v for k, v in ticket.items() if k != "_runtimeDigest"}
            if not self.dns_enabled or row != intent or not row["enabled"]:
                return False
            state = self._dns_state(manifest)
            expected_runtime = self._dns_digest(row, state["rows"].get(row["id"]))
            if ticket.get("_runtimeDigest") != expected_runtime:
                return False
            try:
                updated = self.dns.apply(manifest, state, row, answer, check_address)
            except PingError:
                # Even if replace succeeded before fsync failed, that row is
                # untrusted. Only withdraw, leaving other published entries intact.
                self.dns.invalidate(row)
                try:
                    self._withdraw_dns(manifest, state)
                except PingError:
                    LOG.warning("custom_ping dns_withdrawal_pending id=%s", row["id"])
                raise
            self._publish(manifest, updated)
            return True

    def observed_dns(self, candidates: dict, *, confirm: bool = False) -> dict:
        """Revalidate scrape identities after I/O and record their first discovery time."""
        if not candidates:
            return {}
        with self._locked():
            manifest, _ = self._read()
            if manifest["schema"] != 2 or not self.dns_enabled:
                return {}
            state = self._dns_state(manifest)
            try:
                published = decode_json(read_bounded(self.discovery) or b"[]")
                entries = {entry["labels"]["custom_id"]: entry for entry in published}
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                return {}
            now = self.dns.clock()
            views = {}
            for row in self._projection(manifest, state)["targets"]:
                identity = row["id"]
                if row.get("kind") != "dns" or (
                        row["ip"], row["revision"], row["resolutionGeneration"]) != candidates.get(identity):
                    continue
                if entries.get(identity) == discovery_entry(row):
                    views[identity] = row
            if confirm and views:
                try:
                    self.dns.confirm(state, {identity: self.dns.leases[identity] for identity in views}, now)
                except PingError as exc:
                    LOG.warning("custom_ping dns_sample_boundary_pending code=%s", exc.code)
            now = self.dns.clock()
            result = {}
            for identity, row in views.items():
                view = self.dns.view(row, state, now)
                if view["dnsStatus"] == "DNS_OK":
                    result[identity] = view
            return result

    def resolution_history(self, identity: str, cursor: int = 0) -> dict:
        with self._locked():
            manifest, _ = self._read()
            state = self._dns_state(manifest) if manifest["schema"] == 2 else self.dns.read()
            state = self.dns.maintain(manifest, state, self.dns.clock())
            segments = [s for s in state["segments"] if s["id"] == identity and s["generation"] > cursor]
            page = segments[:200]
            return {"ok": True, "segments": page, "more": len(segments) > len(page),
                    "nextCursor": page[-1]["generation"] if page else cursor}

    @staticmethod
    def _write_result(receipt: dict, status: dict, replayed: bool = False) -> tuple[int, dict]:
        code = 201 if receipt["action"] == "POST" else 200
        if status["publication"] == "pending":
            code = 202
        return code, {
            "ok": True, "target": receipt["after"],
            "id": (receipt["after"] or receipt["before"])["id"],
            "savedRevision": receipt["revision"], "replayed": replayed, **status,
        }
