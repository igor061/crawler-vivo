# Vivo Crawler

Automacao para login no Vivo Empresas e download de faturas em PDF.

## Configuracao rapida (pyenv + venv)

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
python vivo_download.py
```

3) Apenas listar sem baixar:

```bash
python vivo_download.py --listar
```

4) Modo debug (salva screenshot + HTML por etapa):

```bash
python vivo_download.py --debug
```

## Estrutura

- `vivo_download.py`: login + navegacao + listagem + download via XPath

## Qualidade

```bash
python -m py_compile *.py
ruff check .
mypy *.py
```
