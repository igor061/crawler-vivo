# Crawler Vivo

Automacao para acessar o portal Vivo Empresas, listar contas/faturas, baixar PDFs e extrair dados financeiros relevantes para auditoria e conciliacao.

## Instalacao via GitHub

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install git+https://github.com/igor061/crawler-vivo.git
playwright install chromium
camoufox fetch
```

Configure as credenciais — escolha uma das formas:

**Forma 1 — arquivo `.env`** (recomendado, persiste entre execucoes):
```bash
cat > .env <<'EOF'
VIVO_CPF=SEU_CPF_OU_CNPJ
VIVO_PASSWORD=SUA_SENHA
EOF
```

**Forma 2 — variaveis de ambiente** (sessao atual do shell):
```bash
export VIVO_CPF=SEU_CPF_OU_CNPJ
export VIVO_PASSWORD=SUA_SENHA
```

**Forma 3 — argumentos na linha de comando:**
```bash
vivo-movel --cpf SEU_CPF_OU_CNPJ --password SUA_SENHA
vivo-fixo  --cpf SEU_CPF_OU_CNPJ --password SUA_SENHA
```

Execute:

```bash
vivo-movel                         # Vivo Movel: baixa faturas
vivo-fixo                          # Vivo Fixo: baixa faturas
vivo-extrator /caminho/fatura.pdf  # Extrai dados de um PDF
```

## Atualizacao

```bash
pip install --upgrade git+https://github.com/igor061/crawler-vivo.git
```

---

## Objetivo do projeto

- Automatizar o processo manual de login e navegacao no portal da Vivo.
- Baixar faturas em PDF com nomes padronizados e rastreaveis por CNPJ/conta/competencia.
- Gerar um JSON consolidado com status da execucao e metadados das faturas.
- Extrair dos PDFs informacoes como codigo de barras, PIX copia e cola, emissor, destinatario, vencimento e valor.

## Desenvolvimento local

```bash
pyenv local 3.11.14
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
```

Para habilitar extracao QR/PIX mais robusta (opcional):

```bash
pip install -r requirements-qr.txt
```

## Configuracao de credenciais

Veja as 3 formas na secao de instalacao acima. Em desenvolvimento local, copie o exemplo:

```bash
cp .env.example .env
# edite .env com seu CPF/CNPJ e senha
```

## Como usar

### Vivo Movel

```bash
vivo-movel           # baixa ate 2 faturas por conta (padrao)
vivo-movel --todas   # baixa todas as faturas disponiveis
vivo-movel --listar  # lista sem baixar
vivo-movel --force   # re-baixa mesmo se PDF ja existir
vivo-movel --debug   # salva HTML + screenshot por etapa
```

### Vivo Fixo

```bash
vivo-fixo            # baixa ate 2 faturas por conta (padrao)
vivo-fixo --todas
vivo-fixo --listar
vivo-fixo --force
vivo-fixo --debug
```

### Extracao de dados de um PDF

```bash
vivo-extrator /caminho/para/fatura.pdf           # imprime JSON no terminal
vivo-extrator /caminho/para/fatura.pdf --salvar  # salva <nome>_dados.json ao lado do PDF
```

## Saidas geradas

- PDFs Movel: `downloads/vivo/vivo-movel-<cnpj>-<conta>-<yyyymm>.pdf`
- PDFs Fixo: `downloads/vivo/vivo-fixo-<cnpj>-<conta>-<yyyymm>.pdf`
- JSON resultado: `downloads/vivo/vivo_movel_resultado_<cnpj>_<timestamp>.json`
- Debug (com `--debug`): `screenshots/scrapling/debug_movel_<timestamp>/`

## Qualidade e testes

```bash
ruff check .
pytest
```

## Observacoes

- O portal pode apresentar variacoes de interface; o projeto usa estrategias de fallback para clique e selecao.
- Mensagens `Early EOD in RunLengthDecode` podem aparecer durante leitura de alguns PDFs e sao nao fatais.
