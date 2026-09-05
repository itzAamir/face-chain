"""Deterministic evidence serialization and hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonicalize(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_digest(payload: dict[str, Any]) -> str:
    return "0x" + hashlib.sha256(canonicalize(payload)).hexdigest()

