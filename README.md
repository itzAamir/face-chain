# FaceChain Verifier

FaceChain Verifier is a localhost-first pipeline that:

1. detects and encodes a face from an uploaded image,
2. searches Google Web Detection and optional SerpAPI Google Lens exact and visual results,
3. confirms candidate and public-page preview images locally with SFace before showing their public pages, and
4. anchors every confirmed match on a blockchain as a tamper-evident record that can be re-verified afterwards.

Exact and near-duplicate images remain the most reliable results. The pipeline also evaluates Google's visually similar public images with SFace, allowing different photos of the selected person to appear when Google discovers them.

What the proof does and does not claim is worth stating plainly. An attestation
proves that **a specific record existed at a specific block time** — this image,
at this URL, with this caption and these scores. It does not prove the search is
reproducible: results come from the live web and change from day to day. Re-running
the same photo tomorrow may find different posts, and that is expected.

## Architecture

- `app/`: FastAPI API, plain HTML/CSS/JavaScript frontend, and bundled YuNet/SFace models.
- `app/services/face.py`: in-memory face detection, selection, alignment, multi-view query encoding, and comparison.
- `app/services/search.py`: Google Vision and SerpAPI Google Lens discovery, safe candidate/page-preview retrieval, social URL classification, and ranking.
- `app/services/evidence.py`: canonical record building, hashing, storage, attestation, and re-verification.
- `app/services/blockchain.py`: JSON-RPC client for the deployed `EvidenceRegistry` contract.
- `app/scripts/verify_onchain.py`: command-line re-verification.
- `app/scripts/demo_verification.py`: scripted anchor / verify / tamper demonstration.
- `compose.yaml`: starts the isolated application container on its own.
- `compose.google.yaml`: mounts the existing web-search credential read-only.
- `compose.blockchain.yaml`: adds the local EvidenceRegistry chain and wires the application to it.
- `blockchain/`: Hardhat project holding the `EvidenceRegistry` contract, its deployment script, and its tests.

Only the application port is published to the host. Uploaded images and face embeddings are processed in memory and are not persisted; see [Privacy and model files](#privacy-and-model-files) for what an evidence record does contain.

## Prerequisites

Install and start Docker Desktop, or install Docker Engine with the Docker Compose plugin.

Verify the installation:

```bash
docker --version
docker compose version
```

No host-level Python, Node.js, Hardhat, or Solidity installation is required.

## Run the project

### 1. Open the project directory

```bash
cd /path/to/face-chain
```

### 2. Create the local environment file

```bash
cp .env.example .env
```

The default application address is <http://localhost:8000>.

If port `8000` is already occupied, edit `.env` and change:

```text
APP_PORT=18080
```

The application will then be available at <http://localhost:18080>.

### 3. Start the application

The application can start without search credentials so its health and configuration can be inspected:

```bash
docker compose up --build
```

Compose builds and starts only FastAPI, the static frontend, and the local OpenCV models. The blockchain services are not started unless `compose.blockchain.yaml` is added.

The first build can take several minutes while Docker installs Python dependencies. Later starts use the build cache.

To run in the background instead:

```bash
docker compose up --build -d
```

### 4. Configure SerpAPI Google Lens

Add the ready API key to the ignored `.env` file; never add it to source code:

```text
SERPAPI_API_KEY=your-serpapi-key
```

Then start the app normally:

```bash
docker compose up --build app
```

The selected face crop is converted to JPEG and kept below SerpAPI's 500 KB upload limit. SerpAPI returns a temporary image identifier, which is used for one Google Lens request covering exact and visual matches. Lens candidates still have to pass local SFace verification. The complete uploaded photo is not sent to SerpAPI.

### 5. Also enable Google Vision

If you already have the credential JSON, save it as:

```text
secrets/google-credentials.json
```

To combine Google Vision exact/partial matching with SerpAPI Google Lens discovery, start the application with both Compose files:

```bash
docker compose \
  -f compose.yaml \
  -f compose.google.yaml \
  up --build app
```

The credential is mounted read-only inside the application container. The `secrets/` directory is ignored by Git.

If one configured provider fails, the other provider can still return results.

### 6. Open and verify the application

Open the port selected in `.env`, for example:

- <http://localhost:8000>
- <http://localhost:18080>

Check the running containers:

```bash
docker compose ps
```

Check the API:

```bash
curl http://localhost:8000/api/health
curl http://localhost:8000/api/status
```

Replace `8000` with your configured `APP_PORT` when necessary.

The health response should be:

```json
{"status":"ok"}
```

The status response reports application, face-model, per-provider, and blockchain readiness without exposing credential data. The `blockchain` object carries the contract address, chain id, deployment time and attesting account when a chain is wired up, and `attestation` reports whether anchoring is enabled.

## API workflow

Detect faces first:

```bash
curl -X POST http://localhost:8000/api/faces/detect \
  -F 'image=@/absolute/path/to/photo.jpg'
```

The response contains normalized bounding boxes and stable face indexes. Send the original image again with the selected index to run the search:

```bash
curl -X POST http://localhost:8000/api/search \
  -F 'image=@/absolute/path/to/photo.jpg' \
  -F 'face_index=0'
```

`status: "matched"` means at least one result passed provider discovery, local SFace confirmation, and recognized social-post URL classification. Confirmed general web pages and direct visual-image matches can be returned with `status: "no_match"` when no qualifying social post is present. Each result identifies whether it came from Google Vision, SerpAPI Google Lens, or both. The summary distinguishes provider candidates from the number of candidate images actually inspected.

Every confirmed result also carries the fingerprint of the matched post, which
is what the blockchain stage will anchor:

| Field | Meaning |
| --- | --- |
| `image_url` | The image URL whose bytes were downloaded and confirmed. |
| `image_sha256` | SHA-256 of those exact bytes. Re-derivable by anyone who downloads the same URL. |
| `image_bytes` | Size of the confirmed image in bytes. |
| `og_title` | The post's own title, read from `og:title` or `twitter:title`. |
| `og_description` | The post's own caption, read from `og:description` or `twitter:description`. |

`page_title` comes from the search provider; `og_title` and `og_description`
come from the post itself, so an edited caption changes them. Post text is
fetched only for results that survive ranking, and reuses the page fetch already
performed during image inspection. Direct image results have no containing page,
so their text fields are `null`.

## Blockchain

Attestation and re-verification are not implemented yet, but the chain is now
wired to the application. Starting the stack with the blockchain overlay brings
up a local Hardhat node, deploys `EvidenceRegistry` to it, and starts the
application only after the deployment succeeds:

```bash
docker compose -f compose.yaml -f compose.blockchain.yaml up --build
```

or, equivalently:

```bash
make up-chain
```

The overlay itself is the opt-in; there is no Compose profile to select. Add
`compose.google.yaml` as a third file to combine the chain with Google Vision.

The deployer writes `contract.json` into the `deployment-data` volume: the
contract address, network, chain id, ABI, and the function selectors, custom
error selectors, and event topic derived from that ABI. The application reads
the selectors from this file rather than computing them, which is why the client
needs no keccak implementation and no web3 dependency — `EvidenceRegistry` takes
one 32-byte argument, so its calldata is a selector plus one word.
`blockchain/test/EvidenceRegistry.test.js` pins those selectors, so changing a
contract signature fails the contract tests instead of silently desyncing the
Python client. The application mounts that volume read-only at `/deployment` and reads
`CONTRACT_DEPLOYMENT_FILE`. Attested records will be
written to `data/evidence/`, which is bind-mounted so they stay readable on the
host after the containers stop.

The plain `docker compose up --build` path is unchanged and still needs no
chain.

### What is recorded

Each confirmed result is frozen into a record and anchored as **two independent
digests**, using the same `attest(bytes32)` entry point:

| Digest | Covers | Answers |
| --- | --- | --- |
| `image_digest` | SHA-256 of the matched image bytes | Were these exact pixels recorded, and when? |
| `record_digest` | SHA-256 of the canonical record | Were this image, at this URL, with this caption and these scores recorded, and when? |

The record holds the post (page URL, provider title, `og:title`, `og:description`,
image URL, image hash and size), the match (type, similarity, provider), the
query (upload hash, face index), the model checksums, the thresholds in force,
the chain the proof was written to, and an RFC3339 `observed_at`. The thumbnail
is deliberately excluded: JPEG encoding is not byte-stable, so including it would
make records fail to re-hash.

Records are written to `data/evidence/<record_digest>.json` as
`{"record": …, "proof": …}`. Only `record` is hashed; `proof` sits beside it and
carries the transaction hashes, block numbers, submitter and timestamps. To
re-verify, canonicalize `record`, hash it, and look the digest up on chain — the
result must equal the file name.

Editing anything inside `record` — a caption, a URL, a similarity score — changes
its digest, and the changed digest is not on chain. That is the tamper-evidence.

Attestation is on by default when the chain is reachable and can be disabled
with `EVIDENCE_ATTEST=false`. It never fails a search: if the node is down, each
result carries `proof: {"state": "unavailable", …}` and the search result is
returned regardless. `EVIDENCE_STORE_IMAGES=true` additionally keeps the exact
bytes that were hashed under `data/evidence/images/`, so a post that is later
deleted can still be re-verified; it is off by default because it is the only
part of the pipeline that persists image data.

### Re-verifying a record

Verification re-derives the digest from the record and looks it up on chain. It
never trusts the `proof` block: that block is only used to detect that the record
has been edited since it was written.

```bash
# From the browser, or:
curl -X POST http://localhost:8000/api/verify -F 'record=@data/evidence/0xABC….json'

# Re-download the post image and compare it with the attested bytes:
curl -X POST http://localhost:8000/api/verify \
  -F 'record=@data/evidence/0xABC….json' -F 'refetch=true'

# Or look up a digest alone, with no record in hand:
curl -X POST http://localhost:8000/api/verify -F 'digest=0xABC…'

# Fetch a stored record so it can be verified elsewhere:
curl http://localhost:8000/api/evidence/0xABC…
```

The same checks run from the command line, which is the easiest thing to hand to
a reviewer:

```bash
make verify RECORD=data/evidence/0xABC….json
make verify RECORD=data/evidence/0xABC….json REFETCH=1
make verify DIGEST=0xABC…
```

Exit code `0` means verified, `1` means it did not verify, `2` means the input or
the chain needs attention.

| Status | Meaning |
| --- | --- |
| `verified` | The record hashes to a digest that is on chain. With `refetch`, the live image still matches too. |
| `digest_mismatch` | The record no longer hashes to the digest its own proof claims. It has been edited. |
| `image_changed` | The record is intact and on chain, but the URL now serves different bytes than the ones attested. |
| `not_attested` | The digest is not on the chain. It was never attested, or its chain state is gone. |
| `chain_reset` | The chain or contract in front of us is not the one this record was written against. |
| `chain_unavailable` | The node could not be reached. Deliberately not a verdict: absence of an answer is not a negative answer. |

An unreachable image — a deleted or private post — reports `verified` with the
re-fetch skipped, never `image_changed`. Failing to fetch is not evidence of
tampering. Verifying by digest alone cannot detect `chain_reset`, because a bare
digest carries no record of the chain it was written to; pass the record file
when you want that distinction.

### In the browser

The results view shows a proof block on every confirmed result: the record and
image digests, the transaction hash, the block time, and the contract address,
with a copy button, a **Download record** link, and a **Re-verify** button that
re-checks the digest against the chain.

Below the results, a drop zone accepts any evidence record — including one edited
by hand — and reports the verdict. The three outcomes are deliberately styled
apart: verified is green, a tampered record or a changed image is red, and an
unreachable chain is grey, because that is the absence of an answer rather than a
negative one. The `CHAIN` tile in the runtime strip shows whether the node is
reachable and whether anchoring is enabled.

### Watch it work

With the chain stack running:

```bash
make demo
```

This anchors a synthetic match, re-verifies it from the stored record, re-verifies
it again with a live image re-fetch, then changes one word of the caption and
shows the proof failing, and finally swaps the image bytes and shows `image_changed`.
It needs no search credentials.

A restarted local node redeploys to the same address on the same chain id, so the
deployment timestamp is the only thing that distinguishes one chain instance from
another. That is why records carry it, and why a proof from before a restart
reports `chain_reset` instead of looking like a forgery. See
[Known limitations](#known-limitations).

## View logs

Follow the application:

```bash
docker compose logs -f app
```

Inspect the application service:

```bash
docker compose logs app
```

Press `Ctrl+C` to stop following logs. This does not stop containers that were started with `-d`.

## Run tests

Run the Python application tests:

```bash
docker compose run --rm --build --no-deps app pytest
```

The default test target runs the current application tests:

```bash
make test
```

Run the contract tests (Hardhat uses its own in-process node, so the chain service does not need to be running):

```bash
make test-contract
```

Both targets rebuild their image first. Without that they would run whatever was
last built and report a false pass.

The Python suite covers the face pipeline, search, the evidence record schema
(including a pinned golden digest, so a schema change fails the build rather than
silently invalidating stored records), the blockchain client, and every
verification outcome. The contract suite pins the selectors the Python client
reads from `contract.json`, so changing a contract signature fails here instead
of desyncing the client at runtime.

The frontend has no automated tests.

## Run the live acceptance check

Use a public or consented image. The production code contains no expected URL; the command succeeds only when the live providers discover a result, SFace confirms it, and its URL matches a supported social-post format.

```bash
docker compose \
  -f compose.yaml \
  -f compose.google.yaml \
  run --rm --no-deps \
  -v /absolute/path/to/photo.jpg:/tmp/demo.jpg:ro \
  app python -m scripts.live_acceptance /tmp/demo.jpg
```

For an image containing several people, repeat with the intended zero-based face index:

```bash
docker compose \
  -f compose.yaml \
  -f compose.google.yaml \
  run --rm --no-deps \
  -v /absolute/path/to/photo.jpg:/tmp/demo.jpg:ro \
  app python -m scripts.live_acceptance /tmp/demo.jpg --face-index 1
```

Exit code `0` proves at least one live social-post match. Exit code `1` means the search completed without a confirmed social post; exit code `2` means the input needs attention.

## Stop or restart

Stop the containers while preserving them:

```bash
docker compose stop
```

Restart stopped containers:

```bash
docker compose start
```

Remove the application container and private network:

```bash
docker compose down
```

Rebuild after changing dependencies or Dockerfiles:

```bash
docker compose up --build
```

## Troubleshooting

### Port is already allocated

Set another port in `.env`:

```text
APP_PORT=18080
```

Then recreate the application container:

```bash
docker compose up -d
```

### Application is running but the page does not load

Confirm the published port:

```bash
docker compose ps app
```

Then check the application logs:

```bash
docker compose logs app
```

### Credential file is not found

Confirm that this file exists with the exact filename:

```text
secrets/google-credentials.json
```

Use both Compose files when starting the credential-enabled stack.

### Results appear but carry no proof

Check that the chain is up and that the application can see it:

```bash
curl http://localhost:8000/api/status
```

`blockchain.state` should be `ready`. Other values mean:

- `not_configured` — the application cannot read `contract.json`. Start the stack with `compose.blockchain.yaml`, or check that the `deployer` service exited successfully with `make logs-chain`.
- `unavailable` — the artifact is readable but the node is not answering.
- `chain_mismatch` — the node reports a different chain id than the one the contract was deployed to.

Each result's `proof.reason` carries the specific failure. Attestation never fails
a search, so results are still returned when the chain is down.

### A record that verified yesterday now fails

If it reports `chain_reset`, the local node was restarted and its state was
discarded. This is expected for the in-memory Hardhat node; nothing is recoverable.
Deploy to a public testnet if proofs need to outlive a restart.

## Known limitations

### Search

- Cross-photo discovery uses Google Vision visual similarity and SerpAPI Google Lens; these are broader than duplicate matching but are not dedicated biometric identity-search engines.
- SerpAPI receives only the selected face crop. Searching the complete photo or a wider crop could improve Lens recall, but would disclose more of the source image and is therefore not enabled implicitly.
- Visual candidates receive local SFace confirmation but may link directly to an image because Google does not associate these results with a containing page.
- Public result pages are inspected only as bounded HTML for Open Graph, Twitter Card, and JSON-LD data. JavaScript is never executed, so login-only or client-rendered media remains unavailable.
- Search quality depends on public indexing and direct access to a matching image URL. Public social platforms may hide private, login-only, or unindexed posts.
- The application intentionally skips inaccessible candidates instead of claiming an unverified match.

### Proof

- An attestation proves a record existed at a block time. It does not prove the record is *true*: it says nothing about whether the face match is correct, only that this exact claim was made and frozen at that moment.
- The default local Hardhat node keeps its state in memory. Restarting it discards every attestation. Records carry the deployment timestamp so this is reported as `chain_reset` rather than being mistaken for a forged record, but the proof is genuinely gone. Use a public testnet for anything that must outlive a restart.
- Anyone who can reach the node can attest any digest. The contract records *who* submitted and *when*, not whether they were entitled to.
- Two transactions are sent per confirmed result, so up to ten per search. This is instant on a local node; on a public network it is real latency and real gas.
- Live re-fetch can only compare bytes that are still served. A deleted or private post reports `verified` with the re-fetch skipped — never `image_changed`, because failing to fetch is not evidence of tampering.
- Verifying a bare digest cannot detect `chain_reset`, since a digest carries no record of the chain it was written to. Pass the record file when you need that distinction.
- The frontend has no automated tests. The Python and contract suites do not cover it.

## Privacy and model files

Uploaded images and face embeddings stay in memory for the duration of a request. The application creates no upload directory, database, or biometric log.

Attestation is the one part of the pipeline that writes anything durable. When it runs, each confirmed result produces a record in `data/evidence/` containing:

- the SHA-256 of the uploaded image and the selected face index — **not the image itself, and not the embedding**;
- the matched post's page URL, provider title, `og:title`, `og:description`, image URL, image SHA-256 and byte size;
- the match type, face similarity, and discovery provider;
- the model checksums, the thresholds in force, the chain details, and an observation timestamp.

The digests of that record and of the matched image are written to the blockchain. **On a public chain those digests are permanent and world-readable.** They reveal nothing directly, but anyone holding a copy of the same image or record can confirm it was attested — that is the entire point, and it is worth being deliberate about before pointing this at a public network.

Set `EVIDENCE_ATTEST=false` to keep the pipeline fully in-memory. `EVIDENCE_STORE_IMAGES=true` additionally writes the matched image bytes to `data/evidence/images/`; it is off by default because it is the only setting that persists image data.

`data/evidence/` is ignored by Git.

The bundled YuNet and SFace ONNX files and their SHA-256 values are documented in `app/models/README.md`; their upstream license files are stored alongside them.
