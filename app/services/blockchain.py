"""Client for the deployed EvidenceRegistry contract over plain JSON-RPC.

The contract exposes two 32-byte operations, so the calldata is a four-byte
selector followed by one word. That is little enough ABI work to do directly,
which keeps a full web3 stack out of the image. Selectors are read from the
deployment artifact rather than derived, so no keccak implementation is needed
here.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from config import Settings


ATTEST = "attest(bytes32)"
VERIFY = "verify(bytes32)"
ALREADY_EXISTS = "EvidenceAlreadyExists(bytes32)"
EMPTY_DIGEST = "EmptyDigest()"

ZERO_ADDRESS = "0x" + "0" * 40


class BlockchainError(RuntimeError):
    """Base class for safe, user-facing blockchain failures."""


class BlockchainUnavailableError(BlockchainError):
    """The node or the deployment artifact could not be reached."""


class BlockchainCallError(BlockchainError):
    """The node accepted the request and rejected it."""

    def __init__(self, message: str, data: str | None = None) -> None:
        super().__init__(message)
        self.data = data


def load_deployment(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    target = Path(
        path or os.getenv("CONTRACT_DEPLOYMENT_FILE", "/deployment/contract.json")
    )
    return json.loads(target.read_text(encoding="utf-8"))


def normalize_digest(digest: str) -> str:
    """Return a lowercase 0x-prefixed 32-byte hex string."""
    value = digest[2:] if digest[:2].lower() == "0x" else digest
    if len(value) != 64:
        raise ValueError("A digest must be exactly 32 bytes.")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("A digest must be hexadecimal.") from exc
    return "0x" + value.lower()


def encode_digest_call(selector: str, digest: str) -> str:
    """Build calldata for a function taking a single bytes32 argument."""
    return selector + normalize_digest(digest)[2:]


@dataclass(frozen=True)
class EvidenceRecord:
    exists: bool
    submitter: str
    timestamp: int

    def as_api_dict(self) -> dict[str, object]:
        return {
            "exists": self.exists,
            "submitter": self.submitter if self.exists else None,
            "timestamp": self.timestamp if self.exists else None,
        }


def decode_evidence_record(result: str) -> EvidenceRecord:
    """Decode the (bool, address, uint64) tuple returned by verify()."""
    body = result[2:] if result[:2].lower() == "0x" else result
    if len(body) != 192:
        raise BlockchainCallError("The contract returned an unreadable response.")
    exists = int(body[0:64], 16) != 0
    submitter = "0x" + body[64:128][-40:]
    timestamp = int(body[128:192], 16)
    return EvidenceRecord(exists=exists, submitter=submitter, timestamp=timestamp)


@dataclass(frozen=True)
class AttestationReceipt:
    digest: str
    state: str  # attested | already_attested
    submitter: str
    timestamp: int
    tx_hash: str | None
    block_number: int | None

    def as_api_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "state": self.state,
            "submitter": self.submitter,
            "timestamp": self.timestamp,
            "tx_hash": self.tx_hash,
            "block_number": self.block_number,
        }


class BlockchainService:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._deployment: dict[str, Any] | None = None
        # A single account submits every attestation, so its nonce must advance
        # one transaction at a time.
        self._send_lock = asyncio.Lock()
        self._request_id = 0

    # -- deployment artifact -------------------------------------------------

    @property
    def deployment(self) -> dict[str, Any]:
        if self._deployment is None:
            try:
                self._deployment = load_deployment(
                    self.settings.contract_deployment_file
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise BlockchainUnavailableError(
                    "The contract deployment artifact is not available."
                ) from exc
        return self._deployment

    @property
    def configured(self) -> bool:
        try:
            self.deployment
        except BlockchainUnavailableError:
            return False
        return True

    def _selector(self, signature: str) -> str:
        selectors = self.deployment.get("selectors") or {}
        selector = selectors.get(signature)
        if not selector:
            raise BlockchainUnavailableError(
                f"The deployment artifact does not define {signature}. "
                "Redeploy the contract to regenerate it."
            )
        return selector

    def _error_selector(self, signature: str) -> str | None:
        return (self.deployment.get("errors") or {}).get(signature)

    # -- transport -----------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.blockchain_rpc_url,
            timeout=httpx.Timeout(self.settings.blockchain_timeout_seconds),
            transport=self._transport,
        )

    async def _rpc(self, client: httpx.AsyncClient, method: str, params: list) -> Any:
        self._request_id += 1
        try:
            response = await client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": method,
                    "params": params,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BlockchainUnavailableError(
                "The blockchain node could not be reached."
            ) from exc
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            data = error.get("data")
            if isinstance(data, dict):
                data = data.get("data")
            raise BlockchainCallError(
                str(error.get("message") or "The node rejected the request."),
                data if isinstance(data, str) else None,
            )
        return payload.get("result") if isinstance(payload, dict) else None

    async def _sender(self, client: httpx.AsyncClient) -> str:
        if self.settings.attester_address:
            return self.settings.attester_address
        accounts = await self._rpc(client, "eth_accounts", [])
        if not accounts:
            raise BlockchainUnavailableError(
                "The node exposes no unlocked account to submit attestations. "
                "Set ATTESTER_ADDRESS or use a node with unlocked accounts."
            )
        return accounts[0]

    # -- contract operations -------------------------------------------------

    async def _verify(self, client: httpx.AsyncClient, digest: str) -> EvidenceRecord:
        result = await self._rpc(
            client,
            "eth_call",
            [
                {
                    "to": self.deployment["address"],
                    "data": encode_digest_call(self._selector(VERIFY), digest),
                },
                "latest",
            ],
        )
        return decode_evidence_record(result or "")

    async def verify(self, digest: str) -> EvidenceRecord:
        """Look up a digest on chain. Never raises for an unknown digest."""
        async with self._client() as client:
            return await self._verify(client, digest)

    async def attest(self, digest: str) -> AttestationReceipt:
        """Record a digest, treating a prior record as success rather than failure."""
        normalized = normalize_digest(digest)
        async with self._send_lock:
            async with self._client() as client:
                existing = await self._verify(client, normalized)
                if existing.exists:
                    return AttestationReceipt(
                        digest=normalized,
                        state="already_attested",
                        submitter=existing.submitter,
                        timestamp=existing.timestamp,
                        tx_hash=None,
                        block_number=None,
                    )

                sender = await self._sender(client)
                try:
                    tx_hash = await self._rpc(
                        client,
                        "eth_sendTransaction",
                        [
                            {
                                "from": sender,
                                "to": self.deployment["address"],
                                "data": encode_digest_call(
                                    self._selector(ATTEST), normalized
                                ),
                            }
                        ],
                    )
                except BlockchainCallError as exc:
                    # Another submitter can win the race between the check above
                    # and this send; the contract, not this client, is the
                    # authority on whether the digest is already recorded.
                    already = self._error_selector(ALREADY_EXISTS)
                    if already and (exc.data or "").startswith(already):
                        record = await self._verify(client, normalized)
                        return AttestationReceipt(
                            digest=normalized,
                            state="already_attested",
                            submitter=record.submitter,
                            timestamp=record.timestamp,
                            tx_hash=None,
                            block_number=None,
                        )
                    empty = self._error_selector(EMPTY_DIGEST)
                    if empty and (exc.data or "").startswith(empty):
                        raise BlockchainCallError(
                            "The contract rejected an empty digest."
                        ) from exc
                    raise

                receipt = await self._await_receipt(client, tx_hash)
                record = await self._verify(client, normalized)
                return AttestationReceipt(
                    digest=normalized,
                    state="attested",
                    submitter=record.submitter or sender,
                    timestamp=record.timestamp,
                    tx_hash=tx_hash,
                    block_number=int(receipt["blockNumber"], 16),
                )

    async def _await_receipt(
        self, client: httpx.AsyncClient, tx_hash: str
    ) -> dict[str, Any]:
        for attempt in range(self.settings.blockchain_receipt_attempts):
            receipt = await self._rpc(client, "eth_getTransactionReceipt", [tx_hash])
            if receipt:
                if receipt.get("status") != "0x1":
                    raise BlockchainCallError(
                        f"Transaction {tx_hash} was mined but reverted."
                    )
                return receipt
            if attempt + 1 < self.settings.blockchain_receipt_attempts:
                await asyncio.sleep(self.settings.blockchain_receipt_interval_seconds)
        raise BlockchainUnavailableError(
            f"Transaction {tx_hash} was not mined in time."
        )

    # -- readiness -----------------------------------------------------------

    async def status(self) -> dict[str, object]:
        """Describe the chain connection without raising."""
        if not self.configured:
            return {"state": "not_configured"}
        deployment = self.deployment
        expected = deployment.get("chainId")
        try:
            async with self._client() as client:
                chain_id = int(await self._rpc(client, "eth_chainId", []), 16)
                attester = await self._sender(client)
        except BlockchainError as exc:
            return {
                "state": "unavailable",
                "contract_address": deployment.get("address"),
                "chain_id": expected,
                "reason": str(exc),
            }
        return {
            "state": "ready" if chain_id == expected else "chain_mismatch",
            "contract_address": deployment.get("address"),
            "chain_id": chain_id,
            "expected_chain_id": expected,
            "deployed_at": deployment.get("deployedAt"),
            "attester": attester,
        }
