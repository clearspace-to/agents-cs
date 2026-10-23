"""The mobile module's replies, unread state, badges, inbox filters and routes (what the push bridge's tests covered, now
against ``/api/plugins/agents-cs/mobile/*`` and the outbox instead of APNs)."""
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mobile_support import (HEADERS, IPAD, PHONE, MobileCase, alerts, outbox, replies, scan, sealed,  # noqa: E402
                            state)


class ReplyScanTests(MobileCase):
    def test_requests_close_database_connections_without_garbage_collection(self):
        # Holding references prevents GC from hiding a leaked SQLite connection.
        opened = []
        connect = sqlite3.connect

        def tracked(*args, **kwargs):
            con = connect(*args, **kwargs)
            opened.append(con)
            return con

        with patch.object(sqlite3, "connect", side_effect=tracked):
            for _ in range(12):
                for path in ["/kinds?profile=jarvis&session=chat", "/reads"]:
                    self.assertEqual(self.call("GET", path).status_code, 200)
                scan.scan(self.con, "jarvis", self.source)
                for con in opened:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        con.execute("SELECT 1")
        self.assertGreater(len(opened), 24)

    def test_baseline_completion_dedup_and_tools(self):
        scan.scan(self.con, "jarvis", self.source)
        self.assertEqual(self.rows(), [])
        self.script('''INSERT INTO messages VALUES(2,'chat','assistant','Checking','[{}]','tool_calls',NULL,1);
            INSERT INTO messages VALUES(3,'chat','tool','Result',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(4,'chat','assistant','New reply',NULL,'stop',NULL,1);
            INSERT INTO messages VALUES(5,'chat','assistant','Internal',NULL,'stop','hidden',1);''')
        self.scan_twice()
        self.assertEqual(self.reply_rows(), [4])

    def test_an_alert_is_sealed_for_the_device_and_carries_the_preview_and_full_text(self):
        scan.scan(self.con, "jarvis", self.source)
        self.sql("INSERT INTO messages VALUES(4,'chat','assistant','**Done.**\n\nThe note is updated.',NULL,'stop',NULL,1)")
        self.scan_twice(now=1_800_000_000)
        (row,) = self.queued()
        payload = json.loads(row["payload_json"])
        self.assertEqual((row["device_id"], row["push_type"], row["priority"], row["state"]), (PHONE, "alert", 10, "queued"))
        self.assertEqual((row["collapse_id"], row["expiration"], row["attempts"]), (row["id"], 1_800_000_000 + 86400, 0))
        # Apple and the hub see a placeholder; the words travel sealed.
        self.assertEqual(payload["aps"]["alert"], alerts.REPLY_PLACEHOLDER)
        self.assertEqual((payload["aps"]["mutable-content"], payload["aps"]["category"], payload["aps"]["thread-id"]),
                         (1, "reply", "jarvis:chat"))
        self.assertEqual((payload["bot"], payload["chat"], payload["reply"]), ("jarvis", "chat", "4"))
        self.assertNotIn("text", payload)
        self.assertNotIn("updated", json.dumps(payload))
        content = self.opened(payload)
        self.assertEqual((content["type"], content["title"], content["body"]), ("reply", "Jarvis", "Done. The note is updated."))
        self.assertEqual(content["text"], "**Done.**\n\nThe note is updated.")
        self.assertEqual(content["eventId"], row["id"])

    def test_another_device_cannot_open_it_and_a_forged_sender_is_refused(self):
        self.link([self.phone, self.ipad])
        scan.scan(self.con, "jarvis", self.source)
        self.reply(10)
        self.scan_twice()
        by_device = {r["device_id"]: json.loads(r["payload_json"]) for r in self.queued()}
        self.assertEqual(set(by_device), {PHONE, IPAD})
        self.assertNotEqual(by_device[PHONE]["sealed"]["ciphertext"], by_device[IPAD]["sealed"]["ciphertext"])
        with self.assertRaises(Exception):
            sealed.open_envelope(by_device[PHONE]["sealed"], self.ipad.private, self.sender_public)
        stranger = sealed.public_bytes(sealed.sealing_key(self.connector / "other.pem").public_key())
        with self.assertRaises(Exception):
            sealed.open_envelope(by_device[PHONE]["sealed"], self.phone.private, stranger)

    def test_the_same_reply_is_never_queued_twice(self):
        scan.scan(self.con, "jarvis", self.source)
        self.reply(10)
        self.scan_twice()
        self.assertEqual(len(self.queued()), 1)
        with patch.object(replies, "_indexes", {}):  # a restarted worker with the cursor kept
            scan.scan(self.con, "jarvis", self.source)
        self.assertEqual(len(self.queued()), 1)

    def test_only_person_facing_replies_alert(self):
        self.scan_twice()  # first look: records the cursor, alerts nothing
        self.script("""INSERT INTO messages VALUES(2,'chat','user','Hi',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(3,'chat','assistant','[SILENT]',NULL,'stop',NULL,1);
            INSERT INTO messages VALUES(4,'chat','user','Message from \U0001F916 Cisco (@cisco): ping',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(5,'chat','assistant','Pong',NULL,'stop',NULL,1);
            INSERT INTO messages VALUES(6,'chat','user','And you?',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(7,'chat','assistant','Here.',NULL,'stop',NULL,NULL);""")
        self.scan_twice()
        self.assertEqual(self.reply_rows(), [7])  # NULL active counts as live
        self.assertTrue(replies.is_silence(" no_reply. "))
        self.assertFalse(replies.is_silence("No reply needed, all done."))
        self.assertEqual(replies.prompt_kind('[Cronjob "Friday: clean-inbox" output — scheduled job, not the user.]', None), "cron")

    def test_compaction_copies_do_not_realert(self):
        """Bot Chat, 2026-10-15: compaction rewrote kept replies under new ids with their old timestamps,
        and every copy rang as new. A copy alerts nothing and leaves the chat read; a later reply alerts."""
        with sqlite3.connect(self.source) as c:
            c.execute("ALTER TABLE messages ADD COLUMN timestamp REAL")
            c.execute("UPDATE messages SET timestamp=100")
        self.scan_twice()
        self.script("""INSERT INTO messages VALUES(2,'chat','user','Hi',NULL,NULL,NULL,1,200);
            INSERT INTO messages VALUES(3,'chat','assistant','Hello',NULL,'stop',NULL,1,201);""")
        self.scan_twice()
        self.clear()
        self.script("""UPDATE messages SET active=0;
            INSERT INTO messages VALUES(4,'chat','assistant','Old reply',NULL,'stop',NULL,1,100);
            INSERT INTO messages VALUES(5,'chat','user','Hi',NULL,NULL,NULL,1,200);
            INSERT INTO messages VALUES(6,'chat','assistant','Hello',NULL,'stop',NULL,1,201);""")
        self.scan_twice()
        self.assertEqual(self.rows(), [])
        self.assertEqual(replies.known_index("jarvis").replies(), {"chat": 3})
        self.sql("INSERT INTO messages VALUES(7,'chat','assistant','New',NULL,'stop',NULL,1,300)")
        self.scan_twice()
        self.assertEqual(self.reply_rows(), [7])

    def test_restart_does_not_realert(self):
        self.scan_twice()
        self.script("""INSERT INTO messages VALUES(2,'chat','user','Hi',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(3,'chat','assistant','Hello',NULL,'stop',NULL,1);""")
        self.scan_twice()
        self.clear()
        replies._indexes.clear()  # a restart rebuilds the index from the start
        self.scan_twice()
        self.assertEqual(self.rows(), [])

    def test_a_device_linked_later_gets_nothing_from_before(self):
        self.link([])
        self.scan_twice()
        self.reply(10)
        self.scan_twice()
        self.link([self.phone])
        self.scan_twice()
        self.assertEqual(self.rows(), [], "no devices, no alerts, and no backlog for a device that joins afterwards")

    def test_no_connector_or_no_sender_key_means_no_alerts(self):
        self.scan_twice()
        (self.root / "hermes-bots" / "connector.json").unlink()
        alerts.devices.forget()
        self.reply(10)
        self.scan_twice()
        self.assertEqual(self.rows(), [])
        self.link([self.phone])
        self.sender_path.unlink()
        alerts.devices.forget()
        self.reply(20)
        self.scan_twice()
        self.assertEqual(self.rows(), [], "an alert nobody can verify is not sent")

    def test_groups_follow_the_app_filter(self):
        self.assertEqual(replies.chat_group("desktop", 0, 0), "chats")
        self.assertEqual(replies.chat_group("tui", 1, 0), "hidden")
        self.assertEqual(replies.chat_group("cron", 1, 1), "archived")  # archived wins
        self.assertEqual(replies.chat_group("cron", 0, 0), "scheduled")
        self.assertEqual(replies.chat_group("oneshot", 0, 0), "oneshot")
        self.assertEqual(replies.chat_group("telegram", 0, 0), "messaging")
        self.assertIsNone(replies.chat_group("kanban", 1, 0))  # worker sessions are never anyone's chat

    def test_no_alerts_for_hidden_scheduled_oneshot_never(self):
        scan.scan(self.con, "jarvis", self.source)
        state.unread_for(self.con, state.OWNER)
        for sid, src, hid in (("h", "desktop", 1), ("s", "cron", 0), ("o", "oneshot", 0), ("k", "kanban", 0), ("m", "telegram", 0)):
            self.session(sid, src, hid)
        for i, sid in enumerate(("h", "s", "o", "k", "m", "chat")):
            self.reply(10 + 2 * i, chat=sid)
        scan.scan(self.con, "jarvis", self.source)
        self.con.commit()
        # A device with no filter yet: the app's default, Chats + Hidden; Scheduled isn't listed, so nothing is held either.
        self.assertEqual(self.reply_chats(), ["chat", "h"])
        self.assertEqual(self.rows("state='held'"), [])
        groups = sorted(g for _, _, g in state.unread_for(self.con, state.OWNER))
        self.assertEqual(groups, ["chats", "hidden", "messaging", "oneshot", "scheduled"])  # kanban never
        self.assertIsNone(replies.chat_group("cron", 1, 0))  # a hidden run is in none of the app's lists

    def test_bot_chat_always_alerts_and_badges(self):
        """Hermes creates each Bot Chat hidden; the app always lists it, so it alerts and badges in every view."""
        scan.scan(self.con, "jarvis", self.source)
        state.unread_for(self.con, state.OWNER)
        self.session("main", hidden=1, title="Bot Chat")
        self.session("main-2", hidden=1, parent="main")  # compression continuation
        self.reply(10, chat="main")
        self.reply(12, chat="main-2")
        scan.scan(self.con, "jarvis", self.source)
        self.con.commit()
        self.assertEqual(self.reply_chats(), ["main", "main-2"])
        self.call("PUT", "/view", json={"groups": [], "bots": ["cisco"]})
        self.assertEqual(state.badge_for(self.con, state.OWNER, PHONE), 2)
        self.assertEqual(len(self.call("GET", "/reads").json()["unread"]), 2)

    def test_a_background_helper_of_the_bot_chat_is_never_unread(self):
        """A helper the Bot Chat dispatched answers the bot, not the person: no alert, no badge, no inbox row."""
        scan.scan(self.con, "jarvis", self.source)
        state.unread_for(self.con, state.OWNER)
        self.session("main", "tui", hidden=1, title="Bot Chat")
        self.session("helper", "tui", title="Send edited update", parent="main")
        self.script('''ALTER TABLE sessions ADD COLUMN model_config TEXT;
            UPDATE sessions SET model_config='{"_delegate_from": "main"}' WHERE id='helper';''')
        self.reply(10, "Both dashboards verified", chat="helper")
        self.reply(12, "Updated the skill", chat="main")
        scan.scan(self.con, "jarvis", self.source)
        self.con.commit()
        self.assertEqual(self.reply_chats(), ["main"])
        self.assertEqual([chat for _, chat, _ in state.unread_for(self.con, state.OWNER)], ["main"])
        self.assertEqual([entry["chat"] for entry in self.call("GET", "/reads").json()["unread"]], ["main"])

    def test_alerts_follow_each_devices_filter(self):
        """A device is alerted only for chats its filter lists (types and bots); Bot Chats always."""
        self.link([self.phone, self.ipad])
        scan.scan(self.con, "jarvis", self.source)
        state.unread_for(self.con, state.OWNER)
        state.set_view(self.con, PHONE, ["scheduled"], [])
        state.set_view(self.con, IPAD, ["chats", "scheduled"], ["cisco"])
        self.con.commit()
        self.session("s", "cron")
        self.session("bc", hidden=1, title="Bot Chat")
        for i, sid in enumerate(("chat", "s", "bc")):
            self.reply(30 + 2 * i, chat=sid)
        scan.scan(self.con, "jarvis", self.source)
        self.con.commit()
        self.assertEqual(self.reply_chats(PHONE), ["bc"])   # Scheduled Runs only (held), plus the Bot Chat
        self.assertEqual(self.reply_chats(IPAD), ["bc"])    # filtered to another bot: Bot Chats still ring
        held = {r["device_id"] for r in self.rows("state='held'")}
        self.assertEqual(held, {PHONE})                       # the scheduled run, only where it is listed
        self.assertEqual(state.badge_for(self.con, state.OWNER, PHONE), 2)

    def test_unlisted_device_defaults_to_chats_and_hidden(self):
        self.scan_twice()
        self.session("h", hidden=1)
        self.reply(10, chat="h")
        self.reply(20)
        self.scan_twice()
        self.assertEqual(self.reply_chats(), ["chat", "h"])


class ScheduledRunTests(MobileCase):
    def test_a_scheduled_runs_alert_waits_and_is_dropped_once_the_run_is_archived(self):
        """The run's result rings once, from the main chat: the run's own alert waits and an archived run never rings."""
        scan.scan(self.con, "jarvis", self.source)
        state.set_view(self.con, PHONE, ["chats", "scheduled"], [])
        self.con.commit()
        for sid in ("kept", "done"):
            self.session(sid, "cron")
        self.reply(40, chat="kept")
        self.reply(42, chat="done")
        self.reply(44)
        scan.scan(self.con, "jarvis", self.source, now=1000)
        self.con.commit()
        self.assertEqual(self.reply_chats(), ["chat"])  # an ordinary chat rings at once; the runs wait
        self.assertEqual(len(self.rows("state='held'")), 2)
        self.sql("UPDATE sessions SET archived=1 WHERE id='done'")
        scan.scan(self.con, "jarvis", self.source, now=1000)
        with patch.object(replies, "run_handoff", return_value="released"):
            scan.release_held(self.con, now=1000 + 5)
            self.assertEqual(self.reply_chats(), ["chat"], "still held inside the wait")
            scan.release_held(self.con, now=1000 + replies.SCHEDULED_HOLD + 1)
        self.con.commit()
        self.assertEqual(self.reply_chats(), ["chat", "kept"])  # still in Scheduled after the wait: it rings; archived: never
        self.assertEqual(self.rows("state='held'"), [])

    def test_a_run_whose_result_is_still_being_handed_over_waits_then_never_rings_once_delivered(self):
        scan.scan(self.con, "jarvis", self.source)
        state.set_view(self.con, PHONE, ["chats", "scheduled"], [])
        self.session("busy", "cron")
        self.reply(50, chat="busy")
        scan.scan(self.con, "jarvis", self.source, now=1000)
        for at in (60, 600):
            with patch.object(replies, "run_handoff", return_value="waiting"):
                scan.release_held(self.con, now=1000 + replies.SCHEDULED_HOLD + at)
        self.con.commit()
        self.assertEqual(self.reply_chats(), [])
        with patch.object(replies, "run_handoff", return_value="delivered"):
            scan.release_held(self.con, now=1000 + replies.SCHEDULED_HOLD + 700)
        self.con.commit()
        self.assertEqual((self.reply_chats(), self.rows()), ([], []))

    def test_stuck_past_the_limit_it_rings_saying_the_main_chat_was_stuck(self):
        scan.scan(self.con, "jarvis", self.source)
        state.set_view(self.con, PHONE, ["chats", "scheduled"], [])
        self.session("stuck", "cron")
        self.reply(60, chat="stuck")
        scan.scan(self.con, "jarvis", self.source, now=1000)
        with patch.object(replies, "run_handoff", return_value="waiting"):
            scan.release_held(self.con, now=1000 + replies.SCHEDULED_MAX_WAIT - 30)
            self.con.commit()
            self.assertEqual(self.reply_chats(), [])
            scan.release_held(self.con, now=1000 + replies.SCHEDULED_MAX_WAIT + 30)
        self.con.commit()
        (payload,) = self.payloads()
        self.assertEqual(self.opened(payload)["body"], replies.STUCK_NOTE + "Hi there")
        self.assertEqual(payload["chat"], "stuck")

    def test_held_alerts_survive_a_restart(self):
        scan.scan(self.con, "jarvis", self.source)
        state.set_view(self.con, PHONE, ["chats", "scheduled"], [])
        self.session("run", "cron")
        self.reply(70, chat="run")
        scan.scan(self.con, "jarvis", self.source, now=1000)
        self.con.commit()
        self.assertEqual(len(self.rows("state='held'")), 1)
        replies._indexes.clear()
        scan.scan(self.con, "jarvis", self.source, now=1005)  # the new process rebuilds its index first
        with patch.object(replies, "run_handoff", return_value="released"):
            scan.release_held(self.con, now=1000 + replies.SCHEDULED_HOLD + 1)
        self.con.commit()
        self.assertEqual(self.reply_chats(), ["run"])

    def test_a_held_alert_for_a_device_that_left_is_dropped(self):
        scan.scan(self.con, "jarvis", self.source)
        state.set_view(self.con, PHONE, ["chats", "scheduled"], [])
        self.session("run", "cron")
        self.reply(70, chat="run")
        scan.scan(self.con, "jarvis", self.source, now=1000)
        self.link([self.ipad])
        scan.release_held(self.con, now=1000 + replies.SCHEDULED_HOLD + 1)
        self.con.commit()
        self.assertEqual(self.rows(), [])

    def test_the_hand_off_check_reads_hermes_and_the_relay_ledger(self):
        root = self.root
        home = root / "profiles" / "ghost"
        (home / "cron").mkdir(parents=True)
        (root / "hermes-bots-cards").mkdir()
        state_db, job = home / "state.db", "b53527d462af"
        with closing(sqlite3.connect(state_db)) as c, c:
            c.execute("CREATE TABLE sessions(id TEXT,started_at REAL)")
        with closing(sqlite3.connect(home / "cron" / "executions.db")) as c, c:
            c.execute("CREATE TABLE executions(id TEXT,job_id TEXT,status TEXT,delivery_outcome TEXT,started_at TEXT)")
        with closing(sqlite3.connect(root / "hermes-bots-cards" / "forms.db")) as c, c:
            c.execute("CREATE TABLE relays(run_session TEXT PRIMARY KEY, replied_at INTEGER)")

        def run(eid, start, status="completed", outcome="queued", relay=None):
            chat = f"cron_{job}_{eid}0000_000000"
            with closing(sqlite3.connect(state_db)) as c, c:
                c.execute("INSERT INTO sessions VALUES(?,?)", (chat, start + 6))
            with closing(sqlite3.connect(home / "cron" / "executions.db")) as c, c:
                c.execute("INSERT INTO executions VALUES(?,?,?,?,?)",
                          (eid, job, status, outcome, replies.datetime.fromtimestamp(start).astimezone().isoformat()))
            if relay is not None:
                with closing(sqlite3.connect(root / "hermes-bots-cards" / "forms.db")) as c, c:
                    c.execute("INSERT INTO relays VALUES(?,?)", (chat, relay))
            return chat

        t = 1791900000.0
        cases = {run("1001", t, relay=int(t) + 50): "delivered", run("1002", t + 600, outcome="suppressed"): "delivered",
                 run("1003", t + 1200): "waiting", run("1004", t + 1800, outcome="delivered", relay=None): "waiting",
                 run("1005", t + 2400, status="running", outcome=None): "waiting",
                 run("1006", t + 3000, outcome="delivered", relay=None): "waiting",
                 run("1007", t + 3600, status="failed", outcome=None): "released", run("1008", t + 4200, outcome="failed"): "released",
                 run("1009", t + 4800, outcome="not_configured"): "released", "not-a-run": "released"}
        for chat, expected in cases.items():
            self.assertEqual(replies.run_handoff(state_db, chat), expected, chat)
        self.assertEqual(replies.cards_db(root / "state.db"), (root / "hermes-bots-cards" / "forms.db").resolve())


class ReadStateTests(MobileCase):
    def test_snapshot_returns_ids_without_private_content(self):
        response = self.call("GET", "/replies?profile=jarvis")
        self.assertEqual(response.json(), {"replies": [{"session_id": "chat", "id": 1}]})
        self.assertNotIn("Old reply", response.text)
        self.assertEqual(self.call("GET", "/replies?profile=nobody").status_code, 404)

    def test_read_through_keeps_later_reply_unread(self):
        state.unread_for(self.con, state.OWNER)  # baseline
        self.script("""INSERT INTO messages VALUES(2,'chat','user','Hi',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(3,'chat','assistant','One',NULL,'stop',NULL,1);
            INSERT INTO messages VALUES(4,'chat','user','More',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(5,'chat','assistant','Two',NULL,'stop',NULL,1);""")

        def put(body):
            return self.call("PUT", "/reads", json=body).json()

        self.assertEqual(put({"profile": "jarvis", "chat": "chat", "unread": False, "through": 3})["count"], 1)
        # Each unread chat says where it was last read, so the app opens it at "Since you last read".
        unread = self.call("GET", "/reads?all=1").json()["unread"]
        self.assertEqual([(u["chat"], u["through"]) for u in unread], [("chat", 3)])
        self.assertEqual(put({"profile": "jarvis", "chat": "chat", "unread": False, "through": 5})["count"], 0)

    def test_shared_read_state(self):
        self.assertEqual(state.unread_for(self.con, state.OWNER), [])  # baseline: history is not unread
        self.script("""INSERT INTO messages VALUES(2,'chat','user','Hi',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(3,'chat','assistant','Hello',NULL,'stop',NULL,1);""")
        self.assertEqual(state.unread_for(self.con, state.OWNER), [("jarvis", "chat", "chats")])

        def mark(unread):
            return self.call("PUT", "/reads", json={"profile": "jarvis", "chat": "chat", "unread": unread}).json()["count"]

        self.assertEqual(mark(False), 0)
        self.assertEqual(mark(True), 1)   # marked unread by hand
        self.assertEqual(mark(False), 0)
        # another bot's message and the answer to it stay read
        self.script("""INSERT INTO messages VALUES(4,'chat','user','Message from \U0001F916 Jarvis (@jarvis): ping',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(5,'chat','assistant','Got it',NULL,'stop',NULL,1);""")
        self.assertEqual(state.unread_for(self.con, state.OWNER), [])
        # a hidden chat is unread, but badged only where it's listed
        self.script("""INSERT INTO messages VALUES(6,'chat','user','Again',NULL,NULL,NULL,1);
            INSERT INTO messages VALUES(7,'chat','assistant','Sure',NULL,'stop',NULL,1);
            UPDATE sessions SET hidden=1;""")
        self.assertEqual(state.unread_for(self.con, state.OWNER), [("jarvis", "chat", "hidden")])
        self.assertEqual(state.badge_for(self.con, state.OWNER, PHONE), 1)  # default view: Chats, Hidden on Desktop

        def view(groups, bots=()):
            response = self.call("PUT", "/view", json={"groups": groups, "bots": list(bots)})
            self.assertEqual(response.status_code, 200)
            return response.json()["count"]

        self.assertEqual(view(["chats"]), 0)  # Hidden on Desktop switched off
        self.assertEqual(view(["chats", "hidden"]), 1)
        self.assertEqual(view(["chats", "hidden"], ["cisco"]), 0)  # filtered to another bot
        self.assertEqual(view(["chats", "bogus"]), 0)
        self.sql("UPDATE sessions SET hidden=0, archived=1")  # archived chats never count, even when listed
        view(["chats", "archived"])
        self.assertEqual(state.unread_for(self.con, state.OWNER), [])
        self.assertEqual(state.badge_for(self.con, state.OWNER, PHONE), 0)

    def test_hidden_then_unhidden_counts_on_default_badge(self):
        state.unread_for(self.con, state.OWNER)
        self.sql("UPDATE sessions SET hidden=1")
        self.reply(2)
        self.assertEqual(state.badge_for(self.con, state.OWNER, None), 1)  # Hidden on Desktop is listed by default
        self.sql("UPDATE sessions SET hidden=0")
        self.assertEqual(state.badge_for(self.con, state.OWNER, None), 1)

    def test_default_view_is_the_apps_default_filter(self):
        state.unread_for(self.con, state.OWNER)
        self.sql("UPDATE sessions SET source='telegram'")
        self.reply(2)
        self.assertEqual(state.badge_for(self.con, state.OWNER, None), 0)  # Messaging isn't listed by default
        self.sql("UPDATE sessions SET source='desktop'")
        self.assertEqual(state.badge_for(self.con, state.OWNER, None), 1)

    def test_archived_after_unread_then_unarchived(self):
        state.unread_for(self.con, state.OWNER)
        self.reply(2)
        self.assertEqual(state.badge_for(self.con, state.OWNER, None), 1)
        self.sql("UPDATE sessions SET archived=1")
        self.assertEqual(state.unread_for(self.con, state.OWNER), [])
        self.sql("UPDATE sessions SET archived=0")
        self.assertEqual(state.badge_for(self.con, state.OWNER, None), 1)

    def test_manual_unread_on_archived_chat_is_dropped(self):
        state.unread_for(self.con, state.OWNER)
        self.sql("UPDATE sessions SET archived=1")
        response = self.call("PUT", "/reads", json={"profile": "jarvis", "chat": "chat", "unread": True})
        self.assertEqual(response.json()["count"], 0)
        self.assertEqual(self.call("GET", "/reads").json()["unread"], [])  # dot vanishes on next sync

    def test_new_owner_baseline(self):
        self.assertEqual(state.unread_for(self.con, "newbie"), [])
        self.assertEqual(self.con.execute("SELECT row_id FROM baselines WHERE owner='newbie' AND profile='jarvis'").fetchone()[0], 1)

    def test_a_session_user_is_the_owner_when_hermes_has_one(self):
        from types import SimpleNamespace
        from mobile_support import routes
        request = SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(user_id="acc_1")))
        self.assertEqual(routes.owner_of(request), "acc_1")
        self.assertEqual(routes.owner_of(SimpleNamespace(state=SimpleNamespace())), state.OWNER)

    def test_view_needs_a_device_and_is_validated(self):
        self.assertEqual(self.call("PUT", "/view", headers={}, json={"groups": ["chats"]}).status_code, 400)
        self.assertEqual(self.call("PUT", "/view", headers={"X-Hermes-Bots-Device": "bad id!"}, json={"groups": []}).status_code, 400)
        self.assertEqual(self.call("PUT", "/view", json={"groups": ["chats"] * 8}).status_code, 422)
        self.assertEqual(self.call("PUT", "/view", json={"groups": ["chats"], "bots": ["x"] * 65}).status_code, 422)
        self.assertEqual(self.call("PUT", "/view", json={"groups": "chats"}).status_code, 422)
        # An invalid bot name is refused, never dropped (dropping every name would widen to all bots).
        self.assertEqual(self.call("PUT", "/view", json={"groups": ["chats"], "bots": ["bad bot!"]}).status_code, 422)
        # An old body that still carries an installation id is not an error; the id is ignored.
        self.assertEqual(self.call("PUT", "/view", json={"installation": "x" * 20, "groups": ["chats"]}).status_code, 200)

    def test_each_device_keeps_its_own_view(self):
        state.unread_for(self.con, state.OWNER)
        self.reply(2)
        self.call("PUT", "/view", json={"groups": [], "bots": ["cisco"]})
        rows = self.queued()
        self.assertEqual([(r["device_id"], json.loads(r["payload_json"])["aps"]["badge"]) for r in rows], [(PHONE, 0)])
        self.assertEqual(self.call("GET", "/reads").json()["count"], 0)
        self.assertEqual(self.call("GET", "/reads", headers={"X-Hermes-Bots-Device": IPAD}).json()["count"], 1)
        self.assertEqual(self.call("GET", "/reads", headers={}).json()["count"], 1, "no device: the app's default view")

    def test_corrupt_view_does_not_break_badge(self):
        self.con.execute("INSERT INTO views VALUES(?,?,?,0)", (PHONE, "not json", "[]"))
        self.con.commit()
        state.badge_for(self.con, state.OWNER, PHONE)  # must not raise: a damaged row never stops alerts

    def test_old_app_reads_list_matches_default_badge(self):
        """Old builds set the icon badge to len(unread) from GET /reads."""
        state.unread_for(self.con, state.OWNER)
        self.session("s", "cron")
        self.reply(2, chat="s")
        self.session("o", "oneshot")
        self.reply(4, chat="o")
        body = self.call("GET", "/reads").json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(len(body["unread"]), body["count"])

    def test_set_read_computes_unread_once(self):
        calls, real = [], state.unread_for

        def spy(con, owner):
            calls.append(owner)
            return real(con, owner)

        with patch.object(state, "unread_for", side_effect=spy):
            self.call("PUT", "/reads", json={"profile": "jarvis", "chat": "chat", "unread": False})
        self.assertEqual(len(calls), 1)

    def test_unread_follows_the_compression_tip(self):
        """A reply unread when Hermes compressed the chat is read by reading the chat's new tip."""
        with sqlite3.connect(self.source) as c:
            c.execute("ALTER TABLE sessions ADD COLUMN end_reason TEXT")
        state.unread_for(self.con, state.OWNER)
        self.reply(2)  # unread in the root segment
        self.sql("UPDATE sessions SET end_reason='compression' WHERE id='chat'")
        self.session("chat-2", parent="chat")
        self.assertEqual(state.unread_for(self.con, state.OWNER), [("jarvis", "chat-2", "chats")])
        response = self.call("PUT", "/reads", json={"profile": "jarvis", "chat": "chat-2", "unread": False})
        self.assertEqual(response.json()["count"], 0)

    def test_reading_updates_both_badges_without_echoing_reads_to_the_reader(self):
        self.link([self.phone, self.ipad])
        state.unread_for(self.con, state.OWNER)
        self.reply(2)
        self.call("PUT", "/reads", json={"profile": "jarvis", "chat": "chat", "unread": False})
        rows = self.queued()
        self.assertEqual({r["device_id"] for r in rows}, {PHONE, IPAD})
        self.assertEqual([r["push_type"] for r in rows if r["device_id"] == PHONE], ["alert"])
        by_type = {r["push_type"]: json.loads(r["payload_json"]) for r in rows}
        self.assertEqual(by_type["alert"], {"aps": {"badge": 0}})
        self.assertEqual(by_type["background"]["hermes"], {"reads": [{"profile": "jarvis", "chat": "chat", "unread": False}], "badge": 0})
        self.assertEqual({r["priority"] for r in rows}, {5})

    def test_usage_route_reads_the_homes(self):
        response = self.call("GET", "/usage?period=today")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.call("GET", "/usage?period=decade").status_code, 422)

    def test_the_bridges_routes_are_gone(self):
        for method, path in (("PUT", "/devices"), ("GET", "/health"), ("POST", "/internal/nudge")):
            self.assertEqual(self.call(method, path, json={}).status_code, 404, path)
        self.assertEqual(self.client().get("/api/plugins/agents-cs/health").status_code, 404)


class KindsTests(MobileCase):
    def test_labels_and_unread_share_one_rule(self):
        rows = [{"id": 1, "role": "user", "content": '[Cronjob "x" output]', "display_kind": None, "tool_calls": None, "finish_reason": None, "active": 1},
                {"id": 2, "role": "assistant", "content": "Ran fine.", "display_kind": None, "tool_calls": None, "finish_reason": "stop", "active": 1},
                {"id": 3, "role": "user", "content": "Thanks", "display_kind": None, "tool_calls": None, "finish_reason": None, "active": 1},
                {"id": 4, "role": "assistant", "content": "[SILENT]", "display_kind": None, "tool_calls": None, "finish_reason": "stop", "active": 1},
                {"id": 5, "role": "assistant", "content": "You are welcome.", "display_kind": None, "tool_calls": None, "finish_reason": "stop", "active": 1}]
        kinds = replies.message_kinds(rows)
        self.assertEqual(kinds, {1: "cron", 2: "summary", 4: "silent"})
        answering = None
        for row in rows:
            if row["role"] == "user":
                answering = replies.answers(row["content"], row["display_kind"])
            else:
                self.assertEqual(replies.is_reply(row, answering), kinds.get(row["id"]) in (None, "summary"))

    def test_message_kinds_for_show_in_chats(self):
        """GET /mobile/kinds labels every machine message, across the chat's compression lineage."""
        rows = [(10, "user", "[System: model switched]", None, None), (11, "user", "Hello?", None, None),
                (12, "assistant", "[SILENT]", None, None), (13, "user", "x", "process_complete", None),
                (14, "assistant", "Done.", None, None), (15, "user", "Message from \U0001F916 Jarvis (@jarvis): hi", None, None),
                (16, "assistant", "Checking…", None, '[{"id":"1"}]'), (17, "assistant", "Sent.", None, None),
                (18, "user", '[Cronjob "Friday: clean" output — scheduled job, not the user.]', None, None),
                (19, "assistant", "Inbox cleaned.", None, None), (20, "user", "Thanks", None, None), (21, "assistant", "Any time.", None, None)]
        with sqlite3.connect(self.source) as c:
            c.execute("INSERT INTO sessions VALUES('root','desktop',0,0,'Plan',NULL)")
            c.execute("INSERT INTO sessions VALUES('tip','desktop',0,0,'Plan','root')")
            for i, (rid, role, text, kind, tools) in enumerate(rows):
                c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,1)", (rid, "root" if i < 6 else "tip", role, text, tools, "stop", kind))
        response = self.call("GET", "/kinds", params={"profile": "jarvis", "session": "tip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kinds"], {"10": "notes", "12": "silent", "13": "background", "14": "background",
                                                    "15": "bots", "16": "bots", "17": "bots", "18": "cron", "19": "summary"})
        self.assertNotIn("Hello", response.text)  # labels only, never message text
        self.assertEqual(self.call("GET", "/kinds", params={"profile": "nobody", "session": "tip"}).status_code, 404)

    def test_kanban_notices_and_their_answers_are_machine_messages(self):
        notices = [
            "⏸ [default] @default Kanban t_b327fe82 blocked: Revised draft saved in comment 318.",
            "⏸️ [default] @friday Kanban t_b327fe82 blocked",
            "✔ @friday Kanban t_b327fe82 done — Send the draft\nSaved.",
            "⛔ Kanban t_b327fe82 is now blocked: it kept failing",
            "✖ [default] Kanban t_b327fe82 — its worker stopped unexpectedly; it will be retried automatically.",
            "⏱ [default] @friday Kanban t_b327fe82 ran past its time limit and was stopped; it will be retried automatically.",
            "🔄 [default] @friday Kanban t_b327fe82 → ready",
            "👀 [default] @friday Kanban t_b327fe82 ready for review — Send the draft",
            "🛑 [default] Kanban t_b327fe82 review requested changes/BLOCK: Update tests",
            "🛑 [default] @friday Kanban t_b327fe82 routed to TRIAGE — for orchestration attention",
        ]
        for notice in notices:
            with self.subTest(notice=notice):
                self.assertEqual(replies.prompt_kind(notice, None), "notes")
                self.assertEqual(replies.answers(notice, None), "notes")
        for text in ["Why is Kanban t_b327fe82 blocked?", "Kanban t_b327fe82 blocked: can you fix it?",
                     "⏸ [default] @default Kanban board help", "Here is the notice:\n" + notices[0]]:
            self.assertIsNone(replies.prompt_kind(text, None))
        scan.scan(self.con, "jarvis", self.source)
        with sqlite3.connect(self.source) as c:
            for rid, role, content, tools in [(2, "user", notices[0], None), (3, "assistant", "Checking the task.", "[{}]"),
                                              (4, "assistant", "The worker is blocked.", None), (5, "user", "What should I do next?", None),
                                              (6, "assistant", "Review the revised draft.", None)]:
                c.execute("INSERT INTO messages VALUES(?,'chat',?,?,?,'stop',NULL,1)", (rid, role, content, tools))
        response = self.call("GET", "/kinds", params={"profile": "jarvis", "session": "chat"})
        self.assertEqual(response.json()["kinds"], {"2": "notes", "3": "notes", "4": "notes"})
        scan.scan(self.con, "jarvis", self.source)
        self.con.commit()
        self.assertEqual(self.reply_rows(), [6])

    def test_request_merged_after_hermes_notes_is_the_persons(self):
        """Jarvis, 2026-10-01: after compaction Hermes merged model-switch notes with the person's
        request into one row; the request and its answer must show, not hide as Hermes notes."""
        note = "[System: The active model for this chat has changed to openai/gpt-5.6-luna via provider nous.]"
        coding = ("[System: You edited code in this turn.\n\nVerification status: unverified\n\n"
                  "[Coding] Before you run tests: keep it KISS.]")

        def row(rid, role, text, kind=None):
            return {"id": rid, "role": role, "content": text, "display_kind": kind, "tool_calls": None, "finish_reason": "stop", "active": 1}

        merged = note + "\n\n" + note + "\n\nI’m trying to add things to the shopping list. Fix it for me"
        self.assertEqual(replies.person_text(merged), "I’m trying to add things to the shopping list. Fix it for me")
        self.assertEqual(replies.message_kinds([row(1, "user", merged, "model_switch"), row(2, "assistant", "The voice path works.")]), {})
        # Notes alone, nested brackets, text running on inside a note, and a merged job report stay machine.
        self.assertEqual(replies.message_kinds([row(1, "user", note), row(2, "assistant", "Noted.")]), {1: "notes", 2: "notes"})
        self.assertIsNone(replies.person_text(coding))
        self.assertEqual(replies.message_kinds([row(1, "user", coding)]), {1: "notes"})
        self.assertIsNone(replies.person_text("[System: switched] and more words"))
        self.assertEqual(replies.message_kinds([row(1, "user", note + '\n\n[Cronjob "x" output]')]), {1: "cron"})

    def test_final_qa_rules(self):
        def row(rid, role, text, kind=None, tools=None):
            return {"id": rid, "role": role, "content": text, "display_kind": kind, "tool_calls": tools, "finish_reason": "stop", "active": 1}

        # A crash-recovery note is a Hermes note, but its answer is a reply to the person.
        kinds = replies.message_kinds([row(1, "user", "[System note: resumed. The interrupted request was:] Plan Q4", "auto_continue"),
                                       row(2, "assistant", "Here is the plan.")])
        self.assertEqual(kinds, {1: "notes"})
        self.assertTrue(replies.is_reply(row(2, "assistant", "Here is the plan."), replies.answers("x", "auto_continue")))
        # Scheduled runs: Hermes' looser silence, and the job's own prompt is a job report.
        self.assertEqual(replies.message_kinds([row(1, "user", "[IMPORTANT: You are running as a scheduled cron job. ...]"),
                                                row(2, "assistant", "[SILENT] No changes detected")], autonomous=True), {1: "cron", 2: "silent"})
        self.assertEqual(replies.message_kinds([row(1, "user", "[IMPORTANT: You are running as a scheduled cron job.]"),
                                                row(2, "assistant", "3 new invoices.")], autonomous=True), {1: "cron", 2: "summary"})
        self.assertFalse(replies.is_silence("[SILENT] No changes detected"))  # the interactive rule stays exact
        # Compaction notes are Hermes notes; text the person types that merely starts [System is theirs.
        self.assertEqual(replies.prompt_kind("[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns", None), "notes")
        self.assertIsNone(replies.prompt_kind("[Systems review] can you look at this?", None))
        # A hidden messaging chat is in none of the app's lists, so it never counts.
        self.assertIsNone(replies.chat_group("telegram", 1, 0))

    def test_preview_strips_marks_and_caps_length(self):
        self.assertEqual(replies.preview("# Title\n\n`code` and **bold**"), "Title code and bold")
        self.assertEqual(replies.preview(None), "Your reply is ready.")
        card = '```hermes-card\n{"schema":"hermes.card","title":"Spending this week","type":"chart"}\n```'
        self.assertEqual(replies.preview("Here's the week.\n\n" + card), "Here's the week. Spending this week")
        self.assertEqual(replies.preview(card), "Spending this week")
        long = replies.preview("word " * 100)
        self.assertLessEqual(len(long), replies.PREVIEW_LIMIT)
        self.assertTrue(long.endswith("…"))

    def test_full_text_only_when_it_fits(self):
        base = {"aps": {"alert": {"title": "Jarvis", "body": "x"}}, "bot": "jarvis", "chat": "chat", "reply": "4"}
        self.assertEqual(sealed.with_full_text(base, "Short")["text"], "Short")
        self.assertNotIn("text", sealed.with_full_text(base, "é" * 1000))  # escapes to 6 bytes each
        self.assertNotIn("text", sealed.with_full_text(base, None))

    def test_attention_and_unread_add_up(self):
        self.reply(10, "New reply")
        state.unread_for(self.con, state.OWNER)  # baseline
        self.reply(20, "Later reply")
        self.con.execute("INSERT OR REPLACE INTO attention VALUES('default', 2, 1, '[]', 0)")
        self.con.commit()
        self.assertEqual(state.badge_for(self.con, state.OWNER, PHONE), 3)


if __name__ == "__main__":
    unittest.main()


class TextBeforeACardIsTheReply(unittest.TestCase):
    """Text a bot writes just before a card call is the answer that goes with the card (Mark, 2026-10-18)."""

    def test_text_beside_a_card_call_is_shown_and_other_tool_notes_stay_hidden(self):
        card = '[{"function": {"name": "hermes_card_selection", "arguments": "{}"}}]'
        other = '[{"function": {"name": "web_search", "arguments": "{}"}}]'
        rows = [
            {"id": 1, "role": "user", "content": "Hi are you there", "display_kind": None, "tool_calls": None},
            {"id": 2, "role": "assistant", "content": "Yes, I'm here. Your map is ready.", "display_kind": None, "tool_calls": card},
            {"id": 3, "role": "assistant", "content": "Searching for that.", "display_kind": None, "tool_calls": other},
            {"id": 4, "role": "assistant", "content": "", "display_kind": None, "tool_calls": card},
        ]
        kinds = replies.message_kinds(rows)
        self.assertNotIn(2, kinds, "text before a card is the reply")
        self.assertEqual(kinds.get(3), "tools")
        self.assertEqual(kinds.get(4), "tools", "a bare card call stays a tool step")
