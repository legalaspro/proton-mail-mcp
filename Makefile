PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff
JOBS ?= 12

# Run the same checks per interpreter, e.g. make check PYTHON=python3.14 RUFF=ruff

.PHONY: lint check wheel-smoke
lint:
	$(RUFF) check src tests scripts/run_tests.py scripts/wheel_smoke.py
	$(RUFF) format --check src tests scripts/run_tests.py scripts/wheel_smoke.py

check: lint
	$(PYTHON) scripts/run_tests.py --suite full --jobs $(JOBS)

# Needs uv and network access for the dependencies; uploads nothing.
wheel-smoke:
	$(PYTHON) scripts/wheel_smoke.py
