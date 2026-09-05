.PHONY: up down stop logs test test-app test-contract config

up:
	docker compose up --build

stop:
	docker compose stop

down:
	docker compose down

logs:
	docker compose logs -f

test: test-app

test-app:
	docker compose run --rm --no-deps app pytest

test-contract:
	docker compose --profile blockchain run --rm --no-deps blockchain npm test

config:
	docker compose config
