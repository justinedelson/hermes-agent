"""Kanban mutation-boundary policy, separate from transport and profile CRUD."""
import contextlib

from hermes_cli.kanban_db_dispatch import is_dispatch_eligible


def eligible_profile_names(*, read_only=False) -> list[str]:
    from hermes_cli.profiles import list_profile_names
    return sorted(name for name in list_profile_names() if is_dispatch_eligible(name, read_only=read_only))


def require_dispatch_eligible(name: str, *, read_only=False) -> None:
    if name and not is_dispatch_eligible(name, read_only=read_only):
        raise ValueError(f"profile {name!r} is not Kanban-eligible. Eligible profiles: "
                         f"{', '.join(eligible_profile_names(read_only=read_only)) or '(none)'}")


def board_verifier(conn):
    """None means absent; an explicit empty override disables verification."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='board_config' AND type='table'").fetchone() is None:
        return None
    row = conn.execute("SELECT value FROM board_config WHERE key='verify_profile'").fetchone()
    return row[0] if row else None


def set_board_verifier(conn, name):
    from hermes_cli import kanban_db as kb
    name = str(name).strip()
    require_dispatch_eligible(name)
    with kb.write_txn(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS board_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO board_config(key,value) VALUES ('verify_profile',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name,))


def cmd_board_config(args):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        if args.config_action == "set":
            set_board_verifier(conn, args.value)
        elif args.config_action == "unset":
            if board_verifier(conn) is not None:
                with kbc.write_txn(conn):
                    conn.execute("DELETE FROM board_config WHERE key='verify_profile'")
        print(board_verifier(conn))
    return 0


@contextlib.contextmanager
def _board_snapshot(path):
    """Inspect a disposable copy without initializing or writing source sidecars."""
    import sqlite3
    import tempfile
    import shutil
    from hermes_cli.sqlite_safe_read import offline_file_access
    if not path.is_file():
        raise ValueError("board has no existing database")
    # SQLite's ordinary read-only WAL connection may still write shared-memory
    # read marks. Inspect a disposable copy instead; never open a source DB fd
    # while this process holds SQLite locks on that database.
    with tempfile.TemporaryDirectory(prefix="kanban-policy-") as temp:
        from pathlib import Path
        copy = Path(temp) / "kanban.db"
        with offline_file_access(path):
            shutil.copyfile(path, copy)
            wal = path.with_name(path.name + "-wal")
            if wal.exists():
                shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        with contextlib.closing(sqlite3.connect(copy)) as conn:
            conn.row_factory = sqlite3.Row
            yield conn


def _read_board_policy(path):
    with _board_snapshot(path) as conn:
        return board_verifier(conn)


def cmd_migrate_verification(args):
    import contextlib
    import sqlite3
    from hermes_cli import kanban_db as kb
    name = args.verify_profile.strip()
    require_dispatch_eligible(name, read_only=not args.apply)
    if args.apply and args.dry_run:
        raise ValueError("choose --dry-run or --apply, not both")
    paths = [(slug, kb.kanban_db_path(board=slug)) for slug in dict.fromkeys(args.boards)]
    for slug, path in paths:
        if not path.is_file() or kb.read_board_metadata(slug).get("archived"):
            raise ValueError(f"board {slug!r} has no existing live database")
    changes = [{"board": slug, "from": _read_board_policy(path), "to": name} for slug, path in paths]
    for (slug, path), change in zip(paths, changes):
        if args.apply:
            # Deliberately bypass connect/init: policy migration must never
            # upgrade historical task/run/event data as an incidental effect.
            with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as conn:
                set_board_verifier(conn, name)
                if board_verifier(conn) != name:
                    raise RuntimeError(f"board {slug!r} policy write did not persist")
        print(f"{slug}: {change['from']!r} -> {name!r} ({'applied' if args.apply else 'preview'})")
    return 0


def warn_root_verification_policy(config=None, *, conn=None):
    """Root inspection is diagnostic only; it must never supply worker policy."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import _get_default_hermes_home
    from hermes_cli.config_backend import read_config_doc_readonly
    from hermes_yaml import YAMLError
    root = _get_default_hermes_home()
    try:
        raw = read_config_doc_readonly(root / "config.yaml") or {}
    except (OSError, ValueError, YAMLError):
        return config
    root_kanban = raw.get("kanban") if isinstance(raw, dict) else None
    if not isinstance(root_kanban, dict) or not str(root_kanban.get("verify_profile") or "").strip():
        return config
    if conn is not None:
        override = board_verifier(conn)
    else:
        import contextlib
        import sqlite3
        path = kb.kanban_db_path()
        override = None
        try:
            if path.is_file():
                with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
                    override = board_verifier(reader)
        except (sqlite3.Error, OSError) as exc:
            kb._log.warning("Unable to inspect board verification policy: %s", exc)
            return config
    if override is None:
        kb._log.warning("Board lacks verify_profile: a named owning profile does not inherit root "
                        "kanban.verify_profile. Set board policy with `hermes kanban --board BOARD "
                        "config set verify_profile NAME` or preview `hermes kanban migrate-verification`.")
    return config


def configured_verifier(conn=None, *, owner_home=None, read_only=False) -> str:
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli import kanban_db as kb
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    # The caller binds the owning profile; named profiles never inherit root policy.
    home = owner_home or get_hermes_home()
    token = set_hermes_home_override(home)
    try:
        cfg = load_user_config_effective(home / "config.yaml", fail_closed=True, backup_good=not read_only) or {}
        override = board_verifier(conn) if conn is not None else None
        name = override if override is not None else str((cfg.get("kanban") or {}).get("verify_profile") or "").strip()
        if name and not is_dispatch_eligible(name, cfg, read_only=read_only):
            kb._log.warning("kanban.verify_profile %r is not eligible; automatic verification disabled", name)
            return ""
        return name
    finally:
        reset_hermes_home_override(token)


def claim_verification_policy(conn, task_id, source_status):
    """Freeze even an empty policy, so later config changes cannot alter a run."""
    from hermes_cli import kanban_db as kb
    if source_status == "review":
        review = kb._latest_event(conn, task_id, "review_requested")
        origin = kb._json_dict(kb._row_get(review, "payload"))
        return {key: origin[key] for key in ("automatic_verification", "verification_sequence") if key in origin}
    from hermes_cli.profiles import get_profile_dir
    task = kb.get_task(conn, task_id)
    assert task is not None  # The caller has just opened this task's run under its write lock.
    name = configured_verifier(conn, owner_home=get_profile_dir(task.assignee) if task.assignee else None)
    previous = conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? AND kind IN "
                            "('changes_requested','unblocked','completed','review_requested') ORDER BY id DESC LIMIT 1",
                            (task_id,)).fetchone()
    prior = kb._json_dict(kb._row_get(previous, "payload"))
    sequence = prior.get("verification_sequence") if previous and previous["kind"] == "changes_requested" else None
    return {"verify_profile": name, "automatic_verification": bool(name),
            "verification_sequence": sequence or kb._current_run_id(conn, task_id)}


def run_verifier(conn, task):
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import get_profile_dir
    claimed = kb._latest_event(conn, task.id, "claimed", task.current_run_id) if task.current_run_id else None
    payload = kb._json_dict(kb._row_get(claimed, "payload"))
    if payload.get("source_status") == "review":
        return ""
    if "verify_profile" in payload:
        return payload["verify_profile"]
    # Old runs have no snapshot. Only the run's explicit owner may supply policy,
    # never newly migrated board metadata or the launch/default profile.
    if task.current_run_id:
        row = conn.execute("SELECT profile FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()
        if row and row["profile"]:
            return configured_verifier(owner_home=get_profile_dir(row["profile"]))
        return ""
    return configured_verifier(conn, owner_home=get_profile_dir(task.assignee) if task.assignee else None)


def automatic_review_origin(conn, task_id, run_id):
    from hermes_cli import kanban_db as kb
    event = kb._latest_event(conn, task_id, "claimed", run_id) if run_id else None
    policy = kb._json_dict(kb._row_get(event, "payload"))
    return {"automatic_verification": True, "verification_sequence": policy.get("verification_sequence") or run_id}


def review_rejection_count(conn, task_id, handoff):
    from hermes_cli import kanban_db as kb
    sequence = handoff.get("verification_sequence")
    if not handoff.get("automatic_verification") or sequence is None:
        return 0
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='changes_requested'", (task_id,))
    return 1 + sum(kb._json_dict(row[0]).get("verification_sequence") == sequence for row in rows)


def is_review_phase(conn, task_id):
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    if task is None:
        return False
    if task.status == "review":
        return True
    if task.status == "blocked":
        return kb._resume_status_from_events(conn, task_id) == "review"
    return task.status == "running" and kb._retry_status_for_run(conn, task_id) == "review"


def completion_verifier(conn, task_id: str) -> str:
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    name = run_verifier(conn, task) if task else ""
    if not name or task is None or task.assignee == name or is_review_phase(conn, task_id):
        return ""
    # A pre-created verification lane must be released, not duplicated on this
    # card: otherwise completion holds the very verifier waiting downstream.
    if conn.execute(
        "SELECT 1 FROM task_links l JOIN tasks t ON t.id = l.child_id "
        "WHERE l.parent_id = ? AND t.assignee = ? "
        "AND t.status NOT IN ('done', 'archived') LIMIT 1", (task_id, name),
    ).fetchone():
        return ""
    event = kb._latest_event(conn, task_id, "claimed", task.current_run_id) if task.current_run_id else None
    if kb._json_dict(kb._row_get(event, "payload")).get("source_status") == "review":
        return ""
    return name


def request_review_changes(conn, task_id, *, reason, expected_run_id=None, ask=None, audience=None):
    """Return review work to its implementer; repeated rejection needs a human."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_notification_fields import notification_fields
    action = notification_fields(ask, audience)
    reason = str(kb.redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"

    with kb.write_txn(conn):
        task_row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if task_row is None:
            return False, "task not found"
        current_run_id = task_row["current_run_id"]
        if task_row["status"] != "running" or current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"

        claimed_event = kb._latest_event(conn, task_id, "claimed", current_run_id)
        claimed_payload = kb._json_dict(kb._row_get(claimed_event, "payload"))
        if claimed_payload.get("source_status") != "review":
            return False, "active run was not claimed from review"

        requested_event = kb._latest_event(conn, task_id, "review_requested")
        if requested_event is None:
            return False, "no prior review_requested event"
        handoff = kb._json_dict(requested_event["payload"])
        implementer = kb._nonblank_str(handoff.get("implementer"))
        if implementer is None:
            return False, "review handoff has no valid implementer provenance"
        reviewer = kb._canonical_assignee(kb._nonblank_str(task_row["assignee"]))

        new_status = kb._landing_status_after_parents(conn, task_id)
        review_cycles = review_rejection_count(conn, task_id, handoff)
        if review_cycles >= 3:
            new_status = "blocked"
        # Review is not success: preserve consecutive_failures until completion.
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = COALESCE(?, assignee),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL
             WHERE id = ? AND status = 'running' AND current_run_id = ?
            """,
            (new_status, implementer, task_id, int(current_run_id)),
        )
        if cur.rowcount != 1:
            return False, "task changed during review handoff"
        run_id = kb._end_run(
            conn, task_id, outcome="changes_requested", status=new_status, summary=reason,
        )
        kb._append_event(
            conn, task_id, "changes_requested",
            {"reason": reason, "implementer": implementer, "reviewer": reviewer,
             "status": new_status, **action,
             **{key: handoff[key] for key in ("automatic_verification", "verification_sequence") if key in handoff}},
            run_id=run_id,
        )
        if review_cycles >= 3:
            conn.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (task_id,))
            kb._append_event(conn, task_id, "blocked", {
                "reason": f"verification requested changes {review_cycles} times; human decision required",
                "kind": "needs_input", "audience": "human", "review_cycles": review_cycles,
                "ask": "Decide whether to continue revision or revise the acceptance criteria.",
            }, run_id=run_id)
    if review_cycles >= 3:
        kb._fire_task_hook("kanban_task_blocked", kb.get_task(conn, task_id), task_id, run_id,
                           reason=f"verification requested changes {review_cycles} times; human decision required")
    return True, implementer


def skip_ineligible_task(conn, task_id, assignee, result, *, dry_run=False) -> bool:
    """Block explicit profile opt-outs, leaving foreign-home claims untouched."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import get_profile_dir, profile_exists, read_profile_meta
    if not profile_exists(assignee) or read_profile_meta(get_profile_dir(assignee)).get("kanban_dispatch") is not False:
        return False
    result.skipped_ineligible.append(task_id)
    if not dry_run and kb.block_task(
        conn, task_id,
        reason=f"assignee {assignee} is not Kanban-eligible (kanban_dispatch: false); reassign to an eligible profile",
        kind="needs_input", ask="Reassign to an eligible profile and unblock the card.", audience="human",
    ):
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "skipped_ineligible", {"assignee": assignee})
    return True


def reassign_ineligible(conn, *, dry_run: bool = False) -> list[dict]:
    """Idempotent open-card migration. Claims and historical runs are immutable."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.config_effective import load_user_config_effective
    cfg = load_user_config_effective(fail_closed=True, backup_good=not dry_run) or {}
    implementer = str((cfg.get("kanban") or {}).get("default_assignee") or "").strip()
    verifier = configured_verifier(conn, read_only=dry_run)
    from hermes_cli.profiles import get_profile_dir, profile_exists, read_profile_meta
    from contextlib import nullcontext
    changes = []
    with nullcontext() if dry_run else kb.write_txn(conn):
        rows = conn.execute("SELECT id, status, assignee FROM tasks WHERE status IN "
                            "('triage', 'todo', 'ready', 'blocked', 'review') ORDER BY id").fetchall()
        for row in rows:
            # A local claim exclusion is not a global retirement: shared-board
            # cards may belong to another dispatch home. Migrate opt-outs only.
            if (not row["assignee"] or not profile_exists(row["assignee"])
                    or read_profile_meta(get_profile_dir(row["assignee"])).get("kanban_dispatch") is not False):
                continue
            target = verifier if is_review_phase(conn, row["id"]) else implementer
            require_dispatch_eligible(target, read_only=dry_run)
            if not target:
                raise ValueError("migration requires eligible default_assignee and verify_profile")
            change = {"task_id": row["id"], "from": row["assignee"], "to": target,
                      "reason": "kanban_dispatch_ineligible"}
            changes.append(change)
            if not dry_run:
                conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (target, row["id"]))
                kb._append_event(conn, row["id"], "reassigned", change)
    return changes


def cmd_reassign_ineligible(args) -> int:
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban as cli
    try:
        connection = _board_snapshot(cli.kb.kanban_db_path()) if args.dry_run else kbc.connect_closing()
        with connection as conn:
            changes = reassign_ineligible(conn, dry_run=args.dry_run)
    except ValueError as exc:
        return cli._err(f"kanban: {exc}", 2)
    if getattr(args, "json", False):
        cli._print_json(changes)
    else:
        for change in changes:
            print(f"{change['task_id']}: {change['from']} -> {change['to']}")
        print(f"{'Would reassign' if args.dry_run else 'Reassigned'} {len(changes)} cards")
    return 0
