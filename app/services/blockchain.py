"""Blockchain client placeholder for the deployed EvidenceRegistry contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_deployment() -> dict[str, Any]:
    path = Path(os.getenv("CONTRACT_DEPLOYMENT_FILE", "/deployment/contract.json"))
    return json.loads(path.read_text(encoding="utf-8"))

