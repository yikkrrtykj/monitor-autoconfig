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
MULTILINE_CISCO_STDOUT = (
    ".1.3.6.1.2.1.1.1.0 = STRING: Cisco IOS Software, C2960X Software "
    "(C2960X-UNIVERSALK9-M), Version 15.2(7)E5\n"
    "Technical Support: http://www.cisco.com/techsupport\n"
    "Copyright (c) 1986-2021 by Cisco Systems, Inc.\n"
    "Compiled Tue 14-Sep-21 12:00 by prod_rel_team\n"
    ".1.3.6.1.2.1.1.2.0 = OID: .1.3.6.1.4.1.9.1.716\n"
    ".1.3.6.1.2.1.1.5.0 = STRING: pmgc-lops.global.com\n"
    ".1.3.6.1.2.1.1.7.0 = INTEGER: 3\n"
)


class _Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def stub_snmpget(monkeypatch, stdout="", returncode=0, stderr=""):
    """Replace subprocess only inside the discovery module."""
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _Completed(stdout, returncode, stderr)

    monkeypatch.setattr(disc, "subprocess", type(
        "SubprocessStub", (), {"run": staticmethod(fake_run)}
    ))
    return calls


def test_snmp_identity_parses_standard_single_line_response(monkeypatch):
    calls = stub_snmpget(monkeypatch, (
        ".1.3.6.1.2.1.1.1.0 = STRING: Cisco IOS Software, C2960 Software\n"
        ".1.3.6.1.2.1.1.2.0 = OID: .1.3.6.1.4.1.9.1.716\n"
        ".1.3.6.1.2.1.1.5.0 = STRING: sw-1\n"
        ".1.3.6.1.2.1.1.7.0 = INTEGER: 3\n"
    ))

    identity = disc.snmp_identity("192.168.10.30", "public")

    assert identity == {
        "sys_descr": "Cisco IOS Software, C2960 Software",
        "sys_object_id": "1.3.6.1.4.1.9.1.716",
        "sys_name": "sw-1",
        "sys_services": "3",
    }
    assert len(calls) == 1, "one bounded GET per device"
    command = calls[0]
    assert "-On" in command and "-Onqv" not in command
    assert sum(1 for arg in command if arg.startswith("1.3.6.1.2.1.1.")) == 4
    assert command[-4:] == list(disc.SNMP_IDENTITY_OIDS)


def test_snmp_identity_keeps_multiline_cisco_sysdescr_from_shifted_fields(monkeypatch):
    stub_snmpget(monkeypatch, MULTILINE_CISCO_STDOUT)

    identity = disc.snmp_identity("192.168.10.30", "public")

    assert identity["sys_object_id"] == "1.3.6.1.4.1.9.1.716"
    assert identity["sys_name"] == "pmgc-lops.global.com"
    assert identity["sys_services"] == "3"
    assert "C2960X" in identity["sys_descr"]
    assert "Technical Support" in identity["sys_descr"]
    assert "Compiled" in identity["sys_descr"]
    assert "\n" not in identity["sys_descr"]


def test_multiline_cisco_switch_is_confirmed_with_correct_name(monkeypatch):
    stub_snmpget(monkeypatch, MULTILINE_CISCO_STDOUT)

    identity = disc.snmp_identity("192.168.10.30", "public")
    targets, state = disc.update_entries(
        {"192.168.10.30": {"ping": True, "snmp": identity}},
        ["192.168.10.30"], {}, now=100.0, retention_seconds=86400,
    )

    assert targets == {"192.168.10.30": "pmgc-lops.global.com"}
    assert state["192.168.10.30"]["source"] == "snmp-description"
    assert state["192.168.10.30"]["sys_object_id"] == "1.3.6.1.4.1.9.1.716"


def test_snmp_identity_tolerates_missing_and_no_such_values(monkeypatch):
    stub_snmpget(monkeypatch, (
        ".1.3.6.1.2.1.1.1.0 = STRING: H3C Comware Software\n"
        ".1.3.6.1.2.1.1.2.0 = No Such Object available on this agent at this OID\n"
        ".1.3.6.1.2.1.1.5.0 = STRING: h3c-sw\n"
    ))

    identity = disc.snmp_identity("192.168.10.52", "public")

    assert identity["sys_descr"] == "H3C Comware Software"
    assert identity["sys_object_id"] == ""
    assert identity["sys_name"] == "h3c-sw"
    assert identity["sys_services"] == ""


def test_snmp_identity_accepts_oid_with_and_without_leading_dot(monkeypatch):
    stub_snmpget(monkeypatch, (
        ".1.3.6.1.2.1.1.1.0 = STRING: H3C Comware Software\n"
        ".1.3.6.1.2.1.1.2.0 = OID: 1.3.6.1.4.1.25506.1.1\n"
        ".1.3.6.1.2.1.1.5.0 = STRING: h3c-sw\n"
        ".1.3.6.1.2.1.1.7.0 = INTEGER: 3\n"
    ))
    without_dot = disc.snmp_identity("192.168.10.52", "public")

    stub_snmpget(monkeypatch, (
        ".1.3.6.1.2.1.1.1.0 = STRING: H3C Comware Software\n"
        ".1.3.6.1.2.1.1.2.0 = OID: .1.3.6.1.4.1.25506.1.1\n"
        ".1.3.6.1.2.1.1.5.0 = STRING: h3c-sw\n"
        ".1.3.6.1.2.1.1.7.0 = INTEGER: 3\n"
    ))
    with_dot = disc.snmp_identity("192.168.10.52", "public")

    assert without_dot["sys_object_id"] == "1.3.6.1.4.1.25506.1.1"
    assert with_dot["sys_object_id"] == "1.3.6.1.4.1.25506.1.1"
    assert disc.vendor_for_object_id(".1.3.6.1.4.1.25506.1.1") == "h3c"
    assert disc.vendor_for_object_id("1.3.6.1.4.1.9.1.716") == "cisco"
    assert disc.vendor_for_object_id("not-an-oid") == ""


def test_snmp_identity_handles_hostname_and_ip_sysname(monkeypatch):
    stub_snmpget(monkeypatch, (
        ".1.3.6.1.2.1.1.1.0 = STRING: Cisco IOS Software, C2960\n"
        ".1.3.6.1.2.1.1.2.0 = OID: .1.3.6.1.4.1.9.1.716\n"
        ".1.3.6.1.2.1.1.5.0 = STRING: 192.168.10.11\n"
        ".1.3.6.1.2.1.1.7.0 = INTEGER: 3\n"
    ))

    identity = disc.snmp_identity("192.168.10.11", "public")
    targets, _state = disc.update_entries(
        {"192.168.10.11": {"ping": True, "snmp": identity}},
        ["192.168.10.11"], {}, now=100.0, retention_seconds=86400,
    )

    assert identity["sys_name"] == "192.168.10.11"
    assert targets == {"192.168.10.11": "192.168.10.11"}


def test_snmp_identity_timeout_and_exceptions_return_unknown(monkeypatch):
    stub_snmpget(monkeypatch, "", returncode=1, stderr="Timeout: No Response")
    assert disc.snmp_identity("192.168.10.30", "public") == {}

    def explode(_command, **_kwargs):
        raise OSError("snmpget missing")

    monkeypatch.setattr(disc, "subprocess", type(
        "SubprocessStub", (), {"run": staticmethod(explode)}
    ))
    assert disc.snmp_identity("192.168.10.30", "public") == {}


def test_snmp_identity_rejects_unparseable_output(monkeypatch):
    stub_snmpget(monkeypatch, "")
    assert disc.snmp_identity("192.168.10.30", "public") == {}

    stub_snmpget(monkeypatch, "Timeout: No Response from 192.168.10.30\n")
    assert disc.snmp_identity("192.168.10.30", "public") == {}

    # Records without type prefixes are still valid values.
    stub_snmpget(monkeypatch, (
        ".1.3.6.1.2.1.1.1.0 = Cisco IOS Software, C2960\n"
        ".1.3.6.1.2.1.1.2.0 = 1.3.6.1.4.1.9.1.716\n"
        ".1.3.6.1.2.1.1.5.0 = sw-1\n"
        ".1.3.6.1.2.1.1.7.0 = 3\n"
    ))
    assert disc.snmp_identity("192.168.10.30", "public")["sys_name"] == "sw-1"


def test_multiline_non_switch_descr_stays_unverified(monkeypatch):
    stub_snmpget(monkeypatch, (
        ".1.3.6.1.2.1.1.1.0 = STRING: HP ETHERNET MULTI-ENVIRONMENT\n"
        "SN: CN1A\n"
        "Firmware: V.1.2\n"
        ".1.3.6.1.2.1.1.2.0 = OID: .1.3.6.1.4.1.11.2.3.9.1\n"
        ".1.3.6.1.2.1.1.5.0 = STRING: printer-9\n"
        ".1.3.6.1.2.1.1.7.0 = INTEGER: 72\n"
    ))

    identity = disc.snmp_identity("192.168.10.9", "public")
    targets, state = disc.update_entries(
        {"192.168.10.9": {"ping": True, "snmp": identity}},
        ["192.168.10.9"], {}, now=100.0, retention_seconds=86400,
    )

    assert "SN: CN1A" in identity["sys_descr"]
    assert targets == {}
    assert state["192.168.10.9"]["status"] == "unverified"
    assert state["192.168.10.9"]["reason"] == "host-signature"


def test_snmp_identity_does_not_assume_one_varbind_per_line(monkeypatch):
    # sysDescr swallows four physical lines; only OID boundaries can split it.
    stub_snmpget(monkeypatch, (
        ".1.3.6.1.2.1.1.1.0 = STRING: line-1\n"
        "line-2\n"
        "line-3\n"
        "line-4\n"
        ".1.3.6.1.2.1.1.2.0 = OID: .1.3.6.1.4.1.9.1.716\n"
        ".1.3.6.1.2.1.1.5.0 = STRING: sw-9\n"
        ".1.3.6.1.2.1.1.7.0 = INTEGER: 3\n"
    ))

    identity = disc.snmp_identity("192.168.10.30", "public")

    assert identity["sys_descr"] == "line-1 line-2 line-3 line-4"
    assert identity["sys_object_id"] == "1.3.6.1.4.1.9.1.716"
    assert identity["sys_name"] == "sw-9"
    assert identity["sys_services"] == "3"


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
    assert entries["192.168.10.57"]["pending_verification"] is True
    assert entries["192.168.10.57"]["identity_confirmed_at"] is None

    targets, state = disc.update_entries(
        {"192.168.10.9": {"ping": True, "snmp": {}}},
        ["192.168.10.9", "192.168.10.57"], entries, now=200.0,
        retention_seconds=86400,
    )
    assert targets == {"192.168.10.57": "pgs-avl"}
    assert state["192.168.10.9"]["status"] == "unverified"
    assert state["192.168.10.57"]["pending_verification"] is True
    assert state["192.168.10.57"]["legacy_deadline"] == (
        200.0 + disc.DEFAULT_LEGACY_GRACE_SECONDS
    )


LEGACY_GRACE = 7 * 24 * 60 * 60


def test_legacy_host_keeps_monitoring_but_stays_provisional_on_first_cycle():
    legacy = {"192.168.10.57": {"name": "pgs-avl", "last_seen": 100.0}}
    entries = disc.normalize_state(legacy)

    targets, state = disc.update_entries(
        {"192.168.10.57": {"ping": True, "snmp": {}}},
        ["192.168.10.57"], entries, now=200.0, retention_seconds=86400,
        legacy_grace_seconds=LEGACY_GRACE,
    )

    assert targets == {"192.168.10.57": "pgs-avl"}
    entry = state["192.168.10.57"]
    assert entry["status"] == "confirmed"
    assert entry["source"] == "legacy-state"
    assert entry["pending_verification"] is True
    assert entry["identity_confirmed_at"] is None
    assert entry["legacy_deadline"] == 200.0 + LEGACY_GRACE
    assert entry["reason"] == "awaiting-verification"


def test_legacy_printer_is_removed_when_snmp_proves_non_switch():
    legacy = {"192.168.10.9": {"name": "printer-9", "last_seen": 100.0}}
    entries = disc.normalize_state(legacy)
    observations = {"192.168.10.9": {"ping": True, "snmp": identity(
        "HP ETHERNET MULTI-ENVIRONMENT,SN:CN1A",
        "1.3.6.1.4.1.11.2.3.9.1", "printer-9", "72",
    )}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.9"], entries, now=200.0,
        retention_seconds=86400, legacy_grace_seconds=LEGACY_GRACE,
    )

    assert targets == {}
    entry = state["192.168.10.9"]
    assert entry["status"] == "unverified"
    assert entry["reason"] == "host-signature"
    assert entry["pending_verification"] is False


def test_legacy_miscollected_host_expires_after_grace_window():
    legacy = {"192.168.10.9": {"name": "printer-9", "last_seen": 100.0}}
    state = disc.normalize_state(legacy)
    observations = {"192.168.10.9": {"ping": True, "snmp": {}}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.9"], state, now=200.0,
        retention_seconds=86400, legacy_grace_seconds=LEGACY_GRACE,
    )
    deadline = state["192.168.10.9"]["legacy_deadline"]
    assert targets == {"192.168.10.9": "printer-9"}

    targets, state = disc.update_entries(
        observations, ["192.168.10.9"], state, now=deadline,
        retention_seconds=86400, legacy_grace_seconds=LEGACY_GRACE,
    )
    assert targets == {"192.168.10.9": "printer-9"}, "deadline itself is still pending"

    targets, state = disc.update_entries(
        observations, ["192.168.10.9"], state, now=deadline + 1,
        retention_seconds=86400, legacy_grace_seconds=LEGACY_GRACE,
    )
    assert targets == {}
    assert state["192.168.10.9"]["status"] == "unverified"
    assert state["192.168.10.9"]["reason"] == "legacy-grace-expired"
    assert state["192.168.10.9"]["pending_verification"] is False


def test_legacy_entry_promotes_once_switch_evidence_arrives():
    legacy = {"192.168.10.57": {"name": "pgs-avl", "last_seen": 100.0}}
    state = disc.normalize_state(legacy)
    observations = {"192.168.10.57": {"ping": True, "snmp": {}}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.57"], state, now=200.0,
        retention_seconds=86400, legacy_grace_seconds=LEGACY_GRACE,
    )
    assert state["192.168.10.57"]["pending_verification"] is True

    observations = {"192.168.10.57": {"ping": True, "snmp": identity(
        CISCO_2960_DESCR, CISCO_2960, "pgs-avl", "3",
    )}}
    targets, state = disc.update_entries(
        observations, ["192.168.10.57"], state, now=300.0,
        retention_seconds=86400, legacy_grace_seconds=LEGACY_GRACE,
    )

    entry = state["192.168.10.57"]
    assert targets == {"192.168.10.57": "pgs-avl"}
    assert entry["source"] == "snmp-description"
    assert entry["pending_verification"] is False
    assert entry["identity_confirmed_at"] == 300.0
    assert entry["legacy_deadline"] is None


def test_legacy_unknown_verdict_keeps_pending_until_deadline():
    legacy = {"192.168.10.57": {"name": "pgs-avl", "last_seen": 100.0}}
    state = disc.normalize_state(legacy)
    observations = {"192.168.10.57": {"ping": True, "snmp": identity(
        name="pgs-avl",
    )}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.57"], state, now=200.0,
        retention_seconds=86400, legacy_grace_seconds=LEGACY_GRACE,
    )

    entry = state["192.168.10.57"]
    assert targets == {"192.168.10.57": "pgs-avl"}
    assert entry["pending_verification"] is True
    assert entry["reason"] == "awaiting-verification"
    assert entry["legacy_deadline"] == 200.0 + LEGACY_GRACE


def test_legacy_offline_retention_contract_is_preserved():
    legacy = {"192.168.10.57": {"name": "pgs-avl", "last_seen": 100.0}}
    state = disc.normalize_state(legacy)

    targets, state = disc.update_entries(
        {}, ["192.168.10.57"], state, now=100.0 + 86400,
        retention_seconds=86400, legacy_grace_seconds=LEGACY_GRACE,
    )
    assert targets == {"192.168.10.57": "pgs-avl"}

    expired, _state = disc.update_entries(
        {}, ["192.168.10.57"], state, now=101.0 + 86400,
        retention_seconds=86400, legacy_grace_seconds=LEGACY_GRACE,
    )
    assert expired == {}


def test_v2_confirmed_switch_is_not_affected_by_legacy_grace():
    previous = {"192.168.10.57": confirmed_entry(confirmed_at=100.0, last_seen=100.0)}
    observations = {"192.168.10.57": {"ping": True, "snmp": {}}}

    targets, state = disc.update_entries(
        observations, ["192.168.10.57"], previous,
        now=100.0 + 30 * 86400, retention_seconds=86400,
        legacy_grace_seconds=LEGACY_GRACE,
    )

    entry = state["192.168.10.57"]
    assert targets == {"192.168.10.57": "pgs-avl"}
    assert entry["status"] == "confirmed"
    assert entry["pending_verification"] is False
    assert entry["identity_confirmed_at"] == 100.0


def test_legacy_window_and_expiry_are_logged(capsys):
    entries = {
        "192.168.10.9": disc._entry(
            "192.168.10.9", "printer-9", "unverified", None, None, 100.0,
            None, "legacy-grace-expired",
        ),
        "192.168.10.57": disc._entry(
            "192.168.10.57", "pgs-avl", "confirmed", "legacy-state", None,
            100.0, None, "awaiting-verification",
            pending_verification=True, legacy_deadline=1000.0,
        ),
    }

    disc._log_legacy_pending(entries, now=200.0)

    err = capsys.readouterr().err
    assert "legacy grace expired: 192.168.10.9 printer-9" in err
    assert "legacy hosts awaiting SNMP verification: 1" in err
    assert "nearest deadline in 0.2h" in err


def test_v2_state_keeps_name_and_last_seen_for_rollback(tmp_path):
    entries = {"192.168.10.57": confirmed_entry()}
    payload = disc.state_payload(entries)

    assert payload["schema_version"] == 2
    assert payload["192.168.10.57"]["name"] == "pgs-avl"
    assert payload["192.168.10.57"]["last_seen"] == 100.0


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
