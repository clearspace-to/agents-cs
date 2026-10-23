"""Tests for the card guard. Run from the repo root:

    python3 -m unittest discover -s plugin/agents-cs/tests
"""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cards import load_plugin  # noqa: E402

plugin = load_plugin()
cards = sys.modules[plugin.__name__ + ".cards"]
guard = sys.modules[plugin.__name__ + ".guard"]

INCIDENT = (
    "Draft (not sent): site contingency question for Tim, Ash and Ranjitha.\n\n"
    "I need two answers before I can schedule it:\n"
    "1. Return date: When is Tim back?\n"
    "2. Channel: Teams group chat or email?\n\n"
    "When I have those, I'll show the final draft on an approval card."
)
QUESTIONS = "I need two answers before I can schedule it:\n1. Return date: When is Tim back?\n2. Channel: Teams group chat or email?"
FORM_PLAN = {"cards": [{"type": "form", "args": {
    "title": "Before I schedule it", "submit_label": "Send", "fields": [
        {"id": "return_date", "kind": "date", "label": "Tim's return date", "required": True},
        {"id": "channel", "kind": "select", "label": "Channel", "required": True,
         "options": [{"id": "teams", "label": "Teams group chat"}, {"id": "email", "label": "Email"}]}]}}],
    "remove": [QUESTIONS]}


def asks(kind="details"):
    return lambda _reply: {"asks": "yes", "asks_confidence": 0.9, "kind": kind, "kind_confidence": 0.7}


def plain(_reply):
    return {"asks": "no", "asks_confidence": 1.0, "kind": "text", "kind_confidence": 0.97}


def llm_returning(plan):
    calls = []

    def llm(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "```json\n" + json.dumps(plan) + "\n```"}}]}
    llm.calls = calls
    return llm


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = patch.object(guard, "_hermes_home", return_value=Path(self.tmp.name))
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def logged(self):
        path = Path(self.tmp.name) / "logs" / "card-guard.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_guard(self, reply, *, classify=asks(), llm=None, current_mode="on"):
        return guard.run(reply, parameters=plugin.parameters, current_mode=current_mode,
                         classify=classify, llm=llm or llm_returning(FORM_PLAN))

    def test_prose_questions_become_a_form_and_the_questions_leave_the_text(self):
        made, text = self.run_guard(INCIDENT)
        self.assertEqual([card["type"] for card in made], ["form"])
        self.assertEqual([f["kind"] for f in made[0]["data"]["fields"]], ["date", "select"])
        self.assertNotIn("When is Tim back?", text)
        self.assertIn("Draft (not sent)", text)
        self.assertEqual(self.logged()[-1]["result"], "carded")
        self.assertNotIn("Tim", json.dumps(self.logged()))  # the log carries no content

    def test_plain_text_skips_the_converter(self):
        llm = llm_returning(FORM_PLAN)
        self.assertIsNone(self.run_guard("Sent to Ash on Teams.", classify=plain, llm=llm))
        self.assertEqual(llm.calls, [])
        self.assertEqual(self.logged()[-1]["result"], "text")

    def test_unsure_text_still_goes_to_the_converter(self):
        unsure = lambda _r: {"asks": "no", "asks_confidence": 0.6, "kind": "text", "kind_confidence": 0.9}
        llm = llm_returning({"cards": [], "remove": []})
        self.assertIsNone(self.run_guard("Done.", classify=unsure, llm=llm))
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(self.logged()[-1]["result"], "no_cards")

    def test_off_silent_carded_and_secret_replies_are_left_alone(self):
        classify = lambda _r: self.fail("Jev must not be called")
        self.assertIsNone(self.run_guard(INCIDENT, classify=classify, current_mode="off"))
        self.assertIsNone(self.run_guard("[SILENT]", classify=classify))
        fenced = "Pick one.\n\n" + cards.fence(cards.render("summary", {"title": "x", "body": "y"}))
        self.assertIsNone(self.run_guard(fenced, classify=classify))
        self.assertIsNone(self.run_guard("api_key: abcdefghijkl1234 which one?", classify=classify))

    def test_invalid_cards_are_dropped_and_nothing_changes(self):
        bad = {"cards": [{"type": "selection", "args": {"title": "Pick", "mode": "single", "options": [
            {"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}, {"id": "d", "label": "D"}]}},
            {"type": "chart", "args": {}}], "remove": [QUESTIONS]}
        self.assertIsNone(self.run_guard(INCIDENT, llm=llm_returning(bad)))

    def test_inexact_removals_keep_the_text(self):
        plan = dict(FORM_PLAN, remove=["When is Tim back? (paraphrased)"])
        made, text = self.run_guard(INCIDENT, llm=llm_returning(plan))
        self.assertEqual(len(made), 1)
        self.assertEqual(text, INCIDENT)

    def test_whole_reply_on_a_card_keeps_only_the_lead(self):
        reply = "There are two Tims, Tim Ng and Tim Park. Which one do you mean?"
        plan = {"cards": [{"type": "selection", "args": {"title": "Which Tim?", "mode": "single", "options": [
            {"id": "ng", "label": "Tim Ng"}, {"id": "park", "label": "Tim Park"}]}}],
            "remove": [reply], "lead": "There are two Tims."}
        made, text = self.run_guard(reply, classify=asks("choice"), llm=llm_returning(plan))
        self.assertEqual(text, "There are two Tims.")
        _, text = self.run_guard(reply, classify=asks("choice"), llm=llm_returning(dict(plan, lead="Which?")))
        self.assertEqual(text, "")

    def test_failures_fail_open(self):
        def boom(**_kwargs):
            raise TimeoutError
        self.assertIsNone(self.run_guard(INCIDENT, llm=boom))
        self.assertEqual(self.logged()[-1], {**self.logged()[-1], "result": "error", "error": "TimeoutError"})

        def broken_jev(_reply):
            raise OSError("down")
        self.assertIsNone(self.run_guard(INCIDENT, classify=broken_jev))

    def test_jev_is_asked_through_routings_client_with_the_guards_key(self):
        jev = importlib.import_module(plugin.__name__.rsplit(".", 1)[0] + ".routing.jev")
        calls = []

        def request(state, questions, *, key=None, timeout=6):
            calls.append((state, sorted(questions), key, timeout))
            return {"answers": {"asks": {"choice": "yes", "confidence": 0.9}, "kind": {"choice": "choice", "confidence": 0.8}}}

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "jev-key"
            key.write_text("sk-guard\n")
            with patch.object(guard, "_settings", return_value={"card_guard_key_file": str(key)}), \
                    patch.object(jev, "request", side_effect=request):
                decision = guard.jev_classify("Teams or email?")
            with patch.object(guard, "_key_file", return_value=None), self.assertRaises(FileNotFoundError):
                guard.jev_classify("Teams or email?")  # no key: run() fails open
        self.assertEqual(calls, [({"reply": "Teams or email?"}, ["asks", "kind"], "sk-guard", guard.JEV_TIMEOUT)])
        self.assertEqual(decision, {"asks": "yes", "asks_confidence": 0.9, "kind": "choice", "kind_confidence": 0.8})

    def test_shadow_decides_but_changes_nothing(self):
        self.assertIsNone(self.run_guard(INCIDENT, current_mode="shadow"))
        self.assertEqual(self.logged()[-1]["result"], "shadow")
        self.assertEqual(self.logged()[-1]["cards"], ["form"])

    def test_draft_approval_card(self):
        plan = {"cards": [{"type": "selection", "args": {"title": "Send via Teams to Ash?", "mode": "single", "options": [
            {"id": "send", "label": "Send it"}, {"id": "revise", "label": "Revise"}, {"id": "dont_send", "label": "Don't send"}]}}],
            "remove": []}
        made, text = self.run_guard("Teams to Ash:\n\nHi Ash, are we still on?\n\nSend it?", classify=asks("draft"),
                                    llm=llm_returning(plan))
        self.assertEqual(made[0]["type"], "selection")
        self.assertIn("Hi Ash, are we still on?", text)


    def test_drafts_mode_leaves_questions_alone(self):
        llm = llm_returning(FORM_PLAN)
        self.assertIsNone(self.run_guard(INCIDENT, current_mode="drafts", llm=llm))
        self.assertEqual(llm.calls, [])
        self.assertEqual(self.logged()[-1]["result"], "not_draft")
        self.assertIsNone(self.run_guard("Which Tim?", classify=asks("choice"), current_mode="drafts", llm=llm))
        self.assertEqual(llm.calls, [])

    def test_drafts_mode_rescues_only_the_message_card(self):
        plan = {"cards": [
            {"type": "message", "args": {"title": "Send via Teams to Ash?", "channel": "teams", "to": ["Ash"],
                                         "body": "Hi Ash, are we still on?"}},
            {"type": "selection", "args": {"title": "Then what?", "mode": "single", "options": [
                {"id": "a", "label": "Follow up"}, {"id": "b", "label": "Wait"}]}}],
            "remove": []}
        made, text = self.run_guard("Teams to Ash:\n\nHi Ash, are we still on?\n\nThen what?", classify=asks("draft"),
                                    current_mode="drafts", llm=llm_returning(plan))
        self.assertEqual([card["type"] for card in made], ["message"])
        self.assertIn("Hi Ash, are we still on?", text)
        self.assertEqual(self.logged()[-1]["cards"], ["message"])

    def test_mode_reads_drafts_from_settings(self):
        with patch.object(guard, "_settings", return_value={"card_guard": "drafts"}):
            self.assertEqual(guard.mode(), "drafts")
        with patch.object(guard, "_settings", return_value={"card_guard": True}):
            self.assertEqual(guard.mode(), "off")


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(guard, "_hermes_home", return_value=Path(self.tmp.name)),
                        patch.object(guard, "mode", return_value="on"),
                        patch.object(guard, "jev_classify", side_effect=asks()),
                        patch.object(plugin, "_attach_supported", return_value=True)]
        for p in self.patches:
            p.start()
        self.llm = llm_returning(FORM_PLAN)
        self.llm_patch = patch.dict(sys.modules, {"agent.auxiliary_client": type(sys)("agent.auxiliary_client")})
        self.llm_patch.start()
        sys.modules["agent.auxiliary_client"].call_llm = self.llm
        plugin._STORE = None
        plugin._DECISIONS = None

    def tearDown(self):
        self.llm_patch.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_app_reply_gains_the_card_and_keeps_context(self):
        plugin.on_pre_llm_call(session_id="s1", user_message="Schedule the contingency note to Tim")
        out = plugin.on_transform_llm_output(response_text=INCIDENT, session_id="s1", platform="tui")
        found = cards.cards_in(out)
        self.assertEqual([card["type"] for card in found], ["form"])
        self.assertNotIn("When is Tim back?", out)
        self.assertIn("Schedule the contingency note", self.llm.calls[0]["messages"][1]["content"])

    def test_bot_made_cards_and_text_channels_skip_the_guard(self):
        card = cards.render("summary", {"title": "Done", "body": "ok"})
        plugin._remember("s2", card)
        out = plugin.on_transform_llm_output(response_text=INCIDENT, session_id="s2", platform="tui")
        self.assertEqual([c["type"] for c in cards.cards_in(out)], ["summary"])
        self.assertIsNone(plugin.on_transform_llm_output(response_text=INCIDENT, session_id="s3", platform="telegram"))
        self.assertEqual(self.llm.calls, [])


    def test_background_review_text_never_becomes_a_decision(self):
        # The review fork shares the chat's session id, but nobody sees its text.
        provenance = type(sys)("tools.skill_provenance")
        provenance.is_background_review = lambda: True
        with patch.dict(sys.modules, {"tools": type(sys)("tools"), "tools.skill_provenance": provenance}):
            self.assertIsNone(plugin.on_transform_llm_output(response_text=INCIDENT, session_id="s4", platform="tui"))
        self.assertEqual(self.llm.calls, [])

if __name__ == "__main__":
    unittest.main()
