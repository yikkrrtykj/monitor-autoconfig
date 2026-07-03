import importlib.util
from pathlib import Path

# discover-switch-targets.py is hyphenated, so load it by path.
_spec = importlib.util.spec_from_file_location(
    "discover_switch_targets",
    Path(__file__).resolve().parent.parent / "discover-switch-targets.py",
)
disc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(disc)


def test_expand_targets_handles_ranges_names_and_cidr():
    assert disc.expand_targets("192.168.10.11-13") == [
        "192.168.10.11", "192.168.10.12", "192.168.10.13",
    ]
    # "name:" prefix is stripped and entries dedupe across the list.
    assert disc.expand_targets("SW:192.168.10.11-12,192.168.10.11") == [
        "192.168.10.11", "192.168.10.12",
    ]
    # A /30 expands to its two usable host addresses.
    assert disc.expand_targets("192.168.10.8/30") == ["192.168.10.9", "192.168.10.10"]


def test_excluded_ips_expands_named_and_ranged_entries():
    assert disc.excluded_ips("CORE:192.168.10.254", "192.168.9.1-2") == {
        "192.168.10.254", "192.168.9.1", "192.168.9.2",
    }


def test_discover_keeps_snmp_hostname_falls_back_to_ip_and_drops_offline():
    hostnames = {"192.168.10.11": "core-sw-01"}      # answers SNMP
    pingable = {"192.168.10.11", "192.168.10.12"}    # .12 answers ping only
    ips = ["192.168.10.11", "192.168.10.12", "192.168.10.13"]  # .13 is offline

    results = disc.discover(
        ips,
        community="public",
        probe_snmp=lambda ip, community, timeout=1: hostnames.get(ip, ""),
        probe_ping=lambda ip, timeout=1: ip in pingable,
        workers=4,
    )
    assert results == {
        "192.168.10.11": "core-sw-01",   # SNMP hostname wins
        "192.168.10.12": "192.168.10.12",  # ping-only -> IP placeholder, never "SW"
    }
    assert "192.168.10.13" not in results  # offline -> not added


def test_discover_skips_snmp_for_dead_hosts():
    # The expensive SNMP probe must only run for ICMP-live hosts, so a sparse
    # range does not SNMP-scan every dead address.
    snmp_calls = []

    def snmp(ip, community, timeout=1):
        snmp_calls.append(ip)
        return "sw-11" if ip == "192.168.10.11" else ""

    results = disc.discover(
        [f"192.168.10.{n}" for n in range(11, 31)],  # 20 addresses, 1 alive
        community="public",
        probe_snmp=snmp,
        probe_ping=lambda ip, timeout=1: ip == "192.168.10.11",
        workers=8,
    )
    assert results == {"192.168.10.11": "sw-11"}
    assert snmp_calls == ["192.168.10.11"]  # SNMP only touched the live host


def test_discover_sweeps_with_snmp_when_ping_unavailable():
    # If ICMP answers for nobody (ping unusable), fall back to an SNMP sweep and
    # keep only addresses that actually respond to SNMP.
    hostnames = {"192.168.10.12": "sw-12"}
    results = disc.discover(
        ["192.168.10.11", "192.168.10.12"],
        community="public",
        probe_snmp=lambda ip, community, timeout=1: hostnames.get(ip, ""),
        probe_ping=lambda ip, timeout=1: False,
        workers=2,
    )
    assert results == {"192.168.10.12": "sw-12"}


def test_discover_rejects_ip_shaped_sysname():
    # Some gear returns its own IP as sysName; treat that as "no hostname".
    results = disc.discover(
        ["192.168.10.11"],
        community="public",
        probe_snmp=lambda ip, community, timeout=1: "192.168.10.11",
        probe_ping=lambda ip, timeout=1: True,
        workers=1,
    )
    assert results == {"192.168.10.11": "192.168.10.11"}


def test_build_file_sd_is_sorted_prometheus_format():
    payload = disc.build_file_sd({"192.168.10.20": "b", "192.168.10.3": "a"})
    assert payload == [
        {"targets": ["192.168.10.3"], "labels": {"display_name": "a"}},
        {"targets": ["192.168.10.20"], "labels": {"display_name": "b"}},
    ]
