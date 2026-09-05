"""Re-verify an evidence record against its on-chain attestation.

    python -m scripts.verify_onchain <record.json | digest> [--refetch]

Exit codes:
    0  the record is on chain and, with --refetch, the live image still matches
    1  the record could not be verified
    2  the input or the environment needs attention
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from config import settings
from services.blockchain import BlockchainService, normalize_digest
from services.evidence import EvidenceService
from services.face import FaceService
from services.search import SearchService


FAILED = {"digest_mismatch", "not_attested", "chain_reset", "image_changed"}


def build_service() -> EvidenceService:
    blockchain = BlockchainService(settings)
    search = SearchService(settings, FaceService(settings))
    return EvidenceService(settings, blockchain, image_fetcher=search.fetch_public_image)


def line(label: str, value: object) -> None:
    print(f"{label:<16} {value}")


def report(result: dict) -> None:
    status = str(result.get("status"))
    line("status", status.upper())
    line("record digest", result.get("record_digest", "-"))
    if result.get("claimed_digest"):
        line("claimed digest", result["claimed_digest"])
    if result.get("image_digest"):
        line("image digest", result["image_digest"])

    chain = result.get("chain") or {}
    if chain.get("contract_address"):
        line("contract", f"{chain['contract_address']} on chain {chain.get('chain_id')}")

    on_chain = (result.get("on_chain") or {}).get("record") or {}
    if on_chain.get("exists"):
        line("attested by", on_chain.get("submitter"))
        line("attested at", f"block timestamp {on_chain.get('timestamp')}")

    refetch = result.get("refetch")
    if refetch:
        if refetch.get("attempted"):
            line("live image", refetch.get("image_sha256"))
            line("live matches", refetch.get("matches"))
        else:
            line("live re-fetch", refetch.get("reason"))

    if result.get("reason"):
        print()
        print(result["reason"])


async def run(target: str, refetch: bool) -> int:
    service = build_service()
    path = Path(target)

    if path.is_file():
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not read {target}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(bundle, dict):
            print("An evidence record must be a JSON object.", file=sys.stderr)
            return 2
        result = await service.verify_bundle(bundle, refetch=refetch)
    else:
        try:
            digest = normalize_digest(target)
        except ValueError:
            print(
                f"{target} is neither a readable file nor a 32-byte digest.",
                file=sys.stderr,
            )
            return 2
        if refetch:
            stored = service.load_record(digest)
            if stored is None:
                print(
                    "--refetch needs the record; no stored copy exists for that digest.",
                    file=sys.stderr,
                )
                return 2
            result = await service.verify_bundle(stored, refetch=True)
        else:
            result = await service.verify_digest(digest)

    report(result)
    status = result.get("status")
    if status == "verified":
        return 0
    if status in FAILED:
        return 1
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-verify a FaceChain evidence record against the blockchain.",
    )
    parser.add_argument("target", help="Path to a record JSON file, or a 32-byte digest.")
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="Re-download the post image and compare it with the attested bytes.",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.target, args.refetch))


if __name__ == "__main__":
    raise SystemExit(main())
