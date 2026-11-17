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


def validate_target(target: dict, revision: int) -> None:
    if (
        not isinstance(target, dict) or target.keys() != TARGET_KEYS
        or not valid_id(target["id"]) or not valid_name(target["name"])
        or validate_ip(target["ip"]) != target["ip"]
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
        or type(manifest["schema"]) is not int or manifest["schema"] != 1
        or type(manifest["revision"]) is not int or not 0 <= manifest["revision"] < 10**15
        or not isinstance(manifest["targets"], list) or len(manifest["targets"]) > MAX_TARGETS
        or not isinstance(manifest["receipts"], list) or len(manifest["receipts"]) > MAX_RECEIPTS
    ):
        raise ValueError("invalid custom Ping manifest")
    targets, receipts, revision = manifest["targets"], manifest["receipts"], manifest["revision"]
    for target in targets:
        validate_target(target, revision)
    if (
        len({row["id"] for row in targets}) != len(targets)
        or len({row["ip"] for row in targets}) != len(targets)
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


def discovery_bytes(manifest: dict) -> bytes:
    return encode_json([
        {
            "targets": [row["ip"]],
            "labels": {
                "custom_id": row["id"],
                "display_name": row["name"],
                "custom_group": row["group"],
                "__meta_custom_ping_revision": str(row["revision"]),
            },
        }
        for row in manifest["targets"] if row["enabled"]
    ])


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

    def _status(self, manifest: dict) -> dict:
        matched = self._publication_matches(discovery_bytes(manifest))
        return {
            "revision": manifest["revision"],
            "publishedRevision": manifest["revision"] if matched else None,
            "publication": "published" if matched else "pending",
        }

    def list_targets(self) -> dict:
        with self._locked():
            manifest, _ = self._read()
            return {"ok": True, "targets": manifest["targets"], **self._status(manifest)}

    def reconcile(self) -> None:
        with self._locked():
            manifest, _ = self._read()
            expected = discovery_bytes(manifest)
            try:
                sync_directory(self.manifest.parent)
                self.discovery.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                if not self._publication_matches(expected):
                    with staged_file(self.discovery, expected, 0o644) as temporary:
                        os.replace(temporary, self.discovery)
                sync_directory(self.discovery.parent)
            except OSError as exc:
                raise PingError(503, "custom_ping_publication_failed") from exc

    def _commit(self, manifest: dict, previous: bytes | None) -> bool:
        # Prepare all writes on their own filesystems before committing the manifest.
        # Its revision and receipts are the delivery intent, even across a crash.
        encoded = encode_json(manifest)
        if len(encoded) > MAX_BYTES:
            raise PingError(HTTPStatus.CONFLICT, "custom_ping_storage_limit")
        with ExitStack() as staged:
            try:
                self.discovery.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                discovery = staged.enter_context(staged_file(self.discovery, discovery_bytes(manifest), 0o644))
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
                os.replace(discovery, self.discovery)
                sync_directory(self.discovery.parent)
            except OSError:
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
            remaining = [row for row in targets if row is not before]
            if after:
                if any(row["ip"] == after["ip"] for row in remaining):
                    raise PingError(409, "custom_ping_duplicate_ip", source=JOB)
                if len(remaining) >= MAX_TARGETS or sum(row["enabled"] for row in remaining) + after["enabled"] > MAX_ACTIVE:
                    raise PingError(409, "custom_ping_capacity")
                if not before or after["ip"] != before["ip"] or (after["enabled"] and not before["enabled"]):
                    check_address(after["ip"])
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
                "schema": 1, "revision": revision, "targets": candidate_targets,
                "receipts": (manifest["receipts"] + [receipt])[-MAX_RECEIPTS:],
            }
            validate_manifest(candidate)
            published = self._commit(candidate, previous)
            LOG.info("custom_ping committed %s", encode_json({
                "actor": actor, "action": action, "time": now, "id": (after or before)["id"],
                "before_ip": before["ip"] if before else None, "after_ip": after["ip"] if after else None,
                "revision": revision, "published": published,
            }).decode("utf-8").rstrip())
            return self._write_result(receipt, {
                "revision": revision, "publishedRevision": revision if published else None,
                "publication": "published" if published else "pending",
            })

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
