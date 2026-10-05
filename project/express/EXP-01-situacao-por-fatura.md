# EXP-01 — Situacao por fatura no slide do Vivo Movel

**Data**: 2026-10-05 | **Branch**: `fix/situacao-por-fatura`

## Problema
`_obter_opcoes_no_slide` propagava o unico badge do slide para todas as faturas
do historico: 29/09 as 12 vieram "Atrasada", 05/10 todas "Paga".

## Descoberta (HTML real do slide, `--debug`)
O portal so exibe status no cabecalho do `.data-card` (fatura mais recente).
As linhas de historico tem so valor, mes e "Baixar" — nao ha status por fatura.

## Plano aprovado / comportamento validado
- Primeira linha recebe o `.data-card-badge` filho direto do card; demais ficam `""`.
- `--debug` salva HTML + print do slide de cada conta.
- Fora: Vivo Fixo, download.

## Validacao (portal real, `--listar --debug`, maquina DEV)
| Conta | 09/2026 | 04–08/2026 |
|---|---|---|
| 0439719185 | Paga | vazio |
| 0466032796 | Paga | vazio |
Aprovada pelo usuario.

## Gauntlet — rodada 1
| Critico | Tier | Veredito |
|---|---|---|
| Arquitetura & Seguranca | sonnet | ✅ — debug ja grava HTML em outras etapas; `screenshots/` no gitignore |
| Qualidade & Testes | sonnet | ✅ — 8 testes (`tests/test_vivo_movel_situacao.py`), 8 mutacoes pegas; corrigiu fixture `_ServicoEspiao` |

Nao bloqueantes: `DebugCollector.capture` sem `--debug` ainda tira screenshot e
descarta (custo pre-existente); `test_app_injeta_debug_no_servico` usa `inspect.getsource`.

Reproduzir: `python -m pytest -q`
