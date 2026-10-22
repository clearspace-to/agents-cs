"""The seam between the plugin and the Agents CS connector: the outbox the connector drains (``outbox.py``, imported by the
connector on its own), the device list and signing key the plugin reads from the connector's home, and the pointer file
``agents-cs connect`` writes so the plugin finds that home."""
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mobile_support import IPAD, PHONE, MobileCase, alerts, devices, home, outbox, scan  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_COLUMNS = ["id", "device_id", "created", "push_type", "priority", "collapse_id", "expiration", "payload_json",
                    "state", "attempts", "last_error"]


class SchemaTests(MobileCase):
    def test_delivery_db_has_exactly_the_spec_tables_and_outbox_columns(self):
        with closing(self.ob.connect()) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = [r[1] for r in con.execute("PRAGMA table_info(outbox)")]
        self.assertEqual(tables, {"outbox", "cursors", "reads", "baselines", "views", "attention"})
        self.assertEqual(columns, EXPECTED_COLUMNS)
        self.assertNotIn("devices", tables)
        self.assertNotIn("content_keys", tables)

    def test_the_database_lives_in_the_roots_agents_cs_folder_owner_only(self):
        self.assertEqual(home.delivery_path(), self.root / "hermes-bots" / "delivery.db")
        self.assertTrue(home.delivery_path().is_file())
        if os.name == "posix":
            self.assertEqual(home.delivery_path().stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "mobile-push").exists(), "nothing of the old bridge's folder")

    def test_outbox_py_imports_alone_without_the_rest_of_the_plugin(self):
        """The connector (and the Claude Code host in it) load this one file."""
        spec = importlib.util.spec_from_file_location("connector_side_outbox", ROOT / "mobile" / "outbox.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        box = module.Outbox(self.root / "other" / "delivery.db")
        self.assertEqual(box.enqueue("dev_x", "alert", {"aps": {}}, id="one"), "one")
        self.assertEqual([r["id"] for r in box.claim_queued()], ["one"])


class ComputerNamedInAlertsTests(MobileCase):
    """Every push names its computer (``computer``: the hub's cmp id from connector.json) so the app can place it."""

    def test_reply_event_and_silent_pushes_carry_the_computer_once_the_connector_knows_it(self):
        self.link([self.phone], computer="cmp_ABC")
        self.assertEqual(home.computer_id(), "cmp_ABC")
        scan.scan(self.con, "jarvis", self.source)   # the first scan only remembers what is there
        self.con.commit()
        self.reply(10, "A finished reply")
        self.scan_twice()
        alerts.push_all(self.con, {"alert": {"title": "t", "body": "b"}}, {"bot": "jarvis", "chat": "c", "approval": True})
        alerts.push_all(self.con, {"content-available": 1}, {"hermes": {"approvalResolved": {"profile": "jarvis", "chat": "c"}}},
                        push_type="background")
        alerts.push_to_owner(self.con, "local", None, [{"profile": "jarvis", "chat": "c", "unread": False}], unread=[])
        self.con.commit()
        payloads = self.payloads()
        self.assertGreaterEqual(len(payloads), 4)
        self.assertTrue(any("reply" in p for p in payloads))
        self.assertEqual({p.get("computer") for p in payloads}, {"cmp_ABC"}, payloads)

    def test_without_an_id_nothing_is_added(self):
        self.assertEqual(home.computer_id(), "")
        alerts.push_all(self.con, {"content-available": 1}, {}, push_type="background")
        self.con.commit()
        self.assertTrue(self.payloads() and all("computer" not in p for p in self.payloads()))


class OutboxApiTests(MobileCase):
    def test_enqueue_is_idempotent_by_id(self):
        self.assertEqual(self.ob.enqueue(PHONE, "alert", {"aps": {"badge": 1}}, id="evt-1"), "evt-1")
        self.assertIsNone(self.ob.enqueue(PHONE, "alert", {"aps": {"badge": 2}}, id="evt-1"))
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNotNone(self.ob.enqueue(PHONE, "alert", {}))  # a generated id
        with self.assertRaises(ValueError):
            self.ob.enqueue(PHONE, "alert", {}, state="sent")

    def test_claim_queued_is_oldest_first_limited_and_never_claims_held_or_finished_rows(self):
        for n in range(5):
            self.ob.enqueue(PHONE, "alert", {"n": n}, id=f"r{n}", now=100 + n)
        self.ob.enqueue(PHONE, "alert", {"n": 9}, id="held", state=outbox.HELD, now=50)
        self.ob.mark("r0", "sent")
        got = self.ob.claim_queued(limit=2)
        self.assertEqual([r["id"] for r in got], ["r1", "r2"])
        self.assertEqual(got[0]["payload"], {"n": 1})
        self.assertEqual(set(got[0]), set(EXPECTED_COLUMNS) | {"payload"})
        self.assertEqual([r["id"] for r in self.ob.claim_queued()], ["r1", "r2", "r3", "r4"])
        self.assertEqual([r["id"] for r in self.ob.claim_queued()], [r["id"] for r in self.ob.claim_queued()],
                         "claiming is a read: nothing changes until mark")

    def test_mark_records_the_outcome_and_counts_attempts_that_carry_an_error(self):
        self.ob.enqueue(PHONE, "alert", {}, id="a")
        self.ob.mark("a", "queued", error="apns_unreachable")  # retry later
        self.ob.mark("a", "queued", error="apns_unreachable")
        (row,) = self.rows("id='a'")
        self.assertEqual((row["state"], row["attempts"], row["last_error"]), ("queued", 2, "apns_unreachable"))
        self.ob.mark("a", "sent")
        self.assertEqual(self.rows("id='a'")[0]["state"], "sent")
        self.assertEqual(self.ob.claim_queued(), [])
        self.ob.enqueue(PHONE, "alert", {}, id="b")
        self.ob.mark("b", "dropped", error="not_your_device")
        self.ob.enqueue(PHONE, "alert", {}, id="c")
        self.ob.mark("c", "failed", error="PayloadTooLarge")
        self.assertEqual([(r["id"], r["state"]) for r in self.rows("id IN ('b','c')")], [("b", "dropped"), ("c", "failed")])
        with self.assertRaises(ValueError):
            self.ob.mark("a", "bogus")
        self.ob.mark("missing", "sent")  # an unknown id is not an error

    def test_purge_drops_rows_older_than_a_week_whatever_their_state(self):
        now = time.time()
        self.ob.enqueue(PHONE, "alert", {}, id="old-sent", now=now - 8 * 86400)
        self.ob.mark("old-sent", "sent")
        self.ob.enqueue(PHONE, "alert", {}, id="old-queued", now=now - 8 * 86400)
        self.ob.enqueue(PHONE, "alert", {}, id="fresh", now=now - 6 * 86400)
        self.assertEqual(self.ob.purge(), 2)
        self.assertEqual([r["id"] for r in self.rows()], ["fresh"])
        self.assertEqual(self.ob.purge(older_than_days=0.0001), 1)

    def test_many_writers_and_a_reader_share_the_database(self):
        """A profile's process, ``hermes serve`` and the connector all open it at once: WAL and a busy timeout."""
        import threading
        errors = []

        def produce(n):
            try:
                box = outbox.Outbox(home.delivery_path())  # each process has its own handle
                for k in range(25):
                    box.enqueue(PHONE, "alert", {"n": n, "k": k}, id=f"w{n}-{k}")
                    box.claim_queued(limit=5)  # the connector reading meanwhile
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=produce, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.ob.claim_queued(limit=1000)), 100)
        with closing(self.ob.connect()) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_a_drain_cycle_end_to_end(self):
        scan.scan(self.con, "jarvis", self.source)
        self.reply(10)
        self.scan_twice()
        (row,) = self.ob.claim_queued()
        self.assertEqual((row["device_id"], row["push_type"], row["priority"]), (PHONE, "alert", 10))
        self.assertEqual(row["payload"]["aps"]["alert"], alerts.REPLY_PLACEHOLDER)
        self.ob.mark(row["id"], "sent")
        self.assertEqual(self.ob.claim_queued(), [])
        scan.scan(self.con, "jarvis", self.source)
        self.assertEqual(self.ob.claim_queued(), [], "a sent alert is not queued again")


class ConnectorDiscoveryTests(MobileCase):
    def test_the_connector_home_comes_from_connector_json(self):
        self.assertEqual(home.connector_home(), self.connector)
        self.assertEqual([d.id for d in devices.devices()], [PHONE])
        self.assertIsNotNone(devices.sender_key())

    def test_without_connector_json_there_are_no_devices_and_it_says_so_once(self):
        (self.root / "hermes-bots" / "connector.json").unlink()
        devices.forget()
        with self.assertLogs("agents_cs.mobile", level="WARNING") as logs:
            self.assertEqual(devices.devices(), [])
            self.assertEqual(devices.devices(), [])
            self.assertIsNone(devices.sender_key())
        self.assertEqual(len(logs.records), 1)
        self.assertIn("connector.json", logs.output[0])
        self.assertIsNone(home.connector_home())

    def test_devices_json_missing_or_damaged(self):
        (self.connector / "devices.json").unlink()
        devices.forget()
        with self.assertLogs("agents_cs.mobile", level="WARNING"):
            self.assertEqual(devices.devices(), [])
        for text in ("not json", "[]", '{"devices": "x"}', '{"devices": [1, {"id": 3}, {"id": "dev_a"}, {"recipient_public_key": "k"}]}'):
            (self.connector / "devices.json").write_text(text)
            devices.forget()
            self.assertEqual(devices.devices(), [], text)
        (self.root / "hermes-bots" / "connector.json").write_text("not json")
        self.assertEqual(home.connector_info(), {})

    def test_a_rewritten_devices_file_is_picked_up_and_a_removed_device_stops_getting_alerts(self):
        self.assertEqual([d.id for d in devices.devices()], [PHONE])
        self.link([self.phone, self.ipad])
        self.assertEqual({d.id for d in devices.devices()}, {PHONE, IPAD})
        self.link([self.ipad])
        scan.scan(self.con, "jarvis", self.source)
        self.reply(10)
        self.scan_twice()
        self.assertEqual({r["device_id"] for r in self.queued()}, {IPAD})

    def test_an_unreadable_sender_key_means_no_alerts(self):
        self.sender_path.write_text("not a key")
        devices.forget()
        with self.assertLogs("agents_cs.mobile", level="WARNING"):
            self.assertIsNone(devices.sender_key())
        self.assertEqual(alerts.recipients(), [])

    def test_the_plugin_never_creates_the_sender_key_or_devices_file(self):
        self.sender_path.unlink()
        (self.connector / "devices.json").unlink()
        devices.forget()
        with self.assertLogs("agents_cs.mobile", level="WARNING"):
            devices.devices()
            devices.sender_key()
        self.assertEqual(sorted(p.name for p in self.connector.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
