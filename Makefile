.PHONY: install lint test up

install:
	pip install -e ".[dev]"

lint:
	ruff check .

test:
	pytest

up:
	docker compose up -d --build
