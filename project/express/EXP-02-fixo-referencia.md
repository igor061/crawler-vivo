# EXP-02 — Referencia AAAAMM e --listar sem download no Vivo Fixo

**Data**: 2026-10-05 | **Branch**: `fix/fixo-referencia-yyyymm`

## Plano aprovado
- `_ref_yyyymm`: normaliza "Ago/2026" e o vencimento "21/09/2026" para AAAAMM.
- Todo resultado do Fixo sai com `referencia` AAAAMM; texto do portal em `referencia_portal`.
- Incluido a pedido: `--listar` do Fixo nao baixa (mesmo defeito corrigido no Movel, #2).
- Fora: `plane.py` continua derivando a ref do vencimento (segue correto).

## Validacao (portal real, `vivo_fixo.py --listar`, maquina DEV)
| Fatura | antes | agora | referencia_portal |
|---|---|---|---|
| set/2026 Aberta | `21/09/2026` | `202609` | `21/09/2026` |
| ago/2026 Paga | `Ago/2026` | `202608` | `Ago/2026` |
- `--listar`: 0 PDFs baixados, downloads_ok 0.
- `buscar_pdf_existente` acha os PDFs ja baixados por 202609/202608.
- `plane.py vivo-fixo --json <novo>` (dry-run): ref 09 casa IDRCADM-177.
Aprovada pelo usuario.
