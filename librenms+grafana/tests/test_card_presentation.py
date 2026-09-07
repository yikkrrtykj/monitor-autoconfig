import copy

from feishu_bridge import card_presentation


def test_card_preview_title_normalizes_whitespace_and_truncates_to_120_characters():
    assert card_presentation.card_preview_title("  Main\n title ", "\tSub   title  ") == "Main title Sub title"
    assert card_presentation.card_preview_title("x" * 121, "") == "x" * 120


def test_make_card_preserves_full_shape_and_extra_element_order_without_mutating_input():
    extra_elements = [
        {"tag": "hr"},
        {"tag": "markdown", "content": "extra"},
    ]
    original_extra_elements = copy.deepcopy(extra_elements)

    card = card_presentation.make_card(
        " Main\nTitle ", " Sub\tTitle ", "orange", "body", extra_elements,
    )

    assert card == {
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
                "title": {"tag": "plain_text", "content": "Main Title Sub Title"},
                "template": "orange",
                "padding": "12px 12px 12px 12px",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "body",
                        "text_align": "left",
                        "text_size": "normal_v2",
                        "margin": "0px 0px 0px 0px",
                    },
                    {"tag": "hr"},
                    {"tag": "markdown", "content": "extra"},
                ],
            },
        },
    }
    assert extra_elements == original_extra_elements


def test_with_event_name_deepcopies_prefixes_once_and_does_not_mutate_input():
    original = {
        "card": {
            "header": {"title": {"content": "Alert"}},
            "body": {"elements": [{"tag": "markdown", "content": "body"}]},
        }
    }
    original_snapshot = copy.deepcopy(original)

    decorated = card_presentation.with_event_name(original, "EWC 上海站")

    assert decorated["card"]["header"]["title"]["content"] == "【EWC 上海站】 Alert"
    assert original == original_snapshot
    assert decorated is not original
    assert decorated["card"] is not original["card"]

    decorated_again = card_presentation.with_event_name(decorated, "EWC 上海站")
    assert decorated_again["card"]["header"]["title"]["content"] == "【EWC 上海站】 Alert"
    assert decorated_again is not decorated


def test_with_event_name_preserves_empty_name_and_malformed_card_semantics():
    original = {"card": {"header": {"title": {"content": "Alert"}}}}
    assert card_presentation.with_event_name(original, "") is original

    malformed = ["card"]
    decorated = card_presentation.with_event_name(malformed, "Event")
    assert decorated == malformed
    assert decorated is not malformed
    assert not hasattr(card_presentation, "EVENT_NAME")
