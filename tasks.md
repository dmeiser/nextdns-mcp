# Migration Plan: Poetry to uv

This plan outlines the steps to migrate the NextDNS MCP Server from Poetry to `uv` for dependency management and packaging.

## 1. Project Initialization & Dependency Migration

- [x] **Install uv**
  - Ensure `uv` is installed in the local environment.
- [x] **Backup existing configuration**
  - Keep `pyproject.toml` and `poetry.lock` as reference until migration is complete.
- [x] **Migrate project metadata to PEP 621**
  - Move `name`, `version`, `description`, `readme`, `license`, and authors from `[tool.poetry]` into a `[project]` table.
  - Set `requires-python` in `[project]` to match the current `python = "^3.14"` constraint.
  - Ensure package layout (src/`nextdns_mcp`) is correctly represented via the chosen build backend configuration.
- [x] **Choose and configure build backend**
  - Select a backend (e.g., `hatchling.build` or `setuptools.build_meta`) and update `[build-system]` accordingly.
  - Add any required backend-specific config (e.g., `tool.hatch.build` or `tool.setuptools.packages.find`) so that `nextdns_mcp` under `src/` is packaged correctly.
- [x] **Remove Poetry-specific configuration**
  - Delete `[tool.poetry]` and any poetry-only fields from `pyproject.toml`.
  - Delete `poetry.lock` once the `uv.lock` file is generated and committed.
- [x] **Initialize uv project / lockfile**
  - Run `uv init` if needed, or manually ensure `[project]` and `[build-system]` conform to uv expectations.
  - Run `uv sync` to create `uv.lock` and a project environment.
- [x] **Add Runtime Dependencies (uv)**
  - Run: `uv add fastmcp httpx pyyaml python-dotenv`.
  - **Runtime Libraries (imported by the app at runtime):**
    - `fastmcp`
    - `httpx`
    - `pyyaml`
    - `python-dotenv`
- [x] **Add Development Dependencies (uv)**
  - Run: `uv add --dev pytest pytest-asyncio pytest-cov black ruff isort mypy types-pyyaml radon`.
  - **Dev Libraries (tests, lint, type-check, metrics):**
    - `pytest`
    - `pytest-asyncio`
    - `pytest-cov`
    - `black`
    - `ruff`
    - `isort`
    - `mypy`
    - `types-pyyaml`
    - `radon`
- [x] **Keep existing tool configurations**
  - Retain `[tool.pytest.ini_options]`, `[tool.black]`, `[tool.ruff]`, etc., in `pyproject.toml` and ensure they still apply with uv.

## 2. Dockerfile Update

- [x] **Update `Dockerfile` builder stage for uv**
  - Install `uv` instead of `poetry` in the builder image.
  - Copy `pyproject.toml` and `uv.lock` into the image.
  - Run `uv sync --frozen --no-dev` to create a production virtual environment for the app.
- [x] **Update `Dockerfile` runtime stage**
  - Copy only the built virtual environment and application source into the final image.
  - Ensure the `PATH` or `VIRTUAL_ENV` is set so that `python -m nextdns_mcp.server` (or the chosen entrypoint) runs using the uv-managed environment.
  - Verify there are no remaining references to Poetry (including comments about `poetry.lock`).

## 3. CI/CD Workflows Update (`.github/workflows/`)

- [x] **Update `unit-tests.yml` for uv**
  - Replace Python + Poetry setup with `astral-sh/setup-uv` (or equivalent uv setup action).
  - Replace `poetry install` with `uv sync --frozen`.
  - Replace all `poetry run ...` with `uv run ...` (e.g., `uv run pytest tests/unit --cov=src/nextdns_mcp --cov-report=term-missing`).
- [x] **Update `e2e-mcp-gateway.yml`**
  - Remove the "Fix poetry.lock timestamp" step, or replace it with any `uv.lock`-specific handling if needed (ideally not required when using `uv sync --frozen`).
  - Decide whether PyYAML / jsonschema installs remain via `pip` (acceptable for CI-only helpers) or should be part of the project dev dependencies and invoked via `uv run`.
  - Ensure there are no remaining references to Poetry in comments or step names.
- [x] **Update `docker-publish.yml`**
  - Confirm it builds using the updated `Dockerfile` and no longer relies on `poetry.lock` semantics anywhere.

## 4. Documentation Update

- [x] **Update `README.md`**
  - Replace installation and development instructions that reference Poetry with uv equivalents (e.g., `uv sync`, `uv run ...`).
  - Document the canonical local dev workflow using uv (tests, type-check, lint, E2E where appropriate).
- [x] **Update `docs/index.md`**
  - Replace references to running locally with Poetry with uv.
- [x] **Update `docs/getting-started.md`**
  - Update "Run locally" instructions (e.g., Option C) to use uv instead of Poetry.
  - Update example commands (`poetry install`, `poetry run ...`) to `uv sync` and `uv run ...`.
- [x] **Update `docs/configuration.md`**
  - Adjust any dependency or tooling references that mention Poetry.
- [x] **Update `AGENT.md`**
  - Update the "Code Quality Requirements" commands from `poetry run ...` to `uv run ...` while preserving the required sequence of checks.
- [x] **Update `.github/copilot-instructions.md`**
  - Update "Code Quality Requirements" commands from `poetry run ...` to `uv run ...`.

## 5. Verification

- [x] **Local Verification (uv environment)**
  - Run `uv sync` to ensure the environment and `uv.lock` are up to date.
  - Run `uv run pytest tests/unit --cov=src/nextdns_mcp` and ensure all unit tests pass with required coverage.
  - Run `uv run pytest tests/integration/test_server_init.py` and ensure integration tests pass.
  - Run the server locally via uv (e.g., `uv run python -m nextdns_mcp.server`), verifying basic startup and health.
- [x] **Quality Gate Verification (uv)**
  - Run `uv run isort src/ tests/`.
  - Run `uv run black src/ tests/`.
  - Run `uv run mypy src/`.
  - Run `uv run radon cc src/ -a` and `uv run radon cc src/ -nc` to confirm complexity requirements.
- [x] **Docker Verification**
  - Build image: `docker build -t nextdns-mcp .`.
  - Run container and verify it starts and responds correctly via Docker MCP Gateway.
  - Ensure the image does not include Poetry and uses the uv-managed environment.
- [ ] **Gateway E2E Verification**
  - Run `scripts/gateway_e2e_run.sh` using the updated image and configuration.
  - Confirm 100% pass rate for all tools and schema validations.
