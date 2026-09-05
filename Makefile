COMPOSE       := docker compose
COMPOSE_CHAIN := docker compose -f compose.yaml -f compose.blockchain.yaml

.PHONY: up down stop logs test test-app test-contract config \
        up-chain down-chain logs-chain config-chain demo verify

up:
	$(COMPOSE) up --build

stop:
	$(COMPOSE) stop

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

# Application plus the local EvidenceRegistry chain.
up-chain:
	$(COMPOSE_CHAIN) up --build

down-chain:
	$(COMPOSE_CHAIN) down

logs-chain:
	$(COMPOSE_CHAIN) logs -f

test: test-app

# --build matters: the images copy the source in, so without it these run
# against whatever was last built and report a false green.
test-app:
	$(COMPOSE) run --rm --build --no-deps app pytest

# Hardhat runs its own in-process node, so the chain service is not needed.
test-contract:
	$(COMPOSE_CHAIN) run --rm --build --no-deps blockchain npm test

# Anchor a synthetic match, re-verify it, then tamper with it and show the
# proof failing. Requires the chain stack to be running.
demo:
	$(COMPOSE_CHAIN) exec app python -m scripts.demo_verification

# Re-verify one record: make verify RECORD=data/evidence/0xABC.json
# Add REFETCH=1 to re-download the post image and compare it with the attested bytes.
verify:
	$(COMPOSE_CHAIN) exec app python -m scripts.verify_onchain \
	  $(if $(RECORD),/data/evidence/$(notdir $(RECORD)),$(DIGEST)) \
	  $(if $(REFETCH),--refetch,)

config:
	$(COMPOSE) config

config-chain:
	$(COMPOSE_CHAIN) config
