PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff
# pytest-xdist workers, at most 12; JOBS=0 runs serially in one process.
JOBS ?= auto
# For instance PYTEST_ARGS='-m "not slow"', which is what a Checks job runs on a pull request.
PYTEST_ARGS ?=

# Run the same checks per interpreter, e.g. make check PYTHON=python3.14 RUFF=ruff

.PHONY: lint check docs wheel-smoke mcpb
lint:
	$(RUFF) check src tests mcpb scripts/wheel_smoke.py scripts/generate_tool_docs.py scripts/build_mcpb.py scripts/check_plugin_lock.py scripts/check_versions.py scripts/release_notes.py scripts/bump_version.py
	$(RUFF) format --check src tests mcpb scripts/wheel_smoke.py scripts/generate_tool_docs.py scripts/build_mcpb.py scripts/check_plugin_lock.py scripts/check_versions.py scripts/release_notes.py scripts/bump_version.py

check: lint
	$(PYTHON) -m pytest -q -p no:cacheprovider -n $(JOBS) --maxprocesses=12 --dist worksteal --durations=15 $(PYTEST_ARGS)

# Writes docs/tools.md from the registered tools; a test fails when it is out of date.
docs:
	$(PYTHON) scripts/generate_tool_docs.py

# Needs uv and network access for the dependencies; uploads nothing.
wheel-smoke:
	$(PYTHON) scripts/wheel_smoke.py

# Writes dist/proton-mail-mcp-<version>.mcpb, the Claude Desktop bundle; uploads nothing.
mcpb:
	$(PYTHON) scripts/build_mcpb.py
