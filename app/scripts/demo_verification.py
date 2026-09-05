"""Demonstrate tamper-evident re-verification end to end.

    python -m scripts.demo_verification

Anchors a synthetic match, re-verifies it from the stored record, then mutates
that record and shows the proof failing. Uses no network provider: it exercises
the evidence and blockchain path only, so it runs against the local chain alone.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys

from config import settings
from services.blockchain import BlockchainService
from services.evidence import EvidenceService, sha256_digest


IMAGE = b"a-synthetic-post-image-for-the-demo"

MATCH = {
    "source_kind": "social_post",
    "platform": "Instagram",
    "page_title": "Provider supplied title",
    "page_url": "https://www.instagram.com/p/DEMO1/",
    "image_url": "https://cdn.example.com/demo.jpg",
    "image_sha256": hashlib.sha256(IMAGE).hexdigest(),
    "image_bytes": len(IMAGE),
    "provider_match_type": "full",
    "face_similarity": 0.5231,
    "thumbnail_data_url": "data:image/jpeg;base64,AAAA",
    "discovery_provider": "serpapi",
    "og_title": "Sunset at the beach",
    "og_description": "Posted from Goa",
}


def heading(step: str, title: str) -> None:
    print(f"\n{step}  {title}")
    print("-" * (len(step) + len(title) + 2))


def show(result: dict) -> None:
    print(f"  status         {str(result.get('status')).upper()}")
    if result.get("record_digest"):
        print(f"  record digest  {result['record_digest']}")
    if result.get("claimed_digest"):
        print(f"  claimed        {result['claimed_digest']}")
    on_chain = (result.get("on_chain") or {}).get("record") or {}
    if on_chain.get("exists"):
        print(f"  attested by    {on_chain['submitter']}")
        print(f"  block time     {on_chain['timestamp']}")
    if result.get("refetch", {}).get("attempted"):
        print(f"  live image     matches={result['refetch']['matches']}")
    if result.get("reason"):
        print(f"  reason         {result['reason']}")


async def main() -> int:
    blockchain = BlockchainService(settings)
    status = await blockchain.status()
    if status.get("state") != "ready":
        print(f"The chain is not ready: {json.dumps(status)}", file=sys.stderr)
        print(
            "Start it with: docker compose -f compose.yaml -f compose.blockchain.yaml up",
            file=sys.stderr,
        )
        return 2

    # The fetcher stands in for the live post, so the demo needs no network.
    async def fetch_original(url: str) -> bytes:
        return IMAGE

    async def fetch_replaced(url: str) -> bytes:
        return b"someone-swapped-this-image"

    service = EvidenceService(settings, blockchain, image_fetcher=fetch_original)

    heading("1/5", "Anchor the match on chain")
    payload = await service.attest_results(
        {"status": "matched", "results": [dict(MATCH)]},
        query_image=b"the-uploaded-photo",
        face_index=0,
    )
    proof = payload["results"][0]["proof"]
    print(f"  state          {proof['state']}")
    print(f"  record digest  {proof['record_digest']}")
    print(f"  image digest   {proof['image_digest']}")
    print(f"  record tx      {proof['record']['tx_hash']}")
    print(f"  image tx       {proof['image']['tx_hash']}")

    stored = service.load_record(proof["record_digest"])
    if stored is None:
        print("The record was not stored.", file=sys.stderr)
        return 2

    heading("2/5", "Re-verify the stored record")
    intact = await service.verify_bundle(copy.deepcopy(stored))
    show(intact)

    heading("3/5", "Re-verify with a live re-fetch of the post image")
    live = await service.verify_bundle(copy.deepcopy(stored), refetch=True)
    show(live)

    heading("4/5", "Tamper: change one word of the caption")
    tampered = copy.deepcopy(stored)
    tampered["record"]["post"]["og_description"] = "Posted from Delhi"
    print(f"  original       {sha256_digest(stored['record'])}")
    print(f"  tampered       {sha256_digest(tampered['record'])}")
    show(await service.verify_bundle(tampered))

    heading("5/5", "Tamper: the post now serves different image bytes")
    swapped = EvidenceService(settings, blockchain, image_fetcher=fetch_replaced)
    show(await swapped.verify_bundle(copy.deepcopy(stored), refetch=True))

    expected = ["verified", "verified", "digest_mismatch", "image_changed"]
    actual = [
        intact["status"],
        live["status"],
        (await service.verify_bundle(tampered))["status"],
        (await swapped.verify_bundle(copy.deepcopy(stored), refetch=True))["status"],
    ]
    print()
    if actual != expected:
        print(f"FAILED: expected {expected}, got {actual}", file=sys.stderr)
        return 1
    print("OK  intact record verifies; edited record and swapped image do not.")
    print(f"    Stored at data/evidence/{proof['record_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
