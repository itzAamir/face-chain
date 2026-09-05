const assert = require("node:assert/strict");
const { ethers } = require("hardhat");

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

