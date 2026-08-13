import json
import os
from http import HTTPStatus

import pytest

from platform_api import dhcp_settings

from .test_platform_transactions import load_api


class SettingsError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": message}


def make_context(
    tmp_path,
    *,
    write_enabled=True,
    cache=None,
    core_host=lambda: "192.168.10.254",
):
    cache = {} if cache is None else cache
    return dhcp_settings.DhcpSettingsContext(
        settings_path=tmp_path / "dhcp-settings.json",
        default_username="env-user",
        default_password=" env-password ",
        default_enable_password=" env-enable ",
        default_port=23,
        write_enabled=write_enabled,
        switch_timeout=8,
        refresh_seconds=60,
        core_host=core_host,
        cache_clear=cache.clear,
        error_factory=SettingsError,
    )


def test_dhcp_settings_dependency_assembly_uses_new_module_without_wrappers(tmp_path):
    api = load_api(tmp_path)

    read_callable = api._read_api_dependencies().get_dhcp_settings
    write_callable = api._write_api_dependencies().save_dhcp_settings

    assert dhcp_settings.dhcp_connection_settings.__module__ == (
        "platform_api.dhcp_settings"
    )
    assert dhcp_settings.get_dhcp_settings.__module__ == "platform_api.dhcp_settings"
    assert dhcp_settings.save_dhcp_settings.__module__ == "platform_api.dhcp_settings"
    assert read_callable.func is dhcp_settings.get_dhcp_settings
    assert write_callable.func is dhcp_settings.save_dhcp_settings
    assert isinstance(read_callable.args[0], dhcp_settings.DhcpSettingsContext)
    assert isinstance(write_callable.args[0], dhcp_settings.DhcpSettingsContext)
    assert read_callable.args[0].core_host is api.configured_core_switch_host
    assert read_callable.args[0].cache_clear is api.platform_dhcp_runtime.clear_cache
    assert not hasattr(api, "dhcp_connection_settings")
    assert not hasattr(api, "get_dhcp_settings")
    assert not hasattr(api, "save_dhcp_settings")


def test_dhcp_connection_settings_keeps_environment_fallback(tmp_path):
    context = make_context(tmp_path)

    assert dhcp_settings.dhcp_connection_settings(context) == {
        "username": "env-user",
        "password": " env-password ",
        "enablePassword": " env-enable ",
        "port": 23,
        "source": "environment",
    }


def test_dhcp_connection_settings_keeps_console_override_and_string_semantics(
    tmp_path,
):
    context = make_context(tmp_path)
    context.settings_path.write_text(json.dumps({
        "username": " console-user ",
        "password": " console-password ",
        "enablePassword": " console-enable ",
        "port": "2323",
    }), encoding="utf-8")

    assert dhcp_settings.dhcp_connection_settings(context) == {
        "username": "console-user",
        "password": " console-password ",
        "enablePassword": " console-enable ",
        "port": 2323,
        "source": "console",
    }


def test_dhcp_connection_settings_keeps_console_source_for_non_dict_file(tmp_path):
    context = make_context(tmp_path)
    context.settings_path.write_text("[]", encoding="utf-8")

    assert dhcp_settings.dhcp_connection_settings(context) == {
        "username": "env-user",
        "password": " env-password ",
        "enablePassword": " env-enable ",
        "port": 23,
        "source": "console",
    }


def test_dhcp_connection_settings_keeps_malformed_port_fallback(tmp_path):
    context = make_context(tmp_path)
    context.settings_path.write_text('{"port":"abc"}', encoding="utf-8")

    assert dhcp_settings.dhcp_connection_settings(context)["port"] == 23


@pytest.mark.parametrize(("stored", "expected"), [(0, 1), (65536, 65535)])
def test_dhcp_connection_settings_keeps_stored_port_clamp(
    tmp_path, stored, expected,
):
    context = make_context(tmp_path)
    context.settings_path.write_text(
        json.dumps({"port": stored}), encoding="utf-8",
    )

    assert dhcp_settings.dhcp_connection_settings(context)["port"] == expected


def test_get_dhcp_settings_keeps_sanitized_schema(tmp_path):
    observed = []
    context = make_context(
        tmp_path,
        core_host=lambda: observed.append("core-host") or "192.168.10.254",
    )
    context.settings_path.write_text(json.dumps({
        "username": "operator",
        "password": "secret",
        "enablePassword": "",
        "port": 2323,
    }), encoding="utf-8")

    payload = dhcp_settings.get_dhcp_settings(context)

    assert payload == {
        "ok": True,
        "host": "192.168.10.254",
        "username": "operator",
        "port": 2323,
        "passwordConfigured": True,
        "enablePasswordConfigured": False,
        "source": "console",
        "timeoutSeconds": 8,
        "refreshSeconds": 60,
    }
    assert "password" not in payload
    assert "enablePassword" not in payload
    assert observed == ["core-host"]


def test_save_dhcp_settings_keeps_write_disabled_diagnostic_error(tmp_path):
    context = make_context(tmp_path, write_enabled=False)

    with pytest.raises(SettingsError) as exc:
        dhcp_settings.save_dhcp_settings(context, {})

    assert exc.value.status == HTTPStatus.FORBIDDEN
    assert exc.value.payload == {
        "ok": False,
        "error": "当前环境不允许保存 Telnet 配置",
    }
    assert not context.settings_path.exists()


def test_save_dhcp_settings_keeps_partial_update_and_blank_passwords(tmp_path):
    context = make_context(tmp_path)
    context.settings_path.write_text(json.dumps({
        "username": "old-user",
        "password": "old-password",
        "enablePassword": "old-enable",
        "port": 2323,
    }), encoding="utf-8")

    result = dhcp_settings.save_dhcp_settings(context, {
        "username": " renamed ",
        "password": "",
        "enablePassword": None,
    })
    stored = json.loads(context.settings_path.read_text(encoding="utf-8"))

    assert stored["username"] == "renamed"
    assert stored["password"] == "old-password"
    assert stored["enablePassword"] == "old-enable"
    assert stored["port"] == 2323
    assert result["passwordConfigured"] is True
    assert result["enablePasswordConfigured"] is True
    assert "password" not in result
    assert "enablePassword" not in result


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"port": "abc"}, "Telnet 端口必须是数字"),
        ({"port": 0}, "Telnet 端口必须在 1-65535 之间"),
        ({"port": 65536}, "Telnet 端口必须在 1-65535 之间"),
        ({"username": "u" * 129}, "Telnet 用户名过长"),
        ({"password": "p" * 513}, "Telnet 密码过长"),
        ({"enablePassword": "e" * 513}, "Telnet 密码过长"),
        ({"username": "user\tname"}, "Telnet 凭据不能包含换行或控制字符"),
        ({"password": "pass\nword"}, "Telnet 凭据不能包含换行或控制字符"),
        ({"enablePassword": "enable\x7f"}, "Telnet 凭据不能包含换行或控制字符"),
    ],
)
def test_save_dhcp_settings_keeps_validation_status_and_messages(
    tmp_path, data, message,
):
    context = make_context(tmp_path)

    with pytest.raises(SettingsError) as exc:
        dhcp_settings.save_dhcp_settings(context, data)

    assert exc.value.status == HTTPStatus.BAD_REQUEST
    assert exc.value.payload == {"ok": False, "error": message}


def test_save_dhcp_settings_keeps_private_mode_chmod_cache_and_timestamp(
    monkeypatch, tmp_path,
):
    cache = {"payload": "stale"}
    context = make_context(tmp_path, cache=cache)
    writes = []
    chmods = []

    def write_json_file(path, payload, mode=None):
        writes.append((path, payload, mode))
        path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        dhcp_settings.platform_storage,
        "write_json_file",
        write_json_file,
    )
    monkeypatch.setattr(
        dhcp_settings.os,
        "chmod",
        lambda path, mode: chmods.append((path, mode)),
    )
    monkeypatch.setattr(dhcp_settings.time, "time", lambda: 1_700_000_123.9)

    dhcp_settings.save_dhcp_settings(context, {
        "username": "operator",
        "password": "secret",
        "enablePassword": "enable",
        "port": 2323,
    })

    assert writes == [(context.settings_path, {
        "username": "operator",
        "password": "secret",
        "enablePassword": "enable",
        "port": 2323,
        "updatedAt": 1_700_000_123,
    }, 0o600)]
    assert chmods == [(context.settings_path, 0o600)]
    assert cache == {}


def test_save_dhcp_settings_keeps_chmod_failure_non_fatal(
    monkeypatch, tmp_path,
):
    cache = {"payload": "stale"}
    context = make_context(tmp_path, cache=cache)

    def fail_chmod(_path, _mode):
        raise OSError("fixture chmod failed")

    monkeypatch.setattr(dhcp_settings.os, "chmod", fail_chmod)

    payload = dhcp_settings.save_dhcp_settings(context, {
        "username": "operator",
        "password": "secret",
        "port": 23,
    })

    assert payload["ok"] is True
    assert payload["username"] == "operator"
    assert cache == {}


def test_entrypoint_save_dependency_keeps_real_diagnostic_error(tmp_path):
    api = load_api(tmp_path)
    save = api._write_api_dependencies().save_dhcp_settings

    with pytest.raises(api.DiagnosticError) as exc:
        save({"port": "not-a-number"})

    assert exc.value.status == HTTPStatus.BAD_REQUEST
    assert exc.value.payload == {
        "ok": False,
        "error": "Telnet 端口必须是数字",
    }


def test_real_private_store_mode_on_posix(tmp_path):
    context = make_context(tmp_path)

    dhcp_settings.save_dhcp_settings(context, {
        "password": "secret",
        "port": 23,
    })

    if os.name != "nt":
        assert context.settings_path.stat().st_mode & 0o077 == 0
