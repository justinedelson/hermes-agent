"""Per-profile dispatch eligibility must not change manual profile resolution."""
from pathlib import Path

import pytest

from hermes_cli import profiles


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return home


@pytest.mark.parametrize("value,eligible", [
    ("false", False), ('"OFF"', False), ('"no"', False), ('"0"', False),
    ("null", True), ("true", True), ('"yes"', True), ("0", True),
])
def test_profile_dispatch_flag_is_normalized_without_hiding_manual_profile(profile_home, value, eligible):
    path = profile_home / "profiles" / "legacy"
    path.mkdir(parents=True)
    (path / "profile.yaml").write_text(f"kanban_dispatch: {value}\n")
    assert profiles.read_profile_meta(path).get("kanban_dispatch") is eligible
    assert profiles.profile_exists("legacy")
    info = next(p for p in profiles.list_profiles() if p.name == "legacy")
    assert info.kanban_dispatch is eligible
    assert profiles.get_profile_dir("legacy") == path


@pytest.mark.parametrize("metadata", [None, "description: Existing profile\n"])
def test_missing_dispatch_flag_defaults_to_eligible(profile_home, metadata):
    from hermes_cli import kanban_db_dispatch as dispatch

    path = profile_home / "profiles" / "existing"
    path.mkdir(parents=True)
    (path / "config.yaml").write_text("{}\n")
    if metadata is not None:
        (path / "profile.yaml").write_text(metadata)
    assert profiles.read_profile_meta(path)["kanban_dispatch"] is True
    assert next(p for p in profiles.list_profiles() if p.name == "existing").kanban_dispatch is True
    assert dispatch.is_dispatch_eligible("existing")


def test_ineligible_profile_remains_resolvable_and_selectable_manually(profile_home):
    path = profile_home / "profiles" / "legacy"
    path.mkdir(parents=True)
    (path / "profile.yaml").write_text("kanban_dispatch: false\n")
    assert profiles.resolve_profile_env("legacy") == str(path)
    profiles.set_active_profile("legacy")
    assert profiles.get_active_profile() == "legacy"
    assert profiles.resolve_profile_env(profiles.get_active_profile()) == str(path)


def test_eligible_roster_preserves_manual_opted_out_profiles(profile_home):
    from hermes_cli import kanban_decompose as decompose, kanban_db_dispatch as dispatch
    eligible = {"builder", "reviewer"}
    legacy = {"retired-builder", "manual-reviewer"}
    for name in eligible | legacy:
        path = profile_home / "profiles" / name
        path.mkdir(parents=True)
        (path / "profile.yaml").write_text(f"description: {name}\nkanban_dispatch: {name in eligible}\n")
    (profile_home / "profile.yaml").write_text("kanban_dispatch: false\n")
    (profile_home / "profiles" / "zz-test-fb").mkdir()
    roster, names = decompose._build_roster()
    assert names == eligible
    assert {row["name"] for row in roster} == eligible
    assert {p.name for p in profiles.list_profiles()} == eligible | legacy | {"default"}
    assert all(profiles.profile_exists(name) for name in legacy)
    assert all(dispatch._profile_exists_fn()(name) for name in eligible)
    assert not any(dispatch._profile_exists_fn()(name) for name in legacy | {"default", "zz-test-fb"})


def test_profile_rename_keeps_flag_and_sessions_but_not_old_dispatch_slug(profile_home):
    from hermes_cli import kanban_decompose as decompose
    old = profile_home / "profiles" / "old-worker"
    old.mkdir(parents=True)
    (old / "profile.yaml").write_text("description: Implementation\nkanban_dispatch: true\n")
    (old / "sessions").mkdir()
    (old / "sessions" / "history.jsonl").write_text('{"text":"retained history"}\n')
    (profile_home / "profile.yaml").write_text("kanban_dispatch: false\n")
    renamed = profiles.rename_profile("old-worker", "builder")
    assert (renamed / "sessions" / "history.jsonl").read_text() == '{"text":"retained history"}\n'
    assert profiles.read_profile_meta(renamed)["kanban_dispatch"] is True
    assert decompose._build_roster()[1] == {"builder"}
    assert not profiles.profile_exists("old-worker")


def test_opt_out_filters_routing_and_blocks_dispatch_then_flag_rollback_restores_it(profile_home):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as dispatch, kanban_decompose as decompose

    for name in ("implementer", "verifier", "legacy"):
        path = profile_home / "profiles" / name
        path.mkdir(parents=True)
        (path / "profile.yaml").write_text(f"description: {name}\nkanban_dispatch: {name != 'legacy'}\n")
    (profile_home / "profile.yaml").write_text("kanban_dispatch: false\n")
    cfg = {"kanban": {"default_assignee": "legacy"}}
    assert decompose._resolve_profile_from_cfg(cfg, "default_assignee", fallback="implementer") == "implementer"
    roster, names = decompose._build_roster()
    assert names == {"implementer", "verifier"}
    assert {r["name"] for r in roster} == names
    assert dispatch._resolve_default_assignee("legacy") is None
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="old card", assignee="legacy")
        res = dispatch.dispatch_once(conn, spawn_fn=lambda *_: pytest.fail("ineligible worker spawned"))
        assert res.skipped_ineligible == [tid]
        assert kb.get_task(conn, tid).status == "blocked"
        assert "skipped_ineligible" in [e.kind for e in kb.list_events(conn, tid)]
        assert not dispatch.has_spawnable_ready(conn)
    (profile_home / "profiles" / "legacy" / "profile.yaml").write_text("kanban_dispatch: true\n")
    assert "legacy" in decompose._build_roster()[1]
    assert dispatch.is_dispatch_eligible("legacy")


def test_opted_out_review_assignee_is_blocked_once(profile_home):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
    path = profile_home / "profiles" / "legacy"
    path.mkdir(parents=True)
    (path / "profile.yaml").write_text("kanban_dispatch: false\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="idle review", assignee="legacy")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *_: pytest.fail("ineligible reviewer spawned"))
        assert result.skipped_ineligible == [tid]
        assert kb.get_task(conn, tid).status == "blocked"
        dispatch.dispatch_once(conn)
        assert sum(e.kind == "skipped_ineligible" for e in kb.list_events(conn, tid)) == 1


def test_no_eligible_assignee_leaves_triage_without_calling_llm(profile_home, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_decompose as decompose
    (profile_home / "profile.yaml").write_text("kanban_dispatch: false\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough task", triage=True)
    monkeypatch.setattr(decompose, "_call_aux", lambda *_a, **_kw: pytest.fail("no eligible worker"))
    assert decompose.decompose_task(tid).reason == "no_eligible_assignee"
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        assert kb.list_events(conn, tid)[-1].payload == {"reason": "no_eligible_assignee"}


def test_automatic_verification_gates_children_and_preserves_handoff(profile_home):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
    for name in ("implementer", "verifier"):
        path = profile_home / "profiles" / name
        path.mkdir(parents=True)
        (path / "profile.yaml").write_text("kanban_dispatch: true\n")
    (profile_home / "config.yaml").write_text("kanban:\n  verify_profile: verifier\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        from hermes_cli.kanban_dispatch_policy import set_board_verifier
        set_board_verifier(conn, "verifier")
        tid = kb.create_task(conn, title="implement", assignee="implementer")
        child = kb.create_task(conn, title="after approval", assignee="implementer", parents=[tid])
        run = kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="tested implementation", metadata={"tests": 12}, expected_run_id=run.current_run_id)
        assert kb.get_task(conn, tid).status == "review"
        assert kb.get_task(conn, tid).assignee == "verifier"
        assert kb.get_task(conn, child).status == "todo"
        assert kb.latest_run(conn, tid).metadata["tests"] == 12
        assert dispatch.dispatch_once(conn, dry_run=True).spawned[0][1] == "verifier"
        review = kb.claim_review_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="independently verified", expected_run_id=review.current_run_id)
        assert kb.get_task(conn, tid).status == "done"
        assert kb.get_task(conn, child).status == "ready"


@pytest.mark.parametrize("child_assignee", ["implementer", "verifier"])
def test_existing_verifier_child_owns_verification_lane(profile_home, child_assignee):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    for name in ("implementer", "verifier"):
        path = profile_home / "profiles" / name
        path.mkdir(parents=True)
        (path / "profile.yaml").write_text("kanban_dispatch: true\n")
    (profile_home / "config.yaml").write_text("kanban:\n  verify_profile: verifier\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        from hermes_cli.kanban_dispatch_policy import set_board_verifier
        set_board_verifier(conn, "verifier")
        tid = kb.create_task(conn, title="implementation", assignee="implementer")
        child = kb.create_task(conn, title="downstream", assignee=child_assignee, parents=[tid])
        run = kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="implementation tested", expected_run_id=run.current_run_id)
        assert kb.get_task(conn, tid).status == ("done" if child_assignee == "verifier" else "review")
        assert kb.get_task(conn, child).status == ("ready" if child_assignee == "verifier" else "todo")


def test_worker_uses_board_snapshot_not_root_inheritance(profile_home, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    for name in ("implementer", "verifier"):
        path = profile_home / "profiles" / name
        path.mkdir(parents=True)
        (path / "profile.yaml").write_text("kanban_dispatch: true\n")
        (path / "config.yaml").write_text("{}\n")
    (profile_home / "config.yaml").write_text("kanban:\n  verify_profile: verifier\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        from hermes_cli.kanban_dispatch_policy import set_board_verifier
        set_board_verifier(conn, "verifier")
        monkeypatch.setenv("HERMES_HOME", str(profile_home / "profiles" / "implementer"))
        tid = kb.create_task(conn, title="board policy", assignee="implementer")
        run = kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="tested", expected_run_id=run.current_run_id)
        assert kb.get_task(conn, tid).status == "review"
        assert kb.get_task(conn, tid).assignee == "verifier"


@pytest.mark.parametrize("accepted", [False, True])
def test_automatic_review_keeps_pr_acceptance_gate_and_owner(profile_home, monkeypatch, accepted):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import kanban_pr_acceptance_store as store
    for name in ("implementer", "verifier"):
        path = profile_home / "profiles" / name
        path.mkdir(parents=True)
        (path / "profile.yaml").write_text("kanban_dispatch: true\n")
    (profile_home / "config.yaml").write_text("kanban:\n  verify_profile: verifier\n")
    owners = []
    def collect(_contract, _pr, *, assignee):
        owners.append(assignee)
        return {"ok": accepted, "classification": "success" if accepted else "checks",
                "detail": "unit test", "recovery": "retry"}
    monkeypatch.setattr(store, "collect_acceptance", collect)
    kb.init_db()
    with kbc.connect_closing() as conn:
        from hermes_cli.kanban_dispatch_policy import set_board_verifier
        set_board_verifier(conn, "verifier")
        tid = kb.create_task(conn, title="publish", assignee="implementer", completion_contract="acme/repo")
        run = kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="published", expected_run_id=run.current_run_id,
                                metadata={"published_pr": "https://github.com/acme/repo/pull/7"}) is accepted
        assert any(e.kind == "pr_acceptance" for e in kb.list_events(conn, tid))
        assert kb.get_task(conn, tid).status == ("review" if accepted else "running")
        if accepted:
            review = kb.claim_review_task(conn, tid)
            assert kb.complete_task(conn, tid, summary="verified", expected_run_id=review.current_run_id)
            assert owners == ["implementer", "implementer"]


def test_three_review_rejections_escalate_to_human(profile_home, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    hooks = []
    monkeypatch.setattr(kb, "_fire_task_hook", lambda name, *_a, **_kw: hooks.append(name))
    (profile_home / "config.yaml").write_text("kanban:\n  verify_profile: default\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        from hermes_cli.kanban_dispatch_policy import set_board_verifier
        set_board_verifier(conn, "default")
        tid = kb.create_task(conn, title="implementation", assignee="builder")
        for cycle in range(3):
            implementation = kb.claim_task(conn, tid)
            assert kb.complete_task(conn, tid, summary="tests run", expected_run_id=implementation.current_run_id)
            review = kb.claim_review_task(conn, tid)
            assert kb.request_changes(conn, tid, reason=f"fix cycle {cycle}", expected_run_id=review.current_run_id)[0]
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.list_events(conn, tid)[-1].payload["audience"] == "human"
        assert hooks.count("kanban_task_blocked") == 1


@pytest.fixture
def ineligible_boundary_card(profile_home):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    path = profile_home / "profiles" / "legacy"
    path.mkdir(parents=True)
    (path / "profile.yaml").write_text("kanban_dispatch: false\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="existing", assignee="default")
        yield conn, tid
        assert len(kb.list_tasks(conn)) == 1


def test_review_rejects_ineligible_profile(ineligible_boundary_card):
    from hermes_cli import kanban_db as kb
    conn, tid = ineligible_boundary_card
    with pytest.raises(ValueError, match="Eligible profiles: default"):
        kb.request_review(conn, tid, summary="test", reviewer="legacy")
    assert kb.get_task(conn, tid).status == "ready"


def test_picker_marks_ineligible_profile_without_hiding_it(ineligible_boundary_card):
    from hermes_cli import kanban_db as kb
    conn, _tid = ineligible_boundary_card
    legacy = next(a for a in kb.known_assignees(conn) if a["name"] == "legacy")
    assert legacy["dispatch_eligible"] is False
    assert legacy["on_disk"] is True


def test_cli_assign_requires_override_for_ineligible_profile(ineligible_boundary_card):
    import argparse
    from hermes_cli import kanban as cli, kanban_db as kb
    conn, tid = ineligible_boundary_card
    args = argparse.Namespace(task_id=tid, profile="legacy", allow_ineligible=False)
    assert cli._cmd_assign(args) == 2
    assert kb.get_task(conn, tid).assignee == "default"
    args.allow_ineligible = True
    assert cli._cmd_assign(args) == 0
    assert kb.get_task(conn, tid).assignee == "legacy"


def test_cli_create_rejects_ineligible_profile(ineligible_boundary_card, capsys):
    import argparse
    from hermes_cli import kanban as cli
    args = argparse.Namespace(title="bad", assignee="legacy", body=None, allow_ineligible=False)
    assert cli._cmd_create(args) == 2
    assert "Eligible profiles: default" in capsys.readouterr().err


def test_tool_create_rejects_ineligible_profile(ineligible_boundary_card):
    import json
    from tools import kanban_tools
    result = json.loads(kanban_tools._handle_create({"title": "bad", "assignee": "legacy"}))
    assert "Eligible profiles: default" in result.get("error", "")


def test_migration_preview_never_opens_a_write_transaction(profile_home, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_dispatch_policy import reassign_ineligible
    kb.init_db()
    with kbc.connect_closing() as conn:
        monkeypatch.setattr(kb, "write_txn", lambda *_a, **_kw: pytest.fail("preview requested mutation authority"))
        assert reassign_ineligible(conn, dry_run=True) == []


def test_migration_does_not_take_cards_excluded_only_by_home_allowlist(profile_home):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_dispatch_policy import reassign_ineligible
    for name in ("implementer", "verifier", "remote"):
        path = profile_home / "profiles" / name
        path.mkdir(parents=True)
        (path / "profile.yaml").write_text("kanban_dispatch: true\n")
    (profile_home / "config.yaml").write_text(
        "kanban:\n  default_assignee: implementer\n  verify_profile: verifier\n"
        "  dispatch_profiles: [implementer, verifier]\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="another home's work", assignee="remote")
        assert reassign_ineligible(conn) == []
        assert kb.get_task(conn, tid).assignee == "remote"


def test_migration_only_reassigns_idle_open_cards_and_is_idempotent(profile_home):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import kanban_dispatch_policy as policy
    for name in ("implementer", "verifier", "legacy"):
        path = profile_home / "profiles" / name
        path.mkdir(parents=True)
        (path / "profile.yaml").write_text(f"kanban_dispatch: {name != 'legacy'}\n")
    (profile_home / "config.yaml").write_text("kanban:\n  default_assignee: implementer\n  verify_profile: verifier\n")
    kb.init_db()
    with kbc.connect_closing() as conn:
        ids = {}
        for status in ("triage", "todo", "ready", "blocked", "review", "running", "done", "archived"):
            tid = kb.create_task(conn, title=status, assignee="legacy")
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
            ids[status] = tid
        before = [tuple(row) for row in conn.execute("SELECT * FROM task_runs")]
        dry = policy.reassign_ineligible(conn, dry_run=True)
        assert len(dry) == 5
        assert all(kb.get_task(conn, tid).assignee == "legacy" for tid in ids.values())
        assert policy.reassign_ineligible(conn) == dry
        assert policy.reassign_ineligible(conn) == []
        for status, tid in ids.items():
            expected = "legacy" if status in ("running", "done", "archived") else "verifier" if status == "review" else "implementer"
            assert kb.get_task(conn, tid).assignee == expected
            assert kb.get_task(conn, tid).status == status
        assert [tuple(row) for row in conn.execute("SELECT * FROM task_runs")] == before
