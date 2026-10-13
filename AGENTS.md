# AGENTS.md

Guidance for agents and contributors working in this repository.

## Project layout

- `export-fix/` — the Python project.
  - `export_fix/` — the package (`naming.py`, `links.py`, `pipeline.py`, `ziputils.py`, `cli.py`).
  - `tests/` — pytest tests (`test_export_fix.py`).
  - `test-data/` — sample Notion export zips used by tests.
  - `drag_drop_entry.py` — entry point used for the PyInstaller executable.
- `pyproject.toml` — project metadata, dev dependencies and pytest/ruff config.
- `.github/workflows/build-and-release-executables.yml` — CI (test, lint, build, release).

## Setup

[uv](https://docs.astral.sh/uv/) manages the environment. No manual venv/pip needed.

## Commands

Run from the repository root:

- Run tests: `uv run pytest`
- Lint: `uv run ruff check export-fix`
- Auto-fix lint: `uv run ruff check --fix export-fix`
- Format: `uv run ruff format export-fix`
- Run the CLI: `uv run notion-export-fix <path-to-export.zip>`

`uv` creates/updates `.venv` automatically from `pyproject.toml`; commit changes to
`uv.lock` when dependencies change.

## Notes

- Runtime code uses only the Python standard library — do not add runtime dependencies
  without reason.
- The primary workflow is to create a Notion workspace export zip and process it; see
  `README.md` for the end-user instructions.
