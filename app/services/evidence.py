"""Deterministic evidence serialization, hashing, storage, and attestation.

A record freezes one confirmed match: the bytes of the matched post image, the
post's own text, the page that carried it, and the settings the match was made
under. Its SHA-256 is what goes on chain.

Verification therefore proves that *this exact record existed at block time T*.
It does not prove the search is reproducible: results come from the live web and
change. That distinction is deliberate and is what keeps the claim honest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from config import Settings
from services.blockchain import (
    AttestationReceipt,
    BlockchainError,
    BlockchainService,
    normalize_digest,
)


logger = logging.getLogger("facechain.evidence")

SCHEMA = "facechain.post.v1"

# Keys carrying data that must never enter the hashed record or the API
# response. search.py attaches them; this module is what strips them.
PRIVATE_KEYS = ("_image_content",)


def canonicalize(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_digest(payload: dict[str, Any]) -> str:
    return "0x" + hashlib.sha256(canonicalize(payload)).hexdigest()


def strip_private(result: dict[str, Any]) -> bytes | None:
    """Remove and return the retained image bytes, if any."""
    content: bytes | None = None
    for key in PRIVATE_KEYS:
        value = result.pop(key, None)
        if key == "_image_content" and isinstance(value, (bytes, bytearray)):
            content = bytes(value)
    return content


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    """RFC3339 UTC at second precision, so the value is stable and readable."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def build_post_record(
    result: dict[str, Any],
    *,
    query_image_sha256: str,
    face_index: int,
    settings: Settings,
    model_digests: dict[str, str],
    chain: dict[str, Any],
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Freeze one confirmed match into the structure that gets hashed."""
    return {
        "schema": SCHEMA,
        "observed_at": observed_at or utc_now(),
        "post": {
            "page_url": result["page_url"],
            "page_title": result["page_title"],
            "og_title": result.get("og_title"),
            "og_description": result.get("og_description"),
            "platform": result["platform"],
            "source_kind": result["source_kind"],
            "image_url": result["image_url"],
            "image_sha256": result["image_sha256"],
            "image_bytes": result["image_bytes"],
        },
        "match": {
            "provider_match_type": result["provider_match_type"],
            "face_similarity": result["face_similarity"],
            "discovery_provider": result["discovery_provider"],
        },
        "query": {
            "image_sha256": query_image_sha256,
            "face_index": face_index,
        },
        "models": model_digests,
        "thresholds": {
            "face_match_threshold": settings.face_match_threshold,
            "candidate_face_detection_threshold": (
                settings.candidate_face_detection_threshold
            ),
        },
        "chain": chain,
    }


class EvidenceService:
    """Builds, anchors, and stores one record per confirmed match."""

    def __init__(
        self,
        settings: Settings,
        blockchain: BlockchainService,
        image_fetcher: Callable[[str], Awaitable[bytes | None]] | None = None,
    ) -> None:
        self.settings = settings
        self.blockchain = blockchain
        # Supplied by SearchService so re-fetching reuses its SSRF guards and
        # size limits rather than opening a second, laxer download path.
        self.image_fetcher = image_fetcher
        self._model_digests: dict[str, str] | None = None

    @property
    def model_digests(self) -> dict[str, str]:
        if self._model_digests is None:
            self._model_digests = {
                "yunet_sha256": file_digest(self.settings.yunet_model),
                "sface_sha256": file_digest(self.settings.sface_model),
            }
        return self._model_digests

    # -- storage -------------------------------------------------------------

    def record_path(self, record_digest: str) -> Path:
        return self.settings.evidence_dir / f"{record_digest}.json"

    def image_path(self, image_sha256: str) -> Path:
        return self.settings.evidence_dir / "images" / image_sha256

    def load_record(self, record_digest: str) -> dict[str, Any] | None:
        path = self.record_path(normalize_digest(record_digest))
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _store(
        self, record: dict[str, Any], proof: dict[str, Any], content: bytes | None
    ) -> None:
        """Write the record verbatim beside its proof, which is not hashed."""
        directory = self.settings.evidence_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = self.record_path(str(proof["record_digest"]))
        payload = {"record": record, "proof": proof}
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if content is not None and self.settings.evidence_store_images:
            image_path = self.image_path(record["post"]["image_sha256"])
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(content)

    # -- attestation ---------------------------------------------------------

    @staticmethod
    def _combine(record: AttestationReceipt, image: AttestationReceipt) -> str:
        states = {record.state, image.state}
        return "already_attested" if states == {"already_attested"} else "attested"

    async def _anchor(self, record: dict[str, Any]) -> dict[str, Any]:
        """Attest the record and the image bytes as two independent digests."""
        record_digest = sha256_digest(record)
        image_digest = normalize_digest(record["post"]["image_sha256"])

        record_receipt = await self.blockchain.attest(record_digest)
        image_receipt = await self.blockchain.attest(image_digest)
        return {
            "state": self._combine(record_receipt, image_receipt),
            "record_digest": record_digest,
            "image_digest": image_digest,
            "contract_address": record["chain"]["contract_address"],
            "chain_id": record["chain"]["chain_id"],
            "record": record_receipt.as_api_dict(),
            "image": image_receipt.as_api_dict(),
        }

    async def attest_results(
        self,
        payload: dict[str, Any],
        *,
        query_image: bytes,
        face_index: int,
    ) -> dict[str, Any]:
        """Anchor every confirmed result. Never raises: search must still return."""
        results: list[dict[str, Any]] = list(payload.get("results") or [])
        retained = {id(result): strip_private(result) for result in results}
        if not results:
            return payload

        if not self.settings.evidence_attest:
            for result in results:
                result["proof"] = {"state": "disabled"}
            return payload

        try:
            chain_status = await self.blockchain.status()
        except BlockchainError as exc:  # pragma: no cover - status never raises
            chain_status = {"state": "unavailable", "reason": str(exc)}

        if chain_status.get("state") != "ready":
            reason = chain_status.get("reason") or (
                f"The chain is {chain_status.get('state')}."
            )
            for result in results:
                result["proof"] = {"state": "unavailable", "reason": reason}
            payload["chain"] = chain_status
            return payload

        chain = {
            "chain_id": chain_status["chain_id"],
            "contract_address": chain_status["contract_address"],
            "deployed_at": chain_status.get("deployed_at"),
        }
        query_image_sha256 = hashlib.sha256(query_image).hexdigest()
        observed_at = utc_now()

        for result in results:
            record = build_post_record(
                result,
                query_image_sha256=query_image_sha256,
                face_index=face_index,
                settings=self.settings,
                model_digests=self.model_digests,
                chain=chain,
                observed_at=observed_at,
            )
            try:
                proof = await self._anchor(record)
            except BlockchainError as exc:
                logger.warning("Attestation failed for %s: %s", result["page_url"], exc)
                result["proof"] = {"state": "unavailable", "reason": str(exc)}
                continue
            try:
                await asyncio.to_thread(
                    self._store, record, proof, retained.get(id(result))
                )
                proof["record_file"] = self.record_path(
                    str(proof["record_digest"])
                ).name
            except OSError as exc:
                logger.warning("Could not store evidence record: %s", exc)
                proof["stored"] = False
            result["proof"] = proof

        payload["chain"] = chain_status
        return payload

    # -- verification --------------------------------------------------------

    @staticmethod
    def extract_record(bundle: dict[str, Any]) -> dict[str, Any]:
        """Accept either a stored bundle or a bare record."""
        record = bundle.get("record")
        return record if isinstance(record, dict) else bundle

    def _chain_reset_reason(
        self, record_chain: dict[str, Any], chain_status: dict[str, Any]
    ) -> str | None:
        """Detect a chain that cannot hold this record's proof."""
        if record_chain.get("chain_id") != chain_status.get("chain_id"):
            return (
                f"The record was written to chain {record_chain.get('chain_id')}; "
                f"this node reports chain {chain_status.get('chain_id')}."
            )
        expected_address = str(record_chain.get("contract_address") or "").lower()
        actual_address = str(chain_status.get("contract_address") or "").lower()
        if expected_address != actual_address:
            return (
                "The record names a different contract address than the one "
                "currently deployed."
            )
        expected_deploy = record_chain.get("deployed_at")
        actual_deploy = chain_status.get("deployed_at")
        if expected_deploy and actual_deploy and expected_deploy != actual_deploy:
            return (
                f"The contract was redeployed at {actual_deploy}; this record was "
                f"written against the deployment of {expected_deploy}. Attestations "
                "made before a local chain restart no longer exist."
            )
        return None

    async def verify_digest(self, digest: str) -> dict[str, Any]:
        """Look a digest up on chain without needing the record it came from."""
        chain_status = await self.blockchain.status()
        if chain_status.get("state") != "ready":
            return {
                "status": "chain_unavailable",
                "record_digest": normalize_digest(digest),
                "chain": chain_status,
                "reason": chain_status.get("reason")
                or f"The chain is {chain_status.get('state')}.",
            }
        try:
            record = await self.blockchain.verify(digest)
        except BlockchainError as exc:
            return {
                "status": "chain_unavailable",
                "record_digest": normalize_digest(digest),
                "chain": chain_status,
                "reason": str(exc),
            }
        return {
            "status": "verified" if record.exists else "not_attested",
            "record_digest": normalize_digest(digest),
            "chain": chain_status,
            "on_chain": {"record": record.as_api_dict()},
            "stored": self.load_record(normalize_digest(digest)) is not None,
        }

    async def verify_bundle(
        self, bundle: dict[str, Any], *, refetch: bool = False
    ) -> dict[str, Any]:
        """Re-derive a record's digest and check it against the chain."""
        record = self.extract_record(bundle)
        if not isinstance(record, dict) or "post" not in record:
            return {
                "status": "digest_mismatch",
                "reason": "The uploaded file is not a FaceChain evidence record.",
            }

        computed = sha256_digest(record)
        stated = (bundle.get("proof") or {}).get("record_digest")
        result: dict[str, Any] = {"record_digest": computed}

        if stated:
            try:
                stated_digest = normalize_digest(str(stated))
            except ValueError:
                stated_digest = None
            if stated_digest and stated_digest != computed:
                # The record no longer hashes to the digest its own proof claims.
                return {
                    **result,
                    "status": "digest_mismatch",
                    "claimed_digest": stated_digest,
                    "reason": (
                        "The record has been modified since it was attested: it no "
                        "longer hashes to the digest recorded in its proof."
                    ),
                }

        try:
            image_digest = normalize_digest(str(record["post"]["image_sha256"]))
        except (KeyError, TypeError, ValueError):
            return {
                **result,
                "status": "digest_mismatch",
                "reason": "The record carries no readable image fingerprint.",
            }
        result["image_digest"] = image_digest

        chain_status = await self.blockchain.status()
        result["chain"] = chain_status
        if chain_status.get("state") != "ready":
            return {
                **result,
                "status": "chain_unavailable",
                "reason": chain_status.get("reason")
                or f"The chain is {chain_status.get('state')}.",
            }

        reset = self._chain_reset_reason(record.get("chain") or {}, chain_status)
        if reset:
            return {**result, "status": "chain_reset", "reason": reset}

        try:
            record_receipt = await self.blockchain.verify(computed)
            image_receipt = await self.blockchain.verify(image_digest)
        except BlockchainError as exc:
            return {**result, "status": "chain_unavailable", "reason": str(exc)}

        result["on_chain"] = {
            "record": record_receipt.as_api_dict(),
            "image": image_receipt.as_api_dict(),
        }
        result["stored"] = self.load_record(computed) is not None

        if not record_receipt.exists:
            return {
                **result,
                "status": "not_attested",
                "reason": (
                    "This record is not on the chain. It was never attested, or it "
                    "was attested to a chain whose state no longer exists."
                ),
            }

        if refetch:
            live = await self._refetch(record)
            result["refetch"] = live
            if live.get("attempted") and live.get("matches") is False:
                return {
                    **result,
                    "status": "image_changed",
                    "reason": (
                        "The record itself is intact and on chain, but the image now "
                        "served at that URL has different bytes than the ones that "
                        "were attested."
                    ),
                }

        return {**result, "status": "verified"}

    async def _refetch(self, record: dict[str, Any]) -> dict[str, Any]:
        """Re-download the post image and compare it with the attested bytes."""
        url = record["post"].get("image_url")
        attested = str(record["post"].get("image_sha256") or "")
        if not self.image_fetcher or not url:
            return {
                "attempted": False,
                "reason": "Live re-fetch is not available in this context.",
            }
        try:
            content = await self.image_fetcher(url)
        except Exception as exc:  # noqa: BLE001 - a fetch failure is not a verdict
            logger.warning("Re-fetch failed for %s: %s", url, exc)
            return {"attempted": False, "reason": f"The image could not be fetched: {exc}"}
        if content is None:
            return {
                "attempted": False,
                "reason": (
                    "The image could not be fetched. The post may have been deleted "
                    "or made private."
                ),
            }
        digest = hashlib.sha256(content).hexdigest()
        return {
            "attempted": True,
            "image_url": url,
            "image_sha256": digest,
            "matches": digest == attested.lower(),
        }
