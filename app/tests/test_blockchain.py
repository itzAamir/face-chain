import asyncio
import json

import httpx
import pytest

from config import Settings
from services.blockchain import (
    ALREADY_EXISTS,
    ATTEST,
    EMPTY_DIGEST,
    VERIFY,
    BlockchainCallError,
    BlockchainService,
    BlockchainUnavailableError,
    decode_evidence_record,
    encode_digest_call,
    normalize_digest,
)


DIGEST = "0x" + "ab" * 32
ADDRESS = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
SUBMITTER = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

# The real selectors deploy.js writes into contract.json, pinned here so a
# contract signature change fails these tests instead of failing at runtime.
DEPLOYMENT = {
    "address": ADDRESS,
    "network": "localhost",
    "chainId": 31337,
    "deployedAt": "2026-09-05T10:00:00Z",
    "abi": [],
    "selectors": {ATTEST: "0x23c3617f", VERIFY: "0x75e36616"},
    "errors": {ALREADY_EXISTS: "0xcb7acfcd", EMPTY_DIGEST: "0x73ca6097"},
    "events": {},
}


def word(value: str) -> str:
    return value.rjust(64, "0")


def verify_result(exists: bool, submitter: str = SUBMITTER, timestamp: int = 0) -> str:
    return (
        "0x"
        + word("1" if exists else "0")
        + word(submitter[2:].lower())
        + word(format(timestamp, "x"))
    )


def make_service(handler, tmp_path, deployment=None) -> BlockchainService:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(deployment or DEPLOYMENT), encoding="utf-8")
    settings = Settings(contract_deployment_file=path)
    return BlockchainService(settings, transport=httpx.MockTransport(handler))


def rpc_ok(result) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def rpc_error(message: str, data: str | None = None) -> httpx.Response:
    error: dict = {"code": 3, "message": message}
    if data is not None:
        error["data"] = data
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": error})


# -- pure encoding/decoding ------------------------------------------------


def test_normalize_digest_accepts_both_prefixes() -> None:
    bare = "AB" * 32
    assert normalize_digest(bare) == DIGEST
    assert normalize_digest("0x" + bare) == DIGEST


@pytest.mark.parametrize("bad", ["0x", "0xab", "zz" * 32, "ab" * 31])
def test_normalize_digest_rejects_wrong_shape(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_digest(bad)


def test_encode_digest_call_is_selector_plus_one_word() -> None:
    data = encode_digest_call("0x23c3617f", DIGEST)
    assert data == "0x23c3617f" + "ab" * 32
    assert len(data) == 10 + 64


def test_decode_evidence_record_reads_the_tuple() -> None:
    record = decode_evidence_record(verify_result(True, SUBMITTER, 1_757_000_000))
    assert record.exists is True
    assert record.submitter == SUBMITTER
    assert record.timestamp == 1_757_000_000


def test_decode_evidence_record_reports_a_missing_digest() -> None:
    record = decode_evidence_record(verify_result(False, "0x" + "0" * 40, 0))
    assert record.exists is False
    assert record.as_api_dict() == {
        "exists": False,
        "submitter": None,
        "timestamp": None,
    }


@pytest.mark.parametrize("bad", ["0x", "0x" + "00" * 64])
def test_decode_evidence_record_rejects_a_short_response(bad: str) -> None:
    with pytest.raises(BlockchainCallError):
        decode_evidence_record(bad)


# -- verify ----------------------------------------------------------------


def test_verify_calls_the_contract_with_the_right_calldata(tmp_path) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return rpc_ok(verify_result(True, SUBMITTER, 42))

    service = make_service(handler, tmp_path)
    record = asyncio.run(service.verify(DIGEST))

    assert record.exists is True and record.timestamp == 42
    assert seen[0]["method"] == "eth_call"
    assert seen[0]["params"][0]["to"] == ADDRESS
    assert seen[0]["params"][0]["data"] == "0x75e36616" + "ab" * 32


def test_verify_reports_an_unreachable_node(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    service = make_service(handler, tmp_path)
    with pytest.raises(BlockchainUnavailableError):
        asyncio.run(service.verify(DIGEST))


# -- attest ----------------------------------------------------------------


def test_attest_sends_and_confirms(tmp_path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        calls.append(method)
        if method == "eth_call":
            # Missing before the send, present afterwards.
            sent = "eth_sendTransaction" in calls
            return rpc_ok(verify_result(sent, SUBMITTER, 1_757_000_000 if sent else 0))
        if method == "eth_accounts":
            return rpc_ok([SUBMITTER])
        if method == "eth_sendTransaction":
            assert body["params"][0]["data"] == "0x23c3617f" + "ab" * 32
            assert body["params"][0]["from"] == SUBMITTER
            return rpc_ok("0xdead")
        if method == "eth_getTransactionReceipt":
            return rpc_ok({"status": "0x1", "blockNumber": "0x2"})
        raise AssertionError(method)

    service = make_service(handler, tmp_path)
    receipt = asyncio.run(service.attest(DIGEST))

    assert receipt.state == "attested"
    assert receipt.tx_hash == "0xdead"
    assert receipt.block_number == 2
    assert receipt.timestamp == 1_757_000_000
    assert receipt.as_api_dict()["digest"] == DIGEST


def test_attest_is_idempotent_when_already_recorded(tmp_path) -> None:
    """A digest seen before is success, not failure: it already proves existence."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        calls.append(method)
        if method == "eth_call":
            return rpc_ok(verify_result(True, SUBMITTER, 1_700_000_000))
        raise AssertionError(f"unexpected {method}")

    service = make_service(handler, tmp_path)
    receipt = asyncio.run(service.attest(DIGEST))

    assert receipt.state == "already_attested"
    assert receipt.tx_hash is None
    assert receipt.timestamp == 1_700_000_000
    assert "eth_sendTransaction" not in calls


def test_attest_handles_a_duplicate_revert_race(tmp_path) -> None:
    """The contract, not the pre-check, is the authority on duplicates."""
    state = {"sent": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        if method == "eth_call":
            return rpc_ok(verify_result(state["sent"], SUBMITTER, 99 if state["sent"] else 0))
        if method == "eth_accounts":
            return rpc_ok([SUBMITTER])
        if method == "eth_sendTransaction":
            state["sent"] = True
            return rpc_error("execution reverted", "0xcb7acfcd" + "ab" * 32)
        raise AssertionError(method)

    service = make_service(handler, tmp_path)
    receipt = asyncio.run(service.attest(DIGEST))

    assert receipt.state == "already_attested"
    assert receipt.timestamp == 99


def test_attest_surfaces_an_empty_digest_revert(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "eth_call":
            return rpc_ok(verify_result(False, "0x" + "0" * 40, 0))
        if method == "eth_accounts":
            return rpc_ok([SUBMITTER])
        if method == "eth_sendTransaction":
            return rpc_error("execution reverted", "0x73ca6097")
        raise AssertionError(method)

    service = make_service(handler, tmp_path)
    with pytest.raises(BlockchainCallError, match="empty digest"):
        asyncio.run(service.attest("0x" + "00" * 32))


def test_attest_rejects_a_reverted_receipt(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "eth_call":
            return rpc_ok(verify_result(False, "0x" + "0" * 40, 0))
        if method == "eth_accounts":
            return rpc_ok([SUBMITTER])
        if method == "eth_sendTransaction":
            return rpc_ok("0xdead")
        if method == "eth_getTransactionReceipt":
            return rpc_ok({"status": "0x0", "blockNumber": "0x2"})
        raise AssertionError(method)

    service = make_service(handler, tmp_path)
    with pytest.raises(BlockchainCallError, match="reverted"):
        asyncio.run(service.attest(DIGEST))


def test_attest_gives_up_when_the_transaction_is_never_mined(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "eth_call":
            return rpc_ok(verify_result(False, "0x" + "0" * 40, 0))
        if method == "eth_accounts":
            return rpc_ok([SUBMITTER])
        if method == "eth_sendTransaction":
            return rpc_ok("0xdead")
        if method == "eth_getTransactionReceipt":
            return rpc_ok(None)
        raise AssertionError(method)

    path = tmp_path / "contract.json"
    path.write_text(json.dumps(DEPLOYMENT), encoding="utf-8")
    settings = Settings(
        contract_deployment_file=path,
        blockchain_receipt_attempts=2,
        blockchain_receipt_interval_seconds=0.0,
    )
    service = BlockchainService(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(BlockchainUnavailableError, match="not mined"):
        asyncio.run(service.attest(DIGEST))


def test_concurrent_attests_are_serialized(tmp_path) -> None:
    """One account submits every attestation, so sends must not interleave."""
    in_flight = 0
    overlaps = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, overlaps
        method = json.loads(request.content)["method"]
        if method == "eth_sendTransaction":
            in_flight += 1
            overlaps = max(overlaps, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return rpc_ok("0xdead")
        if method == "eth_call":
            return rpc_ok(verify_result(False, "0x" + "0" * 40, 0))
        if method == "eth_accounts":
            return rpc_ok([SUBMITTER])
        if method == "eth_getTransactionReceipt":
            return rpc_ok({"status": "0x1", "blockNumber": "0x2"})
        raise AssertionError(method)

    service = make_service(handler, tmp_path)

    async def run() -> None:
        digests = ["0x" + f"{n:02x}" * 32 for n in range(1, 5)]
        await asyncio.gather(*(service.attest(d) for d in digests))

    asyncio.run(run())
    assert overlaps == 1


# -- deployment artifact ---------------------------------------------------


def test_missing_deployment_is_reported_as_not_configured(tmp_path) -> None:
    settings = Settings(contract_deployment_file=tmp_path / "absent.json")
    service = BlockchainService(settings)
    assert service.configured is False
    assert asyncio.run(service.status()) == {"state": "not_configured"}


def test_stale_artifact_without_selectors_is_rejected(tmp_path) -> None:
    stale = {**DEPLOYMENT, "selectors": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made")

    service = make_service(handler, tmp_path, deployment=stale)
    with pytest.raises(BlockchainUnavailableError, match="Redeploy"):
        asyncio.run(service.verify(DIGEST))


def test_status_reports_a_ready_chain(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "eth_chainId":
            return rpc_ok("0x7a69")
        if method == "eth_accounts":
            return rpc_ok([SUBMITTER])
        raise AssertionError(method)

    service = make_service(handler, tmp_path)
    assert asyncio.run(service.status()) == {
        "state": "ready",
        "contract_address": ADDRESS,
        "chain_id": 31337,
        "expected_chain_id": 31337,
        "deployed_at": "2026-09-05T10:00:00Z",
        "attester": SUBMITTER,
    }


def test_status_flags_a_chain_the_contract_was_not_deployed_to(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "eth_chainId":
            return rpc_ok("0x1")
        if method == "eth_accounts":
            return rpc_ok([SUBMITTER])
        raise AssertionError(method)

    service = make_service(handler, tmp_path)
    status = asyncio.run(service.status())
    assert status["state"] == "chain_mismatch"
    assert status["chain_id"] == 1 and status["expected_chain_id"] == 31337


def test_status_reports_an_unreachable_node(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    service = make_service(handler, tmp_path)
    status = asyncio.run(service.status())
    assert status["state"] == "unavailable"
    assert status["contract_address"] == ADDRESS
