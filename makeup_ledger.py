#!/usr/bin/env python3
"""Standalone entry point for the makeup-difference ledger (补差账本).

The canonical implementation lives in :mod:`meter_allocator` so the original
settlement entry point and outputs stay unchanged.  This module loads
``meter_allocator.py`` next to it and re-exports the ledger API.

CLI examples::

    python3 -I /workspace/makeup_ledger.py \\
        --ledger-open /workspace/examples/ledger_opening.json \\
        --ledger-store /tmp/park_ledger.json

    python3 -I /workspace/makeup_ledger.py \\
        --ledger-receive /workspace/examples/ledger_batch_1.json \\
        --ledger-store /tmp/park_ledger.json

    python3 -I /workspace/makeup_ledger.py --ledger-view \\
        --ledger-store /tmp/park_ledger.json

The same flags work through ``meter_allocator.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_MODULE_PATH = _ROOT / "meter_allocator.py"


def _load_meter_allocator():
    if "meter_allocator" in sys.modules:
        return sys.modules["meter_allocator"]
    spec = importlib.util.spec_from_file_location("meter_allocator", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["meter_allocator"] = module
    spec.loader.exec_module(module)
    return module


MA = _load_meter_allocator()

# Re-export the whole public settlement/ledger surface and the ledger names.
for _name in dir(MA):
    if not _name.startswith("__"):
        globals()[_name] = getattr(MA, _name)

__all__ = [name for name in dir(MA) if not name.startswith("_")] + [
    "MA", "main",
]


def main(argv: list[str] | None = None) -> int:
    """Run the shared CLI (ledger and settlement flags are both available)."""
    return MA.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
