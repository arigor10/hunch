"""Post-critic filter: novelty + dedup.

Sits between the Critic and the journal. Drops hunches that are
duplicates of prior hunches or that were already raised in conversation.
Both checks use a fast LLM judge call.

See docs/framework_v0.md §4 for the design rationale.
"""

from hunch.filter.core import FilterResult, HunchFilter

__all__ = ["FilterResult", "HunchFilter"]
