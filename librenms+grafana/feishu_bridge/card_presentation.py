"""Pure presentation helpers for Feishu interactive cards."""

import copy
import re


def card_preview_title(title, subtitle):
    preview = re.sub(r"\s+", " ", f"{title} {subtitle}".strip())
    return preview[:120]


def make_card(title, subtitle, color, body_md, extra_elements=None):
    card = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {
                "style": {
                    "text_size": {
                        "normal_v2": {
                            "default": "normal",
                            "pc": "normal",
                            "mobile": "heading",
                        }
                    }
                }
            },
            "header": {
                "title": {"tag": "plain_text", "content": card_preview_title(title, subtitle)},
                "template": color,
                "padding": "12px 12px 12px 12px",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": body_md,
                        "text_align": "left",
                        "text_size": "normal_v2",
                        "margin": "0px 0px 0px 0px",
                    }
                ],
            },
        },
    }
    if extra_elements:
        card["card"]["body"]["elements"].extend(extra_elements)
    return card


def with_event_name(card, event_name):
    """Prefix a card title with the explicitly supplied event name."""
    if not event_name:
        return card
    decorated = copy.deepcopy(card)
    payload = decorated.get("card") if isinstance(decorated, dict) else None
    header = payload.get("header") if isinstance(payload, dict) else None
    title = header.get("title") if isinstance(header, dict) else None
    if isinstance(title, dict):
        prefix = f"【{event_name}】"
        content = str(title.get("content") or "")
        if not content.startswith(prefix):
            title["content"] = f"{prefix} {content}".strip()
    return decorated
