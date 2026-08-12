import re
from http import HTTPStatus

import pytest

from platform_api import dhcp_telnet

from .test_platform_transactions import load_api


class TelnetError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": message}


class Match:
    def __init__(self, value):
        self.value = value

    def group(self, _index):
        return self.value


class FakeSession:
    def __init__(self, outputs=()):
        self.outputs = iter(outputs)
        self.expects = []
        self.writes = []
        self.closed = False

    def expect(self, patterns, timeout):
        self.expects.append((patterns, timeout))
        return next(self.outputs)

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True


def make_context(settings=None, timeout=8):
    settings = settings or {
        "username": "operator",
        "password": "secret",
        "enablePassword": "enable-secret",
        "port": 23,
    }
    return dhcp_telnet.DhcpTelnetContext(
        timeout=timeout,
        get_settings=lambda: dict(settings),
        error_factory=TelnetError,
    )


def assert_error(exc, status, message):
    assert exc.value.status == status
    assert exc.value.payload == {"ok": False, "error": message}


def test_telnet_transport_is_extracted_without_entrypoint_wrappers(tmp_path):
    api = load_api(tmp_path)
    context = api._dhcp_telnet_context()

    assert dhcp_telnet._telnet_expect.__module__ == "platform_api.dhcp_telnet"
    assert dhcp_telnet._telnet_command.__module__ == "platform_api.dhcp_telnet"
    assert dhcp_telnet._open_cisco_telnet.__module__ == "platform_api.dhcp_telnet"
    assert isinstance(context, dhcp_telnet.DhcpTelnetContext)
    assert context.timeout == api.DHCP_SWITCH_TIMEOUT
    assert context.error_factory is api.DiagnosticError
    assert context.get_settings.func is api.platform_dhcp_settings.dhcp_connection_settings
    assert not hasattr(api, "Telnet")
    assert not hasattr(api, "CISCO_PROMPT_RE")
    assert not hasattr(api, "CISCO_PRIV_PROMPT_RE")
    assert not hasattr(api, "CISCO_USER_PROMPT_RE")
    assert not hasattr(api, "CISCO_MORE_RE")
    assert not hasattr(api, "_telnet_expect")
    assert not hasattr(api, "_telnet_command")
    assert not hasattr(api, "_open_cisco_telnet")


def test_prompt_regexes_keep_exact_patterns_and_strict_matching():
    assert dhcp_telnet.CISCO_PROMPT_RE == (
        br"(?m)^[A-Za-z0-9_.:/()\[\]-]+[>#][ \t]*\r?$"
    )
    assert dhcp_telnet.CISCO_PRIV_PROMPT_RE == (
        br"(?m)^[A-Za-z0-9_.:/()\[\]-]+#[ \t]*\r?$"
    )
    assert dhcp_telnet.CISCO_USER_PROMPT_RE == (
        br"(?m)^[A-Za-z0-9_.:/()\[\]-]+>[ \t]*\r?$"
    )
    assert dhcp_telnet.CISCO_MORE_RE == br"(?i)--More--|<---\s*More\s*--->"
    for prompt in (
        b"core-sw#\r\n",
        b"core.sw_1:/rack(2)[a]>\t\r\n",
    ):
        assert re.search(dhcp_telnet.CISCO_PROMPT_RE, prompt) is not None
    assert re.search(
        dhcp_telnet.CISCO_PROMPT_RE,
        b"counter value is >\r\n",
    ) is None
    assert re.search(dhcp_telnet.CISCO_PRIV_PROMPT_RE, b"core-sw>\r\n") is None
    assert re.search(dhcp_telnet.CISCO_USER_PROMPT_RE, b"core-sw#\r\n") is None


def test_telnet_expect_keeps_timeout_decode_and_return_shape():
    session = FakeSession([(0, Match(b"core-sw#"), b"value \xff")])

    index, match, decoded = dhcp_telnet._telnet_expect(
        make_context(timeout=9), session, [b"fixture"], "登录",
    )

    assert (index, match.group(0), decoded) == (0, b"core-sw#", "value �")
    assert session.expects == [([b"fixture"], 9)]


def test_telnet_expect_keeps_diagnostic_timeout():
    session = FakeSession([(-1, None, b"partial")])

    with pytest.raises(TelnetError) as exc:
        dhcp_telnet._telnet_expect(
            make_context(), session, [b"fixture"], "用户名验证",
        )

    assert_error(
        exc,
        HTTPStatus.BAD_GATEWAY,
        "核心交换机 Telnet 用户名验证超时",
    )


def test_entrypoint_context_keeps_real_diagnostic_error(tmp_path):
    api = load_api(tmp_path)
    session = FakeSession([(-1, None, b"partial")])

    with pytest.raises(api.DiagnosticError) as exc:
        dhcp_telnet._telnet_expect(
            api._dhcp_telnet_context(),
            session,
            [b"fixture"],
            "登录",
        )

    assert exc.value.status == HTTPStatus.BAD_GATEWAY
    assert exc.value.payload == {
        "ok": False,
        "error": "核心交换机 Telnet 登录超时",
    }


def test_telnet_command_keeps_pagination_writes_and_cleanup():
    session = FakeSession([
        (
            1,
            None,
            b"show ip dhcp pool\r\nfirst\x08 page\r\n--More--",
        ),
        (0, None, b"second page\r\ncore-sw#\r\n"),
    ])

    output = dhcp_telnet._telnet_command(
        make_context(), session, "show ip dhcp pool",
    )

    assert output == "first page\nsecond page"
    assert session.writes == [b"show ip dhcp pool\n", b" "]
    assert session.expects == [
        ([dhcp_telnet.CISCO_PROMPT_RE, dhcp_telnet.CISCO_MORE_RE], 8),
        ([dhcp_telnet.CISCO_PROMPT_RE, dhcp_telnet.CISCO_MORE_RE], 8),
    ]


def test_telnet_command_keeps_alternate_more_marker_and_exact_echo_rule():
    session = FakeSession([
        (1, None, b"show version extra\r\n<--- More --->"),
        (0, None, b"done\r\ncore-sw>\r\n"),
    ])

    output = dhcp_telnet._telnet_command(
        make_context(), session, "show version",
    )

    assert output == "show version extra\ndone"


def test_telnet_command_keeps_100_page_limit():
    session = FakeSession([(1, None, b"--More--")] * 100)

    with pytest.raises(TelnetError) as exc:
        dhcp_telnet._telnet_command(make_context(), session, "show run")

    assert_error(
        exc,
        HTTPStatus.BAD_GATEWAY,
        "核心交换机分页输出超过安全上限",
    )
    assert session.writes == [b"show run\n", *([b" "] * 100)]
    assert len(session.expects) == 100


def test_open_telnet_keeps_missing_password_error_without_constructor(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dhcp_telnet,
        "Telnet",
        lambda *args: calls.append(args),
    )
    context = make_context({
        "username": "operator",
        "password": "",
        "enablePassword": "",
        "port": 23,
    })

    with pytest.raises(TelnetError) as exc:
        dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert_error(
        exc,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "尚未配置核心交换机 Telnet 密码，请先在赛事控制台填写",
    )
    assert calls == []


def install_session(monkeypatch, outputs, settings=None, timeout=8):
    session = FakeSession(outputs)
    calls = []

    def telnet(host, port, received_timeout):
        calls.append((host, port, received_timeout))
        return session

    monkeypatch.setattr(dhcp_telnet, "Telnet", telnet)
    context = make_context(settings, timeout)
    return context, session, calls


def test_open_telnet_keeps_constructor_and_direct_command_prompt(monkeypatch):
    context, session, calls = install_session(
        monkeypatch,
        [(2, Match(b"core-sw#"), b"core-sw#")],
        timeout=11,
    )

    result = dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert result is session
    assert calls == [("192.168.10.254", 23, 11)]
    assert session.writes == []
    assert session.closed is False
    assert session.expects == [(
        [
            br"(?im)^(?:user ?name|login):[ \t]*\r?$",
            br"(?im)^password:[ \t]*\r?$",
            dhcp_telnet.CISCO_PROMPT_RE,
            br"(?i)(?:login invalid|authentication failed|access denied)",
        ],
        11,
    )]


def test_open_telnet_keeps_initial_login_rejection(monkeypatch):
    context, _session, _calls = install_session(
        monkeypatch,
        [(3, None, b"Login invalid")],
    )

    with pytest.raises(TelnetError) as exc:
        dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert_error(exc, HTTPStatus.BAD_GATEWAY, "核心交换机拒绝 Telnet 登录")


def test_open_telnet_keeps_missing_username_error(monkeypatch):
    settings = {
        "username": "",
        "password": "secret",
        "enablePassword": "",
        "port": 23,
    }
    context, session, _calls = install_session(
        monkeypatch,
        [(0, None, b"Username:")],
        settings,
    )

    with pytest.raises(TelnetError) as exc:
        dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert_error(
        exc,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "交换机要求用户名，但尚未配置 Telnet 用户名",
    )
    assert session.writes == []


def test_open_telnet_keeps_username_rejection(monkeypatch):
    context, session, _calls = install_session(monkeypatch, [
        (0, None, b"Login:"),
        (2, None, b"Authentication failed"),
    ])

    with pytest.raises(TelnetError) as exc:
        dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert_error(exc, HTTPStatus.BAD_GATEWAY, "核心交换机拒绝 Telnet 用户名")
    assert session.writes == [b"operator\n"]


def test_open_telnet_keeps_username_direct_prompt_success(monkeypatch):
    context, session, _calls = install_session(monkeypatch, [
        (0, None, b"User name:"),
        (1, Match(b"core-sw#"), b"core-sw#"),
    ])

    result = dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert result is session
    assert session.writes == [b"operator\n"]


def test_open_telnet_keeps_username_password_success(monkeypatch):
    context, session, _calls = install_session(monkeypatch, [
        (0, None, b"Username:"),
        (0, None, b"Password:"),
        (0, Match(b"core-sw#"), b"core-sw#"),
    ])

    result = dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert result is session
    assert session.writes == [b"operator\n", b"secret\n"]


def test_open_telnet_keeps_password_prompt_success(monkeypatch):
    context, session, _calls = install_session(monkeypatch, [
        (1, None, b"Password:"),
        (0, Match(b"core-sw#"), b"core-sw#"),
    ])

    result = dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert result is session
    assert session.writes == [b"secret\n"]


@pytest.mark.parametrize("failure_index", [1, 2])
def test_open_telnet_keeps_password_failure(monkeypatch, failure_index):
    context, session, _calls = install_session(monkeypatch, [
        (1, None, b"Password:"),
        (failure_index, None, b"Access denied"),
    ])

    with pytest.raises(TelnetError) as exc:
        dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert_error(exc, HTTPStatus.BAD_GATEWAY, "核心交换机 Telnet 密码错误")
    assert session.writes == [b"secret\n"]


def test_open_telnet_does_not_enable_without_enable_password(monkeypatch):
    settings = {
        "username": "operator",
        "password": "secret",
        "enablePassword": "",
        "port": 23,
    }
    context, session, _calls = install_session(monkeypatch, [
        (1, None, b"Password:"),
        (0, Match(b"core-sw>"), b"core-sw>"),
    ], settings)

    result = dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert result is session
    assert session.writes == [b"secret\n"]


def test_open_telnet_keeps_enable_password_sequence(monkeypatch):
    context, session, _calls = install_session(monkeypatch, [
        (1, None, b"Password:"),
        (0, Match(b"core-sw>"), b"core-sw>"),
        (0, None, b"Password:"),
        (0, Match(b"core-sw#"), b"core-sw#"),
    ])

    result = dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert result is session
    assert session.writes == [b"secret\n", b"enable\n", b"enable-secret\n"]
    assert len(session.expects[2][0]) == 4
    assert len(session.expects[3][0]) == 4


def test_open_telnet_keeps_direct_enable_success(monkeypatch):
    context, session, _calls = install_session(monkeypatch, [
        (1, None, b"Password:"),
        (0, Match(b"core-sw>"), b"core-sw>"),
        (1, Match(b"core-sw#"), b"core-sw#"),
    ])

    result = dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert result is session
    assert session.writes == [b"secret\n", b"enable\n"]


@pytest.mark.parametrize("failure_index", [2, 3])
def test_open_telnet_keeps_enable_failure(monkeypatch, failure_index):
    context, session, _calls = install_session(monkeypatch, [
        (1, None, b"Password:"),
        (0, Match(b"core-sw>"), b"core-sw>"),
        (failure_index, None, b"Access denied"),
    ])

    with pytest.raises(TelnetError) as exc:
        dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert_error(exc, HTTPStatus.BAD_GATEWAY, "核心交换机 Enable 密码错误")
    assert session.writes == [b"secret\n", b"enable\n"]


def test_open_telnet_keeps_enable_password_failure(monkeypatch):
    context, session, _calls = install_session(monkeypatch, [
        (1, None, b"Password:"),
        (0, Match(b"core-sw>"), b"core-sw>"),
        (0, None, b"Password:"),
        (1, None, b"Authentication failed"),
    ])

    with pytest.raises(TelnetError) as exc:
        dhcp_telnet._open_cisco_telnet(context, "192.168.10.254")

    assert_error(exc, HTTPStatus.BAD_GATEWAY, "核心交换机 Enable 密码错误")
    assert session.writes == [b"secret\n", b"enable\n", b"enable-secret\n"]
