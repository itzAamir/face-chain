# Local secrets

Place the Google Cloud service-account credential at:

```text
secrets/google-credentials.json
```

Then start Compose with both configuration files:

```bash
docker compose -f compose.yaml -f compose.google.yaml up --build
```

Everything in this directory except this README is ignored by Git.

