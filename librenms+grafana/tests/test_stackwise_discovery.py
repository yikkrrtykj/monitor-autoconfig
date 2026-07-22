import importlib.util
from pathlib import Path


_spec = importlib.util.spec_from_file_location(
    "discover_stackwise_targets",
    Path(__file__).resolve().parent.parent / "discover-stackwise-targets.py",
)
disc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(disc)


def test_configured_targets_preserves_names_and_expands_ranges():
    assert disc.configured_targets(
        "core:192.168.10.254,edge:192.168.10.31-32,192.168.10.18"
    ) == {
        "192.168.10.254": "core",
        "192.168.10.31": "edge1",
        "192.168.10.32": "edge2",
        "192.168.10.18": "192.168.10.18",
    }


def test_select_stacks_excludes_standalone_and_keeps_real_stacks():
    candidates = {
        "192.168.10.11": "stack-a",
        "192.168.10.18": "edge-a",
        "192.168.10.252": "stack-b",
    }
    selected, confirmed, retained = disc.select_stacks(
        candidates,
        previous={},
        counts={
            "192.168.10.11": 6,
            "192.168.10.18": 1,
            "192.168.10.252": 2,
        },
    )
    assert selected == {
        "192.168.10.11": "stack-a",
        "192.168.10.252": "stack-b",
    }
    assert confirmed == ["192.168.10.11", "192.168.10.252"]
    assert retained == []


def test_select_stacks_retains_confirmed_stack_during_member_loss_or_timeout():
    candidates = {
        "192.168.10.11": "stack-a",
        "192.168.10.252": "stack-b",
    }
    selected, confirmed, retained = disc.select_stacks(
        candidates,
        previous=candidates.copy(),
        counts={"192.168.10.11": 1, "192.168.10.252": None},
    )
    assert selected == candidates
    assert confirmed == []
    assert retained == ["192.168.10.11", "192.168.10.252"]


def test_select_stacks_does_not_retain_reused_ip_with_a_different_name():
    selected, _, retained = disc.select_stacks(
        {"192.168.10.11": "new-edge"},
        previous={"192.168.10.11": "old-stack"},
        counts={"192.168.10.11": 1},
    )
    assert selected == {}
    assert retained == []


def test_stack_member_count_counts_unique_member_numbers(monkeypatch):
    class Result:
        returncode = 0
        stdout = "1\n2\n2\n"

    monkeypatch.setattr(disc.subprocess, "run", lambda *args, **kwargs: Result())
    assert disc.stack_member_count("192.168.10.11", "public") == 2


def test_file_sd_round_trip(tmp_path):
    path = tmp_path / "stackwise_targets.json"
    payload = disc.build_file_sd({"192.168.10.254": "core", "192.168.10.11": "dist"})
    disc.write_file_sd(str(path), payload)
    assert disc.load_file_sd(str(path)) == {
        "192.168.10.11": "dist",
        "192.168.10.254": "core",
    }
