"""Alerts and read state for the Hermes Bots app: what used to be the separate push bridge process (``scripts/mobile-push``).

* ``routes.py``   ``/api/plugins/hermes-bots/mobile/{reads,replies,usage,kinds,view}`` (``hermes serve`` only)
* ``worker.py``   a Hermes background task in ``hermes serve`` (root home) that scans every bot's ``state.db`` and writes
                  sealed alerts to the outbox
* ``events.py``   approvals, questions, board tasks and badges, called in-process by the ``cards`` module from any
                  Hermes process (they write the outbox directly; no HTTP, no shared key)
* ``outbox.py``   ``<root home>/hermes-bots/delivery.db`` and its outbox, which the connector drains and sends via the hub
* ``sealed.py``   end-to-end sealed alert content (also used by the connector and the Claude Code host)
* ``devices.py``  the devices and the signing key, read from the connector's home (``devices.json``, ``sender-key.pem``)

Nothing heavy is imported here: ``register`` only offers the scan worker to Hermes's supervisor, and only in the
dashboard process of the root home.
"""


def register(ctx):
    """No tools or hooks. In ``hermes serve`` (root home) with an event loop running, start the scan worker as a Hermes
    background task; otherwise the dashboard router starts it when ``mobile/routes.py`` is mounted."""
    from . import worker
    worker.register(ctx)
