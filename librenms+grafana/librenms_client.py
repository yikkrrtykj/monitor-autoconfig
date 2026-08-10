#!/usr/bin/env python3
"""Small read-only LibreNMS API client shared by repository collectors.

The client deliberately uses only the Python standard library.  It owns API
authentication, bounded retries, response-envelope validation, device lookup,
and the small amount of type normalization needed by collectors.  Business
rules such as acceptable FDB age remain in the caller.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import time
from collections.abc import Iterable, Mapping
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


DEFAULT_BASE_URL = "http://librenms:8000"
DEFAULT_TOKEN_FILE = "/librenms-data/librenms-api-token"
DEFAULT_TIMEOUT = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 0.2
RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})


class LibreNMSError(RuntimeError):
    """Base class for safe-to-log LibreNMS client failures."""


class LibreNMSUnavailable(LibreNMSError):
    """LibreNMS could not be reached after the bounded retry budget."""


class LibreNMSAPIError(LibreNMSError):
    """LibreNMS returned a non-retryable HTTP or API-level failure."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LibreNMSInvalidResponse(LibreNMSError):
    """LibreNMS returned content that is not a usable JSON response."""


def _positive_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _normalise_rows(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """Return one predictable list for LibreNMS' list/object/null variants."""
    value = payload.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        if not value:
            return []
        # Some releases key rows by an internal id, while other endpoints
        # return a single row object.  A mapping whose every value is another
        # mapping is the keyed-list shape.
        if all(isinstance(item, Mapping) for item in value.values()):
            return [dict(item) for item in value.values()]
        return [dict(value)]
    return []


def _normalise_device(device: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(device)
    hostname = str(device.get("hostname") or "").strip()
    ip = str(device.get("ip") or "").strip()
    if not ip and re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", hostname):
        ip = hostname
    result.update({
        "device_id": device.get("device_id"),
        "hostname": hostname,
        "ip": ip,
        "sysName": str(device.get("sysName") or "").strip(),
    })
    return result


def parse_librenms_timestamp(value: object) -> datetime | None:
    """Parse common LibreNMS timestamps as timezone-aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) or re.fullmatch(r"-?\d+(?:\.\d+)?", str(value).strip()):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_seconds(timestamp: object, now: object = None) -> float | None:
    """Return timestamp age without imposing a collector-specific threshold."""
    parsed = parse_librenms_timestamp(timestamp)
    if parsed is None:
        return None
    current = parse_librenms_timestamp(now) if now is not None else datetime.now(timezone.utc)
    if current is None:
        return None
    return (current - parsed).total_seconds()


def is_fresh(timestamp: object, max_age: object, now: object = None) -> bool:
    """Check a caller-provided freshness limit; no API-wide age is assumed."""
    age = age_seconds(timestamp, now=now)
    try:
        limit = float(max_age)
    except (TypeError, ValueError):
        return False
    return age is not None and limit >= 0 and 0 <= age <= limit


class LibreNMSClient:
    """Bounded, read-only client for the LibreNMS v0 API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        token_file: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
    ):
        self.base_url = str(
            os.environ.get("LIBRENMS_URL", DEFAULT_BASE_URL) if base_url is None else base_url
        ).strip().rstrip("/")
        self.token_file = str(
            os.environ.get("LIBRENMS_TOKEN_FILE", DEFAULT_TOKEN_FILE)
            if token_file is None else token_file
        ).strip()
        self.token = self._resolve_token(token)
        self.timeout = _positive_float(
            os.environ.get("LIBRENMS_API_TIMEOUT", DEFAULT_TIMEOUT) if timeout is None else timeout,
            DEFAULT_TIMEOUT,
        )
        self.max_attempts = _positive_int(
            os.environ.get("LIBRENMS_API_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
            DEFAULT_MAX_ATTEMPTS,
        )
        self.retry_delay = _positive_float(
            os.environ.get("LIBRENMS_API_RETRY_DELAY", DEFAULT_RETRY_DELAY),
            DEFAULT_RETRY_DELAY,
        )
        self._opener = urlrequest.urlopen
        self._sleep = time.sleep
        self._devices_cache: list[dict[str, Any]] | None = None

    def _resolve_token(self, explicit_token: str | None) -> str:
        token = str(explicit_token or "").strip()
        if token:
            return token
        if self.token_file:
            try:
                token = Path(self.token_file).read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
            if token:
                return token
        return os.environ.get("LIBRENMS_API_TOKEN", "").strip()

    def clear_cache(self) -> None:
        self._devices_cache = None

    def _build_url(self, path: str, params: Mapping[str, Any] | Iterable[tuple[str, Any]] | None) -> str:
        if not self.base_url:
            raise LibreNMSUnavailable("LibreNMS URL is not configured")
        base = urlparse.urlsplit(self.base_url)
        if base.scheme not in ("http", "https") or not base.netloc:
            raise LibreNMSUnavailable("LibreNMS URL is invalid")
        split = urlparse.urlsplit(str(path or ""))
        if split.scheme or split.netloc:
            raise LibreNMSAPIError("LibreNMS API path must be relative")
        url = urlparse.urljoin(f"{self.base_url}/", split.path.lstrip("/"))
        query = list(urlparse.parse_qsl(split.query, keep_blank_values=True))
        if params:
            items = params.items() if isinstance(params, Mapping) else params
            query.extend((str(key), value) for key, value in items if value is not None)
        encoded_query = urlparse.urlencode(query, doseq=True)
        target = urlparse.urlsplit(url)
        return urlparse.urlunsplit((target.scheme, target.netloc, target.path, encoded_query, split.fragment))

    def _retry_wait(self, attempt: int) -> None:
        if self.retry_delay > 0:
            self._sleep(self.retry_delay * (attempt + 1))

    def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """GET and validate a LibreNMS JSON response without leaking secrets."""
        if not self.token:
            raise LibreNMSUnavailable("LibreNMS API token is not configured")
        url = self._build_url(path, params)
        request = urlrequest.Request(
            url,
            headers={"Accept": "application/json", "X-Auth-Token": self.token},
        )
        raw = b""
        for attempt in range(self.max_attempts):
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    status_value = getattr(response, "status", None)
                    if status_value is None:
                        getcode = getattr(response, "getcode", None)
                        status_value = getcode() if getcode else 200
                    status = int(status_value or 200)
                    if status >= 400:
                        if status in RETRYABLE_HTTP_STATUS:
                            if attempt + 1 < self.max_attempts:
                                self._retry_wait(attempt)
                                continue
                            raise LibreNMSUnavailable(
                                f"LibreNMS is unavailable after HTTP {status}"
                            )
                        raise LibreNMSAPIError(
                            f"LibreNMS request failed with HTTP {status}", status_code=status
                        )
                    raw = response.read()
                    break
            except urlerror.HTTPError as exc:
                status = int(exc.code)
                if status in RETRYABLE_HTTP_STATUS:
                    if attempt + 1 < self.max_attempts:
                        self._retry_wait(attempt)
                        continue
                    raise LibreNMSUnavailable(
                        f"LibreNMS is unavailable after HTTP {status}"
                    ) from None
                raise LibreNMSAPIError(
                    f"LibreNMS request failed with HTTP {status}", status_code=status
                ) from None
            except (socket.timeout, TimeoutError, ConnectionResetError, urlerror.URLError, OSError):
                if attempt + 1 < self.max_attempts:
                    self._retry_wait(attempt)
                    continue
                raise LibreNMSUnavailable(
                    f"LibreNMS is unavailable after {self.max_attempts} attempts"
                ) from None
        try:
            payload = json.loads(raw.decode("utf-8-sig", errors="strict") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LibreNMSInvalidResponse("LibreNMS returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise LibreNMSInvalidResponse("LibreNMS returned a non-object JSON response")
        status = payload.get("status")
        if status is not None and str(status).strip().lower() != "ok":
            raise LibreNMSAPIError("LibreNMS API reported a failure")
        return payload

    def list_devices(self) -> list[dict[str, Any]]:
        if self._devices_cache is None:
            payload = self.get_json("/api/v0/devices")
            self._devices_cache = [
                _normalise_device(device)
                for device in _normalise_rows(payload, "devices")
            ]
        return [dict(device) for device in self._devices_cache]

    def resolve_device(self, identifier: object) -> dict[str, Any]:
        if isinstance(identifier, Mapping):
            return _normalise_device(identifier)
        raw = str(identifier or "").strip()
        folded = raw.casefold()
        if not raw:
            raise LibreNMSAPIError("LibreNMS device identifier is empty")
        devices = self.list_devices()
        for device in devices:
            if str(device.get("device_id") or "").strip() == raw:
                return device
        for device in devices:
            if any(
                str(device.get(field) or "").strip().casefold() == folded
                for field in ("hostname", "ip", "sysName")
            ):
                return device
        raise LibreNMSAPIError("LibreNMS device was not found")

    def _device_ref(self, device: object) -> str:
        resolved = self.resolve_device(device)
        ref = (
            resolved.get("device_id")
            or resolved.get("hostname")
            or resolved.get("ip")
            or resolved.get("sysName")
        )
        if ref is None or str(ref).strip() == "":
            raise LibreNMSAPIError("LibreNMS device has no usable identifier")
        return urlparse.quote(str(ref), safe="")

    def get_device_ports(
        self,
        device: object,
        columns: str | Iterable[str] | None = None,
        with_vlans: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if columns:
            params["columns"] = columns if isinstance(columns, str) else ",".join(columns)
        if with_vlans:
            params["with"] = "vlans"
        payload = self.get_json(f"/api/v0/devices/{self._device_ref(device)}/ports", params)
        return _normalise_rows(payload, "ports")

    def get_device_ip_addresses(self, device: object) -> list[dict[str, Any]]:
        payload = self.get_json(f"/api/v0/devices/{self._device_ref(device)}/ip")
        return _normalise_rows(payload, "addresses")

    def get_device_fdb(self, device: object) -> list[dict[str, Any]]:
        payload = self.get_json(f"/api/v0/devices/{self._device_ref(device)}/fdb")
        return _normalise_rows(payload, "ports_fdb")

    def get_device_links(self, device: object) -> list[dict[str, Any]]:
        payload = self.get_json(f"/api/v0/devices/{self._device_ref(device)}/links")
        return _normalise_rows(payload, "links")

    def get_device_port_stack(
        self,
        device: object,
        valid_mappings: bool = True,
    ) -> list[dict[str, Any]]:
        params = {"valid_mappings": "1"} if valid_mappings else None
        payload = self.get_json(f"/api/v0/devices/{self._device_ref(device)}/port_stack", params)
        return _normalise_rows(payload, "mappings")

    def get_device_arp(self, device: object) -> list[dict[str, Any]]:
        resolved = self.resolve_device(device)
        ref = resolved.get("device_id") or resolved.get("hostname") or resolved.get("ip")
        if ref is None or str(ref).strip() == "":
            raise LibreNMSAPIError("LibreNMS device has no usable identifier")
        payload = self.get_json("/api/v0/resources/ip/arp/all", {"device": str(ref)})
        return _normalise_rows(payload, "arp")
