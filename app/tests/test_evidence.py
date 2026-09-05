import asyncio
import json

import pytest

from config import Settings
from services.blockchain import AttestationReceipt, BlockchainUnavailableError
from services.evidence import (
    SCHEMA,
    EvidenceService,
    build_post_record,
    canonicalize,
    sha256_digest,
    strip_private,
    utc_now,
)


IMAGE_SHA = "ab" * 32
SUBMITTER = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

RESULT = {
    "source_kind": "social_post",
    "platform": "Instagram",
    "page_title": "Provider title",
    "page_url": "https://www.instagram.com/p/ABC123/",
    "image_url": "https://cdn.example.com/post.jpg",
    "image_sha256": IMAGE_SHA,
    "image_bytes": 2048,
    "provider_match_type": "full",
    "face_similarity": 0.5123,
    "thumbnail_data_url": "data:image/jpeg;base64,AAAA",
    "discovery_provider": "serpapi",
    "og_title": "A caption",
    "og_description": "Shot in Goa",
}

CHAIN = {"chain_id": 31337, "contract_address": "0x5FbDB2315678afecb367f032d93F642f64180aa3"}
MODELS = {"yunet_sha256": "aa" * 32, "sface_sha256": "bb" * 32}

# Recompute deliberately if the schema changes; never auto-update it.
GOLDEN_DIGEST = "0xfc76e213f1be50749955867c496e51e181e1026540d5f36d76cd8d095baa4bf7"


def make_record(**overrides):
    result = {**RESULT, **overrides.pop("result", {})}
    return build_post_record(
        result,
        query_image_sha256="cc" * 32,
        face_index=0,
        settings=overrides.pop("settings", Settings()),
        model_digests=MODELS,
        chain=CHAIN,
        observed_at="2026-09-05T10:00:00Z",
        **overrides,
    )


# -- canonicalization ------------------------------------------------------


def test_canonicalize_is_key_order_independent() -> None:
    assert canonicalize({"a": 1, "b": 2}) == canonicalize({"b": 2, "a": 1})


def test_canonicalize_keeps_unicode_and_drops_whitespace() -> None:
    assert canonicalize({"t": "café"}) == '{"t":"café"}'.encode("utf-8")


def test_digest_is_a_32_byte_hex_string() -> None:
    digest = sha256_digest({"a": 1})
    assert digest.startswith("0x") and len(digest) == 66


# -- record schema ---------------------------------------------------------


def test_record_has_a_fixed_golden_digest() -> None:
    """Pinned so a schema change is a deliberate act, never an accident."""
    record = make_record()
    assert record["schema"] == SCHEMA
    assert sha256_digest(record) == GOLDEN_DIGEST


def test_record_excludes_the_thumbnail() -> None:
    """JPEG encoding is not byte-stable, so it must never enter the hash."""
    record = make_record()
    assert "thumbnail_data_url" not in json.dumps(record)


def test_record_carries_the_post_fingerprint_and_text() -> None:
    post = make_record()["post"]
    assert post["image_sha256"] == IMAGE_SHA
    assert post["image_url"] == "https://cdn.example.com/post.jpg"
    assert post["og_title"] == "A caption"
    assert post["og_description"] == "Shot in Goa"


def test_record_digest_is_stable_across_builds() -> None:
    assert sha256_digest(make_record()) == sha256_digest(make_record())


def test_record_digest_changes_when_the_caption_changes() -> None:
    """Editing the post's text must break the proof."""
    original = sha256_digest(make_record())
    edited = sha256_digest(make_record(result={"og_description": "Shot in Delhi"}))
    assert original != edited


def test_record_digest_changes_when_the_image_changes() -> None:
    original = sha256_digest(make_record())
    swapped = sha256_digest(make_record(result={"image_sha256": "cd" * 32}))
    assert original != swapped


def test_record_digest_changes_when_the_threshold_changes() -> None:
    """A match made under looser settings is not the same claim."""
    original = sha256_digest(make_record())
    loose = sha256_digest(make_record(settings=Settings(face_match_threshold=0.2)))
    assert original != loose


def test_missing_post_text_is_recorded_as_null() -> None:
    record = make_record(result={"og_title": None, "og_description": None})
    assert record["post"]["og_title"] is None
    assert sha256_digest(record) != sha256_digest(make_record())


def test_utc_now_is_second_precision_zulu() -> None:
    value = utc_now()
    assert value.endswith("Z") and "." not in value


# -- private key handling --------------------------------------------------


def test_strip_private_removes_retained_bytes() -> None:
    result = {**RESULT, "_image_content": b"raw"}
    assert strip_private(result) == b"raw"
    assert "_image_content" not in result


def test_strip_private_is_a_noop_without_bytes() -> None:
    result = dict(RESULT)
    assert strip_private(result) is None


# -- attestation orchestration ---------------------------------------------


class _FakeChain:
    def __init__(self, state="ready", attest_state="attested", fail=False):
        self._state = state
        self._attest_state = attest_state
        self._fail = fail
        self.attested: list[str] = []

    async def status(self):
        if self._state != "ready":
            return {"state": self._state, "reason": "node down",
                    "contract_address": CHAIN["contract_address"],
                    "chain_id": CHAIN["chain_id"]}
        return {"state": "ready", **CHAIN, "expected_chain_id": 31337,
                "attester": SUBMITTER}

    async def attest(self, digest):
        if self._fail:
            raise BlockchainUnavailableError("node down")
        self.attested.append(digest)
        return AttestationReceipt(
            digest=digest, state=self._attest_state, submitter=SUBMITTER,
            timestamp=1_757_000_000, tx_hash="0xdead", block_number=2,
        )


def service(tmp_path, chain, **kwargs) -> EvidenceService:
    settings = Settings(evidence_dir=tmp_path, evidence_attest=True, **kwargs)
    return EvidenceService(settings, chain)


def run(svc, results, query=b"upload"):
    payload = {"status": "matched", "results": [dict(r) for r in results]}
    return asyncio.run(svc.attest_results(payload, query_image=query, face_index=0))


def test_each_result_anchors_two_digests(tmp_path) -> None:
    """The record and the raw image bytes are independently verifiable."""
    chain = _FakeChain()
    payload = run(service(tmp_path, chain), [RESULT])

    proof = payload["results"][0]["proof"]
    assert proof["state"] == "attested"
    assert proof["image_digest"] == "0x" + IMAGE_SHA
    assert proof["record_digest"] != proof["image_digest"]
    assert chain.attested == [proof["record_digest"], proof["image_digest"]]


def test_record_is_written_next_to_its_proof(tmp_path) -> None:
    chain = _FakeChain()
    payload = run(service(tmp_path, chain), [RESULT])
    proof = payload["results"][0]["proof"]

    stored = json.loads((tmp_path / f"{proof['record_digest']}.json").read_text())
    # The record is stored verbatim; the proof sits beside it and is not hashed.
    assert sha256_digest(stored["record"]) == proof["record_digest"]
    assert stored["record"]["post"]["image_sha256"] == IMAGE_SHA
    assert stored["proof"]["record"]["tx_hash"] == "0xdead"
    assert proof["record_file"] == f"{proof['record_digest']}.json"


def test_stored_record_rehashes_to_its_filename(tmp_path) -> None:
    """The file name is the claim; re-canonicalizing must reproduce it."""
    chain = _FakeChain()
    payload = run(service(tmp_path, chain), [RESULT])
    digest = payload["results"][0]["proof"]["record_digest"]

    loaded = EvidenceService(
        Settings(evidence_dir=tmp_path), chain
    ).load_record(digest)
    assert sha256_digest(loaded["record"]) == digest


def test_already_attested_results_report_that_state(tmp_path) -> None:
    chain = _FakeChain(attest_state="already_attested")
    payload = run(service(tmp_path, chain), [RESULT])
    assert payload["results"][0]["proof"]["state"] == "already_attested"


def test_search_still_returns_when_the_chain_is_down(tmp_path) -> None:
    """Attestation is additive: a dead node must not lose the search result."""
    payload = run(service(tmp_path, _FakeChain(state="unavailable")), [RESULT])
    proof = payload["results"][0]["proof"]
    assert payload["status"] == "matched"
    assert proof["state"] == "unavailable" and proof["reason"] == "node down"


def test_a_failing_attest_degrades_only_that_result(tmp_path) -> None:
    payload = run(service(tmp_path, _FakeChain(fail=True)), [RESULT])
    assert payload["results"][0]["proof"]["state"] == "unavailable"
    assert list(tmp_path.iterdir()) == []


def test_attestation_can_be_switched_off(tmp_path) -> None:
    settings = Settings(evidence_dir=tmp_path, evidence_attest=False)
    svc = EvidenceService(settings, _FakeChain())
    payload = run(svc, [RESULT])
    assert payload["results"][0]["proof"] == {"state": "disabled"}
    assert list(tmp_path.iterdir()) == []


def test_retained_bytes_never_reach_the_response(tmp_path) -> None:
    chain = _FakeChain()
    payload = run(service(tmp_path, chain), [{**RESULT, "_image_content": b"raw"}])
    assert "_image_content" not in payload["results"][0]
    assert "_image_content" not in json.dumps(payload["results"][0])


def test_bytes_are_stored_only_when_enabled(tmp_path) -> None:
    chain = _FakeChain()
    off = service(tmp_path / "off", chain)
    run(off, [{**RESULT, "_image_content": b"raw"}])
    assert not (tmp_path / "off" / "images").exists()

    on = service(tmp_path / "on", chain, evidence_store_images=True)
    run(on, [{**RESULT, "_image_content": b"raw"}])
    assert (tmp_path / "on" / "images" / IMAGE_SHA).read_bytes() == b"raw"


def test_empty_results_are_left_alone(tmp_path) -> None:
    payload = asyncio.run(
        service(tmp_path, _FakeChain()).attest_results(
            {"status": "no_match", "results": []}, query_image=b"x", face_index=0
        )
    )
    assert payload["results"] == []
    assert "chain" not in payload


# -- verification ----------------------------------------------------------


class _VerifyChain(_FakeChain):
    """Answers verify() from an explicit set of known digests."""

    def __init__(self, known=(), deployed_at="2026-09-05T10:00:00Z", **kwargs):
        super().__init__(**kwargs)
        self.known = set(known)
        self.deployed_at = deployed_at

    async def status(self):
        base = await super().status()
        if base.get("state") == "ready":
            base["deployed_at"] = self.deployed_at
        return base

    async def verify(self, digest):
        from services.blockchain import EvidenceRecord

        exists = digest.lower() in {d.lower() for d in self.known}
        return EvidenceRecord(
            exists=exists,
            submitter=SUBMITTER if exists else "0x" + "0" * 40,
            timestamp=1_757_000_000 if exists else 0,
        )


DEPLOYED_AT = "2026-09-05T10:00:00Z"


def bundle_for(tmp_path, chain=None, **record_overrides):
    """Produce a stored bundle the way attest_results would."""
    chain = chain or _VerifyChain()
    svc = service(tmp_path, chain)
    payload = run(svc, [{**RESULT, **record_overrides}])
    proof = payload["results"][0]["proof"]
    return svc, svc.load_record(proof["record_digest"]), proof


def verified_chain(bundle):
    """A chain that knows both digests in a bundle."""
    record = bundle["record"]
    from services.evidence import sha256_digest as digest_of

    return _VerifyChain(
        known={digest_of(record), "0x" + record["post"]["image_sha256"]},
        deployed_at=record["chain"]["deployed_at"],
    )


def test_a_stored_record_verifies(tmp_path) -> None:
    _, bundle, proof = bundle_for(tmp_path)
    svc = service(tmp_path, verified_chain(bundle))
    result = asyncio.run(svc.verify_bundle(bundle))

    assert result["status"] == "verified"
    assert result["record_digest"] == proof["record_digest"]
    assert result["on_chain"]["record"]["exists"] is True
    assert result["on_chain"]["image"]["exists"] is True
    assert result["stored"] is True


def test_a_bare_record_without_its_proof_verifies(tmp_path) -> None:
    """A verifier does not have to trust the proof block to check the record."""
    _, bundle, proof = bundle_for(tmp_path)
    svc = service(tmp_path, verified_chain(bundle))
    result = asyncio.run(svc.verify_bundle(bundle["record"]))
    assert result["status"] == "verified"
    assert result["record_digest"] == proof["record_digest"]


def test_an_edited_caption_is_a_digest_mismatch(tmp_path) -> None:
    """The headline tamper case: one word changed, proof broken."""
    _, bundle, proof = bundle_for(tmp_path)
    chain = verified_chain(bundle)
    bundle["record"]["post"]["og_description"] = "Shot in Delhi"

    result = asyncio.run(service(tmp_path, chain).verify_bundle(bundle))
    assert result["status"] == "digest_mismatch"
    assert result["claimed_digest"] == proof["record_digest"]
    assert result["record_digest"] != proof["record_digest"]
    assert "modified" in result["reason"]


def test_an_edited_similarity_score_is_a_digest_mismatch(tmp_path) -> None:
    _, bundle, _ = bundle_for(tmp_path)
    chain = verified_chain(bundle)
    bundle["record"]["match"]["face_similarity"] = 0.99
    result = asyncio.run(service(tmp_path, chain).verify_bundle(bundle))
    assert result["status"] == "digest_mismatch"


def test_an_unattested_record_is_reported_as_such(tmp_path) -> None:
    _, bundle, _ = bundle_for(tmp_path)
    chain = _VerifyChain(known=(), deployed_at=bundle["record"]["chain"]["deployed_at"])
    result = asyncio.run(service(tmp_path, chain).verify_bundle(bundle["record"]))
    assert result["status"] == "not_attested"
    assert result["on_chain"]["record"]["exists"] is False


def test_a_redeployed_chain_is_reported_as_a_reset(tmp_path) -> None:
    """A restarted local node redeploys to the same address; only time differs."""
    _, bundle, _ = bundle_for(tmp_path)
    chain = verified_chain(bundle)
    chain.deployed_at = "2026-09-06T09:00:00Z"

    result = asyncio.run(service(tmp_path, chain).verify_bundle(bundle))
    assert result["status"] == "chain_reset"
    assert "redeployed" in result["reason"]


def test_a_different_chain_id_is_reported_as_a_reset(tmp_path) -> None:
    """A record written to another chain cannot be checked against this one."""
    _, bundle, _ = bundle_for(tmp_path)
    chain = verified_chain(bundle)
    # Verify the bare record, so editing the chain block is not itself tampering.
    record = bundle["record"]
    record["chain"]["chain_id"] = 11155111

    result = asyncio.run(service(tmp_path, chain).verify_bundle(record))
    assert result["status"] == "chain_reset"
    assert "chain 11155111" in result["reason"]


def test_editing_the_chain_block_of_a_signed_bundle_is_tampering(tmp_path) -> None:
    """Precedence matters: a modified record is a mismatch before anything else."""
    _, bundle, _ = bundle_for(tmp_path)
    chain = verified_chain(bundle)
    bundle["record"]["chain"]["chain_id"] = 11155111
    result = asyncio.run(service(tmp_path, chain).verify_bundle(bundle))
    assert result["status"] == "digest_mismatch"


def test_an_unreachable_node_is_not_a_verdict(tmp_path) -> None:
    """A node that cannot be reached must never read as 'not attested'."""
    _, bundle, _ = bundle_for(tmp_path)
    chain = _VerifyChain(state="unavailable")
    result = asyncio.run(service(tmp_path, chain).verify_bundle(bundle))
    assert result["status"] == "chain_unavailable"
    assert result["reason"] == "node down"


def test_a_non_record_upload_is_rejected(tmp_path) -> None:
    result = asyncio.run(
        service(tmp_path, _VerifyChain()).verify_bundle({"hello": "world"})
    )
    assert result["status"] == "digest_mismatch"
    assert "not a FaceChain evidence record" in result["reason"]


# -- live re-fetch ---------------------------------------------------------


def fetcher(content):
    async def fetch(url):
        return content

    return fetch


def test_refetch_confirms_an_unchanged_image(tmp_path) -> None:
    import hashlib as _h

    body = b"the-original-post-bytes"
    _, bundle, _ = bundle_for(tmp_path, image_sha256=_h.sha256(body).hexdigest())
    chain = verified_chain(bundle)
    svc = EvidenceService(
        Settings(evidence_dir=tmp_path, evidence_attest=True), chain, fetcher(body)
    )
    result = asyncio.run(svc.verify_bundle(bundle, refetch=True))

    assert result["status"] == "verified"
    assert result["refetch"]["matches"] is True


def test_refetch_detects_a_swapped_image(tmp_path) -> None:
    """The record is intact and on chain, but the post now serves other bytes."""
    import hashlib as _h

    body = b"the-original-post-bytes"
    _, bundle, _ = bundle_for(tmp_path, image_sha256=_h.sha256(body).hexdigest())
    chain = verified_chain(bundle)
    svc = EvidenceService(
        Settings(evidence_dir=tmp_path, evidence_attest=True),
        chain,
        fetcher(b"a-different-image"),
    )
    result = asyncio.run(svc.verify_bundle(bundle, refetch=True))

    assert result["status"] == "image_changed"
    assert result["refetch"]["matches"] is False
    # The original attestation is untouched: it still proves the old bytes existed.
    assert result["on_chain"]["image"]["exists"] is True


def test_a_deleted_post_is_not_a_tamper_verdict(tmp_path) -> None:
    """An unreachable image must not be reported as a changed image."""
    _, bundle, _ = bundle_for(tmp_path)
    chain = verified_chain(bundle)
    svc = EvidenceService(
        Settings(evidence_dir=tmp_path, evidence_attest=True), chain, fetcher(None)
    )
    result = asyncio.run(svc.verify_bundle(bundle, refetch=True))

    assert result["status"] == "verified"
    assert result["refetch"]["attempted"] is False
    assert "deleted" in result["refetch"]["reason"]


def test_refetch_is_skipped_without_a_fetcher(tmp_path) -> None:
    _, bundle, _ = bundle_for(tmp_path)
    svc = service(tmp_path, verified_chain(bundle))
    result = asyncio.run(svc.verify_bundle(bundle, refetch=True))
    assert result["status"] == "verified"
    assert result["refetch"]["attempted"] is False


# -- digest-only verification ----------------------------------------------


def test_verify_digest_finds_an_attested_digest(tmp_path) -> None:
    digest = "0x" + "ab" * 32
    chain = _VerifyChain(known={digest})
    result = asyncio.run(service(tmp_path, chain).verify_digest(digest))
    assert result["status"] == "verified"
    assert result["on_chain"]["record"]["submitter"] == SUBMITTER


def test_verify_digest_reports_an_unknown_digest(tmp_path) -> None:
    result = asyncio.run(
        service(tmp_path, _VerifyChain()).verify_digest("0x" + "cd" * 32)
    )
    assert result["status"] == "not_attested"
    assert result["stored"] is False
