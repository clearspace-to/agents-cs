"""Shared fixtures for the ``mobile`` module's tests: a throwaway Hermes root home (one profile, ``jarvis``, whose
``state.db`` the tests write), a throwaway connector home (``devices.json``, ``sender-key.pem``) and helpers to read
the outbox and open the sealed alerts the way the phone would.

No Hermes, no network, no APNs: the module's only output is rows in ``<root>/hermes-bots/delivery.db``.
"""
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader  # noqa: E402

plugin_loader.load_root()
PACKAGE = plugin_loader.PACKAGE


def mobile(name):
    """A submodule of the plugin's ``mobile`` module (``replies``, ``state``, ``alerts``...)."""
    return importlib.import_module("%s.mobile.%s" % (PACKAGE, name))


paths = importlib.import_module(PACKAGE + ".paths")
home, devices, outbox, sealed = mobile("home"), mobile("devices"), mobile("outbox"), mobile("sealed")
replies, state, alerts, scan = mobile("replies"), mobile("state"), mobile("alerts"), mobile("scan")
events, worker, routes = mobile("events"), mobile("worker"), mobile("routes")

PHONE, IPAD = "dev_phone00000000000000000000", "dev_ipad000000000000000000000"
HEADERS = {"X-Hermes-Bots-Device": PHONE}


class Device:
    """A phone: its device id and its P-256 recipient key pair."""

    def __init__(self, device_id):
        self.id = device_id
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.public = sealed.b64url(sealed.public_bytes(self.private.public_key()))

    def entry(self):
        return {"id": self.id, "name": self.id[4:9], "platform": "ios", "recipient_public_key": self.public}


class MobileCase(unittest.TestCase):
    """A Hermes root with profile ``jarvis`` (chat ``chat``, one old reply) and a linked connector with one device (PHONE)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.connector = base / "hermes", base / "connector"
        (self.root / "profiles" / "jarvis").mkdir(parents=True)
        self.connector.mkdir()
        self.source = self.root / "profiles" / "jarvis" / "state.db"
        with sqlite3.connect(self.source) as c:
            c.executescript('''CREATE TABLE sessions(id TEXT,source TEXT,archived INT,hidden INT,title TEXT,parent_session_id TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,
              tool_calls TEXT,finish_reason TEXT,display_kind TEXT,active INT);
            INSERT INTO sessions(id,source,archived,hidden) VALUES('chat','desktop',0,0);
            INSERT INTO messages VALUES(1,'chat','assistant','Old reply',NULL,'stop',NULL,1);''')
        for patcher in (patch.object(home, "root", return_value=self.root),
                        patch.object(paths, "hermes_home", return_value=self.root),
                        patch.object(paths, "hermes_root", return_value=self.root)):
            patcher.start()
            self.addCleanup(patcher.stop)
        replies._indexes.clear()
        devices.forget()
        worker.reset()
        self.addCleanup(worker.reset)
        self.addCleanup(devices.forget)
        self.phone, self.ipad = Device(PHONE), Device(IPAD)
        self.sender_path = self.connector / "sender-key.pem"
        self.sender = sealed.sealing_key(self.sender_path)
        self.sender_public = sealed.public_bytes(self.sender.public_key())
        self.link([self.phone])
        self.ob = alerts.store()
        self.con = self.ob.connect()
        self.addCleanup(self.con.close)

    # --- the connector's side ----------------------------------------------------------------
    def link(self, people, computer=None):
        """Write ``connector.json`` (with the computer's hub id once the hub has welcomed it) and ``devices.json`` as the
        connector does."""
        (self.root / "hermes-bots").mkdir(exist_ok=True)
        pointer = {"home": str(self.connector), **({"computer_id": computer} if computer else {})}
        (self.root / "hermes-bots" / "connector.json").write_text(json.dumps(pointer))
        (self.connector / "devices.json").write_text(json.dumps({"updated": 1, "devices": [p.entry() for p in people]}))
        devices.forget()

    # --- the bot's database ------------------------------------------------------------------
    def sql(self, statement, *args):
        with sqlite3.connect(self.source) as c:
            c.execute(statement, args)

    def script(self, text):
        with sqlite3.connect(self.source) as c:
            c.executescript(text)

    def reply(self, rid, text="Hi there", chat="chat"):
        with sqlite3.connect(self.source) as c:
            c.execute("INSERT INTO messages VALUES(?,?,?,?,NULL,NULL,NULL,1)", (rid, chat, "user", "q"))
            c.execute("INSERT INTO messages VALUES(?,?,?,?,NULL,'stop',NULL,1)", (rid + 1, chat, "assistant", text))

    def session(self, sid, source="desktop", hidden=0, title="", archived=0, parent=None):
        with sqlite3.connect(self.source) as c:
            c.execute("INSERT INTO sessions(id,source,archived,hidden,title,parent_session_id) VALUES(?,?,?,?,?,?)",
                      (sid, source, archived, hidden, title, parent))

    def scan_twice(self, now=None):
        scan.scan(self.con, "jarvis", self.source, now=now)
        self.con.commit()
        scan.scan(self.con, "jarvis", self.source, now=now)
        self.con.commit()

    # --- the outbox --------------------------------------------------------------------------
    def rows(self, where="1=1", *args):
        with closing(self.ob.connect()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM outbox WHERE %s ORDER BY created, rowid" % where, args)]

    def queued(self, device=None):
        return [r for r in self.rows("state='queued'") if device is None or r["device_id"] == device]

    def payloads(self, device=None):
        return [json.loads(r["payload_json"]) for r in self.queued(device)]

    def reply_rows(self, device=None):
        """The row ids of the queued reply alerts (their ``reply`` field), oldest first."""
        return [int(p["reply"]) for p in self.payloads(device) if "reply" in p]

    def reply_chats(self, device=None):
        return sorted(p["chat"] for p in self.payloads(device) if "reply" in p)

    def clear(self):
        with closing(self.ob.connect()) as con, con:
            con.execute("DELETE FROM outbox")

    def opened(self, payload, who=None):
        """What the phone sees: the sealed content of a queued alert, decrypted with that device's key and checked
        against the computer's sender key."""
        who = who or self.phone
        return sealed.open_envelope(payload["sealed"], who.private, self.sender_public)

    # --- the app's requests ------------------------------------------------------------------
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(routes.router, prefix="/api/plugins/hermes-bots")
        return TestClient(app)

    def call(self, method, url, headers=HEADERS, **kw):
        return self.client().request(method, "/api/plugins/hermes-bots/mobile" + url, headers=headers, **kw)
