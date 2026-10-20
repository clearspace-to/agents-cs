"""The plugin's contract with Hermes: every Hermes function the adapter calls, with the keyword arguments
it passes. Runs only where Hermes is installed (``scripts/check-hermes-contract.sh``, on the Hermes Mac)
and skips elsewhere, so a Hermes upgrade that renames or re-shapes one of these fails a test there
instead of the phone's board."""
import inspect
import unittest

try:
    from hermes_cli import kanban_db  # noqa: F401
    HERMES = True
except Exception:  # pragma: no cover - the usual case on a dev Mac
    HERMES = False

# (module, attribute, keyword arguments the adapter passes) — tasks.HermesBoard, cards/routes.py,
# creation.py and coordinator.py.
CONTRACT = [
    ("hermes_cli.kanban_db", "init_db", {"board"}),
    ("hermes_cli.kanban_db", "board_exists", set()),
    ("hermes_cli.kanban_db", "DEFAULT_BOARD", None),
    ("hermes_cli.kanban_db", "get_current_board", set()),
    ("hermes_cli.kanban_db", "scoped_current_board", set()),
    ("hermes_cli.kanban_db", "list_tasks", {"include_archived"}),
    ("hermes_cli.kanban_db", "latest_summaries", set()),
    ("hermes_cli.kanban_db", "get_task", set()),
    ("hermes_cli.kanban_db", "list_comments", set()),
    ("hermes_cli.kanban_db", "unsatisfied_parents", set()),
    ("hermes_cli.kanban_db", "add_comment", set()),
    ("hermes_cli.kanban_db", "create_task", {"title", "body", "assignee", "created_by", "priority", "triage", "idempotency_key", "board"}),
    ("hermes_cli.kanban_db", "promote_task", {"actor", "reason"}),
    ("hermes_cli.kanban_db", "unblock_task", set()),
    ("hermes_cli.kanban_db", "specify_triage_task", {"author"}),
    ("hermes_cli.kanban_db", "reopen_review_task", set()),
    ("hermes_cli.kanban_db", "schedule_task", {"reason", "expected_run_id"}),
    ("hermes_cli.kanban_db", "archive_task", set()),
    ("hermes_cli.kanban_db", "link_tasks", set()),
    ("hermes_cli.kanban_db", "unlink_tasks", set()),
    ("plugins.kanban.dashboard.plugin_api", "update_task", {"board"}),
    ("plugins.kanban.dashboard.plugin_api", "UpdateTaskBody", None),
    ("hermes_cli.kanban_db_connect", "connect", {"board"}),
    ("hermes_cli.kanban_db_dispatch", "dispatch_once", {"dry_run", "max_spawn", "board"}),
    ("hermes_cli.kanban_specify", "specify_task", {"author"}),
    ("hermes_cli.kanban_decompose", "decompose_task", {"author"}),
    ("hermes_cli.config", "load_config_readonly", set()),
    ("hermes_cli.profiles", "get_active_profile_name", set()),
    ("hermes_cli.profiles", "profile_exists", set()),
    ("hermes_cli.web_server_chat", "_server_internal_ws_url", set()),
    ("agent.secret_scope", "build_profile_secret_scope", set()),
    ("agent.secret_scope", "set_secret_scope", {"profile_home"}),
    ("agent.secret_scope", "reset_secret_scope", set()),
    # cron_archive.py
    ("hermes_state", "SessionDB", {"db_path"}),
    ("tools.bot_live_delivery", "read_delivery_result", set()),
]
TABLES = {"tasks": {"id", "status", "assignee", "current_run_id"}, "task_comments": {"task_id"},
          "task_events": {"id", "task_id", "kind", "payload", "created_at", "run_id"},
          "task_links": {"parent_id", "child_id"}}
HOOKS = {"kanban_task_blocked", "kanban_task_completed", "kanban_task_claimed", "on_kanban_dispatch_tick",
         "pre_tool_call", "post_tool_call", "pre_llm_call", "transform_llm_output", "transform_tool_result",
         "pre_approval_request", "post_approval_response", "post_llm_call"}


@unittest.skipUnless(HERMES, "Hermes is not installed here; run scripts/check-hermes-contract.sh against the Hermes Mac")
class HermesContractTests(unittest.TestCase):
    def test_functions_and_keyword_arguments(self):
        import importlib
        for module_name, attribute, keywords in CONTRACT:
            with self.subTest(f"{module_name}.{attribute}"):
                module = importlib.import_module(module_name)
                self.assertTrue(hasattr(module, attribute), f"{module_name} has no {attribute}")
                if keywords is None:
                    continue
                target = getattr(module, attribute)
                parameters = inspect.signature(target).parameters
                accepts_any = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
                for keyword in keywords:
                    self.assertTrue(accepts_any or keyword in parameters, f"{module_name}.{attribute} no longer takes {keyword}")

    def test_tables_and_columns_the_adapter_reads(self):
        from hermes_cli import kanban_db, kanban_db_connect
        kanban_db.init_db(board=None)
        conn = kanban_db_connect.connect(board=None)
        try:
            for table, columns in TABLES.items():
                present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertTrue(columns <= present, f"{table} lacks {columns - present}")
        finally:
            conn.close()

    def test_what_the_cron_run_archive_reads(self):
        # cron_archive.py swallows every error so it can't break Hermes; a rename upstream would quietly
        # stop archiving. Its reads, archive call and Hermes' Bot Chat delivery id are checked here.
        import sqlite3
        from pathlib import Path
        from hermes_state import SessionDB
        from cron import executions
        self.assertTrue(callable(getattr(SessionDB, "set_session_archived", None)))
        con = sqlite3.connect(":memory:")
        executions._initialize_schema(con)
        present = {row[1] for row in con.execute("PRAGMA table_info(executions)")}
        self.assertTrue({"id", "job_id", "status", "delivery_outcome", "started_at"} <= present)
        source = (Path(executions.__file__).parent / "scheduler_delivery.py").read_text()
        self.assertIn("[str(source_home), job_id, str(run_id), str(home)]", source,
                      "Hermes changed how it keys a Bot Chat delivery; update cron_archive.delivery_id")

    def test_hooks_are_still_valid(self):
        from hermes_cli.plugins import VALID_HOOKS
        self.assertTrue(HOOKS <= set(VALID_HOOKS), HOOKS - set(VALID_HOOKS))

    def test_helpers_the_alerts_import_still_exist(self):
        # alerts.py swallows import errors to never break Hermes, so a rename upstream would silently
        # lose profile names and redaction. Check them directly.
        from hermes_constants import get_hermes_home
        from agent.redact import redact_sensitive_text
        self.assertTrue(callable(get_hermes_home))
        self.assertIsInstance(redact_sensitive_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789"), str)

    def test_what_the_mobile_module_relies_on(self):
        # mobile/routes.py and mobile/scan.py: the session a dashboard route can read, the root home, and the columns of the bots' state.db that decide what counts as a reply (mobile/replies.py).
        import sqlite3
        import tempfile
        from pathlib import Path
        from hermes_cli.dashboard_auth.base import Session
        from hermes_constants import get_default_hermes_root
        from hermes_state import SessionDB
        self.assertTrue(callable(get_default_hermes_root))
        self.assertIn("user_id", {field for field in getattr(Session, "__dataclass_fields__", {})})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.db"
            SessionDB(db_path=path).close()
            with sqlite3.connect(path) as con:
                messages = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
                sessions = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
        self.assertTrue({"id", "session_id", "role", "content", "display_kind", "finish_reason", "tool_calls", "active"} <= messages,
                        messages)
        self.assertTrue({"id", "source", "hidden", "archived", "title", "parent_session_id"} <= sessions, sessions)

    def test_websockets_is_available_for_the_coordinator(self):
        from websockets.sync.client import connect  # noqa: F401


if __name__ == "__main__":
    unittest.main()
