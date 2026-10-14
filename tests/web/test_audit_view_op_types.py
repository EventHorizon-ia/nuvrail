"""
Drift guard for the web audit view's op-type lists (#178).

The web UI decides whether to show "Undo" from a hard-coded TS set. If that set
and the backend's UNDOABLE_OP_TYPES diverge, Undo silently disappears (or shows
for ops the backend refuses). Parsing the TS literal is cheaper than adding an
API field, and fails CI the next time either side changes alone.

    web/src/views/AuditView.tsx            gateway/undo.py
    const UNDOABLE_OP_TYPES = new Set([    UNDOABLE_OP_TYPES = frozenset({
      'move', ...                ──equal──    "move", ...
    ])                                     })
"""
from __future__ import annotations

import re
from pathlib import Path

from gateway.undo import UNDOABLE_OP_TYPES

AUDIT_VIEW = Path(__file__).resolve().parents[2] / "web" / "src" / "views" / "AuditView.tsx"


def _ts_string_list(source: str, pattern: str) -> set[str]:
    match = re.search(pattern, source, re.DOTALL)
    assert match, f"could not find {pattern!r} in {AUDIT_VIEW.name}"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def _source() -> str:
    return AUDIT_VIEW.read_text(encoding="utf-8")


def test_web_undoable_set_matches_backend() -> None:
    web = _ts_string_list(_source(), r"const UNDOABLE_OP_TYPES = new Set\(\[(.*?)\]\)")
    assert web == set(UNDOABLE_OP_TYPES)


def test_web_op_type_filter_covers_every_undoable_type() -> None:
    filters = _ts_string_list(_source(), r"const OP_TYPE_FILTERS = \[(.*?)\]")
    assert set(UNDOABLE_OP_TYPES) <= filters


def test_web_op_type_filter_has_no_phantom_types() -> None:
    # 'archive'/'star'/'unstar' were never emitted as op_types (#178).
    filters = _ts_string_list(_source(), r"const OP_TYPE_FILTERS = \[(.*?)\]")
    assert not filters & {"archive", "star", "unstar"}
