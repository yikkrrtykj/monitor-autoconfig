import importlib.util
from pathlib import Path

from feishu_bridge import alert_reason


_spec = importlib.util.spec_from_file_location(
    "feishu_bridge_alert_reason_wrappers",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def test_errdisable_reason_preserves_all_fixed_mapping_outputs():
    expected = {
        "link-flap": "link-flap（链路频繁抖动）",
        "bpduguard": "bpduguard（BPDU保护触发）",
        "bpdu-guard": "bpdu-guard（BPDU保护触发）",
        "loopback": "loopback（检测到二层环路）",
        "loop-back": "loop-back（检测到二层环路）",
    }
    for value, formatted in expected.items():
        assert alert_reason.format_errdisable_reason(value) == formatted


def test_errdisable_reason_normalizes_only_for_lookup_and_preserves_raw_token():
    assert alert_reason.format_errdisable_reason("  LiNk_FlAp  ") == "LiNk_FlAp（链路频繁抖动）"
    assert alert_reason.format_errdisable_reason("BPDU GUARD") == "BPDU GUARD（BPDU保护触发）"
    assert alert_reason.format_errdisable_reason("Loop_Back") == "Loop_Back（检测到二层环路）"


def test_errdisable_reason_preserves_unknown_empty_and_non_string_semantics():
    assert alert_reason.format_errdisable_reason("storm-control") == "storm-control"
    assert alert_reason.format_errdisable_reason(17) == "17"
    for value in (None, "", "   ", 0, False):
        assert alert_reason.format_errdisable_reason(value) == "未知"


def test_mac_flap_reason_has_fixed_output():
    assert alert_reason.format_mac_flap_reason() == "MAC flap（MAC地址漂移）"


def test_bridge_wrappers_match_fixed_outputs():
    assert bridge.format_errdisable_reason("link_flap") == "link_flap（链路频繁抖动）"
    assert bridge.format_errdisable_reason("custom") == "custom"
    assert bridge.format_mac_flap_reason() == "MAC flap（MAC地址漂移）"


def test_bridge_wrappers_delegate_arguments_and_return_values(monkeypatch):
    marker = object()
    seen = []
    monkeypatch.setattr(
        bridge,
        "_alert_reason_format_errdisable",
        lambda reason: seen.append(reason) or marker,
    )
    monkeypatch.setattr(bridge, "_alert_reason_format_mac_flap", lambda: marker)

    assert bridge.format_errdisable_reason("raw reason") is marker
    assert seen == ["raw reason"]
    assert bridge.format_mac_flap_reason() is marker
