import json
import os
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mobile_support import MobileCase, alerts, events, scan, sealed  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[3] / "qa" / "fixtures"


def private_key(value):
    return ec.derive_private_key(int.from_bytes(sealed.unb64url(value), "big"), ec.SECP256R1())


needs_fixtures = unittest.skipUnless(FIXTURES.is_dir(), "qa/fixtures is not in this checkout (the public plugin mirror)")


class VectorTests(unittest.TestCase):
    @needs_fixtures
    def test_opens_bighelps_vector_with_their_labels(self):
        # Same envelope format as bighelp-plugin's sealed_alerts.py: their published vector opens.
        vector = json.loads((FIXTURES / "bighelp-sealed-alert-v2-vector.json").read_text())
        opened = sealed.open_envelope(vector["envelope"], private_key(vector["recipientPrivateKey"]),
                                      sealed.unb64url(vector["senderPublicKey"]), sealed.LOOPDY)
        self.assertEqual(opened, vector["plaintext"])

    @needs_fixtures
    def test_opens_our_vector(self):
        # The app's regression opens the same file, so the bridge and the extension can't drift apart.
        vector = json.loads((FIXTURES / "sealed-alert-v1-vector.json").read_text())
        opened = sealed.open_envelope(vector["envelope"], private_key(vector["recipientPrivateKey"]),
                                      sealed.unb64url(vector["senderPublicKey"]))
        self.assertEqual(opened, vector["plaintext"])

    def test_tampering_and_the_wrong_sender_are_refused(self):
        recipient, sender, other = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
        envelope = sealed.seal(installation="device-1234567890", event_id="event", content={"title": "Friday", "body": "Hi"},
                               recipient=sealed.public_bytes(recipient.public_key()), sender=sender, issued=1)
        with self.assertRaises(Exception):
            sealed.open_envelope(envelope, recipient, sealed.public_bytes(other.public_key()))
        forged = {**envelope, "grantId": "device-other000000"}
        with self.assertRaises(Exception):
            sealed.open_envelope(forged, recipient, sealed.public_bytes(sender.public_key()))

    def test_content_key_validation(self):
        good = sealed.b64url(sealed.public_bytes(ec.generate_private_key(ec.SECP256R1()).public_key()))
        self.assertEqual(len(sealed.recipient_key(good)), 65)
        for bad in ("A" * 87, sealed.b64url(b"\x04" + b"\x01" * 64)):
            with self.assertRaises(ValueError):
                sealed.recipient_key(bad)


class SealedAlertTests(MobileCase):
    """What the phone gets: the plugin seals each alert for one device with the connector's sender key."""

    def setUp(self):
        super().setUp()
        self.script("""INSERT INTO messages VALUES(4,'chat','assistant','**Done.** The flight is booked for Friday.',NULL,'stop',NULL,1);""")

    def test_reply_alert_is_sealed_with_the_full_text(self):
        scan.scan(self.con, "jarvis", self.source)
        self.sql("DELETE FROM messages WHERE id=4")
        self.sql("INSERT INTO messages VALUES(5,'chat','assistant','**Done.** The flight is booked for Friday.',NULL,'stop',NULL,1)")
        self.scan_twice()
        (payload,) = self.payloads()
        self.assertEqual(payload["aps"]["alert"], alerts.REPLY_PLACEHOLDER)
        self.assertNotIn("text", payload)
        self.assertNotIn("booked", json.dumps(payload))  # nothing readable leaves in the clear
        self.assertEqual((payload["bot"], payload["chat"], payload["reply"]), ("jarvis", "chat", "5"))
        self.assertEqual(payload["aps"]["category"], "reply")
        opened = self.opened(payload)
        self.assertEqual(opened["title"], "Jarvis")
        self.assertEqual(opened["body"], "Done. The flight is booked for Friday.")
        self.assertEqual(opened["text"], "**Done.** The flight is booked for Friday.")
        self.assertEqual(opened["type"], "reply")

    def test_long_reply_drops_the_full_text_but_stays_sealed(self):
        device = alerts.devices.devices()[0]
        out = alerts.seal_for(device, {"aps": {"alert": {"title": "Jarvis", "body": "x"}}, "text": "long"},
                              {"type": "reply", "title": "Jarvis", "body": "x", "text": sealed.b64url(os.urandom(6000))},
                              "event", alerts.REPLY_PLACEHOLDER)
        self.assertLessEqual(len(json.dumps(out).encode()), sealed.PAYLOAD_LIMIT)
        self.assertNotIn("text", self.opened(out))

    def test_a_broken_device_key_never_falls_back_to_plain_text(self):
        broken = alerts.devices.Device(self.phone.id, "x", "ios", "A" * 87)
        out = alerts.seal_for(broken, {"aps": {"alert": {"title": "Jarvis", "body": "secret"}}, "text": "secret"},
                              {"title": "Jarvis", "body": "secret"}, "e", alerts.REPLY_PLACEHOLDER)
        self.assertNotIn("secret", json.dumps(out))
        self.assertEqual(out["aps"]["alert"], alerts.REPLY_PLACEHOLDER)

    def test_without_a_sender_key_sealing_gives_the_placeholder_only(self):
        device = alerts.devices.devices()[0]
        out = sealed.seal_payload({"aps": {"alert": {"title": "J", "body": "secret"}}, "text": "secret"}, {"title": "J", "body": "secret"},
                                  device_id=device.id, device_key=device.recipient_public_key, sender=lambda: None,
                                  event_id="e", placeholder=alerts.REPLY_PLACEHOLDER, log=lambda *_: None)
        self.assertNotIn("secret", json.dumps(out))
        self.assertNotIn("sealed", out)

    def test_approval_alert_is_sealed(self):
        events.approval({"event": "requested", "profile": "jarvis", "session_id": "chat",
                         "command": "rm -rf build", "description": "delete the build folder"})
        (payload,) = self.payloads()
        self.assertEqual(payload["aps"]["alert"], alerts.APPROVAL_PLACEHOLDER)
        self.assertEqual(payload["aps"]["category"], "approval")
        self.assertTrue(payload["approval"])
        self.assertNotIn("rm -rf", json.dumps(payload))
        opened = self.opened(payload)
        self.assertEqual(opened["title"], "Jarvis needs approval")
        self.assertIn("rm -rf build", opened["body"])


if __name__ == "__main__":
    unittest.main()
