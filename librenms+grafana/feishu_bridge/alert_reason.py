"""Pure helpers for formatting operator-facing alert reasons."""

import re


def format_errdisable_reason(reason):
    """Keep the switch's raw reason searchable and append an operator-friendly hint."""
    raw = str(reason or "").strip() or "未知"
    normalized = re.sub(r"[\s_]+", "-", raw.lower())
    explanations = {
        "link-flap": "链路频繁抖动",
        "bpduguard": "BPDU保护触发",
        "bpdu-guard": "BPDU保护触发",
        "loopback": "检测到二层环路",
        "loop-back": "检测到二层环路",
    }
    explanation = explanations.get(normalized)
    return f"{raw}（{explanation}）" if explanation else raw


def format_mac_flap_reason():
    """Keep the raw event name visible and append its operator-friendly meaning."""
    return "MAC flap（MAC地址漂移）"
