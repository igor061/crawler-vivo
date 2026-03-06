# Blueprint (minimalista)

## Objetivo

Baixar faturas da Vivo com robustez operacional e rastreabilidade.

## Requisitos funcionais

- Login por CPF e senha.
- Navegacao para `Contas` -> `Acessar faturas`.
- Download de `Conta detalhada e nota fiscal (.pdf)`.
- Filtros por conta, mes e ano.
- Tentativas automaticas (3) por item.

## Requisitos nao funcionais

- Script idempotente no filesystem (nomes de arquivo previsiveis).
- Evidencias de execucao em JSON e screenshots.
- Configuracao por argumentos CLI.
- Compatibilidade Python 3.11.

## Limites

- Fluxo depende da interface atual do portal Vivo.
- Cloudflare e protecoes anti-bot podem alterar estabilidade.

## Melhorias futuras

- Logs estruturados em arquivo.
- Dedupe de downloads por hash.
- Testes E2E com mocks de navegador.
