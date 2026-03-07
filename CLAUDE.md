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
python vivo_download.py

# Vivo Fixo — full flow (switches context to Fixo before listing/downloading)
python vivo_download_fixo.py

# List invoices only (no download) — both scripts support --listar
python vivo_download.py --listar
python vivo_download_fixo.py --listar

# Debug mode (saves HTML + screenshots per step)
python vivo_download.py --debug
python vivo_download_fixo.py --debug

# Extract data from a PDF
python vivo_fatura_extrator.py /path/to/fatura.pdf
python vivo_fatura_extrator.py /path/to/fatura.pdf --salvar
```

## Architecture

Two independent scripts with no shared module:

**`vivo_download_fixo.py`** — Same structure as `vivo_download.py` but adds a `ContextSwitchService` step after login: clicks the "Vivo Móvel ∨" dropdown in the header, then clicks "Vivo Fixo" in the sidebar panel. Uses CSS text selectors (`button:has-text`, `h1:has-text`) with multiple absolute XPath fallbacks (the overlay `div` index varies). After switching, navigates to `/sec/invoices` and runs the same XPath-loop collection/download/enrichment flow. Output files are prefixed `vivo-fixo-` and the JSON result is `vivo_fixo_resultado_{cnpj}_{timestamp}.json`.

**`vivo_download.py`** — Main automation flow for Vivo Móvel. Uses `scrapling.StealthyFetcher` (Playwright + Camoufox under the hood) to drive a browser session. The entry point is `VivoDownloadApp.run()`, which calls `StealthyFetcher.fetch()` with a `page_action` callback. The page action orchestrates three service classes sequentially:
- `AuthService` — fills CPF/CNPJ, advances through the multi-step login, fills password
- `InvoiceService` — navigates to `/sec/invoices`, iterates XPath-addressed rows (section × div index loops) via `XPathInvoiceCollectionStrategy` to collect invoice metadata
- `DownloadService` — clicks the toggle button and download link for each invoice, saves PDF to `downloads/vivo/` with a `vivo-{cnpj}-{conta}-{year}-{month}.pdf` naming convention
- `ResultService` — enriches invoice data from the extractor and writes a JSON summary

After download, `ResultService._enriquecer_com_extrator` dynamically imports `vivo_fatura_extrator.extrair_dados_fatura` to enrich each invoice entry with barcode, PIX, dates, and value from the PDF.

**`vivo_fatura_extrator.py`** — Standalone PDF extractor. Uses `pypdf` for text extraction, regex chains (`RegexChainExtractor`) for field parsing, and optionally `opencv`/`pymupdf` for QR decoding (`PixQrExtractor`). Can be imported as a library or run as a CLI.

## Credentials

Via `.env` file (copy `.env.example`):
```env
VIVO_CPF=SEU_CPF_OU_CNPJ
VIVO_PASSWORD=SUA_SENHA
```

Or as environment variables `VIVO_CPF` / `VIVO_PASSWORD`.

## Outputs

- PDFs: `downloads/vivo/vivo-{cnpj}-{conta}-{year}-{month}.pdf`
- Result JSON: `downloads/vivo/vivo_download_resultado_{cnpj}_{timestamp}.json`
- Debug snapshots (with `--debug`): `screenshots/scrapling/debug_xpath_loop_{timestamp}/`

## Testing Patterns

Tests use `monkeypatch` to stub `ler_texto_pdf` and `PixQrExtractor.extract` — no real PDFs required. Browser-dependent code in `vivo_download.py` is tested via service classes directly with fake strategies/mocks, not end-to-end.
