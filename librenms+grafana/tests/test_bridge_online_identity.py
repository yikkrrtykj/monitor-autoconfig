import threading

import pytest

from feishu_bridge.online_identity import OnlineIdentityService


def make_service(*, initial=(), save_results=(), send_results=(), send=None):
    stored = set(initial)
    loads = []
    saves = []
    sent = []
    pending_save_results = iter(save_results)
    pending_send_results = iter(send_results)

    def load_set(path):
        loads.append(path)
        return set(stored)

    def save_set(path, values):
        result = next(pending_save_results, True)
        saves.append((path, set(values), result))
        if isinstance(result, OSError):
            raise result
        if result:
            stored.clear()
            stored.update(values)
        return result

    def default_send(card):
        sent.append(card)
        return next(pending_send_results, True)

    service = OnlineIdentityService(
        state_file="/state/notified-devices.json",
        load_set=load_set,
        save_set=save_set,
        send=send or default_send,
    )
    return service, {
        "stored": stored,
        "loads": loads,
        "saves": saves,
        "sent": sent,
    }


def test_known_identities_loads_each_time_and_returns_a_copy():
    service, observed = make_service(initial={"switch-1"})

    first = service.known_identities()
    first.add("local-only")
    second = service.known_identities()

    assert first == {"switch-1", "local-only"}
    assert second == {"switch-1"}
    assert observed["loads"] == [
        "/state/notified-devices.json",
        "/state/notified-devices.json",
    ]


def test_known_identities_returns_empty_set_for_empty_persistence():
    service, _observed = make_service()

    assert service.known_identities() == set()


def test_mark_empty_and_already_known_return_false_without_saving():
    service, observed = make_service(initial={"switch-1"})

    assert service.mark_notified("", None, "  ") is False
    assert service.mark_notified(" switch-1 ") is False
    assert observed["saves"] == []


def test_mark_normalizes_and_persists_multiple_new_aliases():
    service, observed = make_service(initial={"existing"})

    assert service.mark_notified(" switch-1 ", "10.0.0.1", "switch-1") is True
    assert observed["stored"] == {"existing", "switch-1", "10.0.0.1"}
    assert observed["saves"] == [(
        "/state/notified-devices.json",
        {"existing", "switch-1", "10.0.0.1"},
        True,
    )]


@pytest.mark.parametrize("failure", [False, OSError("fixture")], ids=["false", "oserror"])
def test_mark_new_identity_keeps_dedupe_until_storage_recovers(failure):
    service, observed = make_service(initial={"existing"}, save_results=[failure, failure, True])

    assert service.mark_notified("switch-1") is False
    assert observed["stored"] == {"existing"}
    assert service.send_once("duplicate", "switch-1") is True
    assert observed["stored"] == {"existing"}
    assert service.known_identities() == {"existing", "switch-1"}
    assert observed["stored"] == {"existing", "switch-1"}
    assert observed["sent"] == []


def test_migrate_rejects_empty_primary_and_unknown_legacy():
    service, observed = make_service(initial={"old-name"})

    assert service.migrate("", "old-name") is False
    assert service.migrate("unifi-ap:aabb", "unknown") is False
    assert observed["saves"] == []


def test_migrate_existing_primary_returns_true_without_saving():
    service, observed = make_service(initial={"unifi-ap:aabb", "old-name"})

    assert service.migrate(" unifi-ap:aabb ", "old-name") is True
    assert observed["saves"] == []


def test_migrate_attaches_primary_without_removing_legacy():
    service, observed = make_service(initial={"old-name"})

    assert service.migrate("unifi-ap:aabb", "missing", " old-name ") is True
    assert observed["stored"] == {"old-name", "unifi-ap:aabb"}


@pytest.mark.parametrize("failure", [False, OSError("fixture")], ids=["false", "oserror"])
def test_migrate_keeps_new_primary_until_storage_recovers(failure):
    service, observed = make_service(initial={"old-name"}, save_results=[failure, failure, True])

    assert service.migrate("unifi-ap:aabb", "old-name") is False
    assert observed["stored"] == {"old-name"}
    assert service.send_once("duplicate", "unifi-ap:aabb") is True
    assert observed["stored"] == {"old-name"}
    assert service.migrate("unifi-ap:aabb", "old-name") is True
    assert observed["stored"] == {"old-name", "unifi-ap:aabb"}
    assert observed["sent"] == []


def test_send_once_empty_identities_returns_false_without_delivery():
    service, observed = make_service()

    assert service.send_once({"card": True}, "", None) is False
    assert observed["sent"] == []


def test_send_once_already_known_returns_true_without_delivery():
    service, observed = make_service(initial={"switch-1"})

    assert service.send_once({"card": True}, "switch-1") is True
    assert observed["sent"] == []
    assert observed["saves"] == []


def test_send_once_partially_known_aliases_are_filled_and_return_true():
    service, observed = make_service(initial={"switch-1"})

    assert service.send_once({"card": True}, "switch-1", "10.0.0.1") is True
    assert observed["sent"] == []
    assert observed["stored"] == {"switch-1", "10.0.0.1"}


def test_send_once_partial_alias_save_failure_still_returns_true():
    service, observed = make_service(
        initial={"switch-1"},
        save_results=[False],
    )

    assert service.send_once({"card": True}, "switch-1", "10.0.0.1") is True
    assert observed["sent"] == []
    assert observed["stored"] == {"switch-1"}


def test_send_once_failure_clears_reservation_and_allows_retry():
    service, observed = make_service(send_results=[False, True])

    assert service.send_once("first", "switch-1") is False
    assert observed["stored"] == set()
    assert service.send_once("retry", "switch-1") is True
    assert observed["sent"] == ["first", "retry"]
    assert observed["stored"] == {"switch-1"}


def test_send_once_success_returns_final_persistence_result():
    service, observed = make_service(save_results=[False])

    assert service.send_once("card", "switch-1") is False
    assert observed["sent"] == ["card"]
    assert observed["stored"] == set()


def test_concurrent_same_identity_allows_only_one_delivery():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def send(card):
        calls.append(card)
        entered.set()
        assert release.wait(3)
        return True

    service, observed = make_service(send=send)
    results = {}
    first = threading.Thread(
        target=lambda: results.setdefault("first", service.send_once("first", "switch-1"))
    )
    first.start()
    assert entered.wait(1)

    second_done = threading.Event()

    def run_second():
        results["second"] = service.send_once("second", "switch-1")
        second_done.set()

    second = threading.Thread(target=run_second)
    second.start()
    completed_while_first_was_sending = second_done.wait(1)
    release.set()
    first.join(2)
    second.join(2)

    assert completed_while_first_was_sending is True
    assert results == {"second": False, "first": True}
    assert calls == ["first"]
    assert observed["stored"] == {"switch-1"}


def test_overlapping_multi_identity_reservation_blocks_duplicate_delivery():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def send(card):
        calls.append(card)
        entered.set()
        assert release.wait(3)
        return True

    service, observed = make_service(send=send)
    result = []
    first = threading.Thread(
        target=lambda: result.append(service.send_once("first", "name", "10.0.0.1"))
    )
    first.start()
    assert entered.wait(1)
    try:
        assert service.send_once("overlap", "10.0.0.1", "alias") is False
    finally:
        release.set()
    first.join(2)

    assert result == [True]
    assert calls == ["first"]
    assert observed["stored"] == {"name", "10.0.0.1"}


def test_delivery_callback_can_read_ledger_without_deadlock():
    callback_finished = []
    service = None

    def send(_card):
        done = threading.Event()

        def read_ledger():
            callback_finished.append(service.known_identities())
            done.set()

        reader = threading.Thread(target=read_ledger)
        reader.start()
        assert done.wait(1)
        reader.join(1)
        return True

    service, observed = make_service(send=send)

    assert service.send_once("card", "switch-1") is True
    assert callback_finished == [set()]
    assert observed["stored"] == {"switch-1"}


def test_new_lifecycle_ignores_historical_dedupe_and_persists_identity():
    service, observed = make_service(initial={"switch-1"})

    assert service.send_new_lifecycle("card", "switch-1", "10.0.0.1") is True
    assert observed["sent"] == ["card"]
    assert observed["stored"] == {"switch-1", "10.0.0.1"}


@pytest.mark.parametrize("first_method, second_method", [
    ("send_once", "send_new_lifecycle"),
    ("send_new_lifecycle", "send_once"),
    ("send_new_lifecycle", "send_new_lifecycle"),
])
def test_new_lifecycle_shares_inflight_reservation(first_method, second_method):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def send(card):
        calls.append(card)
        if card == "first":
            entered.set()
            assert release.wait(3)
        return True

    service, observed = make_service(send=send)
    result = []
    first = threading.Thread(
        target=lambda: result.append(
            getattr(service, first_method)("first", "switch-1")
        )
    )
    first.start()
    try:
        assert entered.wait(1)
        assert getattr(service, second_method)("overlap", "switch-1") is False
    finally:
        release.set()
        first.join(2)

    assert result == [True]
    assert calls == ["first"]
    assert observed["stored"] == {"switch-1"}


def test_new_lifecycle_send_failure_does_not_persist():
    service, observed = make_service(send_results=[False])

    assert service.send_new_lifecycle("card", "switch-1") is False
    assert observed["sent"] == ["card"]
    assert observed["stored"] == set()


@pytest.mark.parametrize("failure", [False, OSError("fixture")], ids=["false", "oserror"])
def test_new_lifecycle_delivery_survives_persistence_failure(failure):
    service, observed = make_service(save_results=[failure, failure, True])

    assert service.send_new_lifecycle("card", "switch-1") is True
    assert observed["stored"] == set()
    assert service.send_once("duplicate", "switch-1") is True
    assert observed["stored"] == set()
    assert service.known_identities() == {"switch-1"}
    assert observed["stored"] == {"switch-1"}
    assert observed["sent"] == ["card"]





def test_delivered_save_failure_retries_storage_without_redelivery(caplog):
    service, observed = make_service(save_results=[False, False, True])
    assert service.send_once("first", "switch-1", "10.0.0.1") is False
    assert service.known_identities() == {"switch-1", "10.0.0.1"}
    assert service.send_once("duplicate", "switch-1", "alias") is True
    assert observed["sent"] == ["first"]
    assert observed["stored"] == {"switch-1", "10.0.0.1", "alias"}
    assert "persistence failed" in caplog.text
    restarted, after = make_service(initial=observed["stored"])
    assert restarted.send_once("again", "10.0.0.1") is True
    assert after["sent"] == []


def test_pending_capacity_defers_new_delivery():
    service, observed = make_service(save_results=[False])
    service._pending_limit = 1
    assert service.send_once("first", "one") is False
    assert service.send_once("second", "two") is False
    assert observed["sent"] == ["first"]


@pytest.mark.parametrize("method", ["send_once", "send_new_lifecycle"])
def test_send_exception_releases_reservation(method):
    calls = []
    def send(card):
        calls.append(card)
        if len(calls) == 1:
            raise RuntimeError("fixture")
        return True
    service, _ = make_service(send=send)
    with pytest.raises(RuntimeError):
        getattr(service, method)("first", "one")
    assert service.send_once("retry", "one") is True
    assert calls == ["first", "retry"]



def test_same_identity_retry_flushes_pending_without_resending():
    service, observed = make_service(save_results=[False, True])
    assert service.send_once("first", "one") is False
    assert service.send_once("retry", "one") is True
    assert observed["sent"] == ["first"]
    assert observed["stored"] == {"one"}


def test_save_oserror_keeps_delivered_identity_and_warns(caplog):
    service, observed = make_service()
    original = service.save_set
    service.save_set = lambda *args: (_ for _ in ()).throw(OSError("sensitive fixture detail"))
    assert service.send_once("first", "one") is False
    assert service.send_once("retry", "one") is True
    assert observed["sent"] == ["first"]
    assert "persistence failed" in caplog.text
    assert "sensitive fixture detail" not in caplog.text
    service.save_set = original
    assert service.known_identities() == {"one"}
    assert observed["stored"] == {"one"}



def test_failed_alias_save_prevents_alias_only_redelivery():
    service, observed = make_service(initial={"name"}, save_results=[False, False, True])
    assert service.send_once("known", "name", "alias") is True
    assert service.send_once("alias-only", "alias") is True
    assert observed["sent"] == []
    assert service.known_identities() == {"name", "alias"}
    assert observed["stored"] == {"name", "alias"}


def test_alias_pending_capacity_is_bounded():
    service, observed = make_service(initial={"name"}, save_results=[False])
    service._pending_limit = 1
    assert service.send_once("known", "name", "alias") is True
    assert service.send_once("overflow", "name", "another") is False
    assert service._pending == {"alias"}
    assert observed["sent"] == []


def test_pending_capacity_defers_mark_migration_and_lifecycle(caplog):
    service, observed = make_service(initial={"legacy"}, save_results=[False, True])
    service._pending_limit = 1
    assert service.mark_notified("pending") is False

    assert service.mark_notified("untracked-mark") is False
    assert service.migrate("untracked-primary", "legacy") is False
    assert service.send_new_lifecycle("overflow", "untracked-lifecycle") is False
    assert observed["sent"] == []
    assert observed["stored"] == {"legacy"}
    assert service.known_identities() == {"legacy", "pending"}
    assert service.send_new_lifecycle("later", "untracked-lifecycle") is True
    assert observed["sent"] == ["later"]
    assert observed["stored"] == {"legacy", "pending", "untracked-lifecycle"}
    assert "pending identity capacity reached" in caplog.text
    assert "untracked-" not in caplog.text
