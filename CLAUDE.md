# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pyenv local 3.11.14
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
```

For QR/PIX extraction (optional):
```bash
pip install -r requirements-qr.txt
```

## Commands

```bash
# Lint
ruff check .

# Tests
pytest

# Run a single test
pytest tests/test_vivo_fatura_extrator.py::test_arrecadacao_extractor_prioritizes_48_digit_format

# Vivo Móvel — full flow (login, list, download, JSON)
python vivo_movel.py

# Vivo Fixo — full flow (switches context to Fixo before listing/downloading)
python vivo_fixo.py

# List invoices only (no download) — both scripts support --listar
python vivo_movel.py --listar
python vivo_fixo.py --listar

# Debug mode (saves HTML + screenshots per step)
python vivo_movel.py --debug
python vivo_fixo.py --debug

# Force re-download even if PDF already exists
python vivo_movel.py --force
python vivo_fixo.py --force

# Extract data from a PDF
python vivo_fatura_extrator.py /path/to/fatura.pdf
python vivo_fatura_extrator.py /path/to/fatura.pdf --salvar
```

## Architecture

Three modules with clear separation of concerns:

**`vivo_core.py`** — Shared library. Contains `BaseConfig`, `BaseVivoApp` (template method base class), `AuthService`, `DebugCollector`, `BrowserActions`, `DownloadPanelService`, `ResultService`, `NamingService`, and utilities (`referencia_para_yyyymm`, `buscar_pdf_existente`, `enriquecer_com_extrator`, `imprimir_tabela_listagem`). The `executar_fetch` function drives `StealthyFetcher` (Playwright + Camoufox).

**`BaseVivoApp`** template flow: `login → _pos_login() → goto /sec/invoices → _coletar_secoes() → _popular_faturas() → _baixar_ou_listar() → enriquecer PDFs → salvar JSON`. Subclasses override the hook methods and class constants (`PREFIXO_RESULTADO`, `PDF_PREFIXO`, `TITULO_TABELA`).

**`vivo_movel.py`** — `VivoMovelApp(BaseVivoApp)` for Vivo Móvel. Uses DOM selectors on `section[data-test-invoices-line-grid]` to collect accounts, then `MovelDownloadService` opens each account's detail slide, finds toggles via `datacardsection[data-test-card-section-content]`, and downloads "Conta detalhada e nota fiscal" PDFs. Output prefixed `vivo-movel-`.

**`vivo_fixo.py`** — `VivoFixoApp(BaseVivoApp)` for Vivo Fixo. `_pos_login` adds `ContextSwitchService` step (clicks `#service-select-desktop` → `[data-service-id="WIR"]`). `FixoDownloadService` opens each line's "Ver detalhes", finds `[data-test-drop-down]` toggles (JS DOM traversal to extract "Fev/2026"-style referência), and downloads boleto PDFs. Output prefixed `vivo-fixo-`.

**`vivo_fatura_extrator.py`** — Standalone PDF extractor. Uses `pypdf` for text extraction, regex chains (`RegexChainExtractor`) for field parsing, and optionally `opencv`/`pymupdf` for QR decoding (`PixQrExtractor`). Extracts: barcode, PIX, NFe URL, phone, dates, value. Can be imported as a library or run as a CLI.

**`DownloadPanelService`** — Shared service managing the floating download panel: `cancelar_dialog_se_visivel`, `minimizar`, `tem_falha` (detects "Tentar novamente"), `aguardar_download` (2s polling loop, not `page.expect_download`).

## Credentials

Via `.env` file (copy `.env.example`):
```env
VIVO_CPF=SEU_CPF_OU_CNPJ
VIVO_PASSWORD=SUA_SENHA
```

Or as environment variables `VIVO_CPF` / `VIVO_PASSWORD`.

## Outputs

- Móvel PDFs: `downloads/vivo/vivo-movel-{cnpj}-{conta}-{yyyymm}.pdf`
- Fixo PDFs: `downloads/vivo/vivo-fixo-{cnpj}-{conta}-{yyyymm}.pdf`
- Result JSON: `downloads/vivo/vivo_movel_resultado_{cnpj}_{timestamp}.json` (or `vivo_fixo_resultado_...`)
- Debug snapshots (with `--debug`): `screenshots/scrapling/debug_movel_{timestamp}/` or `debug_fixo_{timestamp}/`

## Testing Patterns

Tests use `monkeypatch` to stub `ler_texto_pdf` and `PixQrExtractor.extract` — no real PDFs required. `tests/test_vivo_core.py` covers shared utilities (`referencia_para_yyyymm`, `buscar_pdf_existente`, `enriquecer_com_extrator`, etc.) imported directly from `vivo_core`.
