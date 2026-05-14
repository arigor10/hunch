"""Render replay events for nose mining.

Thin re-export of ``hunch.render``.  Mining callers use
``render_chunk`` (an alias for ``render_events``).
"""

from hunch.render import render_events as render_chunk  # noqa: F401

__all__ = ["render_chunk"]
