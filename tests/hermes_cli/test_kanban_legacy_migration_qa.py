"""In-flight work, eligibility rollback, and profile-data preservation."""
import hashlib
from pathlib import Path

import pytest

from hermes_cli import profiles

ELIGIBLE = ("implementer", "verifier")
LEGACY = ("retired-worker", "manual-worker")


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(key, raising=False)
    for name in ELIGIBLE + LEGACY:
        p = home / "profiles" / name
        (p / "sessions").mkdir(parents=True)
        (p / "memories").mkdir()
        (p / "profile.yaml").write_text(f"description: {name}\nkanban_dispatch: {name in ELIGIBLE}\n")
        (p / "sessions" / "s1.jsonl").write_text(f'{{"profile":"{name}","text":"history"}}\n')
        (p / "memories" / "MEMORY.md").write_text(f"{name} memory\n")
        (p / "state.db").write_bytes(b"legacy-state-" + name.encode())
    (home / "profile.yaml").write_text("kanban_dispatch: false\n")
    (home / "config.yaml").write_text(
        "kanban:\n  default_assignee: implementer\n  verify_profile: verifier\n")
    return home


def _snapshot(home):
    out = {}
    for name in LEGACY:
        for f in sorted((home / "profiles" / name).rglob("*")):
            if f.is_file() and f.name != "profile.yaml":
                out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def test_running_legacy_card_finishes_on_own_profile_then_verifier_reviews(fleet):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import kanban_dispatch_policy as policy
    kb.init_db()
    with kbc.connect_closing() as conn:
        policy.set_board_verifier(conn, "verifier")
        # A board policy applies even to a card owned by a now-retired profile.
        tid = kb.create_task(conn, title="in flight", assignee="implementer")
        conn.execute("UPDATE tasks SET assignee='retired-worker' WHERE id=?", (tid,))
        run = kb.claim_task(conn, tid)
        conn.execute("UPDATE task_runs SET profile='retired-worker' WHERE id=?", (run.current_run_id,))
        assert kb.get_task(conn, tid).status == "running"
        # Migration must not touch the running card.
        assert all(r.get("task_id", r.get("id")) != tid for r in policy.reassign_ineligible(conn))
        assert kb.get_task(conn, tid).assignee == "retired-worker"
        assert kb.complete_task(conn, tid, summary="done by legacy", expected_run_id=run.current_run_id)
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("review", "verifier")
        first = conn.execute("SELECT profile FROM task_runs WHERE task_id=? ORDER BY id LIMIT 1",
                             (tid,)).fetchone()[0]
        assert first == "retired-worker"


def test_flag_rollback_restores_full_legacy_roster(fleet):
    from hermes_cli import kanban_decompose as decompose, kanban_db_dispatch as dispatch
    assert decompose._build_roster()[1] == set(ELIGIBLE)
    for name in LEGACY:
        assert not dispatch.is_dispatch_eligible(name)
    for name in LEGACY:  # R1: delete the flag
        (fleet / "profiles" / name / "profile.yaml").write_text(f"description: {name}\n")
    assert decompose._build_roster()[1] == set(ELIGIBLE) | set(LEGACY)
    for name in LEGACY:
        assert dispatch.is_dispatch_eligible(name)
    for name in LEGACY:  # forward again
        (fleet / "profiles" / name / "profile.yaml").write_text("kanban_dispatch: false\n")
    assert decompose._build_roster()[1] == set(ELIGIBLE)


def test_dispatch_and_migration_never_write_legacy_profile_data(fleet):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as dispatch, kanban_dispatch_policy as policy
    before = _snapshot(fleet)
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="implementer")
        conn.execute("UPDATE tasks SET assignee='manual-worker' WHERE id=?", (tid,))
        dispatch.dispatch_once(conn, spawn_fn=lambda *_: pytest.fail("legacy spawned"))
        policy.reassign_ineligible(conn)
    assert _snapshot(fleet) == before
    for name in LEGACY:
        assert profiles.profile_exists(name)
