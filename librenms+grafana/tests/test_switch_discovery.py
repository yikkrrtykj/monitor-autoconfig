import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# discover-switch-targets.py is hyphenated, so load it by path.
_spec = importlib.util.spec_from_file_location(
    "discover_switch_targets", ROOT / "discover-switch-targets.py",
)
disc = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(disc)

CISCO_2960 = "1.3.6.1.4.1.9.1.716"
CISCO_2960_DESCR = (
    "Cisco IOS Software, C2960 Software (C2960-LANBASEK9-M), "
    "Version 15.0(2)SE11, RELEASE SOFTWARE (fc3)"
)
CISCO_ISR_DESCR = (
    "Cisco IOS Software, ISR Software (X86_64_LINUX_IOSD-UNIVERSALK9-M), "
    "Version 15.5(3)M"
)


def identity(descr="", object_id="", name="", services=""):
    return {
        "sys_descr": descr,
        "sys_object_id": object_id,
        "sys_name": name,
        "sys_services": services,
    }


def confirmed_entry(name="pgs-avl", confirmed_at=100.0, last_seen=100.0):
    return disc._entry(
        "192.168.10.57", name, "confirmed", "snmp-description",
        confirmed_at, last_seen, CISCO_2960,
    )


def test_expand_targets_handles_ranges_names_and_cidr():
    assert disc.expand_targets("192.168.10.11-13") == [
        "192.168.10.11", "192.168.10.12", "192.168.10.13",
    ]
    assert disc.expand_targets("SW:192.168.10.11-12,192.168.10.11") == [
        "192.168.10.11", "192.168.10.12",
    ]
    assert disc.expand_targets("192.168.10.8/30") == ["192.168.10.9", "192.168.10.10"]


def test_excluded_ips_expands_named_and_ranged_entries():
    assert disc.excluded_ips("CORE:192.168.10.254", "192.168.9.1-2") == {
        "192.168.10.254", "192.168.9.1", "192.168.9.2",
    }


def test_ping_only_host_is_unverified_and_not_monitored():
    """Regression: 192.168.10.9 answers Ping only and was never a switch."""
    observations = {"192.168.10.9": {"ping": True, "snmp": {}}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.9"], {}, now=200.0, retention_seconds=86400,
    )

    assert targets == {}
    entry = state["192.168.10.9"]
    assert entry["status"] == "unverified"
    assert entry["reason"] == "no-snmp"
    assert entry["identity_confirmed_at"] is None
    assert entry["last_seen"] == 200.0


def test_new_cisco_switch_is_admitted_with_its_sysname():
    observations = {"192.168.10.30": {"ping": True, "snmp": identity(
        CISCO_2960_DESCR, CISCO_2960, "pmgc-lops.global.com", "3",
    )}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.30"], {}, now=300.0, retention_seconds=86400,
    )

    assert targets == {"192.168.10.30": "pmgc-lops.global.com"}
    entry = state["192.168.10.30"]
    assert entry["status"] == "confirmed"
    assert entry["source"] == "snmp-description"
    assert entry["identity_confirmed_at"] == 300.0
    assert entry["last_seen"] == 300.0


def test_confirmed_switch_with_temporary_snmp_failure_keeps_ping_target():
    previous = {"192.168.10.57": confirmed_entry(confirmed_at=100.0, last_seen=100.0)}
    observations = {"192.168.10.57": {"ping": True, "snmp": {}}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.57"], previous, now=400.0,
        retention_seconds=86400,
    )

    assert targets == {"192.168.10.57": "pgs-avl"}
    entry = state["192.168.10.57"]
    assert entry["status"] == "confirmed"
    assert entry["identity_confirmed_at"] == 100.0
    assert entry["last_seen"] == 400.0


def test_confirmed_switch_offline_is_retained_for_24_hours():
    previous = {"192.168.10.57": confirmed_entry(confirmed_at=50.0, last_seen=100.0)}
    age = 24 * 60 * 60

    targets, _state = disc.update_entries(
        {}, ["192.168.10.57"], previous, now=100.0 + age,
        retention_seconds=age,
    )
    assert targets == {"192.168.10.57": "pgs-avl"}

    expired, state = disc.update_entries(
        {}, ["192.168.10.57"], previous, now=101.0 + age,
        retention_seconds=age,
    )
    assert expired == {}
    assert "192.168.10.57" not in state


def test_non_switch_snmp_devices_are_not_admitted():
    observations = {
        "192.168.10.41": {"ping": True, "snmp": identity(
            "HP ETHERNET MULTI-ENVIRONMENT,SN:CN1A,ST:...",
            "1.3.6.1.4.1.11.2.3.9.1", "printer-1", "72",
        )},
        "192.168.10.42": {"ping": True, "snmp": identity(
            "Linux fileserver 5.15.0-91-generic",
            "1.3.6.1.4.1.8072.3.2.10", "fileserver", "72",
        )},
    }

    targets, state = disc.update_entries(
        observations, list(observations), {}, now=500.0,
        retention_seconds=86400,
    )

    assert targets == {}
    assert state["192.168.10.41"]["status"] == "unverified"
    assert state["192.168.10.41"]["reason"] == "host-signature"
    assert state["192.168.10.42"]["status"] == "unverified"
    assert state["192.168.10.42"]["reason"] == "non-switch-description"


def test_cisco_router_signature_is_not_a_switch():
    observations = {"192.168.10.1": {"ping": True, "snmp": identity(
        CISCO_ISR_DESCR, "1.3.6.1.4.1.9.1.1045", "core-router", "6",
    )}}

    targets, state = disc.update_entries(
        observations, list(observations), {}, now=510.0,
        retention_seconds=86400,
    )

    assert targets == {}
    assert state["192.168.10.1"]["status"] == "unverified"
    assert state["192.168.10.1"]["reason"] == "non-switch-description"


def test_unrecognized_real_switch_stays_unverified_with_reason():
    observations = {"192.168.10.51": {"ping": True, "snmp": identity(
        "MysteryBox 9000", "1.3.6.1.4.1.99999.1.2", "sw-unknown", "3",
    )}}

    targets, state = disc.update_entries(
        observations, list(observations), {}, now=520.0,
        retention_seconds=86400,
    )

    assert targets == {}
    entry = state["192.168.10.51"]
    assert entry["status"] == "unverified"
    assert entry["reason"] == "unknown-vendor"
    assert entry["name"] == "sw-unknown"


def test_switching_vendor_with_l2_only_sys_services_is_admitted():
    # H3C access switch: no ASCII switch model in sysDescr, L2-only services.
    observations = {"192.168.10.52": {"ping": True, "snmp": identity(
        "H3C Comware Software, Version 7.1.070, Release 3506P08",
        "1.3.6.1.4.1.25506.1.1", "h3c-access", "3",
    )}}

    targets, state = disc.update_entries(
        observations, list(observations), {}, now=530.0,
        retention_seconds=86400,
    )

    assert targets == {"192.168.10.52": "h3c-access"}
    assert state["192.168.10.52"]["source"] == "snmp-l2-services"


def test_inconclusive_snmp_does_not_demote_a_confirmed_switch():
    previous = {"192.168.10.57": confirmed_entry()}
    observations = {"192.168.10.57": {"ping": True, "snmp": identity(
        name="pgs-avl",
    )}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.57"], previous, now=540.0,
        retention_seconds=86400,
    )

    assert targets == {"192.168.10.57": "pgs-avl"}
    assert state["192.168.10.57"]["status"] == "confirmed"
    assert state["192.168.10.57"]["identity_confirmed_at"] == 100.0


def test_explicit_non_switch_evidence_demotes_a_stale_confirmation():
    previous = {"192.168.10.57": confirmed_entry()}
    observations = {"192.168.10.57": {"ping": True, "snmp": identity(
        "HP ETHERNET MULTI-ENVIRONMENT,SN:CN1A",
        "1.3.6.1.4.1.11.2.3.9.1", "printer-1", "72",
    )}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.57"], previous, now=550.0,
        retention_seconds=86400,
    )

    assert targets == {}
    assert state["192.168.10.57"]["status"] == "unverified"


def test_range_removal_prunes_targets_and_state():
    previous = {
        "192.168.10.9": disc._entry(
            "192.168.10.9", "192.168.10.9", "unverified", None, None, 100.0,
            None, "no-snmp",
        ),
        "192.168.10.57": confirmed_entry(),
    }

    targets, state = disc.update_entries(
        {}, ["192.168.10.57"], previous, now=600.0, retention_seconds=86400,
    )

    assert "192.168.10.9" not in state
    assert targets == {"192.168.10.57": "pgs-avl"}


def test_legacy_v1_state_migrates_without_admitting_ping_placeholders():
    legacy = {
        "192.168.10.9": {"name": "192.168.10.9", "last_seen": 100.0},
        "192.168.10.57": {"name": "pgs-avl", "last_seen": 100.0},
    }

    entries = disc.normalize_state(legacy)

    assert entries["192.168.10.9"]["status"] == "unverified"
    assert entries["192.168.10.9"]["reason"] == "legacy-ping-placeholder"
    assert entries["192.168.10.57"]["status"] == "confirmed"
    assert entries["192.168.10.57"]["source"] == "legacy-state"
    assert entries["192.168.10.57"]["identity_confirmed_at"] == 100.0

    targets, state = disc.update_entries(
        {"192.168.10.9": {"ping": True, "snmp": {}}},
        ["192.168.10.9", "192.168.10.57"], entries, now=200.0,
        retention_seconds=86400,
    )
    assert targets == {"192.168.10.57": "pgs-avl"}
    assert state["192.168.10.9"]["status"] == "unverified"


def test_v2_state_round_trips_through_file(tmp_path):
    path = tmp_path / "state.json"
    entries = {"192.168.10.57": confirmed_entry()}
    path.write_text(
        json.dumps(disc.state_payload(entries)), encoding="utf-8",
    )

    loaded = disc.load_state(str(path))

    assert loaded["192.168.10.57"]["status"] == "confirmed"
    assert loaded["192.168.10.57"]["identity_confirmed_at"] == 100.0


def test_discover_probes_every_candidate_even_when_icmp_is_blocked():
    probed = []

    def probe_identity(ip, community, timeout=1):
        probed.append(ip)
        if ip == "192.168.10.12":
            return identity(CISCO_2960_DESCR, CISCO_2960, "sw-12", "3")
        return {}

    observations = disc.discover(
        ["192.168.10.11", "192.168.10.12"],
        community="public",
        probe_identity=probe_identity,
        probe_ping=lambda ip, timeout=1: False,
        workers=2,
    )

    assert sorted(probed) == ["192.168.10.11", "192.168.10.12"]
    assert set(observations) == {"192.168.10.12"}
    assert observations["192.168.10.12"]["ping"] is False

    targets, _state = disc.update_entries(
        observations, ["192.168.10.11", "192.168.10.12"], {}, now=700.0,
        retention_seconds=86400,
    )
    assert targets == {"192.168.10.12": "sw-12"}


def test_ip_shaped_sysname_keeps_the_address_as_display_name():
    observations = {"192.168.10.11": {"ping": True, "snmp": identity(
        CISCO_2960_DESCR, CISCO_2960, "192.168.10.11", "3",
    )}}

    targets, _state = disc.update_entries(
        observations, list(observations), {}, now=800.0,
        retention_seconds=86400,
    )

    assert targets == {"192.168.10.11": "192.168.10.11"}


def test_build_file_sd_is_sorted_prometheus_format():
    payload = disc.build_file_sd({"192.168.10.20": "b", "192.168.10.3": "a"})
    assert payload == [
        {"targets": ["192.168.10.3"], "labels": {"display_name": "a"}},
        {"targets": ["192.168.10.20"], "labels": {"display_name": "b"}},
    ]


def test_confirmed_targets_and_topology_collector_agree(tmp_path, monkeypatch):
    """infra-dist-ping, SNMP jobs and the topology collector share this file."""
    import importlib.util as _il

    spec = _il.spec_from_file_location(
        "generate_topology_edges_switch_admission",
        ROOT / "generate-topology-edges.py",
    )
    topology = _il.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(topology)

    observations = {
        "192.168.10.31": {"ping": True, "snmp": identity(
            CISCO_2960_DESCR, CISCO_2960, "pmgc-hero.global.com", "3",
        )},
        "192.168.10.9": {"ping": True, "snmp": {}},
    }
    targets, _state = disc.update_entries(
        observations, list(observations), {}, now=900.0,
        retention_seconds=86400,
    )
    path = tmp_path / "switch_targets.json"
    path.write_text(json.dumps(disc.build_file_sd(targets)), encoding="utf-8")
    monkeypatch.setenv("SWITCH_TARGETS_FILE", str(path))

    assert targets == {"192.168.10.31": "pmgc-hero.global.com"}
    assert topology.load_discovered_switch_ips() == ["192.168.10.31"]


def test_confirmed_switch_target_cooperates_with_topology_identity(tmp_path, monkeypatch):
    """Requirement 9: a confirmed .31 target is polled and its neighbour resolves."""
    import importlib.util as _il

    spec = _il.spec_from_file_location(
        "generate_topology_edges_switch_identity",
        ROOT / "generate-topology-edges.py",
    )
    topology = _il.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(topology)

    path = tmp_path / "switch_targets.json"
    path.write_text(json.dumps(disc.build_file_sd(
        {"192.168.10.31": "pmgc-hero.global.com"}
    )), encoding="utf-8")
    monkeypatch.setenv("SWITCH_TARGETS_FILE", str(path))
    assert topology.load_discovered_switch_ips() == ["192.168.10.31"]

    def device(ip, sysname, ifname):
        item = topology._empty_device(ip)
        item.update({
            "sysname": sysname,
            "ifname": dict(ifname),
            "ifoper": {ifindex: 1 for ifindex in ifname},
        })
        return item

    access = device("192.168.10.30", "pmgc-lops.global.com", {10111: "Gi0/11"})
    hero = device("192.168.10.31", "pmgc-hero.global.com", {12: "Gi0/12"})
    access["neighbors"] = [{
        "protocol": "cdp",
        "active": 1,
        "local_ifindex": 10111,
        "local_port": "Gi0/11",
        "neighbor_name": "pmgc-hero.global.com",
        "neighbor_device_id": 0,
        "neighbor_port": "GigabitEthernet0/12",
    }]
    devices = {access["ip"]: access, hero["ip"]: hero}

    edges, _diagnostics = topology.build_edges(
        devices, topology.build_name_index(devices),
    )

    assert len(edges) == 1
    assert edges[0]["to_ip"] == "192.168.10.31"
    assert edges[0]["to_port"] == "Gi0/12"


def test_unverified_reason_is_logged(capsys):
    entries = {
        "192.168.10.9": disc._entry(
            "192.168.10.9", "192.168.10.9", "unverified", None, None, 100.0,
            None, "no-snmp",
        ),
    }

    disc._log_unverified(entries)

    err = capsys.readouterr().err
    assert "unverified candidates: 1" in err
    assert "192.168.10.9" in err
    assert "reason=no-snmp" in err


def test_single_probe_per_candidate_keeps_snmp_bounded():
    calls = []

    def probe_identity(ip, community, timeout=1):
        calls.append((ip, timeout))
        return {}

    disc.discover(
        ["192.168.10.11", "192.168.10.12"], community="public",
        probe_identity=probe_identity,
        probe_ping=lambda ip, timeout=1: False, workers=2, snmp_timeout=1,
    )

    assert sorted(calls) == [("192.168.10.11", 1), ("192.168.10.12", 1)]
