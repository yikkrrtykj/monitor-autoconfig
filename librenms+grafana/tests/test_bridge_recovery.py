import importlib.util
from pathlib import Path

# alertmanager-feishu-bridge.py is hyphenated; load it by path. Importing only
# defines functions (the server starts under __main__), so this is side-effect free.
_spec = importlib.util.spec_from_file_location(
    "feishu_bridge",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def test_recovery_waits_for_sustained_up():
    state = {"up_since": None}
    # First UP sample at t=100 starts the stable-up window.
    assert bridge.recovery_ready(state, now=100, sample_ts=100, recover_stable=10) is False
    assert state["up_since"] == 100
    # Still UP but only 5s in -> not yet recovered.
    assert bridge.recovery_ready(state, now=105, sample_ts=105, recover_stable=10) is False
    # 10s of continuous UP -> recovery is due.
    assert bridge.recovery_ready(state, now=110, sample_ts=110, recover_stable=10) is True


def test_recovery_immediate_when_stable_seconds_zero():
    # Legacy behaviour: recover on the first UP sample.
    state = {"up_since": None}
    assert bridge.recovery_ready(state, now=100, sample_ts=100, recover_stable=0) is True


def test_flap_restarts_the_stable_window():
    state = {"up_since": None}
    # UP at t=100, not yet stable.
    assert bridge.recovery_ready(state, now=100, sample_ts=100, recover_stable=10) is False
    # A dip clears up_since (this is what the watcher's down-branch does).
    state["up_since"] = None
    # UP again at t=108 -> window restarts from 108, so at t=115 (7s) still not stable.
    assert bridge.recovery_ready(state, now=115, sample_ts=108, recover_stable=10) is False
    assert state["up_since"] == 108
    # Only once it has been continuously UP for the full 10s (t>=118) does it recover.
    assert bridge.recovery_ready(state, now=118, sample_ts=118, recover_stable=10) is True


def test_interconnect_port_text_prefers_physical_members():
    # With resolved members: show physical ports + alias, aggregate in parens.
    assert bridge._interconnect_port_text("Po4", "to-stage4", ["Gi1/0/4", "Gi1/0/5"]) == \
        "Gi1/0/4、Gi1/0/5 / to-stage4（Po4）"
    # Without members (switch lacks ifStackTable): fall back to the aggregate.
    assert bridge._interconnect_port_text("Po4", "to-stage4", []) == "Po4 / to-stage4"
    assert bridge._interconnect_port_text("Po4", "", []) == "Po4"


def test_fetch_interconnect_members_maps_aggregate_to_member_names(monkeypatch):
    # ifStackTable rows: higher=aggregate ifIndex, lower=member ifIndex; 0 is a
    # top/bottom sentinel and must be ignored.
    stack_rows = [
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "400", "ifStackLowerLayer": "4"}, "value": [0, "1"]},
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "400", "ifStackLowerLayer": "5"}, "value": [0, "1"]},
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "0", "ifStackLowerLayer": "400"}, "value": [0, "1"]},
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "4", "ifStackLowerLayer": "0"}, "value": [0, "1"]},
    ]
    monkeypatch.setattr(bridge, "prometheus_query", lambda q: stack_rows)
    index_names = {
        ("10.0.0.1", "400"): "Po4",
        ("10.0.0.1", "4"): "Gi1/0/4",
        ("10.0.0.1", "5"): "Gi1/0/5",
    }
    members = bridge.fetch_interconnect_members("infra-switch-ifmib", index_names)
    assert members == {("10.0.0.1", "400"): ["Gi1/0/4", "Gi1/0/5"]}
