import importlib.util
from pathlib import Path

from feishu_bridge import device_name


_spec = importlib.util.spec_from_file_location(
    "feishu_bridge_device_name_wrappers",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def test_looks_like_ip_preserves_shape_only_matching_and_direct_input_handling():
    for value in ("1.2.3.4", "192.168.10.1", "999.999.999.999"):
        assert device_name.looks_like_ip(value) is True

    for value in (None, "", " 1.2.3.4 ", "1x2x3x4", "2001:db8::1", 1234):
        assert device_name.looks_like_ip(value) is False


def test_first_non_ip_strips_candidates_preserves_internal_text_and_order():
    assert device_name.first_non_ip("", " 192.168.1.1 ", "123", "  Core  Switch  ", "Backup") == "Core  Switch"
    assert device_name.first_non_ip(" First ", "Second") == "First"
    assert device_name.first_non_ip(None, "  ", 0, False, "42", "10.0.0.1") == ""


def test_first_non_ip_preserves_current_non_string_conversion_semantics():
    assert device_name.first_non_ip(True) == "True"
    assert device_name.first_non_ip(["switch"]) == "['switch']"
    assert device_name.first_non_ip(17, "valid") == "valid"


def test_meaningful_sysname_matches_first_non_ip_contract():
    expected = {
        None: "",
        "": "",
        " 2 ": "",
        " 192.168.71.8 ": "",
        " AVL ": "AVL",
        "Core  Switch": "Core  Switch",
    }
    for value, result in expected.items():
        assert device_name.meaningful_sysname(value) == result


def test_sysname_changed_requires_two_meaningful_case_distinct_names():
    assert device_name.sysname_changed("old-avl", "avl") is True
    assert device_name.sysname_changed("AVL", "avl") is False
    assert device_name.sysname_changed("  AVL  ", "avl") is False
    assert device_name.sysname_changed("2", "avl") is False
    assert device_name.sysname_changed("old-avl", "192.168.71.8") is False
    assert device_name.sysname_changed(None, "avl") is False


def test_bridge_wrappers_preserve_fixed_results_and_watcher_injection():
    assert bridge._looks_like_ip("999.999.999.999") is True
    assert bridge._looks_like_ip(" 1.2.3.4 ") is False
    assert bridge._first_non_ip(" 1.2.3.4 ", "42", " Core ") == "Core"
    assert bridge._meaningful_sysname("  Core  ") == "Core"
    assert bridge._sysname_changed("CORE", "core") is False
    assert bridge._sysname_changed("old", "new") is True
    assert bridge._SYSNAME_WATCHER.meaningful_sysname is bridge._meaningful_sysname
    assert bridge._SYSNAME_WATCHER.sysname_changed is bridge._sysname_changed


def test_bridge_wrappers_delegate_arguments_order_and_return_values(monkeypatch):
    marker = object()
    calls = []
    monkeypatch.setattr(bridge, "_device_name_looks_like_ip", lambda value: calls.append(("ip", value)) or marker)
    monkeypatch.setattr(bridge, "_device_name_first_non_ip", lambda *values: calls.append(("first", values)) or marker)
    monkeypatch.setattr(bridge, "_device_name_meaningful_sysname", lambda value: calls.append(("meaningful", value)) or marker)
    monkeypatch.setattr(bridge, "_device_name_sysname_changed", lambda old, new: calls.append(("changed", old, new)) or marker)

    assert bridge._looks_like_ip("raw") is marker
    assert bridge._first_non_ip("one", "two", "three") is marker
    assert bridge._meaningful_sysname("name") is marker
    assert bridge._sysname_changed("old", "new") is marker
    assert calls == [
        ("ip", "raw"),
        ("first", ("one", "two", "three")),
        ("meaningful", "name"),
        ("changed", "old", "new"),
    ]
