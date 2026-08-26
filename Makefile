.PHONY: install install-api install-web lint test build migrate run-api run-web up down

install: install-api install-web

install-api:
	python -m pip install -e "./apps/api[dev]"

install-web:
	npm install

lint:
	cd apps/api && ruff check . && mypy app
	npm run lint:web

test:
	cd apps/api && pytest

build:
	npm run build:web

migrate:
	cd apps/api && alembic upgrade head

run-api:
	cd apps/api && uvicorn app.main:app --reload

run-web:
	npm run dev:web

up:
	docker compose up --build -d

down:
	docker compose down
