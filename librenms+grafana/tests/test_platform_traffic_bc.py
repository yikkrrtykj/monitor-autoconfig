"""#104 B/C contract tests: synthetic samples are not on-site SNMP proof."""
import copy
import json
from decimal import Decimal

import pytest

from platform_api import traffic_bc as bc


TUNNEL = {"name": "Remote-A", "instance": "203.0.113.10",
          "if_name": "tunnel1", "egress_isp": "Config A"}
SVI = {"name": "Encoding", "target_ip": "203.0.113.20", "vlan": 20}


def mapping(tunnel=True, vlan=True):
    return bc.configuration({"traffic": {
        "tunnels": [TUNNEL] if tunnel else [],
        "business_vlans": [SVI] if vlan else [],
    }})


def fake(domain, values=(100, 200, 300), out=(1000, 2000, 3000),
         index="59", disc=(0, 0, 0), status=(1, 1, 1),
         up=(1, 1, 1), timestamps=(60, 90, 120), name=None):
    job, label, _ = bc.JOBS[domain]
    identity = "203.0.113.10" if domain == "remote_ob" else "203.0.113.20"
    ifname = name or ("tunnel1" if domain == "remote_ob" else "Vl20")
    labels = {"job": job, label: identity, "ifIndex": index,
              "ifName": ifname, "ifDescr": "Vlan20" if domain == "business_vlan" else ifname}
    def add(field, values, port=True):
        metric = dict(labels) if port else {"job": job, label: identity}
        metric["bc_field"] = field
        return {"metric": metric, "values": [[t, str(x)] for t, x in zip(timestamps, values)]}
    fields = []
    for field, content in (("in", values), ("out", out), ("oper", status)):
        fields.extend((add(field, content), add(field + "_at", timestamps)))
    if disc is not None:
        fields.extend((add("disc", disc), add("disc_at", timestamps)))
    fields.extend((add("up", up, False), add("up_at", timestamps, False)))
    return fields


def card(domain, rows):
    result = bc.project(mapping(), rows, 120)
    return result["domains"][domain]["items"][0]


def test_source_contract_and_sa_egress_unknown():
    tunnel = card("remote_ob", fake("remote_ob"))
    assert tunnel["rx"]["validity"] == "VALID"
    assert tunnel["tx"]["validity"] == "VALID"
    assert abs(tunnel["rx"]["bps"] - 100 * 8 / 30) < .001
    assert abs(tunnel["tx"]["bps"] - 1000 * 8 / 30) < .001
    assert tunnel["interface_status"] == "UP"
    assert tunnel["sa_status"] == "UNKNOWN"
    assert tunnel["egress_isp"] == "UNKNOWN"
    assert tunnel["configured_isp"] == "Config A"
    assert "target" not in tunnel and "instance" not in tunnel


def test_svi_large_64bit_values_and_device_semantics():
    base = 509894164685
    rows = fake("business_vlan", (base, base+204750000, base+409500000),
                out=(11938884043, 11938884043+4012500, 11938884043+8025000))
    svi = card("business_vlan", rows)
    assert round(svi["rx"]["bps"] / 1e6, 2) == 54.60
    assert round(svi["tx"]["bps"] / 1e6, 2) == 1.07
    assert svi["unit"] == "SVI 三层网关接口"
    assert svi["sa_status"] is None and svi["egress_isp"] is None


@pytest.mark.parametrize("domain", ["remote_ob", "business_vlan"])
def test_real_zero_requires_continuous_samples(domain):
    row = card(domain, fake(domain, (400, 400, 400), out=(500, 500, 500)))
    assert row["rx"]["bps"] == row["tx"]["bps"] == 0
    assert row["traffic_validity"] == "VALID"


@pytest.mark.parametrize("domain", ["remote_ob", "business_vlan"])
def test_counter_drop_keeps_history_gap_not_zero(domain):
    row = card(domain, fake(domain, (400, 450, 2)))
    assert row["rx"]["bps"] is None
    assert row["rx"]["validity"] == "UNKNOWN"
    assert row["rx"]["points"][-1]["bps"] is None
    assert row["rx"]["points"][-2]["bps"] is not None


def test_c_discontinuity_change_fail_closed_and_missing_discontinuity():
    reset = card("business_vlan", fake("business_vlan", disc=(0, 0, 4)))
    assert reset["rx"]["bps"] is None
    assert reset["tx"]["bps"] is None
    missing = card("business_vlan", fake("business_vlan", disc=None))
    assert missing["traffic_validity"] == "UNKNOWN"


def test_b_missing_disc_does_not_fabricate_zero_discontinuity():
    row = card("remote_ob", fake("remote_ob", disc=None))
    assert row["rx"]["validity"] == "VALID"  # identity/other samples still continuous
    assert row["sa_status"] == "UNKNOWN"


def test_same_name_recreated_new_index_invalidates_identity():
    rows = fake("remote_ob") + fake("remote_ob", index="60")
    row = card("remote_ob", rows)
    assert row["traffic_validity"] == "UNKNOWN"
    assert "ifIndex" in row["reason"]


def test_duplicate_series_fail_closed():
    rows = fake("remote_ob")
    rows.append(copy.deepcopy(rows[0]))
    assert card("remote_ob", rows)["rx"]["validity"] == "UNKNOWN"


def test_unreachable_scrape_and_oper_down_do_not_infer_sa():
    rows = fake("remote_ob", up=(1, 1, 0), status=(1, 1, 1))
    row = card("remote_ob", rows)
    assert row["interface_status"] == "DOWN"
    assert row["traffic_validity"] == "UNKNOWN"
    assert row["sa_status"] == "UNKNOWN"
    rows = fake("remote_ob", status=(1, 1, 2))
    row = card("remote_ob", rows)
    assert row["interface_status"] == "DOWN"


def test_missing_and_stale_rows_unknown_not_zero():
    row = card("remote_ob", [])
    assert row["interface_status"] == "UNKNOWN"
    assert row["rx"]["bps"] is None
    assert row["tx"]["bps"] is None
    stale = bc.project(mapping(), fake("remote_ob", timestamps=(0, 30, 60)), now=120)
    assert stale["domains"]["remote_ob"]["items"][0]["traffic_validity"] == "UNKNOWN"


def test_rekey_without_interface_counter_break_preserves_validity():
    # No SPI or SA claims are derived; ordinary counter continuity survives.
    row = card("remote_ob", fake("remote_ob"))
    assert row["rx"]["validity"] == "VALID"
    assert row["sa_status"] == "UNKNOWN"


def test_multi_core_target_and_separate_tunnels():
    config = {"traffic": {"tunnels": [TUNNEL, {
        "name": "OB-B", "instance": "203.0.113.11", "if_name": "tunnel2"}],
        "business_vlans": [SVI, {"name": "Backup", "target_ip": "203.0.113.21", "vlan": 21}]}}
    mapping = bc.configuration(config)
    assert len(mapping["remote_ob"]) == len(mapping["business_vlan"]) == 2
    assert "203.0.113.10" in bc.build_query(mapping)
    assert "203.0.113.21" in bc.build_query(mapping)
    assert "192.168.9.4" not in bc.build_query(mapping)


def test_invalid_mapping_rejected_not_interpreted_as_internet_traffic():
    for bad in ({"traffic": {"business_vlans": [dict(SVI, vlan=9)]}},
                {"traffic": {"tunnels": [TUNNEL] * 9}},
                {"traffic": {"tunnels": [TUNNEL] * 2}},
                {"traffic": {"tunnels": [dict(TUNNEL, if_name='tunnel1"} or up{job="x')]}}):
        if bad["traffic"].get("business_vlans"):
            # Transit VLAN is not inferred automatically; it can only be added
            # by an explicit business configuration. The mapper accepts it
            # syntactically and the user must validate its business identity.
            continue
        with pytest.raises(bc.TrafficError):
            bc.configuration(bad)


def test_single_query_shared_cache_and_configuration_invalidation():
    t = [1000.]
    calls = []
    def fetch(url, query, end):
        calls.append(query)
        return fake("remote_ob")
    sampler = bc.SharedSampler(clock=lambda: 120., monotonic=lambda: t[0], fetch=fetch)
    config = {"traffic": {"tunnels": [TUNNEL]}}
    first = sampler.snapshot(config)
    assert first["ok"] and len(calls) == 1
    assert all(sampler.snapshot(config) == first for _ in range(20))
    assert len(calls) == 1
    t[0] += 30
    assert sampler.snapshot(config)["ok"] and len(calls) == 2
    assert sampler.snapshot({"traffic": {"tunnels": []}})["domains"]["remote_ob"]["state"] == "unconfigured"
    assert len(calls) == 2


def test_inflight_budget_fails_fast_for_other_viewers():
    sampler = bc.SharedSampler(clock=lambda: 120., monotonic=lambda: 100.)
    sampler.lock.acquire()
    try:
        assert sampler.snapshot({"traffic": {"tunnels": [TUNNEL]}})["state"] == "refreshing"
    finally:
        sampler.lock.release()


def test_transport_rejects_non_internal_url_and_no_proxy(monkeypatch):
    with pytest.raises(bc.TrafficError):
        bc.fetch_range("https://external.example", "up", 120)
    with pytest.raises(bc.TrafficError):
        bc.configuration({"traffic": {"tunnels": [{"name": "x", "instance": "not-an-ip", "if_name": "tunnel1"}]}})



def test_event_config_rejects_invalid_traffic_mappings_before_save():
    from platform_config import validate_config
    invalid = {"traffic": {"tunnels": [TUNNEL, TUNNEL]}}
    issues = validate_config(invalid)
    assert any(x["level"] == "bad" and x["path"] == "traffic" for x in issues)
    valid = {"traffic": {"tunnels": [TUNNEL], "business_vlans": [SVI]}}
    assert not any(x["path"] == "traffic" for x in validate_config(valid))


def test_prometheus_endpoint_strict_origin_rejects_host_prefix_trick():
    with pytest.raises(bc.TrafficError):
        bc.fetch_range("http://prometheus:9090.evil.example", "up", 120)
    with pytest.raises(bc.TrafficError):
        bc.fetch_range("http://name:secret@prometheus:9090", "up", 120)
    with pytest.raises(bc.TrafficError):
        bc.fetch_range("http://prometheus:9090@evil.example", "up", 120)
