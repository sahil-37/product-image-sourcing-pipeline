"""Wholesale product-image automation.

Two deliberately separate phases:

* ``phase1`` sources and verifies a product image per catalogue row. Errors here
  are judgment errors — the wrong flavour or pack count attached to a product —
  and must be caught before anything goes live.
* ``phase2`` uploads approved images into the OMS. Errors here are mechanical
  and need independent retry.

They are not merged into one command on purpose: a human review gate sits
between them.
"""

__version__ = "0.1.0"
