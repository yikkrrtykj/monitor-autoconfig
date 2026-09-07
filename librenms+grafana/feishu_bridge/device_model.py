"""Pure helpers for selecting concrete device model names."""

import re


GENERIC_DEVICE_MODEL_RE = re.compile(
    r"^(?:c\d+xx\s+stacking|cisco\s+ios|generic|unknown|n/?a|none|not\s+available)$",
    re.IGNORECASE,
)


def clean_device_model(value):
    model = re.sub(r"\s+", " ", str(value or "")).strip()
    if not model or GENERIC_DEVICE_MODEL_RE.fullmatch(model):
        return ""
    if re.fullmatch(r"(?:0x[0-9a-f]+|zeroDotZero|\d+)", model, re.IGNORECASE):
        return ""
    return model


def inventory_device_model(inventory):
    """Return concrete chassis model(s), ignoring ports/PSUs/generic labels."""
    rows = [row for row in (inventory or []) if isinstance(row, dict)]
    groups = [
        [row for row in rows if str(row.get("entPhysicalClass") or "").lower() in ("chassis", "3")],
        [row for row in rows if str(row.get("entPhysicalContainedIn") or "") in ("", "0")],
    ]
    for group in groups:
        models = []
        for row in group:
            model = clean_device_model(row.get("entPhysicalModelName"))
            if model and model not in models:
                models.append(model)
        if models:
            return " / ".join(models)
    return ""


def best_device_model(device):
    for field in ("inventory_model", "hardware", "model"):
        model = clean_device_model(device.get(field))
        if model:
            return model
    return ""
