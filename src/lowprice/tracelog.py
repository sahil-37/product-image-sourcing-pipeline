"""Per-product debug trace: every stage from raw CSV name -> final decision.

Always writes work/traces/<id>.json (small, cheap, never skipped) so any run
can be inspected after the fact. --verbose additionally prints the same
stages to stdout as they happen.

Not used for pipeline control flow — purely observational. If tracing itself
throws, the pipeline must not fail because of it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any

from . import config

log = logging.getLogger("trace")

TRACES_DIR = config.WORK_DIR / "traces"


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of dataclasses/sets/other non-JSON types."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class Trace:
    """Accumulates one product's trace; write() persists it and (if verbose)
    prints a human-readable version to stdout."""

    def __init__(self, item_id: str, name: str, verbose: bool = False) -> None:
        self.item_id = item_id
        self.name = name
        self.verbose = verbose
        self.stages: dict[str, Any] = {"id": item_id, "name": name}
        if verbose:
            print(f"\n{'=' * 100}\n[{item_id}] {name}\n{'=' * 100}")

    def log_stage(self, stage: str, **fields: Any) -> None:
        self.stages[stage] = _jsonable(fields)
        if self.verbose:
            print(f"\n--- {stage} ---")
            for key, value in fields.items():
                _print_field(key, value)

    def write(self) -> None:
        try:
            TRACES_DIR.mkdir(parents=True, exist_ok=True)
            path = TRACES_DIR / f"{self.item_id}.json"
            path.write_text(json.dumps(self.stages, indent=2, sort_keys=False, default=str))
        except OSError as exc:  # tracing must never break the pipeline
            log.warning("failed to write trace for %r: %s", self.name, exc)


def _print_field(key: str, value: Any, indent: str = "  ") -> None:
    if isinstance(value, str) and len(value) > 500:
        print(f"{indent}{key}: {value[:500]}... [{len(value)} chars total]")
    elif isinstance(value, list):
        print(f"{indent}{key}: [{len(value)} items]")
        for i, item in enumerate(value):
            if isinstance(item, dict):
                summary = ", ".join(f"{k}={v}" for k, v in item.items() if k not in ("prompt",))
                print(f"{indent}  [{i}] {summary}")
            else:
                print(f"{indent}  [{i}] {item}")
    elif isinstance(value, dict):
        print(f"{indent}{key}:")
        for k, v in value.items():
            _print_field(k, v, indent + "  ")
    else:
        print(f"{indent}{key}: {value}")
