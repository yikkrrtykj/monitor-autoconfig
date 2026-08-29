from lag_ownership import resolve_lag_ownership


def test_pagp_direct_mapping_overrides_contradictory_ifstack():
    result = resolve_lag_ownership(
        ifstack_claims={47: [10], 48: [10]},
        pagp_group_ifindex={10: 47},
    )

    assert result["members_by_aggregate"] == {47: [10]}
    assert result["provenance"] == {10: "pagp"}


def test_attached_aggregate_mapping_overrides_contradictory_ifstack():
    result = resolve_lag_ownership(
        ifstack_claims={47: [11], 183: [11]},
        attached_aggregate_id={11: 183},
    )

    assert result["members_by_aggregate"] == {183: [11]}
    assert result["provenance"] == {11: "attached"}


def test_detached_lacp_member_uses_unique_admin_key():
    result = resolve_lag_ownership(
        ifstack_claims={47: [11], 183: [11, 30]},
        attached_aggregate_id={11: 0, 30: 0},
        aggregate_admin_keys={47: 2, 183: 3},
        physical_admin_keys={11: 3, 30: 3},
    )

    assert result["members_by_aggregate"] == {183: [11, 30]}
    assert result["provenance"] == {11: "admin-key", 30: "admin-key"}


def test_duplicate_aggregate_admin_key_does_not_guess_owner():
    result = resolve_lag_ownership(
        ifstack_claims={183: [11], 187: [11]},
        aggregate_admin_keys={183: 3, 187: 3},
        physical_admin_keys={11: 3},
    )

    assert result["members_by_aggregate"] == {}
    assert result["conflicts"][11]["reason"] == "ambiguous-ifstack"


def test_conflicting_direct_sources_isolate_member():
    result = resolve_lag_ownership(
        ifstack_claims={47: [11], 183: [11]},
        pagp_group_ifindex={11: 47},
        attached_aggregate_id={11: 183},
    )

    assert result["members_by_aggregate"] == {}
    assert result["conflicts"][11] == {
        "reason": "direct-mapping-conflict",
        "candidates": [47, 183],
        "sources": {"pagp": 47, "attached": 183},
    }


def test_ambiguous_ifstack_without_authoritative_data_is_isolated():
    result = resolve_lag_ownership(ifstack_claims={47: [10], 48: [10]})

    assert result["members_by_aggregate"] == {}
    assert result["conflicts"][10]["reason"] == "ambiguous-ifstack"


def test_single_ifstack_owner_remains_backward_compatible():
    result = resolve_lag_ownership(ifstack_claims={47: [10, 29]})

    assert result["members_by_aggregate"] == {47: [10, 29]}
    assert result["provenance"] == {10: "ifstack", 29: "ifstack"}


def test_36430_ownership_is_resolved_without_unioning_stale_claims():
    result = resolve_lag_ownership(
        ifstack_claims={47: [10, 11, 29], 183: [11, 30]},
        pagp_group_ifindex={10: 47, 29: 47},
        attached_aggregate_id={11: 0, 30: 0},
        aggregate_admin_keys={47: 2, 183: 3},
        physical_admin_keys={11: 3, 30: 3},
    )

    assert result["members_by_aggregate"] == {
        47: [10, 29],
        183: [11, 30],
    }
    assert result["conflicts"] == {}
