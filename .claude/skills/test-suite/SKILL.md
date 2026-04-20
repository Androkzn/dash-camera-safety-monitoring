---
name: test-suite
description: Run the full pytest suite (and optional frontend type-check) for fleet-safety-demo. Use when the user asks to run tests, verify changes, or check before commit.
allowed-tools: Bash(.venv/bin/pytest:*), Bash(pytest:*), Bash(make test), Bash(cd frontend && npx tsc:*), Bash(cd frontend && npm run build)
---

# /test-suite

Run the project's verification matrix. Default scope is **backend pytest**. Pass `frontend` to also type-check / build the React app, or `all` for both.

## Steps

1. **Backend tests** (always):
   ```
   .venv/bin/pytest tests/ -v --tb=short
   ```
   Fall back to `pytest tests/ -v --tb=short` if `.venv/bin/pytest` is missing.

2. **Frontend type-check** (if `$ARGUMENTS` contains `frontend` or `all`):
   ```
   cd frontend && npx tsc -b --noEmit
   ```

3. **Frontend build** (only if `$ARGUMENTS` contains `all`):
   ```
   cd frontend && npm run build
   ```

## Reporting

- On failure: show the failing test names + first error context. Do not re-run automatically — surface the failure and ask before fixing.
- On success: one-line summary (`N passed in Xs`).
- Never mark complete if any step failed.

## Notes

- Detection-gate changes **must** pass `tests/test_core.py` — call out specifically if those tests fail.
- `pytest-asyncio` is configured; async tests are first-class.
