"""Pure LAG member ownership resolution shared by monitoring paths.

SNMP ifStack rows are useful as a compatibility fallback, but some switches
retain active-looking relationships after a port moves to another channel
group.  Direct Cisco/IEEE mappings and an unambiguous LACP admin key therefore
take precedence.  Ambiguous members are deliberately omitted from every
aggregate rather than being allowed to manufacture a degraded-link alert.
"""


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize_aggregate_members(mapping):
    normalized = {}
    for aggregate, members in (mapping or {}).items():
        aggregate_index = _positive_int(aggregate)
        if aggregate_index is None:
            continue
        bucket = normalized.setdefault(aggregate_index, set())
        for member in members or []:
            member_index = _positive_int(member)
            if member_index is not None and member_index != aggregate_index:
                bucket.add(member_index)
    return normalized


def _normalize_direct_mapping(mapping):
    normalized = {}
    for member, aggregate in (mapping or {}).items():
        member_index = _positive_int(member)
        aggregate_index = _positive_int(aggregate)
        if (
            member_index is not None
            and aggregate_index is not None
            and member_index != aggregate_index
        ):
            normalized[member_index] = aggregate_index
    return normalized


def _normalize_keys(mapping):
    normalized = {}
    for index, key in (mapping or {}).items():
        interface_index = _positive_int(index)
        admin_key = _positive_int(key)
        if interface_index is not None and admin_key is not None:
            normalized[interface_index] = admin_key
    return normalized


def resolve_lag_ownership(
    ifstack_claims=None,
    pagp_group_ifindex=None,
    attached_aggregate_id=None,
    aggregate_admin_keys=None,
    physical_admin_keys=None,
):
    """Resolve one device's physical-member ownership deterministically.

    Inputs use SNMP ifIndex values.  The result contains integer indexes:

    ``members_by_aggregate``
        Resolved aggregate -> sorted physical members.
    ``owner_by_member``
        Resolved physical member -> aggregate.
    ``provenance``
        Resolution source for each accepted physical member.
    ``conflicts``
        Rejected members with their reason and candidate aggregates.
    """
    ifstack = _normalize_aggregate_members(ifstack_claims)
    pagp = _normalize_direct_mapping(pagp_group_ifindex)
    attached = _normalize_direct_mapping(attached_aggregate_id)
    aggregate_keys = _normalize_keys(aggregate_admin_keys)
    physical_keys = _normalize_keys(physical_admin_keys)

    ifstack_owners = {}
    for aggregate, members in ifstack.items():
        for member in members:
            ifstack_owners.setdefault(member, set()).add(aggregate)

    aggregates_by_key = {}
    for aggregate, admin_key in aggregate_keys.items():
        aggregates_by_key.setdefault(admin_key, set()).add(aggregate)

    all_members = set(ifstack_owners) | set(pagp) | set(attached) | set(physical_keys)
    owner_by_member = {}
    provenance = {}
    conflicts = {}

    for member in sorted(all_members):
        direct = {}
        if member in pagp:
            direct["pagp"] = pagp[member]
        if member in attached:
            direct["attached"] = attached[member]
        direct_owners = set(direct.values())

        if len(direct_owners) > 1:
            conflicts[member] = {
                "reason": "direct-mapping-conflict",
                "candidates": sorted(direct_owners),
                "sources": direct,
            }
            continue
        if direct_owners:
            owner = next(iter(direct_owners))
            owner_by_member[member] = owner
            provenance[member] = "+".join(sorted(direct))
            continue

        admin_key = physical_keys.get(member)
        key_owners = aggregates_by_key.get(admin_key, set()) if admin_key else set()
        if len(key_owners) == 1:
            owner_by_member[member] = next(iter(key_owners))
            provenance[member] = "admin-key"
            continue

        fallback_owners = ifstack_owners.get(member, set())
        if len(fallback_owners) == 1:
            owner_by_member[member] = next(iter(fallback_owners))
            provenance[member] = "ifstack"
            continue
        if len(fallback_owners) > 1:
            conflicts[member] = {
                "reason": "ambiguous-ifstack",
                "candidates": sorted(fallback_owners),
            }

    members_by_aggregate = {}
    for member, aggregate in owner_by_member.items():
        members_by_aggregate.setdefault(aggregate, []).append(member)
    for members in members_by_aggregate.values():
        members.sort()

    return {
        "members_by_aggregate": members_by_aggregate,
        "owner_by_member": owner_by_member,
        "provenance": provenance,
        "conflicts": conflicts,
    }
