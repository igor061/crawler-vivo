#!/usr/bin/env python3
"""vivo_fixo.py — Download de faturas Vivo Fixo via portal Vivo Empresas.

Usa vivo_core.py para login, browser, debug e utilitários compartilhados.
"""

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vivo_core import (
    DEFAULT_DASHBOARD_URL,
    DEFAULT_INVOICES_URL,
    DEFAULT_URL,
    AuthService,
    BaseConfig,
    BrowserActions,
    DebugCollector,
    Logger,
    NamingService,
    ResultService,
    add_common_args,
    build_common_dirs,
    carregar_env_arquivo,
    coleta_data_hora_gmt_menos3,
    enriquecer_com_extrator,
    executar_fetch,
    extrair_cnpj_da_pagina,
    imprimir_resumo_telefones,
    normalize_document,
    resolve_mode,
)


# ---------------------------------------------------------------------------
# Config Fixo
# ---------------------------------------------------------------------------

@dataclass
class FixoConfig(BaseConfig):
    limite: int | None = 2

    @property
    def debug_dir(self) -> Path:
        stamp = self.coleta_dt.strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"debug_fixo_{stamp}"


# ---------------------------------------------------------------------------
# Helper: referência → YYYYMM
# ---------------------------------------------------------------------------

_MESES_PT: dict[str, str] = {
    "jan": "01", "fev": "02", "mar": "03", "abr": "04",
    "mai": "05", "jun": "06", "jul": "07", "ago": "08",
    "set": "09", "out": "10", "nov": "11", "dez": "12",
}


def referencia_para_yyyymm(referencia: str) -> str:
    """Converte 'Fev/2026', 'Fevereiro/26' ou 'Fev/26' → '202602'. Retorna '' se não parsear."""
    m = re.match(r"([A-Za-zÀ-ú]+)[/\-](\d{2,4})", referencia.strip())
    if not m:
        return ""
    mes_str = m.group(1)[:3].lower()
    mes_str = mes_str.replace("á", "a").replace("ã", "a").replace("é", "e").replace("ê", "e")
    ano_raw = m.group(2)
    ano = f"20{ano_raw}" if len(ano_raw) == 2 else ano_raw
    mes_num = _MESES_PT.get(mes_str, "")
    if not mes_num:
        return ""
    return f"{ano}{mes_num}"


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
                    if "PAID" in cls.upper():
                        situacao = "Paga"
                    elif "OPEN" in cls.upper() or "DUE" in cls.upper():
                        situacao = "Aberta"
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

    def _minimizar_painel(self, page: Any) -> None:
        """Minimiza o painel 'Download de arquivos' e rola a página ao topo."""
        try:
            btn = page.locator('[data-test-minimize-dialog]').first
            if btn.count() > 0:
                cls = btn.get_attribute("class") or ""
                if "opened" in cls:
                    BrowserActions.click_with_fallback(btn, timeout_ms=3000)
                    page.wait_for_timeout(300)
                    self.logger.log("info", "Painel de download minimizado")
        except Exception:
            pass
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
        except Exception:
            pass

    def _maximizar_painel(self, page: Any) -> None:
        try:
            btn = page.locator('[data-test-maximize-dialog]').first
            if btn.count() > 0:
                cls = btn.get_attribute("class") or ""
                if "opened" not in cls:
                    BrowserActions.click_with_fallback(btn, timeout_ms=3000)
                    page.wait_for_timeout(400)
                    self.logger.log("info", "Painel de download reaberto")
        except Exception:
            pass

    def _clicar_ver_detalhes(self, page: Any, sec_locator: Any) -> bool:
        btn = sec_locator.locator('button[data-test-detail-button]').first
        if btn.count() == 0:
            return True
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

    def _obter_toggles_por_linha(self, page: Any) -> list[Any]:
        """Retorna os toggles 'Baixar' por linha (exclui 'Baixar agora' da seção).
        Busca no escopo da página inteira pois as linhas de detalhe renderizam fora de section.mve-grid.
        """
        todos = page.locator("[data-test-drop-down] button.dropdown-toggle").all()
        por_linha = []
        for toggle in todos:
            try:
                texto = toggle.inner_text(timeout=1000) or ""
            except Exception:
                texto = ""
            if "agora" not in texto.lower():
                por_linha.append(toggle)
        return por_linha

    def _aguardar_e_baixar_do_painel(
        self,
        page: Any,
        codigo_cliente: str,
        tipo: str = "Boleto",
        timeout_ms: int = 120000,
    ) -> "tuple[Path | None, str]":
        import time
        self._maximizar_painel(page)
        self.logger.log("info", "Aguardando item no painel", conta=codigo_cliente, tipo=tipo)
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            items = page.locator("li.download-item").all()
            for item in items:
                try:
                    texto = item.inner_text(timeout=1000).strip()
                except Exception:
                    continue
                if not texto.startswith(codigo_cliente):
                    continue
                if tipo not in texto:
                    continue
                dl_btn = item.locator('[data-download-available]').first
                if dl_btn.count() == 0:
                    continue
                self.logger.log("ok", "Item disponivel no painel, baixando", texto=texto[:60])
                try:
                    with page.expect_download(timeout=30000) as dl_info:
                        BrowserActions.click_with_fallback(dl_btn, timeout_ms=5000)
                    dl = dl_info.value
                    return Path(dl.path()), ""
                except Exception as exc:
                    return None, str(exc)
            page.wait_for_timeout(3000)
        return None, f"Timeout aguardando download-available ({tipo}) para {codigo_cliente}"

    def _baixar_boleto_por_linha(
        self,
        page: Any,
        toggle: Any,
        codigo_cliente: str,
        cnpj: str,
        idx_linha: int,
        referencia: str,
        config: FixoConfig,
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        self._minimizar_painel(page)
        try:
            toggle.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        if not BrowserActions.click_with_fallback(toggle, timeout_ms=5000):
            return {
                "codigo_cliente": codigo_cliente,
                "linha_idx": idx_linha,
                "referencia": referencia,
                "download_ok": False,
                "arquivo_download": "",
                "erro_download": "Falha ao abrir dropdown Baixar da linha",
                "coleta_data_hora": config.coleta_data_hora,
            }
        page.wait_for_timeout(400)

        self._minimizar_painel(page)
        btn_boleto = page.locator('div.dropdown.show button[data-e2e-download-bills="invoice"]').first
        try:
            btn_boleto.wait_for(state="visible", timeout=4000)
        except Exception:
            btn_boleto = page.locator('button[data-e2e-download-bills="invoice"]').first
        if btn_boleto.count() == 0:
            return {
                "codigo_cliente": codigo_cliente,
                "linha_idx": idx_linha,
                "referencia": referencia,
                "download_ok": False,
                "arquivo_download": "",
                "erro_download": "Botao Boleto (.pdf) nao encontrado",
                "coleta_data_hora": config.coleta_data_hora,
            }

        self.logger.log("info", "Solicitando Boleto (.pdf)", linha=idx_linha, referencia=referencia)

        tmp: Path | None = None
        erro = ""
        try:
            with page.expect_download(timeout=30000) as dl_info:
                BrowserActions.click_with_fallback(btn_boleto, timeout_ms=5000)
            dl = dl_info.value
            tmp = Path(dl.path())
            self.logger.log("ok", "Download direto capturado", linha=idx_linha)
        except Exception:
            self.logger.log("info", "Download direto nao disparado, verificando painel", linha=idx_linha)
            tmp, erro = self._aguardar_e_baixar_do_painel(
                page, codigo_cliente, tipo="Boleto (.pdf)", timeout_ms=60000
            )

        if not tmp or erro:
            runtime["downloads_falhos"] = int(runtime.get("downloads_falhos", 0)) + 1
            self.logger.log("warn", "Falha no download boleto", erro=erro, linha=idx_linha)
            return {
                "codigo_cliente": codigo_cliente,
                "linha_idx": idx_linha,
                "referencia": referencia,
                "download_ok": False,
                "arquivo_download": "",
                "erro_download": erro or "Download nao capturado",
                "coleta_data_hora": config.coleta_data_hora,
            }

        # Salva com nome padronizado: vivo-fixo-{cnpj}-{conta}-{YYYYMM}.pdf
        cnpj_seguro = normalize_document(cnpj) or "semcnpj"
        cod_seguro = re.sub(r"\D", "", codigo_cliente) or "semconta"
        ref_segura = referencia_para_yyyymm(referencia) if referencia else ""
        if not ref_segura:
            ref_segura = f"linha{idx_linha:02d}"
        base = f"vivo-fixo-{cnpj_seguro}-{cod_seguro}-{ref_segura}"
        target = config.download_dir / f"{base}.pdf"
        counter = 2
        while target.exists():
            target = config.download_dir / f"{base}-{counter}.pdf"
            counter += 1
        target.write_bytes(tmp.read_bytes())
        runtime["downloads_ok"] = int(runtime.get("downloads_ok", 0)) + 1
        self.logger.log("ok", "Boleto PDF salvo", arquivo=target.name)
        return {
            "codigo_cliente": codigo_cliente,
            "linha_idx": idx_linha,
            "referencia": referencia,
            "download_ok": True,
            "arquivo_download": str(target.resolve()),
            "erro_download": "",
            "coleta_data_hora": config.coleta_data_hora,
        }

    def baixar_todos(
        self,
        page: Any,
        secoes: list[dict[str, Any]],
        config: FixoConfig,
        runtime: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if config.listar:
            self.logger.log("info", "Modo listar ativo, download desabilitado")
            return []

        resultados: list[dict[str, Any]] = []
        cnpj = str(runtime.get("cnpj_cliente", "") or config.cnpj_inicial or "semcnpj")

        for sec in secoes:
            sec_locator = sec["_sec_locator"]
            codigo_cliente = sec.get("codigo_cliente", "")
            faturas_sec = sec.get("faturas", [])

            self._minimizar_painel(page)
            if not self._clicar_ver_detalhes(page, sec_locator):
                resultados.append({
                    "codigo_cliente": codigo_cliente,
                    "download_ok": False,
                    "arquivo_download": "",
                    "erro_download": "Falha ao expandir Ver detalhes",
                    "coleta_data_hora": config.coleta_data_hora,
                })
                continue

            page.wait_for_timeout(config.wait_ms // 2)

            toggles = self._obter_toggles_por_linha(page)
            limite = config.limite
            toggles_para_baixar = toggles if limite is None else toggles[:limite]
            self.logger.log(
                "info", "Faturas para download",
                total_disponiveis=len(toggles),
                baixando=len(toggles_para_baixar),
                conta=codigo_cliente,
            )

            if not toggles:
                resultados.append({
                    "codigo_cliente": codigo_cliente,
                    "download_ok": False,
                    "arquivo_download": "",
                    "erro_download": "Nenhuma linha de download encontrada apos Ver detalhes",
                    "coleta_data_hora": config.coleta_data_hora,
                })
                continue

            for idx, toggle in enumerate(toggles_para_baixar, start=1):
                referencia = (
                    faturas_sec[idx - 1].get("referencia", "")
                    if idx - 1 < len(faturas_sec) else ""
                )
                runtime["tentativas"] = int(runtime.get("tentativas", 0)) + 1
                resultado = self._baixar_boleto_por_linha(
                    page, toggle, codigo_cliente, cnpj, idx, referencia, config, runtime
                )
                resultados.append(resultado)
                page.wait_for_timeout(500)

        return resultados


# ---------------------------------------------------------------------------
# App principal Fixo
# ---------------------------------------------------------------------------

class VivoFixoApp:
    def __init__(self, config: FixoConfig) -> None:
        self.config = config
        self.logger = Logger()
        self.debug = DebugCollector(config, self.logger)
        self.auth_service = AuthService(self.logger)
        self.context_service = ContextSwitchService(self.logger)
        self.download_service = FixoDownloadService(self.logger)
        self.result_service = ResultService(NamingService())
        self.runtime: dict[str, Any] = {
            "campo_senha_detectado": False,
            "senha_enviada": False,
            "dashboard_detectado": False,
            "contexto_fixo_ok": False,
            "faturas_aberto": False,
            "tentativas": 0,
            "downloads_ok": 0,
            "downloads_falhos": 0,
            "faturas_disponiveis": [],
            "cnpj_cliente": config.cnpj_inicial,
            "erro_execucao": "",
            "debug_paginas": 0,
        }

    def run(self) -> Path:
        response = executar_fetch(self.config, self._page_action, self.runtime)
        result_file = self.result_service.salvar_resultado(
            response, self.config, self.runtime,
            extra={
                "contexto_fixo_ok": self.runtime["contexto_fixo_ok"],
                "tentativas": self.runtime["tentativas"],
            },
            prefixo="vivo_fixo_resultado",
        )
        self.logger.log("ok", "Execucao finalizada", status=response.status)
        self.logger.log("ok", "Resumo", downloads_ok=self.runtime["downloads_ok"])
        self.logger.log("ok", "Resumo", downloads_falhos=self.runtime["downloads_falhos"])
        imprimir_resumo_telefones(self.runtime["faturas_disponiveis"], self.logger)
        self.logger.log("ok", "Resultado salvo", arquivo=result_file)
        return result_file

    def _page_action(self, page: Any) -> Any:
        self.logger.log("info", "Pagina inicial", url=self.config.url)
        page.wait_for_timeout(self.config.wait_ms)
        self.debug.capture(page, "01_login_page", self.runtime)

        if not self.auth_service.executar_login(page, self.config, self.runtime):
            self.logger.log("warn", "Login falhou")
            return page

        if self.config.dashboard_url not in (page.url or ""):
            self.logger.log("info", "Navegando ao dashboard explicitamente")
            try:
                page.goto(self.config.dashboard_url, wait_until="domcontentloaded")
                page.wait_for_timeout(self.config.wait_ms)
            except Exception as exc:
                self.logger.log("warn", "Falha ao navegar ao dashboard", erro=str(exc))

        try:
            page.locator("#service-select-desktop").first.wait_for(
                state="visible", timeout=20000
            )
        except Exception:
            pass
        self.debug.capture(page, "02_apos_login", self.runtime)

        ok = self.context_service.selecionar_vivo_fixo(page, self.config)
        self.runtime["contexto_fixo_ok"] = ok
        if not ok:
            self.logger.log("warn", "Troca para Vivo Fixo falhou")
        self.debug.capture(page, "03_contexto_fixo", self.runtime)

        try:
            page.goto(self.config.invoices_url, wait_until="domcontentloaded")
            page.wait_for_timeout(self.config.wait_ms)
            self.runtime["faturas_aberto"] = "/sec/invoices" in (page.url or "")
        except Exception as exc:
            self.logger.log("erro", "Falha ao abrir faturas", erro=str(exc))
            return page

        cnpj = extrair_cnpj_da_pagina(page.content())
        if cnpj:
            self.runtime["cnpj_cliente"] = cnpj
            self.logger.log("ok", "CNPJ extraido", cnpj=cnpj)
        self.debug.capture(page, "04_faturas", self.runtime)

        secoes = _extrair_secoes_fixo(page, self.config, self.logger)
        self.logger.log("info", "Secoes fixo coletadas", total=len(secoes))

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

        resultados = self.download_service.baixar_todos(
            page, secoes, self.config, self.runtime
        )

        for item in resultados:
            enriquecer_com_extrator(item)

        if resultados:
            self.runtime["faturas_disponiveis"] = resultados

        self.debug.capture(page, "05_final", self.runtime)
        return page


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download de faturas Vivo Fixo via portal Vivo Empresas"
    )
    add_common_args(parser)
    parser.add_argument(
        "--limite", type=int, default=2, metavar="N",
        help="Numero maximo de faturas para baixar por conta (default: 2)",
    )
    parser.add_argument(
        "--todas", action="store_true",
        help="Baixar todas as faturas disponiveis (ignora --limite)",
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
    )


def main() -> None:
    carregar_env_arquivo(Path(".env"))
    args = parse_args()
    config = build_config(args)
    app = VivoFixoApp(config)
    app.run()


if __name__ == "__main__":
    main()
