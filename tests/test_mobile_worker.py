"""The scan worker: runs once per computer, in ``hermes serve`` of the root home only, as a Hermes background task
(or a thread when no loop is running yet), and nowhere else."""
import asyncio
import sys
import sqlite3
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mobile_support import MobileCase, home, routes, scan, worker  # noqa: E402


class FakeCtx:
    """The slice of Hermes's PluginContext the worker uses: ``spawn_task`` (a supervised, named asyncio task)."""

    def __init__(self):
        self.spawned = []

    def spawn_task(self, coro, *, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.spawned.append((name, task))
        return task


class WhereItRunsTests(MobileCase):
    def setUp(self):
        super().setUp()
        self.ran = threading.Event()

        async def run():
            self.ran.set()

        patcher = patch.object(worker, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_starts_once_as_a_thread_when_no_loop_is_running(self):
        self.assertEqual(worker.start(), "started")
        self.assertTrue(self.ran.wait(5))
        self.assertEqual(worker.start(), "running")
        self.assertEqual(worker._started["how"], "thread")

    def test_no_worker_starts_when_the_lease_file_is_unavailable(self):
        with patch("builtins.open", side_effect=PermissionError("worker.lock unavailable")):
            self.assertEqual(worker.start(), "off")
        self.assertFalse(self.ran.wait(.05))
        self.assertIsNone(worker._started["how"])

    def test_a_profile_home_never_runs_it(self):
        with patch.object(home, "is_root_home", return_value=False):
            self.assertEqual(worker.start(), "off")
        self.assertFalse(self.ran.wait(0.3))

    def test_a_second_copy_of_the_plugin_or_a_second_serve_does_not_start_another(self):
        self.assertEqual(worker.start(), "started")
        self.assertFalse(worker._lease(), "the one-worker lease is held by the first")
        worker._started["how"] = None  # a second module copy has its own flag, but the same lock file
        self.assertEqual(worker.start(), "running")
        self.assertEqual(worker._started["how"], "elsewhere")

    def test_register_does_nothing_outside_the_dashboard_process(self):
        async def main():
            ctx = FakeCtx()
            self.assertEqual(worker.register(ctx), "off")  # no hermes_cli.web_server in this process
            self.assertEqual(ctx.spawned, [])

        asyncio.run(main())

    def test_register_without_a_running_loop_leaves_it_to_the_dashboard_router(self):
        with patch.dict(sys.modules, {"hermes_cli.web_server": types.ModuleType("hermes_cli.web_server")}):
            self.assertEqual(worker.register(FakeCtx()), "off")
        self.assertFalse(self.ran.wait(0.3))

    def test_register_in_the_dashboard_with_a_loop_spawns_a_supervised_task(self):
        async def main():
            ctx = FakeCtx()
            with patch.dict(sys.modules, {"hermes_cli.web_server": types.ModuleType("hermes_cli.web_server")}):
                self.assertEqual(worker.register(ctx), "started")
            (name, task), = ctx.spawned
            self.assertEqual(name, worker.NAME)
            await task
            await asyncio.sleep(0)  # the done callback frees the lease so an unload/reload can start it again
            self.assertIsNone(worker._started["how"])
            self.assertEqual(worker.start(), "started")

        asyncio.run(main())
        self.assertTrue(self.ran.is_set())

    def test_mounting_the_routes_starts_it(self):
        with patch.object(worker, "start", return_value="started") as start:
            routes.on_mount()
        start.assert_called_once_with()


class LoopTests(MobileCase):
    def test_cancelled_scan_holds_its_lease_until_the_thread_really_stops(self):
        entered, release = threading.Event(), threading.Event()
        def cycle(purge):
            entered.set()
            release.wait(5)
        async def main():
            ctx = FakeCtx()
            with patch.object(worker, "_cycle", cycle):
                self.assertEqual(worker.start(ctx), "started")
                task = ctx.spawned[0][1]
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    task.cancel()
                    await asyncio.sleep(.03)
                    self.assertFalse(task.done(), "Cancellation cannot release the lease while a scan still writes")
                    self.assertFalse(worker._lease())
                finally:
                    release.set()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    await asyncio.sleep(0)
            self.assertIsNone(worker._started["how"])
            self.assertTrue(worker._lease())
        asyncio.run(main())

    def test_the_loop_scans_and_a_nudge_makes_it_look_at_once(self):
        """A reply saved after the first scan is queued within about a second of the nudge, not after the 10 s poll."""
        async def main():
            task = asyncio.get_running_loop().create_task(worker.run())
            try:
                await asyncio.sleep(0.6)  # the first cycle only records where every database is
                self.reply(10)
                self.assertEqual(self.reply_rows(), [])
                started = time.monotonic()
                (self.root / "hermes-bots").mkdir(exist_ok=True)
                home.nudge_path().touch()
                while not self.reply_rows() and time.monotonic() - started < 6:
                    await asyncio.sleep(0.1)
                return time.monotonic() - started
            finally:
                task.cancel()

        elapsed = asyncio.run(main())
        self.assertEqual(self.reply_rows(), [11])
        self.assertLess(elapsed, 4)

    def test_a_failing_cycle_is_survived_and_logged_without_content(self):
        calls = []

        def cycle(purge):
            calls.append(purge)
            if len(calls) == 1:
                raise RuntimeError("secret reply text")

        async def main():
            with patch.object(worker, "_cycle", cycle), patch.object(worker, "POLL_SECONDS", 0.2), patch.object(worker, "LOOK_SECONDS", 0.05):
                task = asyncio.get_running_loop().create_task(worker.run())
                await asyncio.sleep(1)
                task.cancel()

        with self.assertLogs("hermes_bots.mobile", level="WARNING") as logs:
            asyncio.run(main())
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(any("RuntimeError" in line for line in logs.output))
        self.assertFalse(any("secret" in line for line in logs.output))

    def test_cycle_scans_every_source_and_one_bad_database_does_not_stop_the_rest(self):
        (self.root / "profiles" / "broken").mkdir()
        (self.root / "profiles" / "broken" / "state.db").write_text("this is not a database")
        scan.cycle()
        self.reply(10)
        with self.assertLogs("hermes_bots.mobile", level="WARNING"):
            scan.cycle()
        self.assertEqual(self.reply_rows(), [11])

    def test_a_failed_outbox_write_retries_the_reply_on_the_next_scan(self):
        scan.cycle()
        self.reply(10)
        with patch.object(scan.alerts, "queue_reply", side_effect=sqlite3.OperationalError("database is locked")):
            scan.cycle()
        self.assertEqual(self.reply_rows(), [])
        scan.cycle()
        self.assertEqual(self.reply_rows(), [11])

    def test_reply_arriving_during_badge_refresh_is_not_skipped_by_the_cursor(self):
        scan.cycle()
        self.reply(10)
        original = scan.state.unread_for
        def refreshed(con, owner):
            self.reply(20)
            return original(con, owner)  # refreshes the shared index to row 21 while row 11 queues
        with patch.object(scan.state, "unread_for", side_effect=refreshed):
            scan.cycle()
        self.assertEqual(self.reply_rows(), [11])
        scan.cycle()
        self.assertEqual(self.reply_rows(), [11, 21], "The cursor covers only the batch actually consumed")

    def test_finished_outbox_rows_are_purged_by_the_loop(self):
        self.ob.enqueue("dev_x", "alert", {}, id="ancient", now=time.time() - 30 * 86400)
        worker._cycle(True)
        self.assertEqual([r["id"] for r in self.rows()], [])


if __name__ == "__main__":
    unittest.main()
