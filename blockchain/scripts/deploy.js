const fs = require("node:fs");
const path = require("node:path");
const { artifacts, ethers, network } = require("hardhat");

async function main() {
  const registry = await ethers.deployContract("EvidenceRegistry");
  await registry.waitForDeployment();

  const address = await registry.getAddress();
  const artifact = await artifacts.readArtifact("EvidenceRegistry");

  // Derive selectors here so the Python client never needs a keccak
  // implementation: it looks these up by signature instead.
  const signature = (entry) =>
    `${entry.name}(${entry.inputs.map((input) => input.type).join(",")})`;
  const collect = (type, transform) =>
    Object.fromEntries(
      artifact.abi
        .filter((entry) => entry.type === type)
        .map((entry) => [signature(entry), transform(signature(entry))]),
    );

  // A restarted local node redeploys to the same address on the same chain id,
  // so only the wall-clock deploy time distinguishes one chain instance from
  // another. Records carry this, which is what makes a reset detectable.
  const deployedAt = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

  const selectors = collect("function", (sig) => ethers.id(sig).slice(0, 10));
  const errors = collect("error", (sig) => ethers.id(sig).slice(0, 10));
  const events = collect("event", (sig) => ethers.id(sig));
  const outputPath = process.env.DEPLOYMENT_OUTPUT
    || path.join(__dirname, "..", "deployments", "local.json");

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(
    outputPath,
    `${JSON.stringify({
      address,
      network: network.name,
      chainId: Number((await ethers.provider.getNetwork()).chainId),
      deployedAt,
      abi: artifact.abi,
      selectors,
      errors,
      events,
    }, null, 2)}\n`,
    "utf8",
  );

  console.log(`EvidenceRegistry deployed to ${address}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
