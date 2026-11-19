"""执行现行发布手册的片段，仅使用临时脚本、Git 仓库和本机回环 HTTP。"""

import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

import pytest


RUNBOOK = Path(__file__).resolve().parents[2] / "docs/runbooks/company-deployment.md"
ASSET = Path("bigscreen/release asset.js")
NESTED_ASSET = Path("bigscreen/new folder/detail.js")
PAYLOAD = b"// isolated Bigscreen fixture\n"
TRACKED = Path("existing tracked file.txt" if os.name == "nt" else "existing tracked\tfile\nS quoted' file.txt")


def example(name):
    markdown = RUNBOOK.read_text(encoding="utf-8")
    start = f"<!-- release-example: {name} -->\n```bash\n"
    return markdown.split(start, 1)[1].split("\n```", 1)[0]


@pytest.fixture
def bash():
    candidate = Path(r"C:\Program Files\Git\bin\bash.exe") if os.name == "nt" else None
    binary = str(candidate) if candidate and candidate.is_file() else shutil.which("bash")
    if not binary:
        pytest.skip("Bash is required for release script behavior")
    return binary


def run(command, cwd, **env_overrides):
    # Honor the fixture cwd even if pytest was launched with a Git work tree
    # or hook configuration in the environment.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(BASH_ENV="", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    env.update(env_overrides)
    return subprocess.run(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", timeout=15,
    )


def release(bash, cwd, workspace, body, *args, invocation="plain", setup=""):
    scratch = workspace / "script files"
    scratch.mkdir(exist_ok=True)
    launch = """run_release_script "$@" <<'FIXTURE'"""
    tail = ""
    if invocation == "if":
        launch = "if " + launch
        tail = 'then exit 0; else exit "$?"; fi\n'
    elif invocation == "or":
        launch += ' || exit "$?"'
    command = "\n".join((example("runner"), setup, launch, body, "FIXTURE", tail))
    return run([bash, "-c", command, "fixture", *args], cwd, TMPDIR=scratch.as_posix())


def saved_script(result, workspace):
    match = re.search(r"^Release script (.+)$", result.stdout, re.MULTILINE)
    assert match, result
    # Git for Windows may report a path under its /tmp mount.
    return workspace / "script files" / Path(match[1]).name


def test_heredoc_reader_returns_zero_before_completion_but_file_script_finishes(bash, tmp_path):
    work = tmp_path / "release workspace"
    work.mkdir()
    reader = r"""bash -c 'cat >/dev/null; printf "reader finished\n"'"""
    legacy = f"""bash <<'OLD'
set -e
printf 'started\\n'
{reader}
printf 'legacy completion\\n'
OLD
"""
    old = run([bash, "-c", legacy], work)
    assert old.returncode == 0, old.stderr
    assert old.stdout.splitlines() == ["started", "reader finished"]

    body = f"""stage=stdin-check
if [[ $(uname -s) == Linux ]]; then
  test /proc/$$/fd/0 -ef /dev/null
fi
printf 'started\\n'
{reader} </dev/null
printf 'fixture record\\n' | bash -c 'IFS= read -r input; test "$input" = "fixture record"'
printf 'checked\\n' >"$1"
"""
    sentinel = work / "finished marker"
    result = release(bash, work, tmp_path, body, sentinel.name)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[1:] == ["started", "reader finished", "COMPLETE stage=stdin-check"]
    assert sentinel.read_bytes() == b"checked\n"
    assert saved_script(result, tmp_path).is_file()
    if sys.platform == "linux":
        assert stat.S_IMODE(saved_script(result, tmp_path).stat().st_mode) == 0o600


@pytest.mark.parametrize("command", [
    "bash -c 'exit 37'",
    "bash -c 'exit 37' | cat",
    "result=$(bash -c 'exit 37'; printf 'unreachable')",
])
@pytest.mark.parametrize("invocation", ["plain", "if", "or"])
def test_child_failure_keeps_status_and_stops_before_completion(bash, tmp_path, command, invocation):
    body = f"stage=verify-fixture\n{command}\nprintf 'unexpected\\n' >'finished marker'"
    result = release(bash, tmp_path, tmp_path, body, invocation=invocation)
    line = saved_script(result, tmp_path).read_text(encoding="utf-8").splitlines().index(command) + 1
    assert result.returncode == 37, result
    assert f"STOP stage=verify-fixture line={line} exit=37" in result.stderr
    assert "COMPLETE" not in result.stdout
    assert not (tmp_path / "finished marker").exists()


def test_missing_completion_sentinel_is_a_failure(bash, tmp_path):
    result = release(bash, tmp_path, tmp_path, "stage=verify-fixture\nexit 0")
    assert result.returncode == 1, result
    assert re.search(r"STOP stage=verify-fixture line=\d+ exit=1 \(incomplete\)", result.stderr)
    assert "COMPLETE" not in result.stdout


@pytest.mark.parametrize("invocation", ["plain", "if", "or"])
def test_syntax_failure_has_no_script_side_effect(bash, tmp_path, invocation):
    body = "printf 'unexpected\\n' >'finished marker'\nif then"
    result = release(bash, tmp_path, tmp_path, body, invocation=invocation)
    assert result.returncode == 2, result
    assert re.search(r"STOP stage=script-syntax line=\d+ exit=2", result.stderr)
    assert "COMPLETE" not in result.stdout
    assert not (tmp_path / "finished marker").exists()


@pytest.mark.parametrize("invocation", ["plain", "if", "or"])
def test_valid_script_completes_in_each_invocation_context(bash, tmp_path, invocation):
    body = "stage=verify-fixture\nprintf 'checked\\n' >'finished marker'"
    result = release(bash, tmp_path, tmp_path, body, invocation=invocation)
    assert result.returncode == 0, result
    assert "COMPLETE stage=verify-fixture" in result.stdout
    assert (tmp_path / "finished marker").read_bytes() == b"checked\n"


@pytest.mark.parametrize("setup, stage, status", [
    ("mktemp() { return 41; }", "script-create", 41),
    ("chmod() { return 42; }", "script-permissions", 42),
    ("""cat() {
  command cat || return "$?"
  copy_count=$(( ${copy_count:-0} + 1 ))
  if (( copy_count == 2 )); then return 43; fi
}""", "script-write", 43),
    ("""mktemp() { command mktemp -d "$@"; }
chmod() { return 0; }""", "script-write", 1),
], ids=["mktemp", "chmod", "partial-write", "redirection"])
def test_launcher_failures_preserve_status_and_do_not_start_script(bash, tmp_path, setup, stage, status):
    result = release(
        bash, tmp_path, tmp_path, "printf 'unexpected\\n' >'finished marker'",
        invocation="if", setup=setup,
    )
    assert result.returncode == status, result
    assert re.search(rf"STOP stage={stage} line=\d+ exit={status}", result.stderr)
    assert "COMPLETE" not in result.stdout
    assert not (tmp_path / "finished marker").exists()


def test_all_current_bash_examples_parse(bash, tmp_path):
    markdown = RUNBOOK.read_text(encoding="utf-8")
    snippets = re.findall(r"^```bash\n(.*?)^```$", markdown, flags=re.MULTILINE | re.DOTALL)
    assert snippets
    for number, snippet in enumerate(snippets, 1):
        result = subprocess.run(
            [bash, "-n"], cwd=tmp_path, input=snippet, capture_output=True,
            text=True, encoding="utf-8", timeout=15,
        )
        assert result.returncode == 0, (number, result.stderr)


def git(cwd, *args):
    result = run(["git", *args], cwd)
    assert result.returncode == 0, result
    return result.stdout.strip()


@pytest.fixture
def release_repos(tmp_path):
    # On Linux, pytest's private parent directories cannot be traversed by a second user.
    parent = "/tmp" if sys.platform == "linux" else tmp_path
    with tempfile.TemporaryDirectory(prefix="company release ", dir=parent) as directory:
        workspace = Path(directory)
        workspace.chmod(0o711)
        source = workspace / "source repo"
        source.mkdir()
        git(source, "init", "--initial-branch=main")
        git(source, "config", "user.email", "fixture@example.invalid")
        git(source, "config", "user.name", "Release fixture")
        (source / "bigscreen").mkdir()
        (source / "bigscreen/index.html").write_text("<title>fixture</title>\n", encoding="utf-8")
        (source / ".gitignore").write_text(".env\nignored.log\nrelease asset.js\n", encoding="utf-8")
        (source / TRACKED).write_bytes(b"tracked baseline\n")
        git(source, "add", "--", ".gitignore", "bigscreen/index.html", str(TRACKED))
        git(source, "commit", "-m", "baseline")
        before = git(source, "rev-parse", "HEAD")
        checkouts = [workspace / name for name in ("old checkout", "new checkout")]
        for checkout in checkouts:
            git(workspace, "clone", "--no-local", "--", str(source), str(checkout))
            checkout.chmod(0o755)
            (checkout / "bigscreen").chmod(0o755)
            (checkout / "VERSION").write_bytes(b"existing untracked version\n")
            (checkout / ".env").write_bytes(b"FIXTURE=private\n")
            (checkout / ".env").chmod(0o600)
            (checkout / "ignored.log").write_bytes(b"existing ignored file\n")
        (source / NESTED_ASSET.parent).mkdir()
        (source / ASSET).write_bytes(PAYLOAD)
        (source / NESTED_ASSET).write_bytes(PAYLOAD)
        git(source, "add", "-f", "--", str(ASSET), str(NESTED_ASSET))
        git(source, "commit", "-m", "release assets")
        target = git(source, "rev-parse", "HEAD")
        for checkout in checkouts:
            git(checkout, "fetch", "--no-tags", "origin", "refs/heads/main")
        yield workspace, *checkouts, before, target


@pytest.fixture
def nonroot(tmp_path):
    if sys.platform != "linux":
        pytest.skip("A separate non-root Linux user is required")
    if os.geteuid() == 0:
        helper = shutil.which("runuser")
        prefix = [helper, "-u", "nobody", "--"]
    else:
        helper = shutil.which("sudo")
        prefix = [helper, "-n", "-u", "nobody", "--"]
    if not helper:
        pytest.skip("No local user-switching tool for non-root file verification")
    identity = run([*prefix, "id", "-u"], tmp_path)
    if identity.returncode != 0:
        pytest.skip("The local test runner cannot switch to the unprivileged nobody user")
    assert int(identity.stdout) not in {0, os.geteuid()}
    return prefix


def fast_forward(bash, repos):
    workspace, old, new, before, target = repos
    legacy = run([bash, "-c", 'umask 077; git merge --ff-only "$1"', "fixture", target], old)
    assert legacy.returncode == 0, legacy
    assert git(old, "rev-parse", "HEAD") == target

    prep = 'umask 077\nbefore=$1\ntarget=$2\nbackup_root=$3\n'
    backup_root = workspace / "private backups"
    backup_root.mkdir(mode=0o700)
    result = release(
        bash, new, workspace,
        prep + example("backup") + "\n" + example("merge") + "\n: >'outside umask marker'",
        before, target, str(backup_root),
    )
    assert result.returncode == 0, result
    assert "COMPLETE stage=git-merge" in result.stdout
    assert git(new, "rev-parse", "HEAD") == target
    return next(backup_root.iterdir())


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_git_fast_forward_permissions_and_private_backup(bash, release_repos, nonroot):
    workspace, old, new, _, _ = release_repos
    backup = fast_forward(bash, release_repos)
    assert mode(old / ASSET) == mode(old / NESTED_ASSET) == 0o600
    assert mode(old / NESTED_ASSET.parent) == 0o700
    assert mode(new / ASSET) == mode(new / NESTED_ASSET) == 0o644
    assert mode(new / NESTED_ASSET.parent) == 0o755
    assert mode(new / "outside umask marker") == 0o600
    for asset in (ASSET, NESTED_ASSET):
        denied = run([*nonroot, "cat", str(old / asset)], workspace)
        allowed = run([*nonroot, "cat", str(new / asset)], workspace)
        assert denied.returncode != 0 and denied.stdout == "", denied
        assert allowed.returncode == 0 and allowed.stdout.encode() == PAYLOAD, allowed

    assert mode(backup) == 0o700
    assert mode(backup / ".env") == mode(backup / "SHA256SUMS") == mode(new / ".env") == 0o600
    assert (backup / ".env").read_bytes() == (new / ".env").read_bytes() == b"FIXTURE=private\n"
    denied = run([*nonroot, "cat", str(backup / ".env")], workspace)
    assert denied.returncode != 0 and denied.stdout == "", denied
    assert (new / "VERSION").read_bytes() == b"existing untracked version\n"
    assert (new / "ignored.log").read_bytes() == b"existing ignored file\n"


@pytest.mark.parametrize("collision", [ASSET, NESTED_ASSET, None], ids=["ignored", "untracked", "wrong-sha"])
def test_release_merge_stops_without_overwriting_local_paths(bash, release_repos, collision):
    workspace, _, new, before, target = release_repos
    if collision is not None:
        (new / collision).parent.mkdir(exist_ok=True)
        (new / collision).write_bytes(b"operator's local file\n")
        ignored = run(["git", "check-ignore", "--quiet", str(collision)], new)
        assert ignored.returncode == (0 if collision == ASSET else 1)
    else:
        target = before
    result = release(
        bash, new, workspace, "before=$1\ntarget=$2\n" + example("merge"), before, target,
    )
    assert result.returncode != 0, result
    assert "STOP stage=git-merge" in result.stderr and "COMPLETE" not in result.stdout
    assert git(new, "rev-parse", "HEAD") == before
    if collision is not None:
        assert (new / collision).read_bytes() == b"operator's local file\n"
    assert (new / ".env").read_bytes() == b"FIXTURE=private\n"
    assert (new / "VERSION").read_bytes() == b"existing untracked version\n"
    assert (new / "ignored.log").read_bytes() == b"existing ignored file\n"


@pytest.mark.parametrize("flag, tag", [("assume-unchanged", "h"), ("skip-worktree", "S")])
@pytest.mark.parametrize("stage", ["preflight", "merge", "acceptance"])
def test_release_checkpoints_reject_hidden_tracked_modifications(bash, release_repos, flag, tag, stage):
    workspace, _, checkout, before, target = release_repos
    # Run the real preflight next to the fixture repository. The fixture has already
    # fetched locally, so exclude only the network stage of the documented script.
    git(checkout, "remote", "set-url", "origin", "https://github.com/yikkrrtykj/monitor-autoconfig.git")
    preflight = example("preflight").split("<<'RELEASE'\n", 1)[1].split("\nstage=git-fetch\n", 1)[0]
    preflight_args = (checkout.as_posix(), before, target)
    merge = "before=$1\ntarget=$2\n" + example("merge")
    check, args, expected_head = preflight, preflight_args, before
    if stage != "preflight":
        clean = release(bash, checkout, workspace, preflight, *preflight_args)
        assert clean.returncode == 0 and "COMPLETE stage=git-preflight" in clean.stdout, clean
        check, args = merge, (before, target)
    if stage == "acceptance":
        merged = release(bash, checkout, workspace, merge, before, target)
        assert merged.returncode == 0 and "COMPLETE stage=git-merge" in merged.stdout, merged
        check, args, expected_head = "target=$1\n" + example("acceptance"), (target,), target

    git(checkout, "update-index", "--" + flag, "--", str(TRACKED))
    (checkout / TRACKED).write_bytes(b"hidden local change\n")
    git(checkout, "diff", "--quiet")
    git(checkout, "diff", "--cached", "--quiet")
    index_flags = git(checkout, "ls-files", "-v", "-z")
    assert git(checkout, "ls-files", "-v", "-z", "--", str(TRACKED)).startswith(tag + " ")
    files = [TRACKED, Path(".env"), Path("VERSION"), Path("ignored.log")]
    contents = {path: (checkout / path).read_bytes() for path in files}

    result = release(bash, checkout, workspace, check, *args)
    assert result.returncode == 1, result
    assert f"Git index flag {tag} on " in result.stderr
    assert re.search(rf"STOP stage=git-{stage} line=\d+ exit=1", result.stderr)
    assert "COMPLETE" not in result.stdout
    assert git(checkout, "rev-parse", "HEAD") == expected_head
    assert git(checkout, "ls-files", "-v", "-z") == index_flags
    assert {path: (checkout / path).read_bytes() for path in files} == contents
    if stage == "merge":
        assert not (checkout / ASSET).exists()


def test_release_rejects_sparse_checkout_even_without_index_flags(bash, release_repos):
    workspace, _, checkout, before, target = release_repos
    git(checkout, "config", "core.sparseCheckout", "true")
    index_flags = git(checkout, "ls-files", "-v", "-z")
    assert all(entry.startswith("H ") for entry in index_flags.rstrip("\0").split("\0"))
    result = release(
        bash, checkout, workspace, "before=$1\ntarget=$2\n" + example("merge"), before, target,
    )
    assert result.returncode == 1, result
    assert "Sparse checkout is unsupported" in result.stderr
    assert "STOP stage=git-merge" in result.stderr and "COMPLETE" not in result.stdout
    assert git(checkout, "rev-parse", "HEAD") == before
    assert git(checkout, "ls-files", "-v", "-z") == index_flags
    assert git(checkout, "config", "core.sparseCheckout") == "true"
    assert (checkout / TRACKED).read_bytes() == b"tracked baseline\n"


def test_nginx_static_http_permissions(bash, release_repos, nonroot):
    nginx = shutil.which("nginx")
    if not nginx and Path("/usr/sbin/nginx").is_file():
        nginx = "/usr/sbin/nginx"
    if not nginx:
        pytest.skip("Nginx HTTP 403/200 integration requires an installed Nginx binary")

    workspace, old, new, _, _ = release_repos
    fast_forward(bash, release_repos)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    runtime = workspace / "nginx runtime"
    runtime.mkdir()
    # Only this disposable directory needs to allow the isolated UID to write its pid.
    runtime.chmod(0o733)
    config = workspace / "nginx.conf"
    temp_paths = []
    for module in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        path = workspace / f"{module} tmp"
        path.mkdir(mode=0o755)
        temp_paths.append(f'{module}_temp_path "{path}";')
    config.write_text(
        f'pid "{runtime / "nginx.pid"}";\nerror_log stderr; events {{}}\n'
        f'http {{ access_log off; {" ".join(temp_paths)}\n'
        f'  server {{ listen 127.0.0.1:{port}; server_name old.fixture; root "{old / "bigscreen"}"; }}\n'
        f'  server {{ listen 127.0.0.1:{port}; server_name new.fixture; root "{new / "bigscreen"}"; }}\n'
        '}\n',
        encoding="utf-8",
    )
    config.chmod(0o644)

    def request(host):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/" + quote(ASSET.name), headers={"Host": host})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    command = [*nonroot, nginx, "-e", "stderr", "-p", str(workspace) + "/", "-c", str(config)]
    with (workspace / "nginx.log").open("w+", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [*command, "-g", "daemon off; master_process off;"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
        )
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    assert request("old.fixture")[0] == 403
                    break
                except OSError:
                    if proc.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        pytest.fail(f"Isolated Nginx did not start: {log.read()}")
                    time.sleep(0.05)
            pid = int((runtime / "nginx.pid").read_text(encoding="ascii"))
            assert Path(f"/proc/{pid}").stat().st_uid not in {0, os.geteuid()}
            assert request("new.fixture") == (200, PAYLOAD)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

def docker_snapshot(bash, tmp_path, frames, *, compare=False):
    fixture = tmp_path / "docker.json"
    fixture.write_text(json.dumps(frames), encoding="utf-8")
    stub = tmp_path / "docker-fixture.py"
    stub.write_text(
        """
import json
from pathlib import Path
import sys

frames = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
frame = frames[int(sys.argv[2])]
args = sys.argv[3:]
assert not sys.stdin.read()
if args == ["compose", "ps", "--all", "--no-trunc", "--quiet", "platform-api", "prometheus"]:
    print("\\n".join(frame["listed"]))
else:
    assert args == ["inspect", "--type", "container", *frame["listed"]]
    print(json.dumps(frame["inspect"]))
    sys.exit(frame.get("exit", 0))
""".lstrip(),
        encoding="utf-8",
    )
    setup = """
timeout() { shift; "$@"; }
docker() { "$FIXTURE_PYTHON" "$FIXTURE_DOCKER" "$FIXTURE_FRAMES" "$phase" "$@"; }
python3() { "$FIXTURE_PYTHON" "$@"; }
"""
    script = """
set -e -o pipefail
phase=0
snapshot_containers platform-api prometheus >before.json
"""
    if compare:
        script += """
phase=1
snapshot_containers platform-api prometheus >after.json
cmp -- before.json after.json
"""
    result = run(
        [bash, "-c", setup + example("snapshot") + script], tmp_path,
        FIXTURE_PYTHON=Path(sys.executable).as_posix(),
        FIXTURE_DOCKER=stub.as_posix(), FIXTURE_FRAMES=fixture.as_posix(),
    )
    return result, json.loads((tmp_path / "before.json").read_text() or "null")


def snapshot_container(container_id, **state):
    return {
        "Id": container_id,
        "RestartCount": 0,
        "State": {"Status": "running", "StartedAt": "2026-11-19T07:00:00Z", **state},
        "Config": {"Env": ["FIXTURE_PRIVATE=do-not-print"]},
    }


def test_snapshot_handles_optional_health_and_ignores_list_order(bash, tmp_path):
    rows = [
        snapshot_container("a" * 64, Health={"Status": "healthy"}),
        snapshot_container("b" * 64),
        snapshot_container("c" * 64, Health=None),
    ]
    frames = [{"listed": [item["Id"] for item in order], "inspect": order}
              for order in (rows, rows[::-1])]
    result, snapshot = docker_snapshot(bash, tmp_path, frames, compare=True)
    assert result.returncode == 0, result
    assert snapshot == [[row["Id"], "running", "2026-11-19T07:00:00Z", 0, health]
                        for row, health in zip(rows, ("healthy", "none", "none"), strict=True)]


@pytest.mark.parametrize("failure", ["empty", "inspect", "vanished", "incomplete"])
def test_snapshot_fails_without_a_complete_inspection(bash, tmp_path, failure):
    rows = [snapshot_container("a" * 64), snapshot_container("b" * 64)]
    frame = {"listed": [item["Id"] for item in rows], "inspect": rows}
    expected = 1
    if failure == "empty":
        frame["listed"] = []
    elif failure == "inspect":
        frame["exit"] = expected = 37
    elif failure == "vanished":
        frame["inspect"] = rows[:1]
        frame["exit"] = expected = 1
    else:
        frame["inspect"] = rows[:1]
    result, snapshot = docker_snapshot(bash, tmp_path, [frame])
    assert result.returncode == expected, result
    assert snapshot is None


@pytest.mark.parametrize("change", ["id", "status", "started", "restarts", "health"])
def test_snapshot_detects_changed_container(bash, tmp_path, change):
    before, after = (snapshot_container("a" * 64, Health={"Status": "healthy"}) for _ in range(2))
    if change == "id":
        after["Id"] = "b" * 64
    elif change == "status":
        after["State"]["Status"] = "exited"
    elif change == "started":
        after["State"]["StartedAt"] = "2026-11-19T07:00:01Z"
    elif change == "restarts":
        after["RestartCount"] = 1
    else:
        after["State"]["Health"]["Status"] = "unhealthy"
    frames = [{"listed": [item["Id"]], "inspect": [item]} for item in (before, after)]
    result, _ = docker_snapshot(bash, tmp_path, frames, compare=True)
    assert result.returncode == 1, result
    assert "differ" in result.stdout, result
