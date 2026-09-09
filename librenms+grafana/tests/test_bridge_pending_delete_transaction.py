import importlib.util
import json
import tempfile
import threading
import unittest
import sys
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SPEC = importlib.util.spec_from_file_location(
    "pending_delete_transaction_bridge",
    Path(__file__).resolve().parents[1] / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class PendingDeleteTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = "infra-dist-ping|192.0.2.27"
        self.state = {
            "job": "infra-dist-ping",
            "ip": "192.0.2.27",
            "name": "test-switch",
            "pending_delete": True,
            "pending_token": " token-with-whitespace ",
            "pending_since": 100,
            "down_since": 10,
            "alerting": True,
            "retired": False,
            "seen_up": True,
        }
        bridge.DEVICE_PENDING_DELETE_ENABLED = True
        bridge.MANUAL_DELETE_GUARDS_READY = True
        bridge.MANUAL_DELETE_GUARD_DIR = str(Path(self.temp.name) / "guards")
        bridge.DEVICE_DOWN_STATE_FILE = str(Path(self.temp.name) / "device-state.json")
        bridge.LIBRENMS_URL = "http://librenms.test"
        bridge.DEVICE_DOWN_STATES = {self.key: self.state}
        bridge.MANUAL_DELETE_OPERATIONS.clear()
        bridge.MANUAL_DELETE_BY_KEY.clear()
        bridge.MANUAL_DELETE_BY_IP.clear()
        bridge.MANUAL_DELETE_GUARDS.clear()

    def run_delete(self, prom="UNKNOWN", probe=False, device_id="42", delete="deleted"):
        with mock.patch.object(bridge, "_pending_delete_target_status", return_value=prom), \
             mock.patch.object(
                 bridge, "_manual_delete_inventory",
                 return_value=("secret", device_id, {"device_id": device_id, "ip": "192.0.2.27"})
                 if device_id else ("secret", "", None),
             ), mock.patch.object(bridge, "_blackbox_icmp_probe", return_value=probe), \
             mock.patch.object(bridge, "_manual_delete_exact_id", return_value=delete) as remove:
            result = bridge.resolve_pending_delete(
                self.key, "delete", " token-with-whitespace ",
            )
        return result, remove

    def test_unknown_prometheus_can_delete_only_after_final_offline_probe(self):
        result, remove = self.run_delete()
        self.assertTrue(result["ok"])
        remove.assert_called_once()
        args = remove.call_args.args
        self.assertEqual(args[:2], ("secret", "42"))
        expected = {
            "pending_delete": False, "pending_since": None, "pending_token": "",
            "pending_notified": False, "pending_last_notified": None,
            "pending_event_title": "", "alerting": False, "retired": True,
            "down_since": None, "up_since": None, "seen_up": False,
            "online_sent": False, "online_pending": False,
            "librenms_deleted": True, "librenms_readded": False,
            "librenms_sync_last_attempt": 0, "pending_snoozed_until": None,
        }
        self.assertEqual({key: self.state.get(key) for key in expected}, expected)
        self.assertIsInstance(self.state["retired_at"], (int, float))
        guards = list(Path(bridge.MANUAL_DELETE_GUARD_DIR).glob("*.json"))
        self.assertEqual(len(guards), 1)
        raw = guards[0].read_text(encoding="utf-8")
        self.assertNotIn("token-with-whitespace", raw)
        guard = json.loads(raw)
        self.assertEqual(guard["result"], "deleted")
        self.assertTrue(guard["state_applied"])

    def test_online_or_unknown_blackbox_preserves_pending_and_never_deletes(self):
        for probe in (True, None, 0, "false"):
            with self.subTest(probe=probe):
                before = dict(self.state)
                result, remove = self.run_delete(probe=probe)
                self.assertFalse(result["ok"])
                remove.assert_not_called()
                self.assertEqual(self.state, before)

    def test_prometheus_online_clears_only_same_generation(self):
        before = dict(self.state)
        with mock.patch.object(bridge, "_pending_delete_target_status", return_value="ONLINE"), \
             mock.patch.object(bridge, "_manual_delete_inventory") as inventory:
            result = bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
        self.assertFalse(result["ok"])
        self.assertFalse(self.state["pending_delete"])
        inventory.assert_not_called()
        self.assertEqual(before["pending_token"], " token-with-whitespace ")

    def test_prometheus_online_from_old_request_does_not_clear_new_generation(self):
        def online_after_replacement(*_args):
            self.state["pending_token"] = "new-generation"
            self.state["pending_since"] = 200
            return "ONLINE"

        with mock.patch.object(
            bridge, "_pending_delete_target_status", side_effect=online_after_replacement,
        ), mock.patch.object(bridge, "_manual_delete_inventory") as inventory:
            result = bridge.resolve_pending_delete(
                self.key, "delete", " token-with-whitespace ",
            )
        self.assertFalse(result["ok"])
        self.assertTrue(self.state["pending_delete"])
        self.assertEqual(self.state["pending_token"], "new-generation")
        inventory.assert_not_called()

    def test_second_request_is_rejected_before_inventory_or_probe(self):
        entered = threading.Event()
        release = threading.Event()
        inventory_calls = []

        def status(*_args):
            entered.set()
            release.wait(2)
            return "UNKNOWN"

        def inventory(operation):
            inventory_calls.append(operation["operation_id"])
            return "secret", "42", {"device_id": 42, "ip": "192.0.2.27"}

        with mock.patch.object(bridge, "_pending_delete_target_status", side_effect=status), \
             mock.patch.object(bridge, "_manual_delete_inventory", side_effect=inventory), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", return_value=True):
            first = []
            thread = threading.Thread(
                target=lambda: first.append(bridge.resolve_pending_delete(
                    self.key, "delete", self.state["pending_token"],
                ))
            )
            thread.start()
            self.assertTrue(entered.wait(1))
            second = bridge.resolve_pending_delete(
                self.key, "delete", self.state["pending_token"],
            )
            release.set()
            thread.join(2)
        self.assertFalse(second["ok"])
        self.assertEqual(len(inventory_calls), 1)

    def test_same_ip_different_keys_use_one_destructive_operation(self):
        other_key = "infra-core-ping|192.0.2.27"
        other = dict(self.state, job="infra-core-ping", pending_token="other-token")
        bridge.DEVICE_DOWN_STATES[other_key] = other
        start = threading.Barrier(3)
        deletes = []
        results = []

        def invoke(key, token):
            start.wait()
            results.append(bridge.resolve_pending_delete(key, "delete", token))

        with mock.patch.object(bridge, "_pending_delete_target_status", return_value="OFFLINE"), \
             mock.patch.object(
                 bridge, "_manual_delete_inventory",
                 return_value=("secret", "42", {"device_id": 42, "ip": "192.0.2.27"}),
             ), mock.patch.object(bridge, "_blackbox_icmp_probe", return_value=False), \
             mock.patch.object(
                 bridge, "_manual_delete_exact_id",
                 side_effect=lambda *args: deletes.append(args) or "deleted",
             ):
            threads = [
                threading.Thread(target=invoke, args=(self.key, self.state["pending_token"])),
                threading.Thread(target=invoke, args=(other_key, other["pending_token"])),
            ]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(2)
        self.assertEqual(len(deletes), 1)
        self.assertEqual(sum(bool(item["ok"]) for item in results), 1)

    def test_keep_cancels_checking_and_prevents_late_delete(self):
        entered = threading.Event()
        release = threading.Event()
        deletes = []

        def status(*_args):
            entered.set()
            release.wait(2)
            return "OFFLINE"

        with mock.patch.object(bridge, "_pending_delete_target_status", side_effect=status), \
             mock.patch.object(bridge, "_manual_delete_inventory", return_value=("secret", "42", {})), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", return_value=False), \
             mock.patch.object(bridge, "_manual_delete_exact_id", side_effect=lambda *a: deletes.append(a)):
            first = []
            thread = threading.Thread(target=lambda: first.append(
                bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
            ))
            thread.start()
            self.assertTrue(entered.wait(1))
            kept = bridge.resolve_pending_delete(self.key, "keep", self.state["pending_token"])
            release.set()
            thread.join(2)
        self.assertTrue(kept["ok"])
        self.assertEqual(deletes, [])
        self.assertFalse(first[0]["ok"])

    def test_keep_refuses_committed_or_unresolved_operation(self):
        with bridge.RETIRE_LOCK:
            operation, reason = bridge._new_manual_delete_operation(
                self.key, self.state, self.state["pending_token"],
            )
            self.assertEqual(reason, "")
            operation["phase"] = "COMMITTED"
        before = dict(self.state)
        result = bridge.resolve_pending_delete(self.key, "keep", self.state["pending_token"])
        self.assertFalse(result["ok"])
        self.assertEqual(self.state, before)

    def test_guard_write_failure_means_zero_delete(self):
        with mock.patch.object(bridge, "_pending_delete_target_status", return_value="OFFLINE"), \
             mock.patch.object(bridge, "_manual_delete_inventory", return_value=("secret", "42", {})), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", return_value=False), \
             mock.patch.object(bridge, "_durable_write_json", side_effect=OSError("disk full")), \
             mock.patch.object(bridge, "_manual_delete_exact_id") as remove:
            result = bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
        self.assertFalse(result["ok"])
        remove.assert_not_called()
        self.assertTrue(self.state["pending_delete"])

    def test_state_write_failure_after_delete_keeps_unresolved_guard(self):
        calls = []
        with mock.patch.object(bridge, "_pending_delete_target_status", return_value="OFFLINE"), \
             mock.patch.object(bridge, "_manual_delete_inventory", return_value=("secret", "42", {})), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", return_value=False), \
             mock.patch.object(
                 bridge, "_manual_delete_exact_id",
                 side_effect=lambda *args: calls.append(args) or "deleted",
             ), mock.patch.object(
                 bridge, "save_device_down_states_durable", side_effect=OSError("disk full"),
             ):
            first = bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
        self.assertFalse(first["ok"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(
            guard["result"] == "deleted" and not guard["state_applied"]
            for guard in bridge.MANUAL_DELETE_GUARDS.values()
        ))
        second = bridge.resolve_pending_delete(self.key, "delete", " token-with-whitespace ")
        self.assertFalse(second["ok"])
        self.assertEqual(len(calls), 1)

    def test_result_receipt_failure_after_delete_never_retries(self):
        real_persist = bridge._persist_manual_delete_guard
        writes = []
        deletes = []

        def persist(guard):
            writes.append(guard["phase"])
            if len(writes) > 1:
                raise OSError("receipt failed")
            return real_persist(guard)

        with mock.patch.object(bridge, "_pending_delete_target_status", return_value="OFFLINE"), \
             mock.patch.object(bridge, "_manual_delete_inventory", return_value=("secret", "42", {})), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", return_value=False), \
             mock.patch.object(
                 bridge, "_manual_delete_exact_id",
                 side_effect=lambda *args: deletes.append(args) or "deleted",
             ), mock.patch.object(bridge, "_persist_manual_delete_guard", side_effect=persist):
            result = bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
        self.assertFalse(result["ok"])
        self.assertEqual(len(deletes), 1)
        self.assertTrue(bridge._manual_delete_guard_blocks_device("42", "192.0.2.27"))

    def test_ambiguous_delete_outcome_blocks_manual_retry_and_auto_delete(self):
        with mock.patch.object(bridge, "_pending_delete_target_status", return_value="OFFLINE"), \
             mock.patch.object(bridge, "_manual_delete_inventory", return_value=("secret", "42", {})), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", return_value=False), \
             mock.patch.object(bridge, "_manual_delete_exact_id", side_effect=TimeoutError):
            first = bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
        self.assertFalse(first["ok"])
        second = bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
        self.assertFalse(second["ok"])
        self.assertTrue(bridge._manual_delete_guard_blocks_device("42", "192.0.2.27"))
        self.assertFalse(bridge._manual_delete_guard_blocks_device("99", "192.0.2.27"))

    def test_corrupt_guard_store_fails_closed(self):
        path = Path(bridge.MANUAL_DELETE_GUARD_DIR)
        path.mkdir(parents=True)
        (path / "broken.json").write_text("{", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            bridge.initialize_manual_delete_safety()

    def test_unknown_schema_or_raw_token_field_fails_closed(self):
        base = {
            "schema_version": 2, "operation_id": "f" * 32, "key": self.key,
            "pending_generation": bridge._pending_generation(self.state["pending_token"]),
            "ip": "192.0.2.27", "job": "infra-dist-ping", "device_id": "42",
            "phase": "OUTCOME_UNKNOWN", "committed_at": 1.0, "updated_at": 1.0,
            "result": "", "state_applied": False, "failure_kind": "timeout",
        }
        with self.assertRaises(RuntimeError):
            bridge._validate_manual_delete_guard(base)
        base["schema_version"] = 1
        base["pending_token"] = "must-never-be-stored"
        with self.assertRaises(RuntimeError):
            bridge._validate_manual_delete_guard(base)

    def test_durable_writer_flushes_and_fsyncs_file_replace_and_directory(self):
        events = []

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def write(self, value):
                events.append(("write", value))

            def flush(self):
                events.append(("flush",))

            def fileno(self):
                return 10

        with mock.patch.object(bridge.os, "name", "posix"), \
             mock.patch.object(bridge.os, "makedirs"), \
             mock.patch.object(bridge.os, "chmod"), \
             mock.patch.object(bridge.os, "open", side_effect=[10, 11]), \
             mock.patch.object(bridge.os, "fdopen", return_value=Stream()), \
             mock.patch.object(bridge.os, "replace", side_effect=lambda *a: events.append(("replace",))), \
             mock.patch.object(bridge.os, "fsync", side_effect=lambda fd: events.append(("fsync", fd))), \
             mock.patch.object(bridge.os, "close", side_effect=lambda fd: events.append(("close", fd))):
            bridge._durable_write_text("/state/guards/op.json", "payload")
        self.assertEqual(
            events,
            [("write", "payload"), ("flush",), ("fsync", 10), ("replace",),
             ("fsync", 11), ("close", 11)],
        )

    def test_generation_change_during_final_probe_prevents_commit(self):
        def change_generation(_ip, timeout=None):
            self.state["pending_token"] = "replacement-generation"
            return False

        with mock.patch.object(bridge, "_pending_delete_target_status", return_value="UNKNOWN"), \
             mock.patch.object(bridge, "_manual_delete_inventory", return_value=("secret", "42", {})), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", side_effect=change_generation), \
             mock.patch.object(bridge, "_manual_delete_exact_id") as remove:
            result = bridge.resolve_pending_delete(
                self.key, "delete", " token-with-whitespace ",
            )
        self.assertFalse(result["ok"])
        remove.assert_not_called()

    def test_key_object_and_identity_changes_before_commit_all_prevent_delete(self):
        mutators = {
            "key removed": lambda: bridge.DEVICE_DOWN_STATES.pop(self.key),
            "object replaced": lambda: bridge.DEVICE_DOWN_STATES.__setitem__(self.key, dict(self.state)),
            "ip changed": lambda: self.state.__setitem__("ip", "192.0.2.99"),
            "job changed": lambda: self.state.__setitem__("job", "infra-core-ping"),
        }
        for label, mutate in mutators.items():
            with self.subTest(label=label):
                self.setUp()

                def final_probe(_ip, timeout=None):
                    mutate()
                    return False

                with mock.patch.object(bridge, "_pending_delete_target_status", return_value="UNKNOWN"), \
                     mock.patch.object(bridge, "_manual_delete_inventory", return_value=("secret", "42", {})), \
                     mock.patch.object(bridge, "_blackbox_icmp_probe", side_effect=final_probe), \
                     mock.patch.object(bridge, "_manual_delete_exact_id") as remove:
                    result = bridge.resolve_pending_delete(
                        self.key, "delete", " token-with-whitespace ",
                    )
                self.assertFalse(result["ok"])
                remove.assert_not_called()

    def test_deadline_expiry_before_commit_means_zero_delete(self):
        def expire(_ip, timeout=None):
            operation = next(iter(bridge.MANUAL_DELETE_OPERATIONS.values()))
            operation["deadline"] = bridge.time.monotonic() - 1
            return False

        with mock.patch.object(bridge, "_pending_delete_target_status", return_value="UNKNOWN"), \
             mock.patch.object(bridge, "_manual_delete_inventory", return_value=("secret", "42", {})), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", side_effect=expire), \
             mock.patch.object(bridge, "_manual_delete_exact_id") as remove:
            result = bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
        self.assertFalse(result["ok"])
        remove.assert_not_called()
        self.assertEqual(list(Path(bridge.MANUAL_DELETE_GUARD_DIR).glob("*.json")), [])

    def test_deadline_after_guard_before_dispatch_is_retryable_without_delete(self):
        real_persist = bridge._persist_manual_delete_guard
        writes = []

        def persist(guard):
            real_persist(guard)
            writes.append(guard["phase"])
            if len(writes) == 1:
                next(iter(bridge.MANUAL_DELETE_OPERATIONS.values()))["deadline"] = (
                    bridge.time.monotonic() - 1
                )

        with mock.patch.object(bridge, "_pending_delete_target_status", return_value="OFFLINE"), \
             mock.patch.object(bridge, "_manual_delete_inventory", return_value=("secret", "42", {})), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", return_value=False), \
             mock.patch.object(bridge, "_persist_manual_delete_guard", side_effect=persist), \
             mock.patch.object(bridge, "_manual_delete_exact_id") as remove:
            result = bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
        self.assertFalse(result["ok"])
        remove.assert_not_called()
        self.assertEqual(writes, ["DELETE_MAY_HAVE_BEEN_DISPATCHED", "FAILED_NOT_DISPATCHED"])
        self.assertFalse(any(bridge._guard_is_unresolved(item) for item in bridge.MANUAL_DELETE_GUARDS.values()))

    def test_unresolved_guard_blocks_auto_delete_by_id(self):
        guard = {
            "schema_version": 1, "operation_id": "a" * 32,
            "key": self.key,
            "pending_generation": bridge._pending_generation(self.state["pending_token"]),
            "ip": "192.0.2.27", "job": "infra-dist-ping", "device_id": "42",
            "phase": "OUTCOME_UNKNOWN", "committed_at": 1.0, "updated_at": 1.0,
            "result": "", "state_applied": False, "failure_kind": "TimeoutError",
        }
        bridge.MANUAL_DELETE_GUARDS[guard["operation_id"]] = guard
        bridge.DEVICE_AUTO_DELETE_ENABLED = True
        bridge.DEVICE_AUTO_DELETE_DRY_RUN = False
        bridge.DEVICE_AUTO_DELETE_AFTER_SECONDS = 1
        calls = []
        device = {
            "device_id": 42, "hostname": "192.0.2.27", "ip": "192.0.2.27",
            "status": 0, "disabled": 0, "last_polled": "2000-01-01 00:00:00",
        }
        with mock.patch.object(bridge, "device_auto_delete_protected_ips", return_value=set()), \
             mock.patch.object(bridge, "notify_device_auto_delete_summary", return_value=False):
            stats = bridge.run_device_auto_delete_cycle(
                now=2_000_000_000, devices=[device], token="secret",
                probe=lambda ip: calls.append(("probe", ip)) or False,
                delete=lambda token, item: calls.append(("delete", item["device_id"])) or True,
            )
        self.assertEqual(calls, [])
        self.assertEqual(stats["deleted"], 0)

    def test_unresolved_guard_without_id_blocks_auto_delete_by_ip(self):
        bridge.MANUAL_DELETE_GUARDS["a" * 32] = {
            "schema_version": 1, "operation_id": "a" * 32, "key": self.key,
            "pending_generation": bridge._pending_generation(self.state["pending_token"]),
            "ip": "192.0.2.27", "job": "infra-dist-ping", "device_id": "",
            "phase": "COMMITTED", "committed_at": 1.0, "updated_at": 1.0,
            "result": "", "state_applied": False, "failure_kind": "",
        }
        self.assertTrue(bridge._manual_delete_guard_blocks_device("99", "192.0.2.27"))

    def test_old_guard_id_does_not_block_reused_ip_with_new_id(self):
        guard = {
            "schema_version": 1, "operation_id": "b" * 32,
            "key": self.key,
            "pending_generation": bridge._pending_generation(self.state["pending_token"]),
            "ip": "192.0.2.27", "job": "infra-dist-ping", "device_id": "42",
            "phase": "OUTCOME_UNKNOWN", "committed_at": 1.0, "updated_at": 1.0,
            "result": "", "state_applied": False, "failure_kind": "TimeoutError",
        }
        bridge.MANUAL_DELETE_GUARDS[guard["operation_id"]] = guard
        self.assertFalse(bridge._manual_delete_guard_blocks_device("99", "192.0.2.27"))

    def test_unguarded_auto_delete_behavior_is_unchanged(self):
        bridge.DEVICE_AUTO_DELETE_ENABLED = True
        bridge.DEVICE_AUTO_DELETE_DRY_RUN = False
        bridge.DEVICE_AUTO_DELETE_AFTER_SECONDS = 1
        calls = []
        device = {
            "device_id": 99, "hostname": "192.0.2.27", "ip": "192.0.2.27",
            "status": 0, "disabled": 0, "last_polled": "2000-01-01 00:00:00",
        }
        with mock.patch.object(bridge, "device_auto_delete_protected_ips", return_value=set()), \
             mock.patch.object(bridge, "notify_device_auto_delete_summary", return_value=False):
            stats = bridge.run_device_auto_delete_cycle(
                now=2_000_000_000, devices=[device], token="secret",
                probe=lambda ip: calls.append(("probe", ip)) or False,
                delete=lambda token, item: calls.append(("delete", item["device_id"])) or True,
            )
        self.assertEqual(calls, [("probe", "192.0.2.27"), ("delete", 99)])
        self.assertEqual(stats["deleted"], 1)

    def test_startup_reconciliation_missing_id_never_repeats_delete(self):
        guard = {
            "schema_version": 1, "operation_id": "c" * 32,
            "key": self.key,
            "pending_generation": bridge._pending_generation(self.state["pending_token"]),
            "ip": "192.0.2.27", "job": "infra-dist-ping", "device_id": "42",
            "phase": "DELETE_MAY_HAVE_BEEN_DISPATCHED", "committed_at": 1.0,
            "updated_at": 1.0, "result": "", "state_applied": False,
            "failure_kind": "",
        }
        bridge.MANUAL_DELETE_GUARDS[guard["operation_id"]] = guard
        client = mock.Mock()
        client.list_devices.return_value = []
        with mock.patch.object(bridge, "_librenms_token", return_value="secret"), \
             mock.patch.object(bridge, "_librenms_client", return_value=client), \
             mock.patch.object(bridge, "_manual_delete_exact_id") as remove:
            bridge.reconcile_manual_delete_guards()
        remove.assert_not_called()
        self.assertTrue(self.state["retired"])
        self.assertTrue(bridge.MANUAL_DELETE_GUARDS[guard["operation_id"]]["state_applied"])

    def test_success_receipt_reapplies_same_pending_generation_without_delete(self):
        guard = {
            "schema_version": 1, "operation_id": "e" * 32,
            "key": self.key,
            "pending_generation": bridge._pending_generation(self.state["pending_token"]),
            "ip": "192.0.2.27", "job": "infra-dist-ping", "device_id": "42",
            "phase": "SUCCEEDED", "committed_at": 1.0, "updated_at": 1.0,
            "result": "deleted", "state_applied": True, "failure_kind": "",
        }
        bridge.MANUAL_DELETE_GUARDS[guard["operation_id"]] = guard
        with mock.patch.object(bridge, "_manual_delete_exact_id") as remove:
            bridge.reconcile_manual_delete_guards()
        remove.assert_not_called()
        self.assertTrue(self.state["retired"])
        self.assertFalse(self.state["pending_delete"])

    def test_startup_reconciliation_new_id_same_ip_is_superseded(self):
        guard = {
            "schema_version": 1, "operation_id": "d" * 32,
            "key": self.key,
            "pending_generation": bridge._pending_generation(self.state["pending_token"]),
            "ip": "192.0.2.27", "job": "infra-dist-ping", "device_id": "42",
            "phase": "OUTCOME_UNKNOWN", "committed_at": 1.0, "updated_at": 1.0,
            "result": "", "state_applied": False, "failure_kind": "TimeoutError",
        }
        bridge.MANUAL_DELETE_GUARDS[guard["operation_id"]] = guard
        client = mock.Mock()
        client.list_devices.return_value = [{"device_id": 99, "ip": "192.0.2.27"}]
        with mock.patch.object(bridge, "_librenms_token", return_value="secret"), \
             mock.patch.object(bridge, "_librenms_client", return_value=client):
            bridge.reconcile_manual_delete_guards()
        stored = bridge.MANUAL_DELETE_GUARDS[guard["operation_id"]]
        self.assertEqual(stored["result"], "superseded")
        self.assertTrue(stored["state_applied"])
        self.assertTrue(self.state["pending_delete"])
        self.assertFalse(bridge._manual_delete_guard_blocks_device("99", "192.0.2.27"))

    def test_real_watcher_skips_committed_generation_and_processes_other_device(self):
        other_key = "infra-dist-ping|192.0.2.28"
        other = {
            "job": "infra-dist-ping", "ip": "192.0.2.28", "name": "other",
            "pending_delete": False, "alerting": False, "seen_up": False,
            "down_since": None,
        }
        bridge.DEVICE_DOWN_STATES[other_key] = other
        with bridge.RETIRE_LOCK:
            operation, reason = bridge._new_manual_delete_operation(
                self.key, self.state, self.state["pending_token"],
            )
            self.assertEqual(reason, "")
            operation["phase"] = "DELETE_MAY_HAVE_BEEN_DISPATCHED"
        before = dict(self.state)
        samples = [
            {"metric": {"job": "infra-dist-ping", "target_ip": "192.0.2.27"}, "value": [100, "1"]},
            {"metric": {"job": "infra-dist-ping", "target_ip": "192.0.2.28"}, "value": [100, "1"]},
        ]
        sleeps = []

        def bounded_sleep(_seconds):
            sleeps.append(_seconds)
            if len(sleeps) > 1:
                raise StopIteration

        bridge.DEVICE_DOWN_ENABLED = True
        bridge.DEVICE_DOWN_JOBS = "infra-dist-ping"
        bridge.DEVICE_DOWN_RUNTIME_LOADED = True
        bridge.DEVICE_DOWN_ROOT_CAUSE_ENABLED = False
        bridge.DEVICE_ONLINE_FROM_PING = False
        with mock.patch.object(bridge, "prometheus_query", return_value=samples), \
             mock.patch.object(bridge, "fetch_librenms_name_cache", return_value={}), \
             mock.patch.object(bridge.time, "sleep", side_effect=bounded_sleep), \
             self.assertRaises(StopIteration):
            bridge.device_down_watcher()
        self.assertEqual(self.state, before)
        self.assertTrue(other["seen_up"])

    def test_real_watcher_recovery_cancels_checking_before_state_change(self):
        with bridge.RETIRE_LOCK:
            operation, reason = bridge._new_manual_delete_operation(
                self.key, self.state, self.state["pending_token"],
            )
            self.assertEqual(reason, "")
        sample = {
            "metric": {"job": "infra-dist-ping", "target_ip": "192.0.2.27"},
            "value": [100, "1"],
        }
        sleeps = []

        def bounded_sleep(_seconds):
            sleeps.append(_seconds)
            if len(sleeps) > 1:
                raise StopIteration

        bridge.DEVICE_DOWN_ENABLED = True
        bridge.DEVICE_DOWN_JOBS = "infra-dist-ping"
        bridge.DEVICE_DOWN_RUNTIME_LOADED = True
        bridge.DEVICE_DOWN_ROOT_CAUSE_ENABLED = False
        bridge.DEVICE_ONLINE_FROM_PING = False
        with mock.patch.object(bridge, "prometheus_query", return_value=[sample]), \
             mock.patch.object(bridge, "fetch_librenms_name_cache", return_value={}), \
             mock.patch.object(bridge, "save_device_down_states", return_value=None), \
             mock.patch.object(bridge.time, "sleep", side_effect=bounded_sleep), \
             self.assertRaises(StopIteration):
            bridge.device_down_watcher()
        self.assertEqual(operation["phase"], "CANCELLED")
        self.assertFalse(self.state["pending_delete"])

    def test_missing_inventory_record_still_requires_probe_and_applies_success(self):
        result, remove = self.run_delete(prom="UNKNOWN", probe=False, device_id="")
        self.assertTrue(result["ok"])
        remove.assert_not_called()
        self.assertTrue(self.state["librenms_deleted"])

    def test_exact_id_delete_treats_404_as_missing_without_fallback(self):
        seen = []

        def missing(req, timeout):
            seen.append((req.full_url, req.method, timeout))
            raise HTTPError(req.full_url, 404, "missing", {}, None)

        with mock.patch.object(bridge.request, "urlopen", side_effect=missing):
            result = bridge._manual_delete_exact_id("secret", "42", 1.25)
        self.assertEqual(result, "missing")
        self.assertEqual(seen, [("http://librenms.test/api/v0/devices/42", "DELETE", 1.25)])

    def test_all_network_timeouts_use_stage_and_remaining_deadline(self):
        seen = {}

        class Client:
            max_attempts = 7
            retry_delay = 9

            def list_devices(self):
                seen["attempts"] = self.max_attempts
                seen["retry_delay"] = self.retry_delay
                return [{"device_id": 42, "ip": "192.0.2.27"}]

        def status(job, ip, timeout):
            seen["prometheus"] = timeout
            return "UNKNOWN"

        def client(token, timeout):
            seen["inventory"] = timeout
            return Client()

        def probe(ip, timeout=None):
            seen["blackbox"] = timeout
            return False

        def remove(token, device_id, timeout):
            seen["delete"] = timeout
            return "deleted"

        with mock.patch.object(bridge, "_pending_delete_target_status", side_effect=status), \
             mock.patch.object(bridge, "_librenms_token", return_value="secret"), \
             mock.patch.object(bridge, "_librenms_client", side_effect=client), \
             mock.patch.object(bridge, "_blackbox_icmp_probe", side_effect=probe), \
             mock.patch.object(bridge, "_manual_delete_exact_id", side_effect=remove):
            result = bridge.resolve_pending_delete(self.key, "delete", self.state["pending_token"])
        self.assertTrue(result["ok"])
        self.assertGreater(seen["prometheus"], 0)
        self.assertLessEqual(seen["prometheus"], 2)
        self.assertLessEqual(seen["inventory"], 3)
        self.assertLessEqual(seen["blackbox"], 3)
        self.assertLessEqual(seen["delete"], bridge.MANUAL_DELETE_DEADLINE_SECONDS)
        self.assertEqual((seen["attempts"], seen["retry_delay"]), (1, 0))

    def test_stage_budget_uses_one_controlled_monotonic_deadline(self):
        class Clock:
            value = 100.0

            def __call__(self):
                return self.value

        clock = Clock()
        with mock.patch.object(bridge.time, "monotonic", side_effect=clock):
            with bridge.RETIRE_LOCK:
                operation, reason = bridge._new_manual_delete_operation(
                    self.key, self.state, self.state["pending_token"],
                )
            self.assertEqual(reason, "")
            self.assertEqual(operation["deadline"], 112.0)
            clock.value = 111.5
            self.assertEqual(bridge._stage_timeout(operation, 3), 0.5)
            clock.value = 112.0
            self.assertEqual(bridge._stage_timeout(operation, 3), 0)


if __name__ == "__main__":
    unittest.main()
