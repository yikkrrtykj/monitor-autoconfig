"""Pure helpers for selecting and comparing meaningful device names."""

import re


def looks_like_ip(value):
    return bool(re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", str(value or "")))


def first_non_ip(*values):
    for value in values:
        value = str(value or "").strip()
        if value and not looks_like_ip(value) and not re.fullmatch(r"\d+", value):
            return value
    return ""


def meaningful_sysname(value):
    """Return a usable sysName or empty for numeric/IP poll artifacts."""
    return first_non_ip(value)


def sysname_changed(old_name, new_name):
    """Compare only meaningful names, case-insensitively."""
    old_name = meaningful_sysname(old_name)
    new_name = meaningful_sysname(new_name)
    return bool(old_name and new_name and old_name.casefold() != new_name.casefold())
