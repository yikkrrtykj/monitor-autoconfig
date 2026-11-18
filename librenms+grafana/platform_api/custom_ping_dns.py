"""受限 A 记录查询、目标规范化和有界刷新调度。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from ipaddress import IPv4Address, IPv4Network
import logging
import re
import threading
import time

PRIVATE = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
MAX_ANSWERS = 64
LOG = logging.getLogger("platform_api.custom_ping")


def hostname(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 254 or value != value.strip():
        raise ValueError("invalid DNS hostname")
    name = value.removesuffix(".").encode("idna").decode("ascii").lower()
    labels = name.split(".")
    if (len(name) > 253 or len(labels) < 2 or re.fullmatch(r"[0-9.]+", name)
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels)):
        raise ValueError("invalid DNS hostname")
    return name


def probe_hostname(value: str) -> str:
    name = hostname(value)
    if name.rsplit(".", 1)[-1] in {"local", "localhost"}:
        raise ValueError("unsupported DNS suffix")
    return name


def private_cidrs(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 16 or any(not isinstance(v, str) for v in value):
        raise ValueError("invalid private CIDRs")
    networks = [IPv4Network(v) for v in value]
    if any(not any(n.subnet_of(p) for p in PRIVATE) for n in networks):
        raise ValueError("private CIDR must be within RFC1918")
    return sorted(set(map(str, networks)))


def signature(row: dict) -> tuple:
    return row["id"], row["address"], row["endpointRevision"]


@dataclass(frozen=True)
class Answer:
    ips: tuple[str, ...] = ()
    ttl: int = 0
    error: str = ""
    started_at: float | None = None
    started_monotonic: float | None = None

    def measured_at(self, wall: float, monotonic: float) -> Answer:
        if self.started_monotonic is not None:
            return self
        return replace(self, started_at=wall, started_monotonic=monotonic)


class Resolver:
    def __init__(self, nameservers=None, port: int = 53, clock=time.monotonic, wall_clock=time.time):
        self.nameservers = nameservers
        self.port = port
        self.clock = clock
        self.wall_clock = wall_clock

    def resolve(self, name: str) -> Answer:
        import dns.exception
        import dns.rdatatype
        import dns.resolver

        started = self.clock()
        wall_started = self.wall_clock()
        resolver = dns.resolver.Resolver(configure=self.nameservers is None)
        if self.nameservers is not None:
            resolver.nameservers = self.nameservers
        resolver.port = self.port
        resolver.cache = None
        ttl = 2**31 - 1
        hops = 0
        try:
            current = probe_hostname(name)
            seen = {current}
            while True:
                remaining = 2 - (self.clock() - started)
                if remaining <= 0:
                    return Answer(error="timeout")
                answer = resolver.resolve(current + ".", "A", search=False,
                                          lifetime=remaining, raise_on_no_answer=False)
                links = {}
                for rrset in answer.response.answer:
                    if rrset.rdtype == dns.rdatatype.CNAME:
                        if len(rrset) != 1:
                            return Answer(error="invalid_response")
                        links[rrset.name.to_text().lower().rstrip(".")] = (
                            rrset[0].target.to_text().lower().rstrip("."), rrset.ttl)
                while current in links:
                    target, link_ttl = links[current]
                    current = probe_hostname(target)
                    hops += 1
                    if current in seen or hops > 8:
                        return Answer(error="cname_limit")
                    seen.add(current)
                    ttl = min(ttl, link_ttl)
                if answer.rrset is not None:
                    if len(answer.rrset) > MAX_ANSWERS:
                        return Answer(error="answer_limit")
                    ips = tuple(sorted({str(IPv4Address(rr.address)) for rr in answer}, key=IPv4Address))
                    return Answer(ips, max(0, min(ttl, answer.rrset.ttl)),
                                  started_at=wall_started, started_monotonic=started)
                if not links:
                    return Answer(error="no_a")
        except dns.resolver.NXDOMAIN:
            return Answer(error="nxdomain")
        except dns.resolver.NoNameservers:
            return Answer(error="servfail")
        except dns.exception.Timeout:
            return Answer(error="timeout")
        except (dns.exception.DNSException, ValueError, OSError):
            return Answer(error="invalid_response")


class Refresh:
    """唯一调度器。网络查询和清单锁分离，提交时重新核对配置和策略。"""
    def __init__(self, store, check_policy, resolver=None):
        self.store = store
        self.check_policy = check_policy
        self.resolver = resolver or Resolver()
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="custom-ping-dns")
        self.pending = {}
        self.lock = threading.Lock()
        self.stopped = False

    def tick(self) -> None:
        with self.lock:
            if self.stopped:
                return
            for identity, future in list(self.pending.items()):
                if future.done():
                    try:
                        future.result()
                    except Exception as exc:
                        LOG.warning("custom_ping dns_refresh_failed type=%s", type(exc).__name__)
                    del self.pending[identity]
            for row in self.store.dns_due():
                if len(self.pending) >= 8:
                    break
                if row["id"] not in self.pending:
                    self.pending[row["id"]] = self.executor.submit(self._resolve, row)

    def _resolve(self, row: dict) -> None:
        try:
            started = self.store.dns.monotonic()
            wall_started = self.store.dns.clock()
            answer = self.resolver.resolve(row["address"]).measured_at(wall_started, started)
        except Exception:
            answer = Answer(error="resolver_error")
        try:
            policy = self.check_policy() if not answer.error else lambda _: None
        except Exception:
            answer, policy = Answer(error="inventory"), lambda _: None
        self.store.resolve_dns(row, answer, policy)

    def stop(self) -> None:
        with self.lock:
            self.stopped = True
        self.executor.shutdown(wait=True, cancel_futures=True)

