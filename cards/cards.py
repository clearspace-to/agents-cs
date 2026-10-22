# Portions adapted from bighelp-plugin (loopdy_plugin/generative_ui.py), Copyright the Loopdy /
# bighelp contributors, Apache License 2.0. See cards/NOTICE and ../LICENSE.
"""Agents CS native cards: the ``hermes.card`` v1 schema, strict validation and delivery.

A card is data, never code: a fixed type, bounded text and numbers, no URLs, HTML, styles or
actions. The bot calls a ``hermes_card_*`` tool; the plugin validates and canonicalises the
arguments into an envelope and carries it in the reply as a fenced block::

    ```hermes-card
    {"schema":"hermes.card","version":1,"type":"metrics",...}
    ```

The app finds these blocks in the reply text (live and in history, since the text is what Hermes
stores) and draws them natively; every envelope carries ``fallback`` text for places that can't.
A person's answer to a form, selection or checklist comes back as an ordinary chat message ending
in a ``hermes-card-response`` block with the card's id and the values.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, NoReturn

SCHEMA = "hermes.card"
VERSION = 1
FENCE = "hermes-card"
RESPONSE_FENCE = "hermes-card-response"
MAX_BYTES = 24_576
RESPONSE_MAX_BYTES = 8_192
FALLBACK_LIMIT = 1_500
FORM_LIFETIME = timedelta(days=7)
TYPES = ("summary", "metrics", "list", "checklist", "table", "timeline", "chart", "form", "selection", "message")
INTERACTIVE = {"form", "selection", "checklist", "message"}
# The answers a drafted message takes (the app also sends edited wording with "send").
MESSAGE_CHOICES = ("send", "dont_send")
TONES = {"neutral", "positive", "warning", "negative"}
# A decision offers at most three answers; the app adds Other for anything else (Mark, 2026-09-30).
MAX_OPTIONS = 3
TOO_MANY_OPTIONS = ("At most 3 options: the app adds Other for anything else. Keep the 3 that matter, "
                    "recommended first, and put the rest in the description or ask in a follow-up.")
FORBIDDEN = {
    "url", "uri", "href", "style", "styles", "css", "html", "javascript", "js", "route", "routes",
    "module", "native", "command", "shell", "rpc", "method", "endpoint", "headers", "token", "secret",
    "password", "script", "eval", "action", "actions",
}
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_FENCE_BLOCK = re.compile(r"^ {0,3}```" + re.escape(FENCE) + r"[ \t]*\n(.*?)\n {0,3}```[ \t]*$",
                          re.MULTILINE | re.DOTALL)


class CardError(ValueError):
    """A card or answer the schema rejects. ``code`` is stable; the message is safe to show a bot."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --- Envelopes ------------------------------------------------------------------------------------

def render(card_type: str, args: Any, *, now: datetime | None = None,
           request_id_factory: Callable[[], str] | None = None) -> dict[str, Any]:
    """Validate tool arguments (``title``, optional ``subtitle`` and the type's own fields) into a
    canonical envelope. Forms get a random request id; every other card is named by its content."""
    if card_type not in TYPES:
        _raise("unsupported_type", f"Unknown card type {card_type!r}")
    if not isinstance(args, dict):
        _raise("invalid_payload", "Card arguments must be an object")
    _size(args, MAX_BYTES)
    _global_check(args)
    fields = dict(args)
    title = _string(fields.pop("title", None), 1, 120, name="title")
    subtitle = fields.pop("subtitle", None)
    card: dict[str, Any] = {"schema": SCHEMA, "version": VERSION, "type": card_type, "title": title}
    if subtitle is not None:
        card["subtitle"] = _string(subtitle, 1, 240, name="subtitle")
    card["data"] = VALIDATORS[card_type](fields)
    if card_type == "form":
        request_id = (request_id_factory or (lambda: secrets.token_hex(16)))()
        if not isinstance(request_id, str) or not _HEX_32.fullmatch(request_id):
            _raise("internal_error", "Form request id generation failed")
        created = _utc(now or datetime.now(timezone.utc))
        card["id"] = request_id
        card["expires_at"] = _timestamp_text(created + FORM_LIFETIME)
    elif card_type == "message":
        # Each draft is its own decision, even when the bot drafts the same words twice.
        request_id = (request_id_factory or (lambda: secrets.token_hex(16)))()
        if not isinstance(request_id, str) or not _HEX_32.fullmatch(request_id):
            _raise("internal_error", "Message id generation failed")
        card["id"] = request_id
    else:
        card["id"] = hashlib.sha256(canonical_json(card).encode()).hexdigest()[:32]
    card["fallback"] = fallback_text(card)
    _size(card, MAX_BYTES)
    return card


def validate_envelope(value: Any) -> dict[str, Any]:
    """Check an envelope that crossed a boundary (a reply block, a fixture). Returns it canonical."""
    if isinstance(value, (str, bytes)):
        value = parse_json(value)
    if not isinstance(value, dict):
        _raise("invalid_payload", "Card must be an object")
    _size(value, MAX_BYTES)
    allowed = {"schema", "version", "type", "title", "subtitle", "data", "id", "fallback", "expires_at"}
    _object(value, allowed, {"schema", "version", "type", "title", "data", "id", "fallback"})
    if value["schema"] != SCHEMA or value["version"] != VERSION or isinstance(value["version"], bool):
        _raise("unsupported_version", "Unsupported card schema or version")
    if value["type"] not in TYPES:
        _raise("unsupported_type", "Unknown card type")
    _global_check({k: v for k, v in value.items() if k != "fallback"})
    card: dict[str, Any] = {"schema": SCHEMA, "version": VERSION, "type": value["type"],
                            "title": _string(value["title"], 1, 120, name="title")}
    if "subtitle" in value:
        card["subtitle"] = _string(value["subtitle"], 1, 240, name="subtitle")
    card["data"] = VALIDATORS[value["type"]](dict(value["data"]) if isinstance(value["data"], dict) else value["data"])
    identity = _string(value["id"], 32, 32, name="id")
    if not _HEX_32.fullmatch(identity):
        _raise("invalid_value", "Card id must be 32 lowercase hex characters")
    card["id"] = identity
    if value["type"] == "form":
        if "expires_at" not in value:
            _raise("missing_field", "A form needs expires_at")
        card["expires_at"] = _timestamp_text(_timestamp(value["expires_at"]))
    elif "expires_at" in value:
        _raise("unknown_field", "Only forms expire")
    card["fallback"] = _string(value["fallback"], 1, FALLBACK_LIMIT + 1, textarea=True, name="fallback")
    return card


def fence(card: dict[str, Any]) -> str:
    """The block that carries a card inside a reply."""
    return f"```{FENCE}\n{canonical_json(card)}\n```"


def cards_in(text: str) -> list[dict[str, Any]]:
    """Every valid card block in a reply, in order. Invalid blocks are skipped (the app shows them as text)."""
    found = []
    for match in _FENCE_BLOCK.finditer(text or ""):
        try:
            found.append(validate_envelope(match.group(1)))
        except CardError:
            continue
    return found


def replace_fences(text: str, replacement: Callable[[dict[str, Any] | None, str], str]) -> str:
    """Rewrite each card block, e.g. to its fallback text for a channel that can't draw cards."""
    def swap(match: re.Match) -> str:
        try:
            card = validate_envelope(match.group(1))
        except CardError:
            card = None
        return replacement(card, match.group(0))
    return _FENCE_BLOCK.sub(swap, text or "")


# What the person reads is the turn's final message. Text a bot writes before a tool call is commentary beside
# the call and the app does not show it (Show the Bot's Work), so a bot told only to "add one short sentence" after a
# card left its real answer in front of the call and the person got "Pick an option on the card." and nothing else:
# Mark's "Hi are you there" to Friday, 2026-10-18.
WHOLE_ANSWER = (
    "The person sees only your final message, never text written before a tool call: put your whole answer there, "
    "what they asked first, then at most one short sentence about the card."
)


def delivery(card: dict[str, Any], *, attached: bool) -> dict[str, Any]:
    """What the tool returns to the bot. ``attached``: this Hermes adds the card to the reply itself
    (``transform_llm_output`` runs before the reply is saved), so the bot must not copy it."""
    result: dict[str, Any] = {"card_id": card["id"], "type": card["type"]}
    if attached:
        result["instruction"] = (
            "The card is added below your reply in the Agents CS app automatically. Do not copy "
            "the card or repeat its contents. " + WHOLE_ANSWER
        )
    else:
        result["display_markdown"] = fence(card)
        result["instruction"] = (
            "Include display_markdown exactly once in your reply, unchanged, on its own lines. "
            "Don't repeat the card's contents. " + WHOLE_ANSWER
        )
    if card["type"] == "form":
        result["request_id"] = card["id"]
        result["instruction"] += (
            " End your turn after this reply. The person's answer arrives as their next message, "
            f"ending in a {RESPONSE_FENCE} block with card_id {card['id']} and the values; call "
            "hermes_card_form_response with this request_id for the checked values. "
            "The person may answer with partial fields plus _comment. Treat both as their answer, "
            "follow their instructions, and ask a new follow-up only if needed; do not invent missing values."
        )
    elif card["type"] == "message":
        result["instruction"] += (
            " Do NOT send, schedule or save this message anywhere yet. End your turn. The person's answer "
            f"arrives as their next message, ending in a {RESPONSE_FENCE} block with card_id {card['id']}: "
            "values.selected is [\"send\"] or [\"dont_send\"], values.other holds what they typed in Other, or "
            "values.skipped is true (deferred: preserve this draft and do not ask again on the next run; reopen only on an explicit request, material new facts or an agreed reminder). When values.other is "
            "set, those are the person's comments: do NOT send; rewrite the draft to follow them and show the new draft with a new "
            "hermes_card_message, then end your turn again. Edits attached to comments are context, not approval. "
            "Only values.selected=[\"send\"] with no values.other authorizes sending. When authorized and values.edited is true, send values.body (and "
            "values.subject) exactly as the person wrote them, not your draft. After sending, say in one "
            "line that it went, and to whom.")
    elif card["type"] in {"selection", "checklist"} and _answerable(card):
        result["instruction"] += (
            " The person's choice arrives as their next message, ending in a "
            f"{RESPONSE_FENCE} block with card_id {card['id']}."
        )
    return result


def _answerable(card: dict[str, Any]) -> bool:
    return card["type"] in {"selection", "message"} or "submit_label" in card["data"]


# --- Answers --------------------------------------------------------------------------------------

def parse_response_block(text: str) -> dict[str, Any] | None:
    """The ``hermes-card-response`` block at the end of a person's message, if any."""
    pattern = re.compile(r"```" + re.escape(RESPONSE_FENCE) + r"[ \t]*\n(.*?)\n```", re.DOTALL)
    match = pattern.search(text or "")
    if not match:
        return None
    try:
        value = parse_json(match.group(1))
    except CardError:
        return None
    return value if isinstance(value.get("card_id"), str) else None


def parse_response_blocks(text: str) -> list[dict[str, Any]]:
    """Every ``hermes-card-response`` block in a message: one message can answer a whole deck."""
    pattern = re.compile(r"```" + re.escape(RESPONSE_FENCE) + r"[ \t]*\n(.*?)\n```", re.DOTALL)
    found = []
    for match in pattern.finditer(text or ""):
        try:
            value = parse_json(match.group(1))
        except CardError:
            continue
        if isinstance(value, dict) and isinstance(value.get("card_id"), str):
            found.append(value)
    return found


def is_decision(card: dict[str, Any]) -> bool:
    """A card the app shows in its decision deck (the same rule as the app's ``Decision``): a single
    choice, or a form that is one menu plus optional text fields."""
    data = card.get("data") or {}
    if card.get("type") == "message":
        return True
    if card.get("type") == "selection":
        return data.get("mode") == "single"
    if card.get("type") != "form":
        return False
    fields = data.get("fields") or []
    menus = [f for f in fields if f.get("kind") == "select"]
    texts = [f for f in fields if f.get("kind") in {"text", "textarea"}]
    return len(menus) == 1 and len(menus) + len(texts) == len(fields) and not any(f.get("required") for f in texts)


def wants_answer(card: dict[str, Any]) -> bool:
    """A card the person answers in the app (the app's ``Decision`` rule): every form and selection, a
    drafted message, and a checklist with a submit button. A Kanban worker's are bound to its task,
    whatever their shape: nobody sees a worker's own session (2026-10-20)."""
    return card.get("type") in {"form", "selection", "message"} or (
        card.get("type") == "checklist" and "submit_label" in (card.get("data") or {}))


def validate_submission(form_data: dict[str, Any], values: Any) -> dict[str, Any]:
    """Check a person's form answer against the stored form. Returns the values canonical."""
    if not isinstance(values, dict):
        _raise("invalid_value", "Form values must be an object")
    _size(values, RESPONSE_MAX_BYTES)
    fields = {field["id"]: field for field in _form(form_data)["fields"]}
    comment = _string(values["_comment"], 1, 16_000, textarea=True, name="Comment").strip() if "_comment" in values else ""
    unknown = set(values) - set(fields) - {"_comment"}
    if unknown:
        _raise("invalid_value", "Unknown form field " + sorted(unknown)[0])
    result: dict[str, Any] = {"_comment": comment} if comment else {}
    for field_id in sorted(fields):
        field = fields[field_id]
        if field_id not in values or values[field_id] in (None, ""):
            if field["required"] and field["kind"] != "toggle" and not comment:
                _raise("invalid_value", f"{field['label']} is required")
            continue
        result[field_id] = _submitted_value(field, values[field_id])
    return result


# --- Fallback text --------------------------------------------------------------------------------

def fallback_text(card: dict[str, Any]) -> str:
    """Plain text standing in for the card where it can't be drawn: alerts, other channels, copy."""
    data, kind = card["data"], card["type"]
    lines = [card["title"]] + ([card["subtitle"]] if card.get("subtitle") else [])
    if kind == "summary":
        lines += ["", data["body"]]
    elif kind == "metrics":
        for item in data["items"]:
            detail = f" ({item['detail']})" if "detail" in item else ""
            lines.append(f"- {item['label']}: {item['value']}{detail}")
    elif kind == "list":
        for index, item in enumerate(data["items"], 1):
            head = f"{index}." if data.get("numbered") else "-"
            extra = "".join(f" — {item[k]}" for k in ("detail", "trailing") if k in item)
            lines.append(f"{head} {item['title']}{extra}")
    elif kind == "checklist":
        for item in data["items"]:
            lines.append(f"[{'x' if item['done'] else ' '}] {item['label']}")
    elif kind == "table":
        lines.append(" | ".join(column["label"] for column in data["columns"]))
        for row in data["rows"]:
            lines.append(" | ".join("" if cell is None else _plain_number(cell) for cell in row))
    elif kind == "timeline":
        marks = {"done": "✓", "current": "●", "upcoming": "○", "failed": "✗"}
        for step in data["steps"]:
            when = f" ({step['time']})" if "time" in step else ""
            lines.append(f"{marks[step['status']]} {step['title']}{when}")
    elif kind == "chart":
        for series in data["series"]:
            points = ", ".join(f"{p['x']}: {with_unit(p['y'], data.get('unit'))}" for p in series["points"])
            lines.append(f"{series['name']}: {points}")
    elif kind == "form":
        if "description" in data:
            lines.append(data["description"])
        lines += [f"- {field['label']}" for field in data["fields"]]
        lines.append("(A form: answer it in the Agents CS app.)")
    elif kind == "selection":
        if "description" in data:
            lines.append(data["description"])
        lines += [f"- {option['label']}" for option in data["options"]]
    elif kind == "message":
        lines.append("To: " + ", ".join(data["to"]))
        if data.get("cc"):
            lines.append("Cc: " + ", ".join(data["cc"]))
        if "subject" in data:
            lines.append("Subject: " + data["subject"])
        lines += ["", data["body"], "", "(A draft: approve it in the Agents CS app. Nothing is sent until then.)"]
    text = "\n".join(lines).strip()
    return text if len(text) <= FALLBACK_LIMIT else text[:FALLBACK_LIMIT - 1].rstrip() + "…"


PREFIX_UNITS = {"$", "€", "£", "¥", "₹", "C$", "US$", "A$"}


def with_unit(value: Any, unit: str | None) -> str:
    """A value as the card shows it: currency symbols before ("$1,240"), other units after ("12 kg", "98%")."""
    text = _plain_number(value)
    if not unit:
        return text
    if unit in PREFIX_UNITS:
        return ("-" + unit + text[1:]) if text.startswith("-") else unit + text
    return text + unit if unit == "%" else f"{text} {unit}"


def _plain_number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


# --- Per-type data --------------------------------------------------------------------------------

def _summary(value: Any) -> dict[str, Any]:
    _object(value, {"body", "tone", "badge"}, {"body"})
    result = {"body": _string(value["body"], 1, 2_000, textarea=True, name="body")}
    if "tone" in value:
        result["tone"] = _enum(value["tone"], TONES, "tone")
    if "badge" in value:
        result["badge"] = _string(value["badge"], 1, 40, name="badge")
    return result


def _metrics(value: Any) -> dict[str, Any]:
    _object(value, {"items"}, {"items"})
    items = []
    for raw in _array(value["items"], 1, 12, "items"):
        _object(raw, {"label", "value", "detail", "trend", "tone"}, {"label", "value"})
        item = {"label": _string(raw["label"], 1, 80, name="label"), "value": _display_value(raw["value"])}
        if "detail" in raw:
            item["detail"] = _string(raw["detail"], 1, 100, name="detail")
        if "trend" in raw:
            item["trend"] = _enum(raw["trend"], {"up", "down", "flat"}, "trend")
        if "tone" in raw:
            item["tone"] = _enum(raw["tone"], TONES, "tone")
        items.append(item)
    return {"items": items}


def _list(value: Any) -> dict[str, Any]:
    _object(value, {"items", "numbered"}, {"items"})
    items = []
    for raw in _array(value["items"], 1, 40, "items"):
        _object(raw, {"title", "detail", "trailing", "tone"}, {"title"})
        item = {"title": _string(raw["title"], 1, 160, name="title")}
        if "detail" in raw:
            item["detail"] = _string(raw["detail"], 1, 240, name="detail")
        if "trailing" in raw:
            item["trailing"] = _display_value(raw["trailing"])
        if "tone" in raw:
            item["tone"] = _enum(raw["tone"], TONES, "tone")
        items.append(item)
    result: dict[str, Any] = {"items": items}
    if "numbered" in value:
        result["numbered"] = _boolean(value["numbered"], "numbered")
    return result


def _checklist(value: Any) -> dict[str, Any]:
    _object(value, {"items", "submit_label"}, {"items"})
    items, ids = [], set()
    for raw in _array(value["items"], 1, 40, "items"):
        _object(raw, {"id", "label", "detail", "done"}, {"id", "label", "done"})
        item_id = _identifier(raw["id"])
        if item_id in ids:
            _raise("invalid_value", "Checklist item ids must be unique")
        ids.add(item_id)
        item = {"id": item_id, "label": _string(raw["label"], 1, 160, name="label"), "done": _boolean(raw["done"], "done")}
        if "detail" in raw:
            item["detail"] = _string(raw["detail"], 1, 240, name="detail")
        items.append(item)
    result: dict[str, Any] = {"items": items}
    if "submit_label" in value:
        result["submit_label"] = _string(value["submit_label"], 1, 40, name="submit_label")
    return result


def _table(value: Any) -> dict[str, Any]:
    _object(value, {"columns", "rows"}, {"columns", "rows"})
    columns = []
    for raw in _array(value["columns"], 1, 8, "columns"):
        _object(raw, {"label", "align"}, {"label"})
        column = {"label": _string(raw["label"], 1, 60, name="column label")}
        if "align" in raw:
            column["align"] = _enum(raw["align"], {"leading", "center", "trailing"}, "align")
        columns.append(column)
    rows = []
    for raw in _array(value["rows"], 1, 50, "rows"):
        cells = _array(raw, len(columns), len(columns), "row cells (one per column)")
        row = []
        for cell in cells:
            if cell is None:
                row.append(None)
            elif isinstance(cell, str):
                row.append(_string(cell, 0, 120, name="cell"))
            else:
                row.append(_number(cell, -1e12, 1e12))
        rows.append(row)
    return {"columns": columns, "rows": rows}


def _timeline(value: Any) -> dict[str, Any]:
    _object(value, {"steps"}, {"steps"})
    steps = []
    for raw in _array(value["steps"], 1, 20, "steps"):
        _object(raw, {"title", "detail", "time", "status"}, {"title", "status"})
        step = {"title": _string(raw["title"], 1, 160, name="title"),
                "status": _enum(raw["status"], {"done", "current", "upcoming", "failed"}, "status")}
        if "detail" in raw:
            step["detail"] = _string(raw["detail"], 1, 240, name="detail")
        if "time" in raw:
            step["time"] = _string(raw["time"], 1, 40, name="time")
        steps.append(step)
    return {"steps": steps}


def _chart(value: Any) -> dict[str, Any]:
    _object(value, {"kind", "series", "x_label", "y_label", "unit"}, {"kind", "series"})
    kind = _enum(value["kind"], {"bar", "line", "area", "pie"}, "kind")
    series_values = _array(value["series"], 1, 1 if kind == "pie" else 6, "series")
    series, names, total = [], set(), 0
    for raw in series_values:
        _object(raw, {"name", "points"}, {"name", "points"})
        name = _string(raw["name"], 1, 60, name="series name")
        if name in names:
            _raise("invalid_value", "Series names must be unique")
        names.add(name)
        points, xs = [], set()
        for point in _array(raw["points"], 1, 12 if kind == "pie" else 60, "points"):
            _object(point, {"x", "y"}, {"x", "y"})
            x = _string(point["x"] if not isinstance(point["x"], (int, float)) or isinstance(point["x"], bool)
                        else _plain_number(point["x"]), 1, 40, name="x")
            if x in xs:
                _raise("invalid_value", "Each x appears once per series")
            xs.add(x)
            y = _number(point["y"], 0 if kind == "pie" else -1e12, 1e12)
            points.append({"x": x, "y": y})
        total += len(points)
        series.append({"name": name, "points": points})
    if total > 240:
        _raise("limit_exceeded", "A chart holds at most 240 points")
    if kind == "pie" and sum(p["y"] for p in series[0]["points"]) <= 0:
        _raise("invalid_value", "A pie chart needs a positive total")
    result: dict[str, Any] = {"kind": kind, "series": series}
    for key, limit in (("x_label", 60), ("y_label", 60), ("unit", 20)):
        if key in value:
            result[key] = _string(value[key], 1, limit, name=key)
    return result


def _form(value: Any) -> dict[str, Any]:
    _object(value, {"description", "submit_label", "fields"}, {"submit_label", "fields"})
    kinds = {
        "text": {"default", "placeholder", "min_length", "max_length"},
        "textarea": {"default", "placeholder", "min_length", "max_length"},
        "select": {"options", "default"}, "multi_select": {"options", "max_selected", "default"},
        "toggle": {"default"}, "integer": {"min", "max", "step", "default"},
        "decimal": {"min", "max", "step", "default"}, "date": {"min", "max", "default"},
    }
    fields, ids = [], set()
    for field in _array(value["fields"], 1, 12, "fields"):
        kind = field.get("kind") if isinstance(field, dict) else None
        if kind not in kinds:
            _raise("invalid_value", "Field kind must be one of " + ", ".join(kinds))
        required = {"id", "kind", "label"} | ({"options"} if kind in {"select", "multi_select"} else set())
        _object(field, {"id", "kind", "label", "help_text", "required"} | kinds[kind], required)
        field_id = _identifier(field["id"])
        if field_id in ids:
            _raise("invalid_value", "Form field ids must be unique")
        ids.add(field_id)
        item: dict[str, Any] = {"id": field_id, "kind": kind, "label": _string(field["label"], 1, 100, name="label"),
                                "required": _boolean(field.get("required", False), "required")}
        if "help_text" in field:
            item["help_text"] = _string(field["help_text"], 1, 240, name="help_text")
        if kind in {"text", "textarea"}:
            maximum = 500 if kind == "text" else 2_000
            low = _integer(field.get("min_length", 0), 0, maximum, "min_length")
            high = _integer(field.get("max_length", maximum), 1, maximum, "max_length")
            if low > high:
                _raise("invalid_value", "min_length cannot exceed max_length")
            for key, number in (("min_length", low), ("max_length", high)):
                if key in field:
                    item[key] = number
            if "placeholder" in field:
                item["placeholder"] = _string(field["placeholder"], 1, 100, name="placeholder")
            if "default" in field:
                item["default"] = _string(field["default"], 0, high, textarea=kind == "textarea", name="default")
        elif kind in {"select", "multi_select"}:
            options, option_ids = [], set()
            if isinstance(field["options"], list) and len(field["options"]) > MAX_OPTIONS:
                _raise("limit_exceeded", TOO_MANY_OPTIONS)
            for option in _array(field["options"], 1, MAX_OPTIONS, "options"):
                _object(option, {"id", "label"}, {"id", "label"})
                option_id = _identifier(option["id"])
                if option_id in option_ids:
                    _raise("invalid_value", "Option ids must be unique")
                option_ids.add(option_id)
                options.append({"id": option_id, "label": _string(option["label"], 1, 80, name="option label")})
            item["options"] = options
            if kind == "multi_select":
                item["max_selected"] = _integer(field.get("max_selected", len(options)), 1, len(options), "max_selected")
            if "default" in field:
                default = field["default"]
                if kind == "select":
                    if default not in option_ids:
                        _raise("invalid_value", "A select default must be one of its options")
                    item["default"] = default
                else:
                    chosen = _array(default, 0, item["max_selected"], "default")
                    if len(set(chosen)) != len(chosen) or any(c not in option_ids for c in chosen):
                        _raise("invalid_value", "Multi-select defaults must be distinct options")
                    item["default"] = list(chosen)
        elif kind == "toggle":
            if "default" in field:
                item["default"] = _boolean(field["default"], "default")
        elif kind in {"integer", "decimal"}:
            convert = (lambda v, lo, hi: _integer(v, lo, hi, "number")) if kind == "integer" else _number
            limits = {key: convert(field[key], -1e12, 1e12) for key in ("min", "max") if key in field}
            if "min" in limits and "max" in limits and limits["min"] > limits["max"]:
                _raise("invalid_value", "min cannot exceed max")
            item.update(limits)
            if "step" in field:
                step = convert(field["step"], 1 if kind == "integer" else 0.000001, 1e9)
                item["step"] = step
            if "default" in field:
                default = convert(field["default"], -1e12, 1e12)
                if ("min" in limits and default < limits["min"]) or ("max" in limits and default > limits["max"]):
                    _raise("invalid_value", "The default is outside min and max")
                item["default"] = default
        elif kind == "date":
            parsed = {key: _date_text(field[key]) for key in ("min", "max", "default") if key in field}
            if "min" in parsed and "max" in parsed and parsed["min"] > parsed["max"]:
                _raise("invalid_value", "min cannot be after max")
            if "default" in parsed and (parsed["default"] < parsed.get("min", parsed["default"])
                                        or parsed["default"] > parsed.get("max", parsed["default"])):
                _raise("invalid_value", "The default date is outside min and max")
            item.update(parsed)
        fields.append(item)
    result = {"submit_label": _string(value["submit_label"], 1, 40, name="submit_label"), "fields": fields}
    if "description" in value:
        result["description"] = _string(value["description"], 1, 300, name="description")
    return result


def _selection(value: Any) -> dict[str, Any]:
    _object(value, {"description", "mode", "options", "max_selected", "submit_label"}, {"mode", "options"})
    mode = _enum(value["mode"], {"single", "multiple"}, "mode")
    options, ids = [], set()
    if isinstance(value["options"], list) and len(value["options"]) > MAX_OPTIONS:
        _raise("limit_exceeded", TOO_MANY_OPTIONS)
    for raw in _array(value["options"], 1, MAX_OPTIONS, "options"):
        _object(raw, {"id", "label", "detail", "recommended"}, {"id", "label"})
        option_id = _identifier(raw["id"])
        if option_id in ids:
            _raise("invalid_value", "Option ids must be unique")
        ids.add(option_id)
        option = {"id": option_id, "label": _string(raw["label"], 1, 120, name="label")}
        if "detail" in raw:
            option["detail"] = _string(raw["detail"], 1, 240, name="detail")
        if raw.get("recommended") is True:
            if any(o.get("recommended") for o in options):
                _raise("invalid_value", "Recommend at most one option")
            option["recommended"] = True
        elif "recommended" in raw and raw["recommended"] is not False:
            _raise("invalid_value", "recommended must be true or false")
        options.append(option)
    result: dict[str, Any] = {"mode": mode, "options": options}
    if mode == "multiple":
        result["max_selected"] = _integer(value.get("max_selected", len(options)), 1, len(options), "max_selected")
        result["submit_label"] = _string(value.get("submit_label", "Send"), 1, 40, name="submit_label")
    elif "max_selected" in value or "submit_label" in value:
        _raise("invalid_value", "A single choice sends on tap: no max_selected or submit_label")
    if "description" in value:
        result["description"] = _string(value["description"], 1, 300, name="description")
    return result


def _message(value: Any) -> dict[str, Any]:
    _object(value, {"channel", "to", "cc", "subject", "body", "from", "thread", "attachments", "description"},
            {"to", "body"})
    channel = _string(value.get("channel", "message"), 1, 80, name="channel").strip().lower()
    if not channel:
        _raise("invalid_string", "channel must not be blank")
    result: dict[str, Any] = {"channel": channel,
                              "to": [_string(r, 1, 200, name="to") for r in _array(value["to"], 1, 20, "to")]}
    if "cc" in value:
        result["cc"] = [_string(r, 1, 200, name="cc") for r in _array(value["cc"], 1, 20, "cc")]
    if "subject" in value:
        if channel != "email":
            _raise("invalid_value", "Only an email has a subject; put conversation context in thread")
        result["subject"] = _string(value["subject"], 1, 200, name="subject")
    result["body"] = _string(value["body"], 1, 6_000, textarea=True, name="body")
    for key, limit in (("from", 200), ("thread", 200), ("description", 300)):
        if key in value:
            result[key] = _string(value[key], 1, limit, name=key)
    if "attachments" in value:
        result["attachments"] = [_string(a, 1, 200, name="attachment") for a in _array(value["attachments"], 1, 10, "attachments")]
    return result


VALIDATORS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "summary": _summary, "metrics": _metrics, "list": _list, "checklist": _checklist, "table": _table,
    "timeline": _timeline, "chart": _chart, "form": _form, "selection": _selection, "message": _message,
}


def _submitted_value(field: dict[str, Any], value: Any) -> Any:
    kind = field["kind"]
    if kind in {"text", "textarea"}:
        return _string(value, field.get("min_length", 0), field.get("max_length", 500 if kind == "text" else 2_000),
                       textarea=kind == "textarea", name=field["label"])
    options = {option["id"] for option in field.get("options", [])}
    if kind == "select":
        if value not in options:
            _raise("invalid_value", f"{field['label']}: choose one of the options")
        return value
    if kind == "multi_select":
        chosen = _array(value, 0, field["max_selected"], field["label"])
        if len(set(chosen)) != len(chosen) or any(item not in options for item in chosen):
            _raise("invalid_value", f"{field['label']}: choose from the options")
        return list(chosen)
    if kind == "toggle":
        return _boolean(value, field["label"])
    if kind in {"integer", "decimal"}:
        number = (_integer(value, -1_000_000_000_000, 1_000_000_000_000, field["label"])
                  if kind == "integer" else _number(value, -1e12, 1e12))
        if ("min" in field and number < field["min"]) or ("max" in field and number > field["max"]):
            _raise("invalid_value", f"{field['label']} is out of range")
        if "step" in field:
            base = field.get("min", 0)
            quotient = (Decimal(str(number)) - Decimal(str(base))) / Decimal(str(field["step"]))
            if quotient != quotient.to_integral_value():
                _raise("invalid_value", f"{field['label']} doesn't match the step")
        return number
    if kind == "date":
        day = _date_text(value)
        if ("min" in field and day < field["min"]) or ("max" in field and day > field["max"]):
            _raise("invalid_value", f"{field['label']} is out of range")
        return day
    _raise("invalid_value", "Unsupported field kind")


# --- Strict JSON helpers (see cards/NOTICE) ------------------------------------------------------

def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise CardError("invalid_payload", "Card is not plain JSON") from error


def parse_json(raw: str | bytes) -> dict[str, Any]:
    def pairs(values):
        result = {}
        for key, item in values:
            if key in result:
                _raise("duplicate_key", "Duplicate JSON keys are not allowed")
            result[key] = item
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _v: _raise("invalid_number", "Non-finite numbers are not allowed"))
    except CardError:
        raise
    except (TypeError, ValueError) as error:
        raise CardError("invalid_json", "Card must be a JSON object") from error
    if not isinstance(value, dict):
        _raise("invalid_payload", "Card must be a JSON object")
    return value


def _global_check(value: Any, depth: int = 0) -> None:
    if depth > 6:
        _raise("limit_exceeded", "Card nesting is too deep")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _raise("invalid_payload", "Keys must be strings")
            if key.lower() in FORBIDDEN:
                _raise("forbidden_field", f"Field {key!r} is not allowed in a card")
            _global_check(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _global_check(item, depth)
    elif isinstance(value, float) and not math.isfinite(value):
        _raise("invalid_number", "Numbers must be finite")
    elif not (value is None or isinstance(value, (str, bool, int, float))):
        _raise("invalid_payload", "Card holds a non-JSON value")


def _object(value: Any, allowed: set[str], required: set[str]) -> None:
    if not isinstance(value, dict):
        _raise("invalid_value", "Expected an object")
    unknown = set(value) - allowed
    if unknown:
        _raise("unknown_field", "Unknown field " + sorted(unknown)[0] + "; allowed: " + ", ".join(sorted(allowed)))
    missing = required - set(value)
    if missing:
        _raise("missing_field", "Missing field " + sorted(missing)[0])


def _array(value: Any, minimum: int, maximum: int, name: str = "list") -> list[Any]:
    if not isinstance(value, list):
        _raise("invalid_value", f"{name} must be a list")
    if not minimum <= len(value) <= maximum:
        _raise("limit_exceeded", f"{name} holds {minimum}–{maximum} entries")
    return value


def _string(value: Any, minimum: int, maximum: int, *, textarea: bool = False, name: str = "text") -> str:
    if not isinstance(value, str):
        _raise("invalid_string", f"{name} must be text")
    normalized = unicodedata.normalize("NFC", value)
    if not minimum <= len(normalized) <= maximum:
        _raise("invalid_string", f"{name} must be {minimum}–{maximum} characters")
    for character in normalized:
        if ord(character) < 32 and not (textarea and character in "\n\t"):
            _raise("invalid_string", f"{name} has control characters")
    return normalized


def _display_value(value: Any) -> str:
    if isinstance(value, bool):
        _raise("invalid_value", "A value must be text or a number")
    if isinstance(value, (int, float)):
        return _plain_number(_number(value, -1e12, 1e12))
    return _string(value, 1, 60, name="value")


def _number(value: Any, minimum: float, maximum: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _raise("invalid_number", "Expected a finite number")
    if not minimum <= value <= maximum:
        _raise("invalid_number", "Number is out of range")
    try:
        decimal = Decimal(str(value)).normalize()
    except InvalidOperation:
        _raise("invalid_number", "Number is invalid")
    if decimal.as_tuple().exponent < -6:
        _raise("invalid_number", "At most six decimal places")
    return int(value) if decimal == decimal.to_integral_value() else value


def _integer(value: Any, minimum: float, maximum: float, name: str = "number") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _raise("invalid_value", f"{name} must be a whole number in range")
    return value


def _boolean(value: Any, name: str = "value") -> bool:
    if not isinstance(value, bool):
        _raise("invalid_value", f"{name} must be true or false")
    return value


def _enum(value: Any, choices: set[str], name: str = "value") -> str:
    if not isinstance(value, str) or value not in choices:
        _raise("invalid_value", f"{name} must be one of " + ", ".join(sorted(choices)))
    return value


def _identifier(value: Any) -> str:
    text = _string(value, 1, 40, name="id")
    if not _ID.fullmatch(text):
        _raise("invalid_value", "ids are lowercase letters, digits, - and _, starting with a letter")
    return text


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        _raise("invalid_value", "Expected an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _raise("invalid_value", "Expected an RFC 3339 timestamp")
    if parsed.tzinfo is None:
        _raise("invalid_value", "Timestamp needs a time zone")
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        _raise("invalid_value", "Clock must include a time zone")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _timestamp_text(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _date_text(value: Any) -> str:
    if not isinstance(value, str):
        _raise("invalid_value", "Expected a date like 2026-09-28")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        _raise("invalid_value", "Expected a date like 2026-09-28")


def _size(value: Any, maximum: int) -> None:
    if len(canonical_json(value).encode()) > maximum:
        _raise("payload_too_large", f"Card is larger than {maximum // 1024} KB")


def _raise(code: str, message: str) -> NoReturn:
    raise CardError(code, message)
