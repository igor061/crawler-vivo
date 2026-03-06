# Vivo Crawler

Automacao para login no Vivo Empresas e download de faturas em PDF.

## Setup rapido (pyenv + venv)

```bash
pyenv local 3.11.14
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
```

## Uso

1) Defina credenciais:

```bash
cp .env.example .env
export VIVO_CPF="SEU_CPF_OU_CNPJ"
export VIVO_PASSWORD="SUA_SENHA"
```

2) Rode o fluxo completo:

```bash
python scrapling_vivo_login_test.py
```

3) Download filtrado por conta + mes/ano:

```bash
python scrapling_vivo_login_test.py --account-number 0000000000 --month 2 --year 2026
```

## Estrutura

- `scrapling_vivo_login_test.py`: login + navegacao + download das faturas
- `download_vivo_bills.py`: fluxo alternativo com Playwright direto
- `screenshot_vivo_first_screen.py`: captura da tela inicial

## Qualidade

```bash
python -m py_compile *.py
ruff check .
mypy *.py
```
