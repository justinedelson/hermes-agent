"""The session teardown stops processes left running against this run's Hermes homes.

A test that runs a real ``hermes chat -q`` child gets a grandchild ``gateway run`` in its own
session; no per-test subprocess patch sees it, and pytest's tmp cleanup removes only directories.
"""
from __future__ import annotations

import builtins
import io
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests import conftest as suite_conftest
from tests.waiting import wait_until


@pytest.fixture
def proc_homes(monkeypatch):
    """Control only /proc enumeration and environ bytes; home metadata stays real."""
    real_scandir, real_open = os.scandir, builtins.open
    homes = {}

    def scandir(path):
        if os.fspath(path) == "/proc":
            return [SimpleNamespace(name=str(pid)) for pid in homes]
        return real_scandir(path)

    def open_environ(path, *args, **kwargs):
        for pid, home in homes.items():
            if os.fspath(path) == f"/proc/{pid}/environ":
                return io.BytesIO(b"HERMES_HOME=" + os.fsencode(home) + b"\0")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(builtins, "open", open_environ)
    return homes


@pytest.mark.platforms("linux")
def test_unrelated_forbidden_home_is_rejected_before_metadata(tmp_path, monkeypatch, proc_homes):
    root = tmp_path / "session"
    root.mkdir()
    forbidden = tmp_path / "unrelated" / "production" / "profile"
    proc_homes[987654321] = str(forbidden)
    real_lstat = os.lstat
    probes = []

    def checked_lstat(path, *args, **kwargs):
        probes.append(os.fspath(path))
        assert not Path(path).is_relative_to(forbidden.parent.parent), "unrelated home metadata I/O"
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", checked_lstat)
    assert suite_conftest._processes_with_hermes_home_under([root]) == []
    assert not any(Path(path).is_relative_to(forbidden.parent.parent) for path in probes)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("home", [
    "", "relative/home", "~/session/home", "file:///session/home",
    "{root}-sibling/home", "{root}/../elsewhere/home", "{root}/child/../../elsewhere",
    "{root}/child/../home",
])
def test_lexically_unowned_or_ambiguous_homes_need_no_resolution(tmp_path, monkeypatch, proc_homes, home):
    root = tmp_path / "session"
    root.mkdir()
    proc_homes[987654321] = home.format(root=root)
    real_resolve = Path.resolve
    resolved = []

    def resolve(path, *args, **kwargs):
        resolved.append(path)
        assert path == root, "unowned or ambiguous candidate was resolved"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert suite_conftest._processes_with_hermes_home_under([root]) == []
    assert resolved == [root]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("roots", [[], [None, ""]])
def test_empty_session_roots_do_not_probe_proc(monkeypatch, roots):
    def forbidden_probe(*args, **kwargs):
        pytest.fail("empty session roots must not probe the host")

    with monkeypatch.context() as patch:
        patch.setattr(os.path, "isdir", forbidden_probe)
        patch.setattr(os, "scandir", forbidden_probe)
        assert suite_conftest._processes_with_hermes_home_under(roots) == []


@pytest.mark.platforms("linux")
@pytest.mark.require_symlinks
def test_session_root_alias_and_contained_symlinks_preserve_canonical_ownership(tmp_path, proc_homes):
    root = tmp_path / "session"
    home = root / "home"
    home.mkdir(parents=True)
    alias = tmp_path / "declared-alias"
    alias.symlink_to(root, target_is_directory=True)
    (root / "relative-link").symlink_to("home", target_is_directory=True)
    (root / "absolute-link").symlink_to(home, target_is_directory=True)
    proc_homes.update({
        987654321: str(home), 987654322: str(alias / "home"),
        987654323: str(root / "relative-link"), 987654324: str(root / "absolute-link"),
        987654325: str(root),
    })
    assert set(suite_conftest._processes_with_hermes_home_under([alias])) == set(proc_homes)


@pytest.mark.platforms("linux")
@pytest.mark.require_symlinks
def test_escaping_links_and_undeclared_external_aliases_are_not_owned(tmp_path, monkeypatch, proc_homes):
    root, outside = tmp_path / "session", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    alias = outside / "undeclared-alias"
    alias.symlink_to(root, target_is_directory=True)
    proc_homes.update({987654321: str(root / "escape"), 987654322: str(alias)})
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        assert path != alias, "undeclared external alias must not be probed"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert suite_conftest._processes_with_hermes_home_under([root]) == []


def _detached(env_home, marker):
    code = "import time\nwhile True: time.sleep(0.2)\n"
    env = {**os.environ, "HERMES_HOME": str(env_home), "LIVE_REAP_MARKER": marker}
    return subprocess.Popen([sys.executable, "-c", code], env=env, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.platforms("linux")
@pytest.mark.live_system_guard_bypass  # signals only the two children this test spawned
def test_session_teardown_terminates_daemons_under_the_session_basetemp_only(tmp_path):
    basetemp, elsewhere = tmp_path / "basetemp", tmp_path / "elsewhere"
    (basetemp / "t0" / "hermes").mkdir(parents=True)
    elsewhere.mkdir()
    children = []
    try:
        inside = _detached(basetemp / "t0" / "hermes", "inside")
        children.append(inside)
        outside = _detached(elsewhere, "outside")
        children.append(outside)
        # Both children have exec'd with their HERMES_HOME (what the reaper reads from /proc).
        wait_until(lambda: {inside.pid, outside.pid} <= set(suite_conftest._processes_with_hermes_home_under(
            [basetemp, elsewhere])), timeout=10, what='both detached children started')
        config = SimpleNamespace(_tmp_path_factory=SimpleNamespace(_basetemp=basetemp))
        reaped = suite_conftest._reap_session_hermes_processes(config)
        assert inside.pid in reaped and outside.pid not in reaped
        assert inside.wait(timeout=5) is not None
        assert outside.poll() is None, "a process outside this session's homes was signalled"
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
