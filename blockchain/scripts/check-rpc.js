const http = require("node:http");

const payload = JSON.stringify({
  jsonrpc: "2.0",
  method: "eth_chainId",
  params: [],
  id: 1,
});

const request = http.request(
  {
    hostname: "127.0.0.1",
    port: 8545,
    path: "/",
    method: "POST",
    headers: {
      "content-type": "application/json",
      "content-length": Buffer.byteLength(payload),
    },
    timeout: 2000,
  },
  (response) => {
    let body = "";
    response.on("data", (chunk) => (body += chunk));
    response.on("end", () => {
      try {
        const result = JSON.parse(body);
        process.exit(response.statusCode === 200 && result.result ? 0 : 1);
      } catch {
        process.exit(1);
      }
    });
  },
);

request.on("error", () => process.exit(1));
request.on("timeout", () => request.destroy());
request.write(payload);
request.end();

