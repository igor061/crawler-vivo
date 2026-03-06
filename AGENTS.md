# AGENTS Guide - vivocrawler
This guide is for coding agents working in this repository.
Follow these defaults unless the user asks otherwise.
## 1) Project context
- Language/runtime: Python 3.11 (`.python-version` = `3.11.14`).
- Domain: browser automation for Vivo Empresas login + invoice PDF downloads.
- Main scripts:
  - `scrapling_vivo_login_test.py` (primary end-to-end flow via Scrapling/Camoufox)
  - `download_vivo_bills.py` (Playwright alternative flow)
  - `screenshot_vivo_first_screen.py` (utility screenshot script)
- Tooling from `pyproject.toml`: `pytest`, `ruff`, `mypy`.
## 2) Setup commands
Use the repository standard setup:
```bash
pyenv local 3.11.14
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
```
Notes:
- `requirements-dev.txt` includes runtime + dev dependencies.
- Playwright browser install is required before Playwright scripts run.
## 3) Build, lint, and test commands
There is no package build step; this is a script-based project.
### 3.1 Syntax sanity check
```bash
python -m py_compile *.py
```
### 3.2 Lint
```bash
ruff check .
```
Optional auto-fix:
```bash
ruff check . --fix
```
### 3.3 Type check
```bash
mypy *.py
```
### 3.4 Full test run
```bash
pytest
```
Important: `pytest` has `testpaths = ["tests"]`, so plain `pytest` only auto-collects tests under `tests/`.
### 3.5 Run a single test (important)
Run one test file:
```bash
pytest tests/test_file.py
```
Run one test function:
```bash
pytest tests/test_file.py::test_specific_behavior
```
Run one class test method:
```bash
pytest tests/test_file.py::TestFlow::test_download_retry
```
Run tests by keyword:
```bash
pytest -k "download and retry"
```
Run an explicit root-level script-style test file:
```bash
pytest scrapling_vivo_login_test.py
```
## 4) Runtime commands (manual flows)
Primary flow:
```bash
python scrapling_vivo_login_test.py
```
Primary flow with filters:
```bash
python scrapling_vivo_login_test.py --account-number 0000000000 --month 2 --year 2026
```
Alternative downloader:
```bash
python download_vivo_bills.py --pause-for-manual-login
```
Screenshot helper:
```bash
python screenshot_vivo_first_screen.py
```
## 5) Code style and conventions
### 5.1 Imports
- Keep imports at file top.
- Group order: standard library, then third-party.
- Prefer one import per line in `from ... import ...` statements.
- Let Ruff (`I`) enforce import order.
### 5.2 Formatting
- 4-space indentation.
- Max line length: 100.
- Keep functions focused; extract helpers for repeated UI logic.
- Prefer `pathlib.Path` over manual path string concatenation.
### 5.3 Typing
- Add type hints for new/changed functions when practical.
- Prefer concrete containers (`list[str]`, `dict[str, str]`).
- Mypy config highlights:
  - `python_version = 3.11`
  - `warn_return_any = true`
  - `ignore_missing_imports = true`
  - `disallow_untyped_defs = false`
### 5.4 Naming
- Modules/functions: `snake_case`.
- Constants: `UPPER_SNAKE_CASE`.
- CLI flags: `kebab-case` (`--account-number`, `--wait-ms`).
- Use explicit names tied to automation intent (`normalize_cpf`, `save_download_object`).
### 5.5 CLI structure
- Prefer this shape in scripts:
  - `parse_args() -> argparse.Namespace`
  - `main() -> None`
  - `if __name__ == "__main__": main()`
- Use env vars for secrets/defaults where possible (`VIVO_CPF`, `VIVO_PASSWORD`).
### 5.6 Error handling
- Browser UI is flaky: retries and guarded fallbacks are acceptable.
- Catch narrow exceptions when feasible.
- If broad `except Exception` is needed, keep block small and emit context (`[warn]`).
- Never silently ignore failures that can invalidate final output.
### 5.7 Logging/output style
- Keep existing print prefixes:
  - `[info]` progress
  - `[ok]` successful step
  - `[warn]` recoverable issue
  - `[done]` final completion
### 5.8 Filesystem/artifacts
- Keep downloaded invoices in `downloads/vivo`.
- Keep screenshots/metadata in `screenshots/...`.
- Always create output dirs with `mkdir(parents=True, exist_ok=True)`.
- Write JSON in machine-friendly form (`ensure_ascii=True`, `indent=2`).
## 6) Ruff and mypy scope
- Ruff selects: `E`, `F`, `I`, `B`, `UP`.
- Lint/type excludes include `.venv`, `downloads`, and `screenshots`.
## 7) Agent testing expectations
- For code edits, run at least `ruff check .`.
- Run `mypy *.py` when touching typed logic or signatures.
- Prefer targeted tests first, then broader test run if relevant.
## 8) Cursor/Copilot rule files
Checked repository for additional agent rule files:
- `.cursor/rules/`: not present
- `.cursorrules`: not present
- `.github/copilot-instructions.md`: not present
If any are added later, treat them as higher-priority local agent instructions.
## 9) Safety and secrets
- Never hardcode CPF/password or other credentials.
- Prefer env vars (`VIVO_CPF`, `VIVO_PASSWORD`) for sensitive values.
- Do not commit `.env`, downloaded invoices, or screenshots unless explicitly requested.
