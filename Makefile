.PHONY: install lint test migrate run up down

install:
	python -m pip install -e ".[dev]"

lint:
	ruff check .
	mypy app

test:
	pytest

migrate:
	alembic upgrade head

run:
	uvicorn app.main:app --reload

up:
	docker compose up --build -d

down:
	docker compose down

