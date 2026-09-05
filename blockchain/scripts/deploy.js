const fs = require("node:fs");
const path = require("node:path");
const { artifacts, ethers, network } = require("hardhat");

async function main() {
  const registry = await ethers.deployContract("EvidenceRegistry");
  await registry.waitForDeployment();

  const address = await registry.getAddress();
  const artifact = await artifacts.readArtifact("EvidenceRegistry");
  const outputPath = process.env.DEPLOYMENT_OUTPUT
    || path.join(__dirname, "..", "deployments", "local.json");

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(
    outputPath,
    `${JSON.stringify({
      address,
      network: network.name,
      chainId: Number((await ethers.provider.getNetwork()).chainId),
      abi: artifact.abi,
    }, null, 2)}\n`,
    "utf8",
  );

  console.log(`EvidenceRegistry deployed to ${address}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
