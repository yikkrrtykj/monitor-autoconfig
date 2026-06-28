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
    # "name:" prefix is stripped, CIDR is ignored, dedupe across entries.
    assert disc.expand_targets("SW:192.168.10.11-12,192.168.10.0/24,192.168.10.11") == [
        "192.168.10.11", "192.168.10.12",
    ]


def test_discover_keeps_snmp_hostname_falls_back_to_ip_and_drops_offline():
    hostnames = {"192.168.10.11": "core-sw-01"}      # answers SNMP
    pingable = {"192.168.10.11", "192.168.10.12"}    # .12 answers ping only
    ips = ["192.168.10.11", "192.168.10.12", "192.168.10.13"]  # .13 is offline

    results = disc.discover(
        ips,
        community="public",
        probe_snmp=lambda ip, community: hostnames.get(ip, ""),
        probe_ping=lambda ip: ip in pingable,
        workers=4,
    )
    assert results == {
        "192.168.10.11": "core-sw-01",   # SNMP hostname wins
        "192.168.10.12": "192.168.10.12",  # ping-only -> IP placeholder, never "SW"
    }
    assert "192.168.10.13" not in results  # offline -> not added


def test_discover_rejects_ip_shaped_sysname():
    # Some gear returns its own IP as sysName; treat that as "no hostname".
    results = disc.discover(
        ["192.168.10.11"],
        community="public",
        probe_snmp=lambda ip, community: "192.168.10.11",
        probe_ping=lambda ip: True,
        workers=1,
    )
    assert results == {"192.168.10.11": "192.168.10.11"}


def test_build_file_sd_is_sorted_prometheus_format():
    payload = disc.build_file_sd({"192.168.10.20": "b", "192.168.10.3": "a"})
    assert payload == [
        {"targets": ["192.168.10.3"], "labels": {"display_name": "a"}},
        {"targets": ["192.168.10.20"], "labels": {"display_name": "b"}},
    ]
