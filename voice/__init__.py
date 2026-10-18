"""Live voice for the Hermes Bots app: media routes only (``routes.py``), served by ``hermes serve``.

There is nothing to register in a bot worker (no tools, no hooks), and nothing heavy is imported here:
``httpx``, ``websockets`` and ``pydantic`` load only when the dashboard imports ``routes``.
"""


def register(ctx):
    """No tools or hooks: this module exists for its dashboard routes."""
    return None
