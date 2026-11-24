"""RFID tag authorisation from a JSON list (same format as the simulator's idtags.json)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TagList:
    tags: frozenset[str]
    accept_any: bool = False

    def is_valid(self, id_tag: str) -> bool:
        return self.accept_any or id_tag in self.tags


def load_tag_list(path: str | None, accept_any: bool) -> TagList:
    if not path:
        return TagList(frozenset(), accept_any=accept_any)
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read id tag file {path}: {exc}") from exc
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise SystemExit(f"id tag file {path} must be a JSON array of strings")
    log.info("loaded %d id tags from %s", len(raw), path)
    return TagList(frozenset(raw), accept_any=accept_any)
