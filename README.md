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

**Forma 1 — prompt interativo:** execute o comando diretamente; se nenhuma credencial for encontrada, o script solicita CPF/CNPJ e senha no terminal (senha mascarada, timeout de 30s) e pergunta se deseja salvar em `.env` para as proximas execucoes.

**Forma 2 — arquivo `.env`** (persiste entre execucoes):
```bash
cat > .env <<'EOF'
VIVO_CPF=SEU_CPF_OU_CNPJ
VIVO_PASSWORD=SUA_SENHA
EOF
```

**Forma 3 — variaveis de ambiente** (sessao atual do shell):
```bash
export VIVO_CPF=SEU_CPF_OU_CNPJ
export VIVO_PASSWORD=SUA_SENHA
```

**Forma 4 — argumentos na linha de comando:**
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
pip install --force-reinstall --no-cache-dir git+https://github.com/igor061/crawler-vivo.git
```

## Servidor Linux headless (VPS) — instavel

**Recomendado rodar em desktop (macOS/Linux com GPU real), onde o login e 100% estavel.**

Em servidor sem GPU o Firefox fica **sem WebGL** e o antifraude da Vivo rejeita o login
com um falso "A senha nao esta correta" (mesmo com a senha certa). Dependencias extras
obrigatorias nesse cenario — Mesa (WebGL por software) e Xvfb:

```bash
sudo apt-get install -y libgl1 libegl1 libgl1-mesa-dri mesa-utils xvfb
```

E execute sempre com display virtual (headless puro continua sem WebGL):

```bash
vivo-movel --mode virtual
vivo-fixo  --mode virtual
```

Mesmo assim o login em VPS **falhava na maioria das tentativas** (testado em Contabo). O motivo
era o **shape do fingerprint**: um browser Linux atrás de proxy residencial é uma contradição que
o antifraude usa para reprovar. A partir do Camoufox 0.5.x (Firefox >= 149) dá para usar os
**fingerprint presets reais** (macOS/Windows/Linux) com `VIVO_CAMOUFOX_OS=macos` + `VIVO_CAMOUFOX_PRESET=51`
(pin do preset macOS Apple M1), o que faz o VPS logar com um fingerprint macOS coerente:

```bash
VIVO_PROXY=http://10.202.0.3:8888 \
VIVO_CAMOUFOX_OS=macos VIVO_CAMOUFOX_PRESET=51 \
vivo-movel --engine camoufox --mode virtual
```

Confira `VIVO_CAMOUFOX_PRESET`: `off` desliga; `on`/`random` sorteia preset (pode crashar com
"No WebGL data found"); um número fixa o preset macOS v150 daquele índice (51 = Apple M1, seguro).

O antifraude ainda pontua a reputação do IP por janela: a 1ª tentativa após ~5min de cooldown
passa, e tentativas em rajada levam falso "OAM-2". Isso não trava a conta. `VIVO_LOGIN_RETRIES`
(default 3) + `VIVO_LOGIN_RETRY_BACKOFF` (default 60s) re-tentam com nova sessão até o dashboard
confirmar. Para uso diário (1 login/dia) o fluxo VPS funciona.

Cuidado: cada falha consome o contador de tentativas de senha da Vivo ("Voce tem mais 3 tentativas")
e pode bloquear a conta; um login bem-sucedido zera o contador.

Em Linux o `navigator.mediaDevices.enumerateDevices()` do Camoufox nunca resolve (sem
hardware de midia), o que trava scripts antifraude. O projeto injeta dispositivos falsos
automaticamente nesse caso — controlado por `VIVO_FIX_MEDIA_DEVICES`:

| Valor | Efeito |
|---|---|
| `auto` (padrao) | aplica o patch so em Linux |
| `on` | aplica sempre |
| `off` | desliga o patch |

### Proxy de saida (opcional)

Para sair pela internet de outra maquina (ex.: IP residencial via WireGuard), defina
`VIVO_PROXY` — o geoip do Camoufox e ativado automaticamente para casar timezone/locale
com o IP de saida (requer `pip install "camoufox[geoip]"`):

```bash
VIVO_PROXY=http://10.202.0.3:8888 vivo-movel --mode virtual
```

`VIVO_CAMOUFOX_OS` (ex.: `macos`) forca o fingerprint de OS do browser, se necessario.

### Engines e warm-up

Selecao de motor de automacao (`--engine` / env `VIVO_ENGINE`):

| Engine | Cloudflare mve.vivo.com.br | Notas |
|---|---|---|
| `camoufox` (padrao) | passa | unica engine que atravessa o challenge "Um momento..." |
| `patchright` | preso | `channel=chrome` nao vence o challenge |
| `nodriver` | preso | CDP direto, sem shim do Playwright, mas Chrome e bloqueado |

- `--warmup-ms <ms>` (env `VIVO_WARMUP_MS`): antes do login, navega para uma pagina neutra e
  aguarda o tempo dado, aquecendo o beacon antifraude. Na pratica nao ajudou a destravar o
  login — manter desligado (0).
- No VPS, o login passou a funcionar com Camoufox 0.5.x + fingerprint preset macOS real
  (ver secao "Servidor Linux headless (VPS)"): o problema era o shape Linux + residencial,
  nao o IP. Retry seguro via `VIVO_LOGIN_RETRIES`/`VIVO_LOGIN_RETRY_BACKOFF`.


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
- JSON resultado: `downloads/vivo/vivo_movel_resultado_<cnpj>_<timestamp>.json` (ou `vivo_fixo_resultado_...`)
- Debug (com `--debug`): `screenshots/scrapling/debug_movel_<timestamp>/` ou `debug_fixo_<timestamp>/`

Ao final da execucao, alem da tabela de listagem, e impresso um resumo **FATURAS EM ATRASO** com as faturas de situacao Atrasada/Aberta/Vencida, incluindo vencimento, valor, telefone, codigo de barras e PIX copia e cola (quando disponivel no PDF).

## Qualidade e testes

```bash
ruff check .
pytest
```

## Observacoes

- O portal pode apresentar variacoes de interface; o projeto usa estrategias de fallback para clique e selecao.
- Mensagens `Early EOD in RunLengthDecode` podem aparecer durante leitura de alguns PDFs e sao nao fatais.
