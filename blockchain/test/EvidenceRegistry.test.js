const assert = require("node:assert/strict");
const { artifacts, ethers } = require("hardhat");

describe("EvidenceRegistry", function () {
  it("attests and verifies an evidence digest", async function () {
    const [submitter] = await ethers.getSigners();
    const registry = await ethers.deployContract("EvidenceRegistry");
    await registry.waitForDeployment();

    const digest = ethers.sha256(ethers.toUtf8Bytes("facechain-test-evidence"));
    await (await registry.attest(digest)).wait();

    const [exists, recordedSubmitter, timestamp] = await registry.verify(digest);
    assert.equal(exists, true);
    assert.equal(recordedSubmitter, submitter.address);
    assert.ok(timestamp > 0n);
  });

  it("reports an unknown digest as missing", async function () {
    const registry = await ethers.deployContract("EvidenceRegistry");
    await registry.waitForDeployment();

    const digest = ethers.sha256(ethers.toUtf8Bytes("unknown"));
    const [exists] = await registry.verify(digest);
    assert.equal(exists, false);
  });
});


describe("EvidenceRegistry ABI surface", function () {
  // The Python client reads these from contract.json instead of computing
  // keccak. Changing a signature must fail here, not silently at runtime.
  const EXPECTED = {
    functions: {
      "attest(bytes32)": "0x23c3617f",
      "verify(bytes32)": "0x75e36616",
      "records(bytes32)": "0x01e64725",
    },
    errors: {
      "EmptyDigest()": "0x73ca6097",
      "EvidenceAlreadyExists(bytes32)": "0xcb7acfcd",
    },
    events: {
      "EvidenceAttested(bytes32,address,uint64)":
        "0x81cb45e50d6227293052d4f1d5d9e2621847e1f234335c4750caa0b3b374de0f",
    },
  };

  it("keeps the selectors the deployment artifact publishes", async function () {
    for (const [signature, selector] of Object.entries(EXPECTED.functions)) {
      assert.equal(ethers.id(signature).slice(0, 10), selector, signature);
    }
    for (const [signature, selector] of Object.entries(EXPECTED.errors)) {
      assert.equal(ethers.id(signature).slice(0, 10), selector, signature);
    }
    for (const [signature, topic] of Object.entries(EXPECTED.events)) {
      assert.equal(ethers.id(signature), topic, signature);
    }
  });

  it("exposes exactly those members in its ABI", async function () {
    const { abi } = await artifacts.readArtifact("EvidenceRegistry");
    const signature = (entry) =>
      `${entry.name}(${entry.inputs.map((input) => input.type).join(",")})`;
    const named = (type) => abi.filter((e) => e.type === type).map(signature).sort();

    assert.deepEqual(named("function"), Object.keys(EXPECTED.functions).sort());
    assert.deepEqual(named("error"), Object.keys(EXPECTED.errors).sort());
    assert.deepEqual(named("event"), Object.keys(EXPECTED.events).sort());
  });
});

describe("EvidenceRegistry reverts", function () {
  it("rejects an empty digest", async function () {
    const registry = await ethers.deployContract("EvidenceRegistry");
    await registry.waitForDeployment();

    await assert.rejects(
      registry.attest(ethers.ZeroHash),
      (error) => error.message.includes("EmptyDigest"),
    );
  });

  it("rejects a digest that is already recorded", async function () {
    const registry = await ethers.deployContract("EvidenceRegistry");
    await registry.waitForDeployment();

    const digest = ethers.sha256(ethers.toUtf8Bytes("duplicate"));
    await (await registry.attest(digest)).wait();

    await assert.rejects(
      registry.attest(digest),
      (error) => error.message.includes("EvidenceAlreadyExists"),
    );
  });

  it("emits the recorded digest, submitter and timestamp", async function () {
    const [submitter] = await ethers.getSigners();
    const registry = await ethers.deployContract("EvidenceRegistry");
    await registry.waitForDeployment();

    const digest = ethers.sha256(ethers.toUtf8Bytes("event-args"));
    const receipt = await (await registry.attest(digest)).wait();
    const log = receipt.logs.find((entry) => entry.address === registry.target);
    const parsed = registry.interface.parseLog(log);

    assert.equal(parsed.name, "EvidenceAttested");
    assert.equal(parsed.args.digest, digest);
    assert.equal(parsed.args.submitter, submitter.address);

    const [, , timestamp] = await registry.verify(digest);
    assert.equal(parsed.args.timestamp, timestamp);
  });
});
