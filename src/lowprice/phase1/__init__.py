"""Phase 1 — source and verify one product image per catalogue row.

Order matters: :mod:`normalize` builds the search query, :mod:`search` fetches
candidates, :mod:`process` filters and downloads them, :mod:`verify` asks the
vision model to choose, and :mod:`gate` re-checks that choice deterministically.
"""
