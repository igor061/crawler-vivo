#!/usr/bin/env python3
"""vivo_fixo.py — Download de faturas Vivo Fixo via portal Vivo Empresas.

Usa vivo_core.py para login, browser, debug e utilitários compartilhados.
Fluxo: login → switch contexto Fixo → /sec/invoices → Ver detalhes →
       Baixar → Boleto (.pdf)
"""

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vivo_core import (
    BaseConfig,
    BaseVivoApp,
    BrowserActions,
    DownloadPanelService,
    Logger,
    add_common_args,
    ano_mes_por_vencimento,
    build_common_dirs,
    buscar_pdf_existente,
    carregar_env_arquivo,
    coleta_data_hora_gmt_menos3,
    normalize_document,
    referencia_para_yyyymm,
    resolve_engine,
    resolve_mode,
    resolve_spike_login,
    resolve_warmup_ms,
    validar_credenciais,
)

# ---------------------------------------------------------------------------
# Config Fixo
# ---------------------------------------------------------------------------

@dataclass
class FixoConfig(BaseConfig):
    limite: int | None = 2
    force: bool = False

    @property
    def debug_dir(self) -> Path:
        stamp = self.coleta_dt.strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"debug_fixo_{stamp}"


# ---------------------------------------------------------------------------
# Coleta de metadados das seções Fixo
# ---------------------------------------------------------------------------

def _extrair_secoes_fixo(page: Any, config: FixoConfig, logger: Logger) -> list[dict[str, Any]]:
    """Coleta metadados de todas as seções/linhas de faturas Vivo Fixo."""
    secoes: list[dict[str, Any]] = []
    try:
        page.locator("[data-test-wireline-grid]").first.wait_for(state="visible", timeout=15000)
    except Exception:
        logger.log("warn", "Grid wireline nao encontrado")

    sections = page.locator("section.mve-grid").all()
    logger.log("info", f"Secoes encontradas: {len(sections)}")

    for sec in sections:
        codigo_cliente = ""
        try:
            el = sec.locator("[data-test-secondary-info] span").first
            if el.count() > 0:
                codigo_cliente = el.inner_text(timeout=1500).strip()
        except Exception:
            pass

        linha_titulo = ""
        try:
            el = sec.locator("[data-test-line-title]").first
            if el.count() > 0:
                linha_titulo = el.inner_text(timeout=1500).strip()
        except Exception:
            pass

        invoice_rows = sec.locator("[data-test-invoices].invoice").all()
        faturas: list[dict[str, Any]] = []
        for row in invoice_rows:
            valor = ""
            referencia = ""
            situacao = ""
            try:
                el = row.locator("[data-test-invoice-amount]").first
                if el.count() > 0:
                    valor = el.inner_text(timeout=1500).replace("\xa0", " ").strip()
            except Exception:
                pass
            try:
                el = row.locator("[data-test-invoice-due-date]").first
                if el.count() > 0:
                    referencia = el.inner_text(timeout=1500).strip()
                    cls = el.get_attribute("class") or ""
                    cls_up = cls.upper()
                    if "PAID" in cls_up or "CLOSED" in cls_up:
                        situacao = "Paga"
                    elif "OPEN" in cls_up or "DUE" in cls_up or "PENDING" in cls_up:
                        situacao = "Aberta"
                    elif "OVERDUE" in cls_up or "LATE" in cls_up or "EXPIRED" in cls_up:
                        situacao = "Vencida"
            except Exception:
                pass
            # fallback: tenta badge de status na linha
            if not situacao:
                try:
                    badge = row.locator(".badge, [data-test-invoice-status]").first
                    if badge.count() > 0:
                        situacao = badge.inner_text(timeout=1000).strip()
                except Exception:
                    pass
            faturas.append({"valor": valor, "referencia": referencia, "situacao": situacao})

        secoes.append({
            "codigo_cliente": codigo_cliente,
            "linha_titulo": linha_titulo,
            "faturas": faturas,
            "_sec_locator": sec,
        })

    return secoes


# ---------------------------------------------------------------------------
# ContextSwitchService: Móvel → Fixo
# ---------------------------------------------------------------------------

class ContextSwitchService:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def selecionar_vivo_fixo(self, page: Any, config: FixoConfig) -> bool:
        self.logger.log("info", "Clicando no menu de servicos no header")
        menu = page.locator("#service-select-desktop").first
        try:
            menu.wait_for(state="visible", timeout=10000)
        except Exception:
            pass
        if menu.count() == 0:
            self.logger.log("warn", "Botao de servico nao encontrado no header")
            return False
        if not BrowserActions.click_with_fallback(menu, timeout_ms=5000):
            self.logger.log("warn", "Falha ao clicar no botao de servico")
            return False
        page.wait_for_timeout(max(600, config.wait_ms // 4))

        self.logger.log("info", "Clicando em Vivo Fixo no slider")
        fixo = page.locator('[data-service-id="WIR"]').first
        try:
            fixo.wait_for(state="visible", timeout=6000)
        except Exception:
            pass
        if fixo.count() == 0:
            self.logger.log("warn", "Item Vivo Fixo nao encontrado no slider")
            return False
        if not BrowserActions.click_with_fallback(fixo, timeout_ms=5000):
            self.logger.log("warn", "Falha ao clicar em Vivo Fixo")
            return False
        page.wait_for_timeout(config.wait_ms)
        self.logger.log("ok", "Contexto Vivo Fixo selecionado")
        return True


# ---------------------------------------------------------------------------
# FixoDownloadService
# ---------------------------------------------------------------------------

class FixoDownloadService:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger
        self.panel = DownloadPanelService(logger)

    def _clicar_ver_detalhes(self, page: Any, sec_locator: Any) -> bool:
        btn = sec_locator.locator("button[data-test-detail-button]").first
        if btn.count() == 0:
            return True  # já expandido
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        if not BrowserActions.click_with_fallback(btn, timeout_ms=5000):
            self.logger.log("warn", "Falha ao clicar Ver detalhes")
            return False
        page.wait_for_timeout(800)
        self.logger.log("info", "Ver detalhes clicado")
        return True

    def _obter_opcoes_no_slide(self, page: Any) -> list[dict[str, Any]]:
        """Retorna lista de {toggle, referencia} das linhas de fatura no slide aberto.

        Exclui o botão 'Baixar agora' (seção) e extrai 'Mês referência' (ex: Fev/2026)
        de cada linha via traversal DOM.
        """
        todos_dd = page.locator("[data-test-drop-down]").all()
        opcoes = []
        for dd in todos_dd:
            toggle = dd.locator("button.dropdown-toggle").first
            if toggle.count() == 0:
                continue
            try:
                texto = toggle.inner_text(timeout=1000) or ""
            except Exception:
                texto = ""
            if "agora" in texto.lower():
                continue

            referencia = ""
            situacao = ""
            try:
                result = dd.evaluate("""el => {
                    const monthRe = /^[A-Za-z\u00C0-\u017F]{3}\/\d{4}$/;
                    const dateRe = /^\d{2}\/\d{2}\/\d{4}$/;
                    let referencia = '';
                    let vencimento = '';
                    let situacao = '';
                    let node = el;
                    for (let depth = 0; depth < 12; depth++) {
                        node = node.parentElement;
                        if (!node || node.tagName === 'BODY') break;
                        const cls = node.className || '';
                        if (!situacao) {
                            if (cls.includes('paid-grid-row') || cls.includes('invoice-paid'))
                                situacao = 'Paga';
                            else if (cls.includes('open-grid-row') || cls.includes('invoice-open'))
                                situacao = 'Aberta';
                            else if (cls.includes('overdue-grid-row') || cls.includes('invoice-overdue'))
                                situacao = 'Vencida';
                        }
                        if (!referencia) {
                            for (const child of node.children) {
                                if (child.contains(el)) continue;
                                const t = (child.textContent || '').trim();
                                if (monthRe.test(t)) { referencia = t; break; }
                                if (!vencimento && dateRe.test(t)) { vencimento = t; }
                                for (const gc of child.children) {
                                    const gt = (gc.textContent || '').trim();
                                    if (monthRe.test(gt)) { referencia = gt; break; }
                                    if (!vencimento && dateRe.test(gt)) { vencimento = gt; }
                                }
                                if (referencia) break;
                            }
                        }
                        if (referencia && situacao) break;
                    }
                    return { referencia: referencia || vencimento, situacao };
                }""") or {}
                referencia = result.get("referencia", "") if isinstance(result, dict) else ""
                situacao = result.get("situacao", "") if isinstance(result, dict) else ""
            except Exception:
                pass

            # Normaliza capitalização: "fev/2026" → "Fev/2026"
            if referencia and referencia[0].islower():
                referencia = referencia[0].upper() + referencia[1:]
            opcoes.append({"toggle": toggle, "referencia": referencia, "situacao": situacao})

        return opcoes

    def _recarregar_e_abrir_secao(
        self,
        page: Any,
        config: "FixoConfig",
        codigo_cliente: str,
    ) -> "tuple[Any | None, list[Any]]":
        """Recarrega /sec/invoices, re-encontra a seção e expande os toggles."""
        self.logger.log("info", "Recarregando pagina para retry", conta=codigo_cliente)
        try:
            page.goto(config.invoices_url, wait_until="domcontentloaded")
            page.wait_for_timeout(config.wait_ms)
        except Exception as exc:
            self.logger.log("warn", "Erro ao recarregar", erro=str(exc))
            return None, []

        sec_locator = None
        try:
            sections = page.locator("section.mve-grid").all()
            for s in sections:
                el = s.locator("[data-test-secondary-info] span").first
                if el.count() > 0 and codigo_cliente in (el.inner_text(timeout=1500) or ""):
                    sec_locator = s
                    break
        except Exception:
            return None, []

        if not sec_locator:
            self.logger.log("warn", "Secao nao encontrada apos reload", conta=codigo_cliente)
            return None, []

        self.panel.minimizar(page)
        if not self._clicar_ver_detalhes(page, sec_locator):
            return None, []

        page.wait_for_timeout(config.wait_ms // 2)
        opcoes = self._obter_opcoes_no_slide(page)
        return sec_locator, opcoes

    def _baixar_boleto_por_linha(
        self,
        page: Any,
        toggle: Any,
        codigo_cliente: str,
        cnpj: str,
        idx_linha: int,
        referencia: str,
        situacao: str,
        config: FixoConfig,
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        # Verifica se PDF já existe
        ref_yyyymm = referencia_para_yyyymm(referencia) if referencia else ""
        if not ref_yyyymm and referencia:
            ano, mes = ano_mes_por_vencimento(referencia)
            if ano != "0000":
                ref_yyyymm = f"{ano}{mes}"
        pdf_existente = buscar_pdf_existente(config.download_dir, "vivo-fixo", cnpj, codigo_cliente, ref_yyyymm) if ref_yyyymm else None
        if pdf_existente and not config.force:
            self.logger.log("info", "PDF ja existe, pulando download", arquivo=pdf_existente.name)
            runtime["downloads_ok"] = int(runtime.get("downloads_ok", 0)) + 1
            return {
                "codigo_cliente": codigo_cliente,
                "referencia": referencia,
                "situacao": situacao,
                "download_ok": True,
                "arquivo_download": str(pdf_existente),
                "erro_download": "",
                "coleta_data_hora": config.coleta_data_hora,
            }

        self.panel.minimizar(page)
        try:
            toggle.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        if not BrowserActions.click_with_fallback(toggle, timeout_ms=5000):
            return self._resultado_falha(codigo_cliente, referencia, situacao, config, "Falha ao abrir dropdown Baixar da linha")
        page.wait_for_timeout(400)

        self.panel.minimizar(page)
        btn_boleto = page.locator('div.dropdown.show button[data-e2e-download-bills="invoice"]').first
        try:
            btn_boleto.wait_for(state="visible", timeout=4000)
        except Exception:
            btn_boleto = page.locator('button[data-e2e-download-bills="invoice"]').first
        if btn_boleto.count() == 0:
            return self._resultado_falha(codigo_cliente, referencia, situacao, config, "Botao Boleto (.pdf) nao encontrado")

        self.logger.log("info", "Solicitando Boleto (.pdf)", linha=idx_linha, referencia=referencia)
        tmp, erro = self.panel.aguardar_download(
            page, btn_boleto, label=f"{codigo_cliente}/{referencia}"
        )

        if not tmp or erro:
            runtime["downloads_falhos"] = int(runtime.get("downloads_falhos", 0)) + 1
            self.logger.log("warn", "Falha no download boleto", erro=erro, linha=idx_linha)
            return self._resultado_falha(codigo_cliente, referencia, situacao, config, erro or "Download nao capturado")

        cnpj_seguro = normalize_document(cnpj) or "semcnpj"
        cod_seguro = re.sub(r"\D", "", codigo_cliente) or "semconta"
        ref_segura = ref_yyyymm or f"linha{idx_linha:02d}"
        base = f"vivo-fixo-{cnpj_seguro}-{cod_seguro}-{ref_segura}"
        target = config.download_dir / f"{base}.pdf"
        target.write_bytes(tmp.read_bytes())
        runtime["downloads_ok"] = int(runtime.get("downloads_ok", 0)) + 1
        self.logger.log("ok", "Boleto PDF salvo", arquivo=target.name)
        return {
            "codigo_cliente": codigo_cliente,
            "referencia": referencia,
            "situacao": situacao,
            "download_ok": True,
            "arquivo_download": str(target.resolve()),
            "erro_download": "",
            "coleta_data_hora": config.coleta_data_hora,
        }

    def _resultado_falha(
        self,
        codigo_cliente: str,
        referencia: str,
        situacao: str,
        config: FixoConfig,
        erro: str,
    ) -> dict[str, Any]:
        return {
            "codigo_cliente": codigo_cliente,
            "referencia": referencia,
            "situacao": situacao,
            "download_ok": False,
            "arquivo_download": "",
            "erro_download": erro,
            "coleta_data_hora": config.coleta_data_hora,
        }

    def _retry_download(
        self,
        page: Any,
        config: FixoConfig,
        runtime: dict[str, Any],
        cnpj: str,
        codigo_cliente: str,
        idx: int,
        referencia: str,
        situacao: str,
    ) -> "dict[str, Any] | None":
        """Retry: recarrega página, re-abre secao e re-tenta download pelo índice."""
        _, opcoes_retry = self._recarregar_e_abrir_secao(page, config, codigo_cliente)
        if not opcoes_retry or idx - 1 >= len(opcoes_retry):
            return None
        opcao = opcoes_retry[idx - 1]
        ref_retry = opcao.get("referencia") or referencia
        return self._baixar_boleto_por_linha(
            page, opcao["toggle"], codigo_cliente, cnpj, idx,
            ref_retry, situacao, config, runtime,
        )

    def _baixar_com_retry(
        self,
        page: Any,
        toggle: Any,
        codigo_cliente: str,
        cnpj: str,
        idx: int,
        referencia: str,
        situacao: str,
        config: FixoConfig,
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        resultado = self._baixar_boleto_por_linha(
            page, toggle, codigo_cliente, cnpj, idx, referencia, situacao, config, runtime
        )
        for tentativa in range(1, 4):
            if resultado["download_ok"]:
                break
            self.logger.log("warn", f"Retry {tentativa}/3", referencia=referencia, conta=codigo_cliente)
            r = self._retry_download(page, config, runtime, cnpj, codigo_cliente, idx, referencia, situacao)
            if r is not None:
                resultado = r
            else:
                break
        return resultado

    def baixar_todos(
        self,
        page: Any,
        secoes: list[dict[str, Any]],
        config: FixoConfig,
        runtime: dict[str, Any],
    ) -> list[dict[str, Any]]:
        resultados: list[dict[str, Any]] = []
        cnpj = str(runtime.get("cnpj_cliente", "") or config.cnpj_inicial or "semcnpj")

        for sec in secoes:
            sec_locator = sec["_sec_locator"]
            codigo_cliente = sec.get("codigo_cliente", "")
            faturas_sec = sec.get("faturas", [])

            self.panel.minimizar(page)
            if not self._clicar_ver_detalhes(page, sec_locator):
                resultados.append(self._resultado_falha(codigo_cliente, "", "", config, "Falha ao expandir Ver detalhes"))
                continue

            page.wait_for_timeout(config.wait_ms // 2)
            opcoes = self._obter_opcoes_no_slide(page)
            limite = config.limite
            opcoes_para_baixar = opcoes if limite is None else opcoes[:limite]
            self.logger.log(
                "info", "Faturas para download",
                total_disponiveis=len(opcoes),
                baixando=len(opcoes_para_baixar),
                conta=codigo_cliente,
            )

            if not opcoes:
                resultados.append(self._resultado_falha(codigo_cliente, "", "", config, "Nenhuma linha de download encontrada"))
                continue

            for idx, opcao in enumerate(opcoes_para_baixar, start=1):
                fatura = faturas_sec[idx - 1] if idx - 1 < len(faturas_sec) else {}
                referencia = opcao.get("referencia") or fatura.get("referencia", "")
                situacao = fatura.get("situacao", "") or opcao.get("situacao", "")
                runtime["tentativas"] = int(runtime.get("tentativas", 0)) + 1
                resultados.append(self._baixar_com_retry(
                    page, opcao["toggle"], codigo_cliente, cnpj, idx,
                    referencia, situacao, config, runtime,
                ))
                page.wait_for_timeout(500)

        return resultados


# ---------------------------------------------------------------------------
# App principal Fixo
# ---------------------------------------------------------------------------

class VivoFixoApp(BaseVivoApp):
    PREFIXO_RESULTADO = "vivo_fixo_resultado"
    PDF_PREFIXO = "vivo-fixo"
    TITULO_TABELA = "LISTAGEM DE FATURAS VIVO FIXO"

    def __init__(self, config: FixoConfig) -> None:
        super().__init__(config)
        self.context_service = ContextSwitchService(self.logger)
        self.download_service = FixoDownloadService(self.logger)
        self.runtime["contexto_fixo_ok"] = False

    def _extra_result(self) -> dict[str, Any]:
        return {"contexto_fixo_ok": self.runtime.get("contexto_fixo_ok", False)}

    def _pos_login(self, page: Any) -> None:
        if self.config.dashboard_url not in (page.url or ""):
            self.logger.log("info", "Navegando ao dashboard explicitamente")
            try:
                page.goto(self.config.dashboard_url, wait_until="domcontentloaded")
                page.wait_for_timeout(self.config.wait_ms)
            except Exception as exc:
                self.logger.log("warn", "Falha ao navegar ao dashboard", erro=str(exc))
        try:
            page.locator("#service-select-desktop").first.wait_for(state="visible", timeout=20000)
        except Exception:
            pass
        ok = self.context_service.selecionar_vivo_fixo(page, self.config)
        self.runtime["contexto_fixo_ok"] = ok
        if not ok:
            self.logger.log("warn", "Troca para Vivo Fixo falhou")

    def _coletar_secoes(self, page: Any) -> list[dict[str, Any]]:
        secoes = _extrair_secoes_fixo(page, self.config, self.logger)
        self.logger.log("info", "Secoes fixo coletadas", total=len(secoes))
        return secoes

    def _popular_faturas(self, secoes: list[dict[str, Any]]) -> None:
        for sec in secoes:
            for fat in sec.get("faturas", []):
                self.runtime["faturas_disponiveis"].append({
                    "codigo_cliente": sec.get("codigo_cliente", ""),
                    "linha_titulo": sec.get("linha_titulo", ""),
                    "valor": fat.get("valor", ""),
                    "referencia": fat.get("referencia", ""),
                    "situacao": fat.get("situacao", ""),
                    "coleta_data_hora": self.config.coleta_data_hora,
                })

    def _baixar_ou_listar(self, page: Any, secoes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.download_service.baixar_todos(page, secoes, self.config, self.runtime)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download de faturas Vivo Fixo via portal Vivo Empresas"
    )
    add_common_args(parser)
    parser.set_defaults(mode="headless")
    parser.add_argument(
        "--limite", type=int, default=2, metavar="N",
        help="Numero maximo de faturas para baixar por conta (default: 2)",
    )
    parser.add_argument(
        "--todas", action="store_true",
        help="Baixar todas as faturas disponiveis (ignora --limite)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Forca re-download mesmo se o PDF ja existir",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> FixoConfig:
    output_dir, download_dir = build_common_dirs(args)
    limite = None if getattr(args, "todas", False) else args.limite
    return FixoConfig(
        url=args.url,
        dashboard_url=args.dashboard_url,
        invoices_url=args.invoices_url,
        cpf_ou_cnpj=normalize_document(args.cpf),
        password=args.password or "",
        output_dir=output_dir,
        download_dir=download_dir,
        wait_ms=args.wait_ms,
        timeout_ms=args.timeout_ms,
        debug=args.debug,
        listar=args.listar,
        mode=resolve_mode(args),
        coleta_dt=coleta_data_hora_gmt_menos3(),
        limite=limite,
        force=getattr(args, "force", False),
        engine=resolve_engine(args),
        spike_login=resolve_spike_login(args),
        warmup_ms=resolve_warmup_ms(args),
    )


def main() -> None:
    carregar_env_arquivo(Path(".env"))
    args = parse_args()
    args.cpf, args.password = validar_credenciais(args.cpf, args.password)
    if not args.cpf or not args.password:
        raise SystemExit(1)
    VivoFixoApp(build_config(args)).run()


if __name__ == "__main__":
    main()
