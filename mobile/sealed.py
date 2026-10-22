"""End-to-end sealed alert content: only the phone that registered a content key can read it.

The plugin's ``mobile`` module (and the Claude Code host inside the connector) encrypts an alert's title and
text for that one phone and signs it with the computer's sender key, which the phone pinned from the hub's
computer list. Apple carries opaque bytes; the phone's notification extension checks the signature, decrypts
and shows the alert. (Until 2.0 this file lived in ``scripts/mobile-push`` beside the push bridge.)

Adapted from bighelp-plugin's ``sealed_alerts.py`` (https://github.com/promptclickrun/bighelp-plugin,
Apache License 2.0; see ../LICENSE). The envelope format is theirs; the labels, the plaintext fields
and the size limit are ours.

Format (binary fields unpadded base64url):
- AAD: label, installation, eventId, recipientKeyId, senderKeyId, issued (newline-joined)
- key: HKDF-SHA256(ECDH(ephemeral, recipient), salt, key label + SHA256(AAD))
- content: AES-256-GCM(raw DEFLATE(canonical JSON plaintext), AAD)
- signature: ECDSA P-256/SHA-256 (r||s) by the sender key over the signature input
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import zlib
from collections import namedtuple
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

Format = namedtuple("Format", "version aad key signature")
HERMES = Format(1, "hermes-bots-sealed-alert-v1", b"hermes-bots-sealed-alert-key-v1\0",
                b"hermes-bots-sealed-alert-signature-v1")


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(value)).rstrip(b"=").decode("ascii")


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def public_bytes(key) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def recipient_key(value: str) -> bytes:
    """A phone's content key as it registers it: a base64url 65-byte uncompressed P-256 point.
    Raises ValueError when it isn't one."""
    raw = unb64url(value)
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("Content key must be an uncompressed P-256 point")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)  # on the curve
    return raw


def key_id(public_key: bytes) -> str:
    return b64url(hashlib.sha256(public_key).digest())


def sealing_key(path: Path):
    """A signing key, created on first use and readable only by this user. (The connector creates its
    ``sender-key.pem`` with this; the plugin only reads that file and never creates one.)"""
    if not path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(pem)
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def aad(fmt: Format, installation, event_id, recipient_id, sender_id, issued) -> bytes:
    fields = [fmt.aad, installation, event_id, recipient_id, sender_id, str(int(issued))]
    if any(not field or "\n" in field for field in fields):
        raise ValueError("Sealed alert fields must be non-empty single-line values")
    return "\n".join(fields).encode("utf-8")


def signature_input(fmt: Format, aad_bytes, ephemeral, salt, nonce, ciphertext, tag) -> bytes:
    parts = [fmt.signature, hashlib.sha256(aad_bytes).hexdigest().encode("ascii")]
    parts += [b64url(v).encode("ascii") for v in (ephemeral, salt, nonce, ciphertext, tag)]
    return b"\n".join(parts)


def _deflate(value: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw DEFLATE, as Apple's COMPRESSION_ZLIB reads it
    return compressor.compress(value) + compressor.flush()


def seal(*, installation: str, event_id: str, content: dict, recipient: bytes, sender, issued: int,
         fmt: Format = HERMES, random=os.urandom, ephemeral=None) -> dict:
    """The envelope carrying ``content`` (the plaintext's own fields; ``v`` and ``eventId`` are
    added) for the phone whose content key is ``recipient``."""
    recipient_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), recipient)
    recipient_id, sender_id = key_id(recipient), key_id(public_bytes(sender.public_key()))
    aad_bytes = aad(fmt, installation, event_id, recipient_id, sender_id, issued)
    plaintext = canonical({**content, "v": fmt.version, "eventId": event_id})
    ephemeral_key = ephemeral() if ephemeral else ec.generate_private_key(ec.SECP256R1())
    ephemeral_public = public_bytes(ephemeral_key.public_key())
    salt, nonce = random(32), random(12)
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                   info=fmt.key + hashlib.sha256(aad_bytes).digest()).derive(
        ephemeral_key.exchange(ec.ECDH(), recipient_public))
    combined = AESGCM(derived).encrypt(nonce, _deflate(plaintext), aad_bytes)
    ciphertext, tag = combined[:-16], combined[-16:]
    r, s = decode_dss_signature(sender.sign(
        signature_input(fmt, aad_bytes, ephemeral_public, salt, nonce, ciphertext, tag), ec.ECDSA(hashes.SHA256())))
    return {"v": fmt.version, "grantId": installation, "eventId": event_id, "recipientKeyId": recipient_id,
            "senderKeyId": sender_id, "issued": int(issued), "ephemeralPublicKey": b64url(ephemeral_public),
            "salt": b64url(salt), "nonce": b64url(nonce), "ciphertext": b64url(ciphertext), "tag": b64url(tag),
            "signature": b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}


def open_envelope(envelope: dict, recipient_private, sender_public: bytes, fmt: Format = HERMES) -> dict:
    """What the phone does, for tests: check the signature, decrypt, inflate."""
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    field = lambda name: unb64url(envelope[name])
    aad_bytes = aad(fmt, envelope["grantId"], envelope["eventId"], envelope["recipientKeyId"],
                    envelope["senderKeyId"], envelope["issued"])
    signature = field("signature")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public).verify(
        encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")),
        signature_input(fmt, aad_bytes, field("ephemeralPublicKey"), field("salt"), field("nonce"),
                        field("ciphertext"), field("tag")), ec.ECDSA(hashes.SHA256()))
    ephemeral = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), field("ephemeralPublicKey"))
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=field("salt"),
                   info=fmt.key + hashlib.sha256(aad_bytes).digest()).derive(
        recipient_private.exchange(ec.ECDH(), ephemeral))
    packed = AESGCM(derived).decrypt(field("nonce"), field("ciphertext") + field("tag"), aad_bytes)
    return json.loads(zlib.decompress(packed, -15))


# --- What an alert carries -------------------------------------------------------------------------------
# Moved here from apns.py in 2.0 (apns.py is now only the hub's APNs client): the payload a device gets, sealed.
# The plugin's alerts and the Claude Code host's both use these, so the two never word or size an alert differently.

PAYLOAD_LIMIT = 4096  # APNs' limit for a notification

# What Apple sees on a sealed alert. The extension replaces it, and names the bot itself even when it
# can't open one, so a failure shows "<Bot>: New message", never the reply.
REPLY_PLACEHOLDER = {"title": "Agents CS", "body": "New message"}
APPROVAL_PLACEHOLDER = {"title": "Approval needed", "body": "Open Agents CS to review the request."}
QUESTION_PLACEHOLDER = {"title": "Question", "body": "Open Agents CS to answer."}
TASK_PLACEHOLDER = {"title": "A task needs you", "body": "Open Agents CS to review it."}

PREVIEW_LIMIT = 240
_MARKS = re.compile(r"(\*\*|__|`+|^#+\s*|^>\s*)", re.MULTILINE)
# A native card travels in the reply as a fenced JSON block; a banner shows its title instead.
_CARD = re.compile(r"^ {0,3}```hermes-card[ \t]*\n(.*?)\n {0,3}```[ \t]*$", re.MULTILINE | re.DOTALL)


def _card_title(match):
    try:
        title = json.loads(match.group(1)).get("title")
    except (ValueError, AttributeError):
        return match.group(0)
    return "\n" + title + "\n" if isinstance(title, str) else ""


def preview(content) -> str:
    """First lines of the reply, Markdown marks stripped, cut to what a banner shows."""
    content = _CARD.sub(_card_title, content or "")
    text = " ".join(line.strip() for line in _MARKS.sub("", content).splitlines() if line.strip())
    if len(text) > PREVIEW_LIMIT:
        text = text[:PREVIEW_LIMIT - 1].rstrip() + "\u2026"
    return text or "Your reply is ready."


def with_full_text(payload: dict, text) -> dict:
    """Add the whole reply when it fits APNs' 4 KB, so a tapped alert opens the chat with the reply
    already in it. Measured with the widest JSON encoding (ASCII escapes, default separators)."""
    if text and text.strip():
        full = {**payload, "text": text}
        if len(json.dumps(full).encode()) <= PAYLOAD_LIMIT:
            return full
    return payload


def seal_payload(payload: dict, content: dict, *, device_id: str, device_key: str, sender, event_id: str,
                 placeholder: dict, log=None) -> dict:
    """The payload as ``device_id`` should get it: the placeholder alert plus the sealed content, with the full
    text inside when it still fits 4 KB. If sealing fails, the placeholder alone, never the text in the clear.
    ``device_key`` is the device's public key (base64url P-256 point); ``sender`` is a callable returning the
    computer's signing key (only called when needed)."""
    bare = {**{k: v for k, v in payload.items() if k != "text"}, "aps": {**payload["aps"], "alert": placeholder}}
    try:
        recipient, key = recipient_key(device_key), sender()
        if key is None:
            raise ValueError("no sender key")
        for candidate in (content, {k: v for k, v in content.items() if k != "text"}):
            envelope = seal(installation=device_id, event_id=event_id, content=candidate, recipient=recipient,
                            sender=key, issued=int(time.time()))
            if len(json.dumps({**bare, "sealed": envelope}).encode()) <= PAYLOAD_LIMIT:
                return {**bare, "sealed": envelope}
    except (ValueError, OSError) as error:
        (log or print)("sealing failed: %s" % type(error).__name__)
    return bare

