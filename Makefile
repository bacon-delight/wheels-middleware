.PHONY: install test test-llm lint fmt

VENV := .venv
PY := $(VENV)/bin/python

install:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[api,dev]"

test:            ## deterministic suite (no LLM, $0)
	$(PY) -m pytest -q

test-llm:        ## live extraction eval (Bedrock/Anthropic); measures token cost
	$(PY) -m pytest -m llm --run-llm -s

lint:
	$(VENV)/bin/ruff check .

fmt:
	$(VENV)/bin/ruff check . --fix
