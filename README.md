# Crawler Vivo

Automacao para acessar o portal Vivo Empresas, listar contas/faturas, baixar PDFs e extrair dados financeiros relevantes para auditoria e conciliacao.

## Objetivo do projeto

- Automatizar o processo manual de login e navegacao no portal da Vivo.
- Baixar faturas em PDF com nomes padronizados e rastreaveis por CNPJ/conta/competencia.
- Gerar um JSON consolidado com status da execucao e metadados das faturas.
- Extrair dos PDFs informacoes como codigo de barras, PIX copia e cola, emissor, destinatario, vencimento e valor.

## Principais scripts

- `vivo_download.py`: fluxo principal (login, coleta, download e consolidacao de resultados).
- `vivo_fatura_extrator.py`: extrator de dados de uma fatura PDF (uso via API Python ou CLI).

## Requisitos

- Python 3.11+
- Dependencias de runtime em `requirements.txt`
- Browser do Playwright (Chromium)

Opcional para melhorar extracao de QR/PIX:

- Dependencias em `requirements-qr.txt`

## Instalacao rapida

```bash
pyenv local 3.11.14
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
```

Para habilitar extracao QR/PIX mais robusta:

```bash
pip install -r requirements-qr.txt
```

## Configuracao de credenciais

Opcao 1 (recomendada): arquivo `.env`.

```bash
cp .env.example .env
```

Defina os valores:

```env
VIVO_CPF=SEU_CPF_OU_CNPJ
VIVO_PASSWORD=SUA_SENHA
```

Opcao 2: variaveis de ambiente no shell.

```bash
export VIVO_CPF="SEU_CPF_OU_CNPJ"
export VIVO_PASSWORD="SUA_SENHA"
```

## Como usar

Fluxo completo (listar + baixar + gerar JSON):

```bash
python vivo_download.py
```

Apenas listar faturas (sem baixar):

```bash
python vivo_download.py --listar
```

Modo debug (salva HTML + screenshot por etapa):

```bash
python vivo_download.py --debug
```

## Extracao de dados de um PDF

Imprimir JSON no terminal:

```bash
python vivo_fatura_extrator.py /caminho/para/fatura.pdf
```

Salvar JSON ao lado do PDF (`<nome>_dados.json`):

```bash
python vivo_fatura_extrator.py /caminho/para/fatura.pdf --salvar
```

## Saidas geradas

- PDFs baixados: `downloads/vivo/`
- Resultado consolidado da execucao:
  - `downloads/vivo/vivo_download_resultado_<cnpj>_<timestamp>.json`
- Debug (quando `--debug`):
  - `screenshots/scrapling/debug_xpath_loop_<timestamp>/`

## Qualidade e testes

```bash
ruff check .
pytest
```

## Observacoes

- O portal pode apresentar variacoes de interface; por isso o projeto usa estrategias de fallback para clique e selecao.
- Mensagens `Early EOD in RunLengthDecode` podem aparecer durante leitura de alguns PDFs e, em geral, sao nao fatais.
