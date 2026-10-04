.PHONY: install up down migrate revision test lint format typecheck dev

install:
	uv sync

up:
	docker compose up -d db

down:
	docker compose down

migrate:
	uv run alembic upgrade head

revision:
	uv run alembic revision --autogenerate -m "$(m)"

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

dev:
	uv run uvicorn app.main:create_app --factory --reload --port 8000
