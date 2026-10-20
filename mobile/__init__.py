"""Alerts and read state for the Hermes Bots app: what used to be the separate push bridge process (``scripts/mobile-push``).

* ``routes.py``   ``/api/plugins/hermes-bots/mobile/{reads,replies,usage,kinds,view}`` (``hermes serve`` only)
* ``scan.py``     turns new replies in every bot's ``state.db`` into sealed outbox rows; the connector runs it (``cycle``)
                  every 2 s and at once on a nudge, outside Hermes
* ``events.py``   approvals, questions, board tasks and badges, called in-process by the ``cards`` module from any
                  Hermes process (they write the outbox directly; no HTTP, no shared key)
* ``outbox.py``   ``<root home>/hermes-bots/delivery.db`` and its outbox, which the connector drains and sends via the hub
* ``sealed.py``   end-to-end sealed alert content (also used by the connector and the Claude Code host)
* ``devices.py``  the devices and the signing key, read from the connector's home (``devices.json``, ``sender-key.pem``)

Nothing is imported here: the connector imports these files on their own (as ``hermes_bots_shared``) to scan and to
drain the outbox.
"""


def register(ctx):
    """No tools, hooks or tasks: the routes mount from ``dashboard/plugin_api.py`` and the scan runs in the connector."""
