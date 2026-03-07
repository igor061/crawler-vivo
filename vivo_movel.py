#!/usr/bin/env python3
"""vivo_movel.py — Download de contas detalhadas Vivo Móvel.

Fluxo: login → /sec/invoices → por conta: Exibir detalhes →
       por fatura: Opções → Conta detalhada e nota fiscal (.pdf)
"""

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vivo_core import (
    DEFAULT_DASHBOARD_URL,
    DEFAULT_INVOICES_URL,
    DEFAULT_URL,
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
    resolve_mode,
    validar_credenciais,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class MovelConfig(BaseConfig):
    limite: int | None = 2
    force: bool = False

    @property
    def debug_dir(self) -> Path:
        stamp = self.coleta_dt.strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"debug_movel_{stamp}"


# ---------------------------------------------------------------------------
# Coleta de metadados das seções Móvel
# ---------------------------------------------------------------------------

def _extrair_secoes_movel(page: Any, config: MovelConfig, logger: Logger) -> list[dict[str, Any]]:
    """Coleta metadados de todas as seções/contas da grade Vivo Móvel.

    Estrutura confirmada via HTML:
      section[data-test-invoices-line-grid]
        h4[aria-label^="Conta:"]                                           → número da conta
        .data-card-section__secondColumn .data-card-cell__description     → valor
        .data-card-section__thirdColumn  .data-card-cell__description     → vencimento
        .badge p                                                           → situacao
    """
    secoes: list[dict[str, Any]] = []

    try:
        page.locator("[data-test-invoices-line-grid]").first.wait_for(state="visible", timeout=15000)
    except Exception:
        logger.log("warn", "Grid data-test-invoices-line-grid nao encontrado")

    sections = page.locator("section[data-test-invoices-line-grid]").all()
    logger.log("info", f"Secoes encontradas: {len(sections)}")

    for sec in sections:
        codigo_cliente = ""
        try:
            el = sec.locator("h4[aria-label^='Conta:']").first
            if el.count() > 0:
                texto = el.inner_text(timeout=1500).strip()
                m = re.search(r"Conta:\s*(\S+)", texto)
                codigo_cliente = m.group(1) if m else texto
        except Exception:
            pass

        valor = ""
        try:
            el = sec.locator(".data-card-section__secondColumn .data-card-cell__description").first
            if el.count() > 0:
                valor = el.inner_text(timeout=1500).replace("\xa0", " ").strip()
        except Exception:
            pass

        vencimento = ""
        try:
            el = sec.locator(".data-card-section__thirdColumn .data-card-cell__description").first
            if el.count() > 0:
                vencimento = el.inner_text(timeout=1500).strip()
        except Exception:
            pass

        situacao = ""
        try:
            el = sec.locator(".badge p").first
            if el.count() > 0:
                situacao = el.inner_text(timeout=1500).strip()
        except Exception:
            pass

        ano, mes = ano_mes_por_vencimento(vencimento)
        referencia_yyyymm = f"{ano}{mes}" if ano != "0000" else referencia_para_yyyymm(vencimento)

        secoes.append({
            "codigo_cliente": codigo_cliente,
            "valor": valor,
            "vencimento": vencimento,
            "referencia": referencia_yyyymm,
            "situacao": situacao,
            "faturas": [{"valor": valor, "referencia": referencia_yyyymm, "situacao": situacao}],
            "_sec_locator": sec,
        })

    return secoes


# ---------------------------------------------------------------------------
# MovelDownloadService
# ---------------------------------------------------------------------------

class MovelDownloadService:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger
        self.panel = DownloadPanelService(logger)

    def _clicar_exibir_detalhes(self, page: Any, sec_locator: Any) -> bool:
        for seletor in [
            "[data-test-detail-button] button",
            "button[aria-label*='Exibir detalhes']",
            "button:has-text('Exibir detalhes')",
        ]:
            btn = sec_locator.locator(seletor).first
            if btn.count() == 0:
                continue
            try:
                btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            texto = ""
            try:
                texto = btn.inner_text(timeout=1000)
            except Exception:
                pass
            self.logger.log("info", "Clicando Exibir detalhes", texto=texto.strip())
            if not BrowserActions.click_with_fallback(btn, timeout_ms=5000):
                continue
            try:
                page.locator("[data-test-open-dropdown-button]").first.wait_for(
                    state="visible", timeout=8000
                )
            except Exception:
                page.wait_for_timeout(2000)
            self.logger.log("info", "Slide de detalhes aberto")
            return True
        self.logger.log("warn", "Botao Exibir detalhes nao encontrado")
        return False

    def _fechar_slide(self, page: Any) -> None:
        try:
            btn = page.locator("[data-test-slider-close-button]").first
            if btn.count() > 0:
                BrowserActions.click_with_fallback(btn, timeout_ms=3000)
                page.wait_for_timeout(500)
        except Exception:
            pass

    def _obter_opcoes_no_slide(self, page: Any) -> list[dict[str, Any]]:
        """Retorna lista de {toggle, vencimento, row, situacao} do slide aberto.

        Estrutura:
          [data-slider-root]
            datacardsection[data-test-card-section-content]
              p[aria-label="DD/MM/YYYY"]              → vencimento
              button[data-test-open-dropdown-button]  → "Opções"
        """
        slider = page.locator("[data-slider-root]").first
        if slider.count() == 0:
            self.logger.log("warn", "Slide nao encontrado")
            return []

        rows = slider.locator("datacardsection[data-test-card-section-content]").all()
        self.logger.log("info", "Linhas no slide", total=len(rows))
        opcoes = []
        for row in rows:
            toggle = row.locator("button[data-test-open-dropdown-button]").first
            if toggle.count() == 0:
                continue
            vencimento = ""
            try:
                el = row.locator("p[aria-label]").first
                if el.count() > 0:
                    val = el.get_attribute("aria-label", timeout=1000) or ""
                    if re.match(r"\d{2}/\d{2}/\d{4}", val):
                        vencimento = val
                    else:
                        vencimento = el.inner_text(timeout=1000).strip()
            except Exception:
                pass
            situacao = ""
            try:
                situacao = row.evaluate("""el => {
                    let node = el.parentElement;
                    while (node) {
                        const badge = node.querySelector('.data-card-badge');
                        if (badge) {
                            return badge.getAttribute('aria-label') || badge.innerText.trim();
                        }
                        if (node.hasAttribute('data-slider-root')) break;
                        node = node.parentElement;
                    }
                    return '';
                }""") or ""
            except Exception:
                pass
            opcoes.append({"toggle": toggle, "vencimento": vencimento, "row": row, "situacao": situacao})

        self.logger.log("info", "Opcoes encontradas", total=len(opcoes))
        return opcoes

    def _recarregar_e_abrir_secao(
        self, page: Any, config: MovelConfig, codigo_cliente: str
    ) -> "tuple[Any | None, list[dict[str, Any]]]":
        self.logger.log("info", "Recarregando pagina para retry", conta=codigo_cliente)
        try:
            page.goto(config.invoices_url, wait_until="domcontentloaded")
            page.wait_for_timeout(config.wait_ms)
        except Exception as exc:
            self.logger.log("warn", "Erro ao recarregar", erro=str(exc))
            return None, []

        sec_locator = None
        try:
            for s in page.locator("section[data-test-invoices-line-grid]").all():
                el = s.locator("h4[aria-label^='Conta:']").first
                if el.count() > 0 and codigo_cliente in (el.inner_text(timeout=1500) or ""):
                    sec_locator = s
                    break
        except Exception:
            return None, []

        if not sec_locator:
            self.logger.log("warn", "Secao nao encontrada apos reload", conta=codigo_cliente)
            return None, []

        self.panel.minimizar(page)
        if not self._clicar_exibir_detalhes(page, sec_locator):
            return None, []

        page.wait_for_timeout(config.wait_ms // 2)
        return sec_locator, self._obter_opcoes_no_slide(page)

    def _baixar_conta_detalhada(
        self,
        page: Any,
        toggle: Any,
        row_locator: Any,
        codigo_cliente: str,
        cnpj: str,
        idx: int,
        referencia: str,
        situacao: str,
        config: MovelConfig,
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        pdf_existente = buscar_pdf_existente(config.download_dir, "vivo-movel", cnpj, codigo_cliente, referencia)
        if pdf_existente and not config.force:
            self.logger.log("info", "PDF ja existe, pulando download", arquivo=pdf_existente.name)
            runtime["downloads_ok"] = int(runtime.get("downloads_ok", 0)) + 1
            return self._resultado(codigo_cliente, referencia, situacao, True, str(pdf_existente), "", config)

        self.panel.minimizar(page)
        try:
            toggle.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        if not BrowserActions.click_with_fallback(toggle, timeout_ms=5000):
            return self._resultado_falha(codigo_cliente, referencia, situacao, config, "Falha ao abrir dropdown")
        page.wait_for_timeout(600)

        btn_detalhe = None
        try:
            dropdown_root = row_locator.locator("[data-test-dropdown-root]").first
            for seletor in [
                "a[data-test-dropdown-list-item-link]:has-text('Conta detalhada')",
                "a[data-test-dropdown-list-item-link]:has-text('nota fiscal')",
                "[data-test-dropdown-list-item]:nth-child(2) a",
            ]:
                el = dropdown_root.locator(seletor).first
                if el.count() > 0:
                    self.logger.log("info", "Opcao encontrada", texto=el.inner_text(timeout=1000).strip())
                    btn_detalhe = el
                    break
        except Exception as exc:
            self.logger.log("warn", "Erro ao buscar opcao", erro=str(exc))

        if btn_detalhe is None:
            return self._resultado_falha(codigo_cliente, referencia, situacao, config, "Botao Conta detalhada nao encontrado")

        self.logger.log("info", "Solicitando Conta detalhada e NF (.pdf)", linha=idx)
        tmp, erro = self.panel.aguardar_download(page, btn_detalhe, label=f"{codigo_cliente}/{referencia}")

        if not tmp or erro:
            runtime["downloads_falhos"] = int(runtime.get("downloads_falhos", 0)) + 1
            return self._resultado_falha(codigo_cliente, referencia, situacao, config, erro or "Download nao capturado")

        cnpj_s = normalize_document(cnpj) or "semcnpj"
        cod_s = re.sub(r"\D", "", codigo_cliente) or "semconta"
        ref_s = referencia if referencia else f"linha{idx:02d}"
        target = config.download_dir / f"vivo-movel-{cnpj_s}-{cod_s}-{ref_s}.pdf"
        target.write_bytes(tmp.read_bytes())
        runtime["downloads_ok"] = int(runtime.get("downloads_ok", 0)) + 1
        self.logger.log("ok", "PDF salvo", arquivo=target.name)
        return self._resultado(codigo_cliente, referencia, situacao, True, str(target.resolve()), "", config)

    def _resultado(
        self,
        codigo_cliente: str,
        referencia: str,
        situacao: str,
        ok: bool,
        arquivo: str,
        erro: str,
        config: MovelConfig,
    ) -> dict[str, Any]:
        return {
            "codigo_cliente": codigo_cliente,
            "referencia": referencia,
            "situacao": situacao,
            "download_ok": ok,
            "arquivo_download": arquivo,
            "erro_download": erro,
            "coleta_data_hora": config.coleta_data_hora,
        }

    def _resultado_falha(
        self, codigo_cliente: str, referencia: str, situacao: str, config: MovelConfig, erro: str
    ) -> dict[str, Any]:
        return self._resultado(codigo_cliente, referencia, situacao, False, "", erro, config)

    def _retry_opcao(
        self, page: Any, config: MovelConfig, runtime: dict[str, Any],
        cnpj: str, codigo_cliente: str, referencia: str, situacao: str, idx: int,
    ) -> "dict[str, Any] | None":
        _, opcoes = self._recarregar_e_abrir_secao(page, config, codigo_cliente)
        if not opcoes:
            return None
        for o in opcoes:
            v = o["vencimento"]
            a, m = ano_mes_por_vencimento(v)
            ref = f"{a}{m}" if a != "0000" else referencia_para_yyyymm(v)
            if ref == referencia:
                return self._baixar_conta_detalhada(
                    page, o["toggle"], o["row"], codigo_cliente, cnpj,
                    idx, referencia, o.get("situacao", situacao), config, runtime,
                )
        self.logger.log("warn", "Opcao nao encontrada apos reload", referencia=referencia)
        return None

    def _processar_com_retry(
        self, page: Any, toggle: Any, row: Any,
        codigo_cliente: str, cnpj: str, idx: int,
        referencia: str, situacao: str, config: MovelConfig, runtime: dict[str, Any],
    ) -> dict[str, Any]:
        resultado = self._baixar_conta_detalhada(
            page, toggle, row, codigo_cliente, cnpj, idx, referencia, situacao, config, runtime
        )
        for tentativa in range(1, 4):
            if resultado["download_ok"]:
                break
            self.logger.log("warn", f"Retry {tentativa}/3", referencia=referencia, conta=codigo_cliente)
            r = self._retry_opcao(page, config, runtime, cnpj, codigo_cliente, referencia, situacao, idx)
            if r is not None:
                resultado = r
            else:
                break
        return resultado

    def baixar_todos(
        self,
        page: Any,
        secoes: list[dict[str, Any]],
        config: MovelConfig,
        runtime: dict[str, Any],
    ) -> list[dict[str, Any]]:
        resultados: list[dict[str, Any]] = []
        cnpj = str(runtime.get("cnpj_cliente", "") or config.cnpj_inicial or "semcnpj")

        for sec in secoes:
            sec_locator = sec["_sec_locator"]
            codigo_cliente = sec.get("codigo_cliente", "")
            self.panel.minimizar(page)

            if not self._clicar_exibir_detalhes(page, sec_locator):
                resultados.append(self._resultado_falha(codigo_cliente, "", "", config, "Falha ao expandir Exibir detalhes"))
                continue

            page.wait_for_timeout(config.wait_ms // 2)
            opcoes = self._obter_opcoes_no_slide(page)

            if config.listar:
                for idx, opcao in enumerate(opcoes, start=1):
                    vencimento = opcao["vencimento"]
                    situacao = opcao.get("situacao", "")
                    ano, mes = ano_mes_por_vencimento(vencimento)
                    referencia = f"{ano}{mes}" if ano != "0000" else referencia_para_yyyymm(vencimento)
                    pdf_existente = buscar_pdf_existente(config.download_dir, "vivo-movel", cnpj, codigo_cliente, referencia)
                    if not pdf_existente or config.force:
                        runtime["tentativas"] = int(runtime.get("tentativas", 0)) + 1
                        resultados.append(self._processar_com_retry(
                            page, opcao["toggle"], opcao["row"],
                            codigo_cliente, cnpj, idx, referencia, situacao, config, runtime,
                        ))
                    else:
                        resultados.append(self._resultado(
                            codigo_cliente, referencia, situacao, False, "", "", config
                        ))
                self._fechar_slide(page)
                continue

            limite = config.limite
            opcoes_para_baixar = opcoes if limite is None else opcoes[:limite]
            self.logger.log("info", "Faturas para download",
                total_disponiveis=len(opcoes), baixando=len(opcoes_para_baixar), conta=codigo_cliente)

            if not opcoes:
                resultados.append(self._resultado_falha(codigo_cliente, "", "", config, "Nenhum toggle encontrado no slide"))
                self._fechar_slide(page)
                continue

            for idx, opcao in enumerate(opcoes_para_baixar, start=1):
                vencimento = opcao["vencimento"]
                situacao = opcao.get("situacao", "")
                ano, mes = ano_mes_por_vencimento(vencimento)
                referencia = f"{ano}{mes}" if ano != "0000" else referencia_para_yyyymm(vencimento)
                runtime["tentativas"] = int(runtime.get("tentativas", 0)) + 1
                resultados.append(self._processar_com_retry(
                    page, opcao["toggle"], opcao["row"],
                    codigo_cliente, cnpj, idx, referencia, situacao, config, runtime,
                ))
                page.wait_for_timeout(500)

            self._fechar_slide(page)
            page.wait_for_timeout(500)

        if config.listar:
            self.logger.log("info", "Listagem concluida", total=len(resultados))

        return resultados


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class VivoMovelApp(BaseVivoApp):
    PREFIXO_RESULTADO = "vivo_movel_resultado"
    PDF_PREFIXO = "vivo-movel"
    TITULO_TABELA = "LISTAGEM DE FATURAS VIVO MOVEL"

    def __init__(self, config: MovelConfig) -> None:
        super().__init__(config)
        self.download_service = MovelDownloadService(self.logger)

    def _coletar_secoes(self, page: Any) -> list[dict[str, Any]]:
        secoes = _extrair_secoes_movel(page, self.config, self.logger)
        self.logger.log("info", "Secoes movel coletadas", total=len(secoes))
        return secoes

    def _popular_faturas(self, secoes: list[dict[str, Any]]) -> None:
        for sec in secoes:
            self.runtime["faturas_disponiveis"].append({
                "codigo_cliente": sec.get("codigo_cliente", ""),
                "valor": sec.get("valor", ""),
                "vencimento": sec.get("vencimento", ""),
                "referencia": sec.get("referencia", ""),
                "situacao": sec.get("situacao", ""),
                "coleta_data_hora": self.config.coleta_data_hora,
            })

    def _baixar_ou_listar(self, page: Any, secoes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.download_service.baixar_todos(page, secoes, self.config, self.runtime)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download de contas detalhadas Vivo Movel"
    )
    add_common_args(parser)
    parser.set_defaults(mode="headless")
    parser.add_argument("--limite", type=int, default=2, metavar="N",
        help="Numero maximo de faturas por conta (default: 2)")
    parser.add_argument("--todas", action="store_true",
        help="Baixar todas as faturas (ignora --limite)")
    parser.add_argument("--force", action="store_true",
        help="Forca re-download mesmo se o PDF ja existir")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> MovelConfig:
    output_dir, download_dir = build_common_dirs(args)
    return MovelConfig(
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
        limite=None if getattr(args, "todas", False) else args.limite,
        force=getattr(args, "force", False),
    )


def main() -> None:
    carregar_env_arquivo(Path(".env"))
    args = parse_args()
    args.cpf, args.password = validar_credenciais(args.cpf, args.password)
    if not args.cpf or not args.password:
        raise SystemExit(1)
    VivoMovelApp(build_config(args)).run()


if __name__ == "__main__":
    main()
