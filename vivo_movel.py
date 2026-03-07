#!/usr/bin/env python3
"""vivo_movel.py — Download de faturas Vivo Móvel via portal Vivo Empresas.

Usa vivo_core.py para login, browser, debug e utilitários compartilhados.
"""

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
# Config Móvel
# ---------------------------------------------------------------------------

@dataclass
class MovelConfig(BaseConfig):
    start_div_index: int = 2
    max_sections: int = 12
    max_div_index: int = 20

    @property
    def debug_dir(self) -> Path:
        stamp = self.coleta_dt.strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"debug_xpath_loop_{stamp}"


# ---------------------------------------------------------------------------
# Helpers de extração de dados de linha DOM
# ---------------------------------------------------------------------------

def montar_xpaths(section_idx: int, div_idx: int) -> tuple[str, str, str]:
    section_xpath = (
        f"/html/body/main/div/div/div/div/div[2]/div[2]/div/div[1]/section[{section_idx}]"
    )
    row_xpath = f"{section_xpath}/div/div[{div_idx}]"
    button_xpath = f"{row_xpath}/div[4]/div/div/div[2]/div/button"
    link_xpath = f"{row_xpath}/div[4]/div/div/div[2]/div/div/ul/li[1]/a"
    return row_xpath, button_xpath, link_xpath


def _extrair_dados_row(row: Any) -> tuple[str, str, str]:
    """Extrai (vencimento, valor, situacao) de uma linha DOM da grade Móvel."""
    vencimento = ""
    valor = ""
    situacao = ""
    try:
        due = row.locator(".data-card-section__thirdColumn p.data-card-cell__description").first
        if due.count() > 0:
            due_text = due.inner_text(timeout=1500)
            due_match = re.search(r"(\d{2}/\d{2}/\d{4})", due_text)
            vencimento = due_match.group(1) if due_match else ""
    except Exception:
        pass
    try:
        value_node = row.locator(
            ".data-card-section__secondColumn p.data-card-cell__description"
        ).first
        if value_node.count() > 0:
            valor = value_node.inner_text(timeout=1500).replace("\xa0", " ").strip()
    except Exception:
        pass
    try:
        status_node = row.locator(".badge p").first
        if status_node.count() > 0:
            situacao = status_node.inner_text(timeout=1500).strip()
    except Exception:
        pass
    return vencimento, valor, situacao


# ---------------------------------------------------------------------------
# InvoiceService
# ---------------------------------------------------------------------------

class InvoiceCollectionStrategy(Protocol):
    def collect(self, page: Any, config: MovelConfig) -> list[dict[str, Any]]: ...


class XPathInvoiceCollectionStrategy:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def collect(self, page: Any, config: MovelConfig) -> list[dict[str, Any]]:
        faturas: list[dict[str, Any]] = []
        for section_idx in range(1, config.max_sections + 1):
            section_xpath = (
                f"/html/body/main/div/div/div/div/div[2]/div[2]/div/div[1]/section[{section_idx}]"
            )
            section_locator = page.locator(f"xpath={section_xpath}").first
            if section_locator.count() == 0:
                if section_idx == 1:
                    continue
                break
            conta = self._extrair_conta_secao(page, section_xpath)
            for div_idx in range(config.start_div_index, config.max_div_index + 1):
                item = self._coletar_item_secao(page, config, section_idx, div_idx, conta)
                if item is None:
                    if self._fim_da_secao(page, section_idx, div_idx, config):
                        break
                    continue
                faturas.append(item)
                self.logger.log(
                    "info", "Fatura encontrada",
                    conta=item.get("conta", ""),
                    vencimento=item.get("vencimento", ""),
                    valor=item.get("valor", ""),
                    situacao=item.get("situacao", ""),
                )
        return faturas

    def _extrair_conta_secao(self, page: Any, section_xpath: str) -> str:
        try:
            title = page.locator(f"xpath={section_xpath}//h4[contains(@aria-label,'Conta')]").first
            if title.count() == 0:
                return ""
            text = title.inner_text(timeout=1500)
            match = re.search(r"(\d{8,})", text)
            return match.group(1) if match else ""
        except Exception:
            return ""

    def _fim_da_secao(self, page: Any, section_idx: int, div_idx: int, config: MovelConfig) -> bool:
        row_xpath, _, _ = montar_xpaths(section_idx, div_idx)
        row = page.locator(f"xpath={row_xpath}").first
        if row.count() == 0 and div_idx == config.start_div_index:
            return True
        return row.count() == 0

    def _coletar_item_secao(
        self,
        page: Any,
        config: MovelConfig,
        section_idx: int,
        div_idx: int,
        conta: str,
    ) -> dict[str, Any] | None:
        row_xpath, button_xpath, link_xpath = montar_xpaths(section_idx, div_idx)
        row = page.locator(f"xpath={row_xpath}").first
        if row.count() == 0:
            return None
        button = page.locator(f"xpath={button_xpath}").first
        if button.count() == 0:
            return None
        vencimento, valor, situacao = _extrair_dados_row(row)
        return {
            "_xpath_botao": button_xpath,
            "_xpath_link": link_xpath,
            "conta": conta,
            "vencimento": vencimento,
            "valor": valor,
            "situacao": situacao,
            "download_ok": False,
            "arquivo_download": "",
            "erro_download": "",
            "codigo_de_barras": "",
            "codigo_de_barras_sem_espaco": "",
            "coleta_data_hora": config.coleta_data_hora,
        }


class InvoiceService:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger
        self.strategies: list[InvoiceCollectionStrategy] = [
            XPathInvoiceCollectionStrategy(logger)
        ]

    def abrir_faturas(self, page: Any, config: MovelConfig, runtime: dict[str, Any]) -> bool:
        try:
            page.goto(config.invoices_url, wait_until="domcontentloaded")
            runtime["faturas_aberto"] = "/sec/invoices" in (page.url or "")
            page.wait_for_timeout(config.wait_ms)
        except Exception:
            runtime["faturas_aberto"] = False
            self.logger.log("erro", "Falha ao abrir pagina de faturas")
            return False
        self.logger.log("ok", "Pagina de faturas aberta", url=page.url)
        cnpj_pagina = extrair_cnpj_da_pagina(page.content())
        if cnpj_pagina:
            runtime["cnpj_cliente"] = cnpj_pagina
            self.logger.log("ok", "CNPJ extraido da pagina", cnpj=cnpj_pagina)
        return True

    def coletar_faturas(self, page: Any, config: MovelConfig) -> list[dict[str, Any]]:
        agregadas: list[dict[str, Any]] = []
        vistos: set[tuple[str, str, str, str]] = set()
        for strategy in self.strategies:
            for item in strategy.collect(page, config):
                chave = (
                    str(item.get("conta", "")),
                    str(item.get("vencimento", "")),
                    str(item.get("valor", "")),
                    str(item.get("situacao", "")),
                )
                if chave not in vistos:
                    vistos.add(chave)
                    agregadas.append(item)
        return agregadas


# ---------------------------------------------------------------------------
# DownloadService Móvel
# ---------------------------------------------------------------------------

class DownloadService:
    def __init__(self, logger: Logger, naming: NamingService) -> None:
        self.logger = logger
        self.naming = naming

    def baixar_faturas(
        self,
        page: Any,
        faturas: list[dict[str, Any]],
        config: MovelConfig,
        runtime: dict[str, Any],
    ) -> None:
        if config.listar:
            self.logger.log("info", "Modo listar ativo, download desabilitado")
            return
        for item in faturas:
            self._baixar_item(page, item, config, runtime)
            page.wait_for_timeout(500)

    def _baixar_item(
        self,
        page: Any,
        item: dict[str, Any],
        config: MovelConfig,
        runtime: dict[str, Any],
    ) -> None:
        runtime["tentativas_xpath"] = int(runtime["tentativas_xpath"]) + 1
        button_xpath = str(item.get("_xpath_botao", ""))
        link_xpath = str(item.get("_xpath_link", ""))
        try:
            self._fechar_toggle_dialog(page, runtime)
            self._clicar_botao_download(page, button_xpath)
            link = self._obter_link_download(page, link_xpath)
            target = self._executar_download(page, link, item, config, runtime)
            item["download_ok"] = True
            item["arquivo_download"] = str(target.resolve())
            runtime["downloads_ok"] = int(runtime["downloads_ok"]) + 1
            self.logger.log("ok", "Download concluido", arquivo=target.name)
        except Exception as exc:
            item["erro_download"] = str(exc)
            runtime["downloads_falhos"] = int(runtime["downloads_falhos"]) + 1
            self.logger.log("warn", "Falha no download", erro=str(exc), conta=item.get("conta", ""))

    def _fechar_toggle_dialog(self, page: Any, runtime: dict[str, Any]) -> None:
        opened_toggle = page.locator("div.toggle-dialog.dialog-icon.opened").first
        if opened_toggle.count() > 0 and BrowserActions.click_with_fallback(
            opened_toggle, timeout_ms=3000
        ):
            runtime["cliques_toggle_dialog_aberto"] = (
                int(runtime["cliques_toggle_dialog_aberto"]) + 1
            )
            page.wait_for_timeout(250)

    def _clicar_botao_download(self, page: Any, button_xpath: str) -> None:
        button = page.locator(f"xpath={button_xpath}").first
        if button.count() == 0:
            raise RuntimeError("Botao nao encontrado")
        try:
            button.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        if not BrowserActions.click_with_fallback(button, timeout_ms=5000):
            raise RuntimeError("Falha ao clicar no botao")
        page.wait_for_timeout(300)

    def _obter_link_download(self, page: Any, link_xpath: str) -> Any:
        link = page.locator(f"xpath={link_xpath}").first
        if link.count() == 0:
            raise RuntimeError("Link de download nao encontrado")
        return link

    def _executar_download(
        self,
        page: Any,
        link: Any,
        item: dict[str, Any],
        config: MovelConfig,
        runtime: dict[str, Any],
    ) -> Path:
        with page.expect_download(timeout=45000) as dl_info:
            if not BrowserActions.click_with_fallback(link, timeout_ms=5000):
                raise RuntimeError("Falha ao clicar no link de download")
        download = dl_info.value
        target = self.naming.montar_nome_arquivo_padrao(
            str(runtime.get("cnpj_cliente", "") or config.cnpj_inicial),
            str(item.get("conta", "")),
            str(item.get("vencimento", "")),
            config.download_dir,
        )
        download.save_as(str(target))
        return target


# ---------------------------------------------------------------------------
# Normalização de faturas Móvel para saída
# ---------------------------------------------------------------------------

def normalizar_faturas_saida(faturas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resultado: list[dict[str, Any]] = []
    for item in faturas:
        item_final = {
            "conta": item.get("conta", ""),
            "vencimento": item.get("vencimento", ""),
            "valor": item.get("valor", ""),
            "situacao": item.get("situacao", ""),
            "download_ok": item.get("download_ok", False),
            "arquivo_download": item.get("arquivo_download", ""),
            "erro_download": item.get("erro_download", ""),
            "codigo_de_barras": item.get("codigo_de_barras", ""),
            "codigo_de_barras_sem_espaco": item.get("codigo_de_barras_sem_espaco", ""),
            "pix_copia_cola": "",
            "emissor": "",
            "destinatario": "",
            "identificador_fatura": "",
            "telefone": "",
            "data_emissao": "",
            "data_vencimento": item.get("vencimento", ""),
            "coleta_data_hora": item.get("coleta_data_hora", ""),
        }
        item_final = enriquecer_com_extrator(item_final)
        resultado.append(item_final)
    return resultado


# ---------------------------------------------------------------------------
# App principal Móvel
# ---------------------------------------------------------------------------

class VivoMovelApp:
    def __init__(self, config: MovelConfig) -> None:
        self.config = config
        self.logger = Logger()
        self.debug = DebugCollector(config, self.logger)
        self.auth_service = AuthService(self.logger)
        self.invoice_service = InvoiceService(self.logger)
        naming = NamingService()
        self.download_service = DownloadService(self.logger, naming)
        self.result_service = ResultService(naming)
        self.runtime: dict[str, Any] = {
            "campo_senha_detectado": False,
            "senha_enviada": False,
            "dashboard_detectado": False,
            "faturas_aberto": False,
            "cliques_toggle_dialog_aberto": 0,
            "tentativas_xpath": 0,
            "downloads_ok": 0,
            "downloads_falhos": 0,
            "faturas_disponiveis": [],
            "cnpj_cliente": config.cnpj_inicial,
            "debug_paginas": 0,
            "erro_execucao": "",
        }

    def run(self) -> Path:
        response = executar_fetch(self.config, self._page_action, self.runtime)
        result_file = self.result_service.salvar_resultado(
            response, self.config, self.runtime,
            extra={
                "cliques_toggle_dialog_aberto": self.runtime["cliques_toggle_dialog_aberto"],
                "tentativas_xpath": self.runtime["tentativas_xpath"],
            },
            prefixo="vivo_movel_resultado",
        )
        self.logger.log("ok", "Execucao finalizada", status=response.status, url=response.url)
        self.logger.log("ok", "Resumo", tentativas=self.runtime["tentativas_xpath"])
        self.logger.log("ok", "Resumo", downloads_ok=self.runtime["downloads_ok"])
        self.logger.log("ok", "Resumo", downloads_falhos=self.runtime["downloads_falhos"])
        imprimir_resumo_telefones(self.runtime["faturas_disponiveis"], self.logger)
        self.logger.log("ok", "Resultado salvo", arquivo=result_file)
        return result_file

    def _page_action(self, page: Any) -> Any:
        self.logger.log("info", "Abertura da pagina inicial", url=self.config.url)
        page.wait_for_timeout(self.config.wait_ms)
        self.debug.capture(page, "01_primeira_tela", self.runtime)

        if not self.auth_service.executar_login(page, self.config, self.runtime):
            return page
        self.debug.capture(page, "02_apos_login", self.runtime)

        if not self.invoice_service.abrir_faturas(page, self.config, self.runtime):
            return page
        self.debug.capture(page, "03_faturas", self.runtime)

        faturas = self.invoice_service.coletar_faturas(page, self.config)
        self.download_service.baixar_faturas(page, faturas, self.config, self.runtime)
        self.runtime["faturas_disponiveis"] = normalizar_faturas_saida(faturas)
        self.debug.capture(page, "04_final", self.runtime)
        return page


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download de faturas Vivo Movel via portal Vivo Empresas"
    )
    add_common_args(parser)
    parser.add_argument("--start-div-index", type=int, default=2)
    parser.add_argument("--max-sections", type=int, default=12)
    parser.add_argument("--max-div-index", type=int, default=20)
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
        start_div_index=args.start_div_index,
        max_sections=args.max_sections,
        max_div_index=args.max_div_index,
    )


def main() -> None:
    carregar_env_arquivo(Path(".env"))
    args = parse_args()
    config = build_config(args)
    app = VivoMovelApp(config)
    app.run()


if __name__ == "__main__":
    main()
