"""Execute the actual [6/6] shell phase, with a local curl double only."""
import json
import shlex
import sys

import pytest

from .test_librenms_secret_logging import run_path, assert_safe
from .test_librenms_auto_config_flow import AUTO_CONFIG


def run_cleanup(tmp_path, *, mode="ok", rules=None):
    if rules is None:
        rules = [{"id": "17", "name": "设备离线告警"}]
    listing = json.dumps({"status": "ok", "rules": rules})
    if mode == "malformed":
        listing = "not json"
    elif mode == "list_app_error":
        listing = '{"status":"error","rules":[]}'
    source = AUTO_CONFIG.read_text(encoding="utf-8")
    start = source.index('echo "[6/6] Setting up alert rules..."')
    end = source.index('echo "  LibreNMS Discovery Complete!"', start)
    setup = "set -e\ncleanup_legacy_alert_rules() { :; }\n"
    setup += "python3() { " + shlex.quote(sys.executable.replace("\\", "/")) + ' "$@"; }\n'
    setup += "MODE=" + shlex.quote(mode) + "\nLISTING=" + shlex.quote(listing) + "\n"
    setup += r'''
curl() {
  method=GET
  with_code=false
  for arg in "$@"; do
    [ "$arg" != DELETE ] || method=DELETE
    [ "$arg" != -w ] || with_code=true
  done
  printf '%s\n' "$*" >> "$INTERNAL_LOG"
  code=200
    if [ "$method" = GET ]; then
    [ "$MODE" != list_timeout ] || return 28
    body=$LISTING
    [ ! -f "$INTERNAL_LOG.absent" ] || body='{"status":"ok","rules":[]}'
    [ "$MODE" != list_401 ] || code=401
  else
    [ "$MODE" != timeout ] || return 28
    body='{"status":"ok","message":"Alert rule has been removed"}'
    case "$MODE" in
      401|403|500) code=$MODE ;;
      app_error) body='{"status":"error","message":"FAKE_ADMIN_BATCH2_78! FAKE_COMMUNITY_BATCH2_93 fake-token"}' ;;
      not_found) code=404; touch "$INTERNAL_LOG.absent" ;;
      false_404) code=404 ;;
    esac
  fi
  printf '%s' "$body"
  [ "$with_code" != true ] || printf '\n%s' "$code"
  return 0
}
'''
    return run_path(tmp_path, source[start:end], setup)


@pytest.mark.parametrize("mode", ["timeout", "401", "403", "500", "app_error",
                                  "list_timeout", "list_401", "malformed", "list_app_error"])
def test_cleanup_failures_are_nonzero_and_never_removed(tmp_path, mode):
    code, output, _ = run_cleanup(tmp_path, mode=mode)
    assert code != 0
    assert " - removed" not in output
    assert_safe(output)
    assert "fake-token" not in output


def test_cleanup_success_deletes_only_exact_name_and_id(tmp_path):
    code, output, calls = run_cleanup(tmp_path, rules=[
        {"id": "17", "name": "设备离线告警"},
        {"id": "18", "name": "设备离线告警-custom"},
    ])
    assert code == 0
    assert "设备离线告警 - removed" in output
    assert "/rules/17" in calls
    assert "/rules/18" not in calls


def test_cleanup_empty_verified_list_is_idempotent(tmp_path):
    for index in range(2):
        directory = tmp_path / str(index)
        directory.mkdir()
        code, output, calls = run_cleanup(directory, rules=[])
        assert code == 0
        assert "already absent" in output
        assert "DELETE" not in calls


@pytest.mark.parametrize("mode,expected", [("not_found", 0), ("false_404", 1)])
def test_delete_404_requires_successful_absence_verification(tmp_path, mode, expected):
    code, output, calls = run_cleanup(tmp_path, mode=mode)
    assert code == expected
    assert " - removed" not in output
    if expected == 0:
        assert "already absent (verified after DELETE 404)" in output
    assert calls.count("-X GET") == 2


@pytest.mark.parametrize("rules", [[None], [{"name":"设备离线告警"}],
    [{"id":"17/other", "name":"设备离线告警"}], {"id":"17"}])
def test_invalid_rule_rows_fail_without_deletion(tmp_path, rules):
    code, output, calls = run_cleanup(tmp_path, rules=rules)
    assert code != 0
    assert "DELETE" not in calls
    assert " - removed" not in output
