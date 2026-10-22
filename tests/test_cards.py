"""Tests for the Agents CS cards plugin. Run from this folder's parent:

    python3 -m unittest discover -s plugin/agents-cs/tests

The dashboard route test needs FastAPI (Hermes' own venv has it) and is skipped without it.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_loader  # noqa: E402


def load_plugin(name=None):
    """The ``cards`` module, imported the way Hermes does: a subpackage of the plugin, relative imports intact."""
    return plugin_loader.load_module("cards")


plugin = load_plugin()
cards = sys.modules[plugin.__name__ + ".cards"]
store_module = sys.modules[plugin.__name__ + ".store"]
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

EXAMPLES = {
    "summary": {"title": "Backup finished", "body": "All 3 volumes copied.\nNo errors.", "tone": "positive", "badge": "Done"},
    "metrics": {"title": "This week", "items": [
        {"label": "Spent", "value": "$1,240", "detail": "of $1,500", "trend": "up", "tone": "warning"},
        {"label": "Meetings", "value": 14}]},
    "list": {"title": "Unread", "numbered": True, "items": [{"title": "Invoice from Acme", "detail": "Due Friday", "trailing": "$320"}]},
    "checklist": {"title": "Before the trip", "submit_label": "Send", "items": [
        {"id": "passport", "label": "Passport", "done": True}, {"id": "charger", "label": "Charger", "done": False}]},
    "table": {"title": "Invoices", "columns": [{"label": "Vendor"}, {"label": "Amount", "align": "trailing"}],
              "rows": [["Acme", 320], ["Globex", 1250.5], ["Initech", None]]},
    "timeline": {"title": "Deploy", "steps": [{"title": "Build", "status": "done", "time": "09:12"},
                                              {"title": "Tests", "status": "current"}, {"title": "Ship", "status": "upcoming"}]},
    "chart": {"title": "Spending", "kind": "bar", "unit": "$", "y_label": "USD",
              "series": [{"name": "This week", "points": [{"x": "Mon", "y": 40}, {"x": "Tue", "y": 125.5}]}]},
    "form": {"title": "Trip details", "submit_label": "Plan it", "description": "A few details first.", "fields": [
        {"id": "city", "kind": "text", "label": "Destination", "required": True, "placeholder": "Lisbon"},
        {"id": "nights", "kind": "integer", "label": "Nights", "min": 1, "max": 30, "default": 3},
        {"id": "budget", "kind": "select", "label": "Budget", "options": [{"id": "low", "label": "Low"}, {"id": "high", "label": "High"}]},
        {"id": "extras", "kind": "multi_select", "label": "Extras", "max_selected": 2,
         "options": [{"id": "car", "label": "Car"}, {"id": "tour", "label": "Tour"}]},
        {"id": "kids", "kind": "toggle", "label": "Travelling with kids"},
        {"id": "start", "kind": "date", "label": "Start", "min": "2026-10-01"},
        {"id": "notes", "kind": "textarea", "label": "Notes"}]},
    "selection": {"title": "Pick a slot", "mode": "single", "options": [{"id": "tue", "label": "Tuesday 3pm"}, {"id": "wed", "label": "Wednesday 10am"}]},
    "message": {"title": "Send this to Morgan?", "channel": "email", "to": ["Morgan Goh <morgan@example.com>"],
                "subject": "Thursday pickup", "body": "Hi Morgan,\n\nI can do pickup on Thursday at 5.\n\nMark",
                "from": "mark@example.com", "description": "You asked me to confirm Thursday."},
}


class CardSchemaTests(unittest.TestCase):
    def test_worker_guard_cannot_bypass_task_policy(self):
        with patch.dict('os.environ', {'HERMES_KANBAN_TASK': 't_fixture'}), patch.object(plugin.guard, 'run') as convert:
            self.assertIsNone(plugin._guard('Please approve this again?', 'fixture-session'))
            convert.assert_not_called()

    def test_skipped_delivery_does_not_reprompt_next_run(self):
        card = cards.render('message', EXAMPLES['message'])
        instruction = cards.delivery(card, attached=True)['instruction']
        self.assertIn('do not ask again on the next run', instruction)
        self.assertNotIn('keep the draft and ask again next time', instruction)

    def test_the_bot_is_told_to_put_its_whole_answer_after_the_card_call(self):
        """2026-10-18: told only to "add one short sentence", Friday wrote its real answer before the card call, where the
        app does not show it (commentary beside a tool call), and the person got "Pick an option on the card." alone."""
        for card_type, args in EXAMPLES.items():
            for attached in (True, False):
                with self.subTest(card_type=card_type, attached=attached):
                    card = cards.render(card_type, args, now=NOW, request_id_factory=lambda: "a" * 32)
                    instruction = cards.delivery(card, attached=attached)["instruction"]
                    self.assertIn("never text written before a tool call", instruction)
                    self.assertIn("put your whole answer there", instruction)
                    self.assertNotIn("at most add one short sentence of your own", instruction)

    def test_every_type_renders_and_round_trips(self):
        for card_type, args in EXAMPLES.items():
            with self.subTest(card_type):
                card = cards.render(card_type, args, now=NOW, request_id_factory=lambda: "a" * 32)
                self.assertEqual((card["schema"], card["version"], card["type"]), ("hermes.card", 1, card_type))
                self.assertRegex(card["id"], "^[0-9a-f]{32}$")
                self.assertTrue(card["fallback"].startswith(args["title"]))
                self.assertEqual(cards.validate_envelope(cards.canonical_json(card)), card)
                self.assertEqual(cards.cards_in("Here it is.\n\n" + cards.fence(card)), [card])

    def test_display_cards_are_named_by_content_forms_are_random(self):
        first = cards.render("metrics", EXAMPLES["metrics"])
        self.assertEqual(first["id"], cards.render("metrics", EXAMPLES["metrics"])["id"])
        form = cards.render("form", EXAMPLES["form"], now=NOW)
        self.assertNotEqual(form["id"], cards.render("form", EXAMPLES["form"], now=NOW)["id"])
        self.assertEqual(form["expires_at"], "2026-10-05T12:00:00Z")

    def test_decisions_hold_three_options_and_one_recommendation(self):
        card = cards.render("selection", {"title": "Refresh the mirror?", "mode": "single", "options": [
            {"id": "now", "label": "Refresh it now", "recommended": True}, {"id": "wait", "label": "Wait"},
            {"id": "retire", "label": "Retire it"}]}, now=NOW)
        self.assertEqual([o.get("recommended", False) for o in card["data"]["options"]], [True, False, False])
        with self.assertRaises(cards.CardError) as raised:
            cards.render("selection", {"title": "T", "mode": "single", "options": [
                {"id": f"o{i}", "label": "x"} for i in range(5)]}, now=NOW)
        self.assertIn("app adds Other", str(raised.exception))

    def test_drafted_message_waits_for_send(self):
        card = cards.render("message", EXAMPLES["message"], request_id_factory=lambda: "b" * 32)
        self.assertEqual(card["id"], "b" * 32, "each draft is its own decision, named at random")
        self.assertTrue(cards.is_decision(card))
        self.assertIn("I can do pickup on Thursday at 5.", card["fallback"])
        self.assertIn("Nothing is sent until then", card["fallback"])
        delivery = cards.delivery(card, attached=True)
        self.assertIn("Do NOT send", delivery["instruction"])
        self.assertIn("values.edited", delivery["instruction"])
        self.assertIn("values.other", delivery["instruction"])
        self.assertNotIn("revise", cards.MESSAGE_CHOICES)
        teams = cards.render("message", {"title": "Reply in Design?", "channel": "teams", "to": ["Design review"],
                                         "thread": "Design review", "body": "Looks good to me."})
        self.assertEqual(teams["data"]["channel"], "teams")
        for bad, code in [({**EXAMPLES["message"], "channel": " "}, "invalid_string"),
                          ({**EXAMPLES["message"], "to": []}, "limit_exceeded"),
                          ({"title": "T", "channel": "teams", "to": ["A"], "body": "x", "subject": "S"}, "invalid_value"),
                          ({**EXAMPLES["message"], "body": "x" * 6_001}, "limit_exceeded")]:
            with self.subTest(code=code), self.assertRaises(cards.CardError):
                cards.render("message", bad)

    def test_any_message_service_and_default_generic_card(self):
        for channel in (None, "upwork", "teams", "imessage", "future-service", " Upwork "):
            args = {"title": "Send this message?", "to": ["Alex on Upwork"], "body": "Can you please refresh the proposal?"}
            if channel is not None:
                args["channel"] = channel
            card = cards.render("message", args)
            self.assertEqual(card["data"]["channel"], channel.strip().lower() if channel else "message")
            self.assertEqual(card["data"]["body"], args["body"])
            self.assertTrue(cards.is_decision(card))
            self.assertIn("Nothing is sent until then", card["fallback"])
        for channel in ("", "   ", "x" * 81, "upwork\n", 123):
            with self.subTest(channel=channel), self.assertRaises(cards.CardError):
                cards.render("message", {"title": "Send?", "channel": channel, "to": ["Alex"], "body": "Hi"})

    def test_upwork_draft_uses_the_message_approval_contract(self):
        card = cards.render("message", {"title": "Send this Upwork message?", "channel": "upwork",
                                        "to": ["Alex (Upwork)"], "body": "Please confirm the scope and dates."})
        self.assertEqual(card["data"]["channel"], "upwork")
        self.assertTrue(cards.is_decision(card))
        self.assertIn("Nothing is sent until then", card["fallback"])
        self.assertIn("Do NOT send", cards.delivery(card, attached=True)["instruction"])

    def test_rejections(self):
        bad = {
            "unknown field": ("summary", {"title": "T", "body": "x", "color": "red"}),
            "forbidden url": ("list", {"title": "T", "items": [{"title": "a", "url": "https://x"}]}),
            "missing title": ("summary", {"body": "x"}),
            "empty metrics": ("metrics", {"title": "T", "items": []}),
            "too many metrics": ("metrics", {"title": "T", "items": [{"label": "a", "value": 1}] * 13}),
            "bool value": ("metrics", {"title": "T", "items": [{"label": "a", "value": True}]}),
            "ragged table": ("table", {"title": "T", "columns": [{"label": "a"}, {"label": "b"}], "rows": [["x"]]}),
            "pie with two series": ("chart", {"title": "T", "kind": "pie", "series": [
                {"name": "a", "points": [{"x": "1", "y": 1}]}, {"name": "b", "points": [{"x": "1", "y": 1}]}]}),
            "negative pie": ("chart", {"title": "T", "kind": "pie", "series": [{"name": "a", "points": [{"x": "1", "y": -1}]}]}),
            "duplicate x": ("chart", {"title": "T", "kind": "bar", "series": [{"name": "a", "points": [{"x": "1", "y": 1}, {"x": "1", "y": 2}]}]}),
            "infinite": ("chart", {"title": "T", "kind": "line", "series": [{"name": "a", "points": [{"x": "1", "y": float("inf")}]}]}),
            "bad status": ("timeline", {"title": "T", "steps": [{"title": "a", "status": "maybe"}]}),
            "duplicate ids": ("checklist", {"title": "T", "items": [{"id": "a", "label": "x", "done": False}] * 2}),
            "bad id": ("selection", {"title": "T", "mode": "single", "options": [{"id": "Bad Id", "label": "x"}]}),
            "single with submit": ("selection", {"title": "T", "mode": "single", "submit_label": "Go", "options": [{"id": "a", "label": "x"}]}),
            "bad field kind": ("form", {"title": "T", "submit_label": "Go", "fields": [{"id": "a", "kind": "file", "label": "x"}]}),
            "select default not offered": ("form", {"title": "T", "submit_label": "Go", "fields": [
                {"id": "a", "kind": "select", "label": "x", "default": "zz", "options": [{"id": "b", "label": "B"}]}]}),
            "four choices": ("selection", {"title": "T", "mode": "single", "options": [
                {"id": f"o{i}", "label": f"Option {i}"} for i in range(4)]}),
            "four menu options": ("form", {"title": "T", "submit_label": "Go", "fields": [
                {"id": "a", "kind": "select", "label": "x", "options": [{"id": f"o{i}", "label": "B"} for i in range(4)]}]}),
            "two recommended": ("selection", {"title": "T", "mode": "single", "options": [
                {"id": "a", "label": "A", "recommended": True}, {"id": "b", "label": "B", "recommended": True}]}),
            "control characters": ("summary", {"title": "T\u0007", "body": "x"}),
            "not an object": ("summary", ["T"]),
        }
        for label, (card_type, args) in bad.items():
            with self.subTest(label):
                with self.assertRaises(cards.CardError):
                    cards.render(card_type, args)
        with self.assertRaises(cards.CardError):
            cards.render("weather", {"title": "T"})

    def test_envelope_checks(self):
        card = cards.render("summary", EXAMPLES["summary"])
        for label, change in {"version": {"version": 2}, "schema": {"schema": "other.card"}, "type": {"type": "weather"},
                              "id": {"id": "XYZ"}, "extra": {"script": "x"}, "expires on display": {"expires_at": "2026-10-01T00:00:00Z"}}.items():
            with self.subTest(label):
                with self.assertRaises(cards.CardError):
                    cards.validate_envelope({**card, **change})
        with self.assertRaises(cards.CardError):
            cards.validate_envelope('{"a":1,"a":2}')

    def test_fence_parsing(self):
        card = cards.render("summary", EXAMPLES["summary"])
        text = f"Intro\n\n{cards.fence(card)}\n\n```hermes-card\nnot json\n```\n\n```json\n{{}}\n```"
        self.assertEqual(cards.cards_in(text), [card])
        swapped = cards.replace_fences(text, lambda c, raw: c["fallback"] if c else raw)
        self.assertIn("Backup finished", swapped)
        self.assertIn("not json", swapped)  # an invalid block stays as it was
        self.assertNotIn('"schema"', swapped)

    def test_fallback_text(self):
        table = cards.render("table", EXAMPLES["table"])
        self.assertIn("Vendor | Amount", table["fallback"])
        self.assertIn("Globex | 1250.5", table["fallback"])
        chart = cards.render("chart", EXAMPLES["chart"])
        self.assertIn("Mon: $40", chart["fallback"])
        self.assertEqual(cards.with_unit(-5, "$"), "-$5")
        self.assertEqual(cards.with_unit(98, "%"), "98%")
        self.assertEqual(cards.with_unit(1200, "kg"), "1,200 kg")
        big = cards.render("list", {"title": "T", "items": [{"title": "x" * 150}] * 40})
        self.assertLessEqual(len(big["fallback"]), cards.FALLBACK_LIMIT)

    def test_submission_values(self):
        form = cards.render("form", EXAMPLES["form"])["data"]
        values = cards.validate_submission(form, {"city": "Lisbon", "nights": 4, "budget": "low", "extras": ["car"],
                                                  "kids": True, "start": "2026-10-02", "notes": ""})
        self.assertEqual(values, {"budget": "low", "city": "Lisbon", "extras": ["car"], "kids": True,
                                  "nights": 4, "start": "2026-10-02"})
        for label, change in {"missing required": {"city": ""}, "out of range": {"nights": 31},
                              "not offered": {"budget": "mid"}, "too many": {"extras": ["car", "tour", "car"]},
                              "early date": {"start": "2026-09-01"}, "unknown": {"pets": True},
                              "wrong type": {"kids": "yes"}}.items():
            with self.subTest(label):
                with self.assertRaises(cards.CardError):
                    cards.validate_submission(form, {"city": "Lisbon", **change})

    def test_partial_form_with_comment_preserves_fields(self):
        form = cards.render("form", EXAMPLES["form"])["data"]
        values = {"nights": 4, "_comment": "Ask about dates first."}
        self.assertEqual(cards.validate_submission(form, values), values)
        self.assertEqual(cards.validate_submission(form, {"_comment": "Ask about dates first."}),
                         {"_comment": "Ask about dates first."})
        for bad in ({"_comment": "   "}, {"_comment": True},
                    {"_comment": "Ask first", "nights": 31},
                    {"_comment": "Ask first", "budget": "not-offered"}):
            with self.subTest(bad=bad), self.assertRaises(cards.CardError):
                cards.validate_submission(form, bad)

    def test_response_block(self):
        text = 'Trip details\nDestination: Lisbon\n\n```hermes-card-response\n{"card_id":"' + "a" * 32 + '","values":{"city":"Lisbon"}}\n```'
        self.assertEqual(cards.parse_response_block(text)["values"], {"city": "Lisbon"})
        self.assertIsNone(cards.parse_response_block("no block"))
        self.assertEqual(cards.cards_in(text), [])  # an answer is never mistaken for a card


class DecisionLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = store_module.DecisionLog(Path(self.temp.name) / "forms.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_draft_reuses_id_despite_title_change(self):
        first = cards.render("message", EXAMPLES["message"])
        second = cards.render("message", {**EXAMPLES["message"], "title": "Approve this email?", "description": "Waiting"})
        self.assertNotEqual(first["id"], second["id"])
        self.log.record(first, profile="friday", session_id="s1")
        self.assertEqual(self.log.record(second, profile="friday", session_id="s1"), first["id"])
        self.assertEqual(len(self.log.open(profile="friday")), 1)

    def test_task_retry_dedupes_across_sessions_but_not_tasks_or_bots(self):
        with patch.dict("os.environ", {"HERMES_KANBAN_TASK": "t_one", "HERMES_KANBAN_BOARD": "default"}):
            first = cards.render("message", EXAMPLES["message"])
            self.log.record(first, profile="friday", session_id="s1")
            repeated = cards.render("message", EXAMPLES["message"])
            self.assertEqual(self.log.record(repeated, profile="friday", session_id="s2"), first["id"])
            self.assertEqual(self.log.record(repeated, profile="jarvis", session_id="s2"), repeated["id"])
        with patch.dict("os.environ", {"HERMES_KANBAN_TASK": "t_two"}):
            other = cards.render("message", EXAMPLES["message"])
            self.assertEqual(self.log.record(other, profile="friday", session_id="s3"), other["id"])
        self.assertEqual(len(self.log.open(profile="friday")), 2)

    def test_changed_payload_or_closed_request_needs_new_approval(self):
        first = cards.render("message", EXAMPLES["message"])
        self.log.record(first, profile="friday", session_id="s1")
        for change in ({"body": "Different words"}, {"to": ["another@example.com"]}, {"subject": "Different subject"}):
            card = cards.render("message", {**EXAMPLES["message"], **change})
            self.assertEqual(self.log.record(card, profile="friday", session_id="s1"), card["id"])
        self.log.close_from('```hermes-card-response\n' + json.dumps({"card_id": first["id"], "values": {"selected": ["send"]}}) + '\n```')
        later = cards.render("message", EXAMPLES["message"])
        self.assertEqual(self.log.record(later, profile="friday", session_id="s1"), later["id"])

    def test_concurrent_retry_creates_one_pending_draft(self):
        from concurrent.futures import ThreadPoolExecutor
        self.log.open(profile="friday")
        def record(_):
            return self.log.record(cards.render("message", EXAMPLES["message"]), profile="friday", session_id="s1")
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(record, range(12)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.log.open(profile="friday")), 1)

    def test_decisions_open_until_answered_or_skipped(self):
        choice = cards.render("selection", EXAMPLES["selection"], now=NOW)
        menu = cards.render("form", {"title": "Refresh?", "submit_label": "Send", "fields": [
            {"id": "d", "kind": "select", "label": "Decision", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]},
            {"id": "n", "kind": "textarea", "label": "Note"}]}, now=NOW, request_id_factory=lambda: "c" * 32)
        trip = cards.render("form", EXAMPLES["form"], now=NOW, request_id_factory=lambda: "d" * 32)
        for card in (choice, menu, trip):
            self.log.record(card, profile="friday", session_id="s1", now=NOW.timestamp())
        self.assertEqual({d["card_id"] for d in self.log.open(profile="friday")}, {choice["id"], menu["id"]},
                         "A single choice and a one-menu form are decisions; a many-field form isn't")
        answer = ("Decisions:\nPick a time — Skipped\n\n```hermes-card-response\n"
                  + json.dumps({"card_id": choice["id"], "values": {"skipped": True}}) + "\n```\n\n```hermes-card-response\n"
                  + json.dumps({"card_id": menu["id"], "values": {"d": "a"}}) + "\n```")
        self.assertEqual(self.log.close_from(answer, profile="jarvis"), 0, "Another bot's answer closes nothing")
        self.assertEqual(self.log.close_from(answer, profile="friday"), 2)
        self.assertEqual(self.log.open(profile="friday"), [])
        self.assertEqual(self.log.close_from(answer), 0, "Closing is once")


class FormStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = store_module.FormStore(Path(self.temp.name) / "forms.db")
        self.card = cards.render("form", EXAMPLES["form"], now=NOW, request_id_factory=lambda: "b" * 32)
        self.store.create(self.card, profile="friday", session_id="s1", now=NOW.timestamp())

    def tearDown(self):
        self.temp.cleanup()

    def test_one_answer_then_readable(self):
        now = NOW.timestamp() + 60
        self.assertEqual(self.store.status("b" * 32, now=now)["state"], "pending")
        refused = self.store.submit("b" * 32, {"nights": 4}, profile="friday", now=now)
        self.assertEqual((refused["state"], refused["code"]), ("error", "invalid_value"))
        accepted = self.store.submit("b" * 32, {"city": "Lisbon", "nights": 4}, profile="friday", now=now)
        self.assertEqual(accepted["state"], "accepted")
        again = self.store.submit("b" * 32, {"city": "Porto"}, profile="friday", now=now)
        self.assertEqual(again["code"], "already_submitted")
        status = self.store.status("b" * 32, profile="friday", mark_read=True, now=now)
        self.assertEqual((status["state"], status["values"]), ("submitted", {"city": "Lisbon", "nights": 4}))

    def test_owner_expiry_and_unknown(self):
        self.assertEqual(self.store.submit("b" * 32, {"city": "x"}, profile="jarvis")["code"], "owner_mismatch")
        self.assertEqual(self.store.status("b" * 32, profile="jarvis")["state"], "not_found")
        self.assertEqual(self.store.submit("c" * 32, {})["code"], "request_not_found")
        late = NOW.timestamp() + 8 * 86_400
        self.assertEqual(self.store.status("b" * 32, now=late)["state"], "expired")
        self.assertEqual(self.store.submit("b" * 32, {"city": "x"}, now=late)["code"], "request_expired")
        with self.assertRaises(cards.CardError):
            self.store.create(self.card, profile="friday", session_id="s1")


class FakeContext:
    profile_name = "friday"

    def __init__(self):
        self.tools, self.hooks, self.skills = {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        assert schema["name"] == name and toolset == plugin.TOOLSET
        self.tools[name] = (schema, handler)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_skill(self, name, path, description=""):
        assert Path(path).exists()
        self.skills[name] = path


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = patch.object(store_module, "hermes_root", return_value=Path(self.temp.name))
        self.root.start()
        plugin._PENDING.clear()

    def tearDown(self):
        self.root.stop()
        self.temp.cleanup()

    def registered(self, attached):
        ctx = FakeContext()
        with patch.object(plugin, "_attach_supported", return_value=attached):
            plugin.register(ctx)
        return ctx

    def test_registers_every_tool_hook_and_skill(self):
        ctx = self.registered(True)
        self.assertEqual(set(ctx.tools), {f"hermes_card_{t}" for t in cards.TYPES} | {"hermes_card_form_response", "hermes_task_wait", "hermes_task_adjust"})
        self.assertEqual(set(ctx.hooks), {"pre_llm_call", "transform_llm_output", "pre_tool_call", "post_tool_call", "transform_tool_result",
                                          "kanban_task_blocked", "kanban_task_completed", "kanban_task_claimed", "on_kanban_dispatch_tick",
                                          "pre_approval_request", "post_approval_response", "post_llm_call"})
        self.assertEqual(set(ctx.skills), {"native-cards"})
        for name, (schema, _handler) in ctx.tools.items():
            self.assertFalse(schema["parameters"]["additionalProperties"], name)
            json.dumps(schema)

    def test_attached_card_is_added_to_the_reply_once(self):
        ctx = self.registered(True)
        result = json.loads(ctx.tools["hermes_card_metrics"][1](EXAMPLES["metrics"], session_id="s1"))
        self.assertNotIn("display_markdown", result)
        transform = ctx.hooks["transform_llm_output"]
        reply = transform(response_text="Here's the week.", session_id="s1", platform="tui")
        self.assertTrue(reply.startswith("Here's the week.\n\n```hermes-card\n"))
        self.assertEqual(cards.cards_in(reply)[0]["id"], result["card_id"])
        self.assertIsNone(transform(response_text="Next turn.", session_id="s1", platform="tui"))

    def test_repeated_draft_tool_returns_and_attaches_original_id(self):
        ctx = self.registered(True)
        handler = ctx.tools["hermes_card_message"][1]
        first = json.loads(handler(EXAMPLES["message"], session_id="s1"))
        second = json.loads(handler({**EXAMPLES["message"], "title": "Approve email?"}, session_id="s1"))
        reply = plugin.on_transform_llm_output(response_text="Draft", session_id="s1", platform="tui")
        self.assertEqual(first["card_id"], second["card_id"])
        self.assertEqual(len(cards.cards_in(reply)), 1)
        self.assertEqual(cards.cards_in(reply)[0]["id"], first["card_id"])

    def test_card_the_bot_already_copied_is_not_added_twice(self):
        ctx = self.registered(True)
        card_id = json.loads(ctx.tools["hermes_card_summary"][1](EXAMPLES["summary"], session_id="s1"))["card_id"]
        copied = "Done.\n\n" + cards.fence(cards.render("summary", EXAMPLES["summary"]))
        self.assertIn(card_id, copied)
        self.assertIsNone(ctx.hooks["transform_llm_output"](response_text=copied, session_id="s1", platform="desktop"))

    def test_garbled_copy_becomes_the_recorded_card(self):
        ctx = self.registered(True)
        card_id = json.loads(ctx.tools["hermes_card_selection"][1](EXAMPLES["selection"], session_id="s1"))["card_id"]
        garbled = cards.fence(cards.render("selection", EXAMPLES["selection"])).replace('"type":"selection"', '"type":"further"')
        self.assertIn('"further"', garbled)
        reply = ctx.hooks["transform_llm_output"](response_text=f"One decision (card {card_id}).\n\n{garbled}\n\nThanks.",
                                                  session_id="s1", platform="desktop")
        self.assertNotIn('"further"', reply)
        self.assertEqual([c["id"] for c in cards.cards_in(reply)], [card_id])
        self.assertTrue(reply.endswith("Thanks."), "The copy is fixed where it stands")

    def test_card_named_in_prose_is_still_attached(self):
        ctx = self.registered(True)
        card_id = json.loads(ctx.tools["hermes_card_selection"][1](EXAMPLES["selection"], session_id="s1"))["card_id"]
        reply = ctx.hooks["transform_llm_output"](response_text=f"See card {card_id} below.", session_id="s1", platform="desktop")
        self.assertEqual([c["id"] for c in cards.cards_in(reply)], [card_id])

    def test_messaging_channels_get_text(self):
        ctx = self.registered(True)
        ctx.tools["hermes_card_list"][1](EXAMPLES["list"], session_id="tg")
        copied = cards.fence(cards.render("summary", EXAMPLES["summary"]))
        reply = ctx.hooks["transform_llm_output"](response_text="Sure.\n\n" + copied, session_id="tg", platform="telegram")
        self.assertNotIn("```", reply)
        self.assertIn("Backup finished", reply)
        self.assertIn("1. Invoice from Acme", reply)

    def test_a_decision_nobody_can_tap_is_not_left_open(self):
        ctx = self.registered(True)
        shown = json.loads(ctx.tools["hermes_card_selection"][1](EXAMPLES["selection"], session_id="chat"))["card_id"]
        ctx.hooks["transform_llm_output"](response_text="Pick one.", session_id="chat", platform="tui")
        cron = json.loads(ctx.tools["hermes_card_selection"][1]({**EXAMPLES["selection"], "title": "Sign the form?"},
                                                                 session_id="job"))["card_id"]
        reply = ctx.hooks["transform_llm_output"](response_text="Needs a decision.", session_id="job", platform="cron")
        self.assertNotIn("```", reply)
        self.assertNotEqual(shown, cron, "Card ids come from their content")
        self.assertEqual({d["card_id"] for d in plugin._DECISIONS.open(profile="friday")}, {shown},
                         "A card swapped for its text in a scheduled job never shows, so it is not an open decision")

    def test_interrupted_turn_leaves_nothing_behind(self):
        ctx = self.registered(True)
        ctx.tools["hermes_card_summary"][1](EXAMPLES["summary"], session_id="s1")
        ctx.hooks["pre_llm_call"](session_id="s1")
        self.assertIsNone(ctx.hooks["transform_llm_output"](response_text="Hi", session_id="s1", platform="tui"))

    def test_older_hermes_gets_markdown_to_copy(self):
        ctx = self.registered(False)
        result = json.loads(ctx.tools["hermes_card_table"][1](EXAMPLES["table"], session_id="s1"))
        self.assertEqual(cards.cards_in(result["display_markdown"])[0]["type"], "table")
        self.assertIsNone(ctx.hooks["transform_llm_output"](response_text="Hi", session_id="s1", platform="tui"))

    def test_invalid_arguments_return_an_error_the_bot_can_fix(self):
        ctx = self.registered(True)
        result = json.loads(ctx.tools["hermes_card_chart"][1]({"title": "T", "kind": "radar", "series": []}, session_id="s1"))
        self.assertEqual(result["error"], "invalid_value")
        self.assertIn("kind", result["message"])

    def test_form_round_trip_through_the_tools(self):
        ctx = self.registered(True)
        result = json.loads(ctx.tools["hermes_card_form"][1](EXAMPLES["form"], session_id="s1"))
        request_id = result["request_id"]
        self.assertIn("hermes_card_form_response", result["instruction"])
        read = ctx.tools["hermes_card_form_response"][1]
        self.assertEqual(json.loads(read({"request_id": request_id}))["state"], "pending")
        store_module.FormStore().submit(request_id, {"city": "Lisbon"}, profile="friday")
        answer = json.loads(read({"request_id": request_id}))
        self.assertEqual((answer["state"], answer["values"]), ("submitted", {"city": "Lisbon"}))
        self.assertEqual(json.loads(read({"request_id": "nope"}))["error"], "invalid_value")


class DashboardRouteTests(unittest.TestCase):
    def test_submit_and_status_routes(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI is not installed (run with Hermes' venv python)")
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(store_module, "hermes_root", return_value=Path(temp)):
                api = plugin_loader.load_fresh("cards.routes", "api_test")
                api._store = store_module.FormStore(Path(temp) / "forms.db")
                card = cards.render("form", EXAMPLES["form"])
                api._store.create(card, profile="friday", session_id="s1")
                app = FastAPI()
                app.include_router(api.router, prefix="/api/plugins/agents-cs")
                client = TestClient(app)
                self.assertEqual(client.get("/api/plugins/agents-cs/ssh/host-key").status_code, 404)
                base = f"/api/plugins/agents-cs/forms/{card['id']}"
                self.assertEqual(client.get(base, params={"profile": "friday"}).json()["state"], "pending")
                bad = client.post(base + "/submit", json={"profile": "friday", "values": {}}).json()
                self.assertEqual(bad["code"], "invalid_value")
                good = client.post(base + "/submit", json={"profile": "friday", "values": {"city": "Lisbon"}}).json()
                self.assertEqual(good["state"], "accepted")
                self.assertEqual(client.get(base).json()["values"], {"city": "Lisbon"})
                self.assertEqual(client.get("/api/plugins/agents-cs/forms/xyz").status_code, 404)

    def test_kanban_specify_binds_the_secret_scope(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("FastAPI is not installed (run with Hermes' venv python)")
        import types
        from collections import namedtuple
        bound = []
        scope = types.ModuleType("agent.secret_scope")
        scope.build_profile_secret_scope = lambda home: {"KEY": "v", "home": str(home)}
        scope.set_secret_scope = lambda mapping, profile_home=None: bound.append(mapping) or "token"
        scope.reset_secret_scope = lambda token: bound.append("reset")
        Outcome = namedtuple("Outcome", "task_id ok reason new_title")
        specify = types.ModuleType("hermes_cli.kanban_specify")
        def specify_task(task_id, author=None):
            self.assertEqual(len(bound), 1, "the scope is bound while the specifier runs")
            return Outcome(task_id, True, "", "Get chair quotes")
        specify.specify_task = specify_task
        Fanout = namedtuple("Fanout", "task_id ok reason fanout child_ids new_title")
        decompose = types.ModuleType("hermes_cli.kanban_decompose")
        def decompose_task(task_id, author=None, timeout=None):
            self.assertEqual(len(bound), 1, "the scope is bound while the decomposer runs")
            return Fanout(task_id, True, "", True, ["c1", "c2"], None)
        decompose.decompose_task = decompose_task
        cli = types.ModuleType("hermes_cli")
        cli.kanban_specify = specify
        cli.kanban_decompose = decompose
        agent = types.ModuleType("agent")
        agent.secret_scope = scope
        fakes = {"agent": agent, "agent.secret_scope": scope, "hermes_cli": cli, "hermes_cli.kanban_specify": specify,
                 "hermes_cli.kanban_decompose": decompose}
        with patch.dict(sys.modules, fakes):
            api = plugin_loader.load_fresh("cards.routes", "api_specify")
            app = FastAPI()
            app.include_router(api.router, prefix="/api/plugins/agents-cs")
            client = TestClient(app)
            answer = client.post("/api/plugins/agents-cs/kanban/specify/t_abc", json={"author": "mark"}).json()
            self.assertEqual(answer, {"ok": True, "task_id": "t_abc", "reason": "", "new_title": "Get chair quotes"})
            self.assertEqual(bound[-1], "reset")
            self.assertEqual(client.post("/api/plugins/agents-cs/kanban/specify/..%2Fx", json={}).status_code, 404)
            bound.clear()
            split = client.post("/api/plugins/agents-cs/kanban/decompose/t_abc", json={"author": "mark"}).json()
            self.assertEqual(split, {"ok": True, "task_id": "t_abc", "reason": "", "fanout": True, "child_ids": ["c1", "c2"], "new_title": None})
            self.assertEqual(bound[-1], "reset")


if __name__ == "__main__":
    unittest.main()
