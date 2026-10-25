.PHONY: install test lint format up demo down logs

install:  ## Create a virtualenv with dev dependencies
	python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

test:  ## Run the test suite
	.venv/bin/pytest

lint:  ## Lint and check formatting
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

format:  ## Auto-format and fix lint issues
	.venv/bin/ruff format .
	.venv/bin/ruff check --fix .

up:  ## Start the full stack (needs OPENAI_API_KEY in .env)
	docker compose up --build -d

demo:  ## Start the full stack with the offline mock AI provider
	AI_PROVIDER=mock docker compose up --build -d

down:  ## Stop the stack
	docker compose down

logs:  ## Follow logs from the app services
	docker compose logs -f api transcriber summarizer
