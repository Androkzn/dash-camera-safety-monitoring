---
name: lint-code
description: Run the project's lightweight syntax/type checks. Backend uses py_compile on key entrypoints; frontend uses tsc --noEmit. Use before commits or after bulk edits.
allowed-tools: Bash(make lint), Bash(.venv/bin/python -m py_compile:*), Bash(python -m py_compile:*), Bash(cd frontend && npx tsc:*)
---

# /lint-code

Run cheap correctness checks. This project deliberately has **no formatter** (black/isort/eslint/prettier) wired up — do not introduce one.

## Steps

1. **Backend** (`make lint` equivalent):
   ```
   .venv/bin/python -m py_compile road_safety/server.py road_safety/config.py start.py
   ```
   Or simply: `make lint`.

2. **Frontend type-check** (if `$ARGUMENTS` contains `frontend` or empty):
   ```
   cd frontend && npx tsc -b --noEmit
   ```

## Reporting

- Surface any compile / type error with file:line.
- On clean run: one-line `lint OK` summary.

## Do NOT

- Do not run black, isort, prettier, eslint, or any auto-formatter — none are configured and the team has chosen not to wire them up.
- Do not add lint config files (`.eslintrc`, `.prettierrc`, `pyproject.toml [tool.black]`) without asking.
