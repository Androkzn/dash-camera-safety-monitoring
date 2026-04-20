.PHONY: install dev test lint run docker-build docker-up docker-down clean

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	pytest tests/ -v --tb=short

lint:
	python -m py_compile road_safety/server.py
	python -m py_compile road_safety/config.py
	python -m py_compile start.py

run:
	python start.py

run-cloud:
	python start.py --cloud

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-up-cloud:
	docker compose --profile cloud up -d

docker-down:
	docker compose down

clean:
	rm -rf dist/ build/ *.egg-info .pytest_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
