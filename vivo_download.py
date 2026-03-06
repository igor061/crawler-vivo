#!/usr/bin/env python3
import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scrapling.fetchers import StealthyFetcher

DEFAULT_URL = "https://mve.vivo.com.br/oauth?logout=true"
DEFAULT_DASHBOARD_URL = "https://mve.vivo.com.br/sec/dashboard"
DEFAULT_INVOICES_URL = "https://mve.vivo.com.br/sec/invoices"


def carregar_env_arquivo(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]

        os.environ.setdefault(key, value)


def normalize_document(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def resolve_headless(mode: str):
    if mode == "virtual":
        return "virtual"
    return mode == "headless"


def coleta_data_hora_gmt_menos3() -> datetime:
    return datetime.now(timezone(timedelta(hours=-3)))


def format_coleta_data_hora(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def ano_mes_por_vencimento(vencimento: str) -> tuple[str, str]:
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", (vencimento or "").strip())
    if not match:
        return "0000", "00"
    return match.group(3), match.group(2)


def montar_xpaths(section_idx: int, div_idx: int) -> tuple[str, str, str]:
    section_xpath = (
        f"/html/body/main/div/div/div/div/div[2]/div[2]/div/div[1]/section[{section_idx}]"
    )
    row_xpath = f"{section_xpath}/div/div[{div_idx}]"
    button_xpath = f"{row_xpath}/div[4]/div/div/div[2]/div/button"
    link_xpath = f"{row_xpath}/div[4]/div/div/div[2]/div/div/ul/li[1]/a"
    return row_xpath, button_xpath, link_xpath


def extrair_dados_fatura(row: Any) -> tuple[str, str, str]:
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


def extrair_cnpj_da_pagina(html: str) -> str:
    if not html:
        return ""

    padrao_dashboard = re.search(
        r"window\.dashboardDocumentNumber\s*=\s*['\"](\d{11,14})['\"]",
        html,
        flags=re.IGNORECASE,
    )
    if padrao_dashboard:
        doc = normalize_document(padrao_dashboard.group(1))
        if len(doc) == 14:
            return doc

    padrao_cnpj = re.search(r"\b\d{14}\b", html)
    if padrao_cnpj:
        doc = normalize_document(padrao_cnpj.group(0))
        if len(doc) == 14:
            return doc

    return ""


class Logger:
    def log(self, level: str, message: str, **fields: Any) -> None:
        prefix_map = {
            "info": "[info]",
            "ok": "[ok]",
            "warn": "[warn]",
            "erro": "[erro]",
        }
        prefix = prefix_map.get(level, "[info]")
        if not fields:
            print(f"{prefix} {message}")
            return
        ordered_keys = sorted(fields)
        details = " ".join(f"{k}={fields[k]}" for k in ordered_keys)
        print(f"{prefix} {message} {details}")


@dataclass
class AppConfig:
    url: str
    dashboard_url: str
    invoices_url: str
    cpf_ou_cnpj: str
    password: str
    output_dir: Path
    download_dir: Path
    wait_ms: int
    timeout_ms: int
    start_div_index: int
    max_sections: int
    max_div_index: int
    debug: bool
    listar: bool
    mode: str
    coleta_dt: datetime

    @property
    def coleta_data_hora(self) -> str:
        return format_coleta_data_hora(self.coleta_dt)

    @property
    def cnpj_inicial(self) -> str:
        doc = normalize_document(self.cpf_ou_cnpj)
        return doc if len(doc) == 14 else ""

    @property
    def debug_dir(self) -> Path:
        stamp = self.coleta_dt.strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"debug_xpath_loop_{stamp}"


class BrowserActions:
    @staticmethod
    def click_with_fallback(locator: Any, timeout_ms: int = 5000) -> bool:
        try:
            locator.click(timeout=timeout_ms)
            return True
        except Exception:
            pass

        try:
            locator.click(timeout=timeout_ms, force=True)
            return True
        except Exception:
            pass

        try:
            locator.evaluate("el => el.click()")
            return True
        except Exception:
            pass

        try:
            locator.evaluate(
                """
                el => {
                  const ev = new MouseEvent('click', {
                    bubbles: true,
                    cancelable: true,
                    view: window
                  })
                  el.dispatchEvent(ev)
                }
                """
            )
            return True
        except Exception:
            return False


class DebugCollector:
    def __init__(self, config: AppConfig, logger: Logger) -> None:
        self.config = config
        self.logger = logger
        self.counter = 0
        if config.debug:
            config.debug_dir.mkdir(parents=True, exist_ok=True)

    def capture(self, page: Any, etapa: str, runtime: dict[str, Any]) -> None:
        if not self.config.debug:
            return

        self.counter += 1
        nome_base = f"{self.counter:02d}_{etapa}"
        screenshot_path = self.config.debug_dir / f"{nome_base}.png"
        html_path = self.config.debug_dir / f"{nome_base}.html"

        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass

        try:
            html_path.write_text(page.content() or "", encoding="utf-8")
        except Exception:
            pass

        runtime["debug_paginas"] = int(runtime["debug_paginas"]) + 1
        self.logger.log(
            "info",
            "Snapshot debug coletado",
            etapa=etapa,
            screenshot=screenshot_path.name,
            html=html_path.name,
        )


class NamingService:
    @staticmethod
    def montar_nome_arquivo_padrao(
        cnpj_cliente: str,
        conta: str,
        vencimento: str,
        download_dir: Path,
    ) -> Path:
        ano, mes = ano_mes_por_vencimento(vencimento)
        cnpj_seguro = normalize_document(cnpj_cliente) or "semcnpj"
        conta_segura = normalize_document(conta) or "semconta"
        base = f"vivo-{cnpj_seguro}-{conta_segura}-{ano}-{mes}"
        target = download_dir / f"{base}.pdf"
        contador = 2
        while target.exists():
            target = download_dir / f"{base}-{contador}.pdf"
            contador += 1
        return target

    @staticmethod
    def montar_nome_resultado(download_dir: Path, cnpj_cliente: str, coleta_dt: datetime) -> Path:
        cnpj_seguro = normalize_document(cnpj_cliente) or "semcnpj"
        stamp = coleta_dt.strftime("%Y%m%d_%H%M%S")
        return download_dir / f"xpath_loop_resultado_{cnpj_seguro}_{stamp}.json"


class AuthService:
    CPF_SELECTORS = [
        "input[name*='cpf' i]",
        "input[id*='cpf' i]",
        "input[placeholder*='cpf' i]",
        "input[type='tel']",
        "input[type='text']",
    ]
    CONTINUE_SELECTORS = [
        "button:has-text('Continuar')",
        "button:has-text('Avancar')",
        "button:has-text('Próximo')",
        "button:has-text('Proximo')",
        "button:has-text('Entrar')",
        "button[type='submit']",
        "input[type='submit']",
    ]
    PASSWORD_SELECTORS = [
        "input[type='password']",
        "input[name*='senha' i]",
        "input[id*='senha' i]",
        "input[placeholder*='senha' i]",
    ]
    ENTER_SELECTORS = [
        "button:has-text('Entrar')",
        "button[type='submit']",
        "input[type='submit']",
    ]

    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def _has_password_field(self, page: Any) -> bool:
        for selector in self.PASSWORD_SELECTORS:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return True
        return False

    def _fill_first_visible(self, page: Any, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.fill(value)
                return True
        return False

    def _click_first_enabled(self, page: Any, selectors: list[str], timeout_ms: int = 3000) -> bool:
        for selector in selectors:
            button = page.locator(selector).first
            if button.count() > 0 and button.is_visible() and button.is_enabled():
                try:
                    button.click(timeout=timeout_ms)
                    return True
                except Exception:
                    continue
        return False

    def executar_login(self, page: Any, config: AppConfig, runtime: dict[str, Any]) -> bool:
        if not config.cpf_ou_cnpj:
            self.logger.log("warn", "CPF/CNPJ nao informado")
            return False

        if not self._fill_first_visible(page, self.CPF_SELECTORS, config.cpf_ou_cnpj):
            self.logger.log("warn", "Campo de CPF/CNPJ nao encontrado")
            return False
        self.logger.log("ok", "CPF/CNPJ preenchido")

        page.wait_for_timeout(config.wait_ms)

        if not self._has_password_field(page):
            for _ in range(4):
                if not self._click_first_enabled(page, self.CONTINUE_SELECTORS):
                    page.keyboard.press("Enter")
                page.wait_for_timeout(config.wait_ms)
                if self._has_password_field(page):
                    break

        runtime["campo_senha_detectado"] = self._has_password_field(page)
        if not runtime["campo_senha_detectado"]:
            self.logger.log("warn", "Campo de senha nao detectado")
            return False

        if not config.password:
            self.logger.log("warn", "Senha nao informada")
            return False

        if not self._fill_first_visible(page, self.PASSWORD_SELECTORS, config.password):
            self.logger.log("warn", "Nao foi possivel preencher senha")
            return False

        if not self._click_first_enabled(page, self.ENTER_SELECTORS):
            page.keyboard.press("Enter")

        runtime["senha_enviada"] = True
        self.logger.log("ok", "Senha enviada")
        page.wait_for_timeout(config.wait_ms)

        try:
            page.wait_for_url("**/sec/dashboard*", timeout=30000)
            runtime["dashboard_detectado"] = True
        except Exception:
            runtime["dashboard_detectado"] = config.dashboard_url in (page.url or "")

        self.logger.log("ok", "Dashboard verificado", detectado=runtime["dashboard_detectado"])
        return True


class InvoiceService:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def abrir_faturas(self, page: Any, config: AppConfig, runtime: dict[str, Any]) -> bool:
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

    def coletar_faturas(self, page: Any, config: AppConfig) -> list[dict[str, Any]]:
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

            conta = ""
            try:
                title = page.locator(
                    f"xpath={section_xpath}//h4[contains(@aria-label,'Conta')]"
                ).first
                if title.count() > 0:
                    text = title.inner_text(timeout=1500)
                    match = re.search(r"(\d{8,})", text)
                    conta = match.group(1) if match else ""
            except Exception:
                pass

            for div_idx in range(config.start_div_index, config.max_div_index + 1):
                row_xpath, button_xpath, link_xpath = montar_xpaths(section_idx, div_idx)
                row = page.locator(f"xpath={row_xpath}").first
                if row.count() == 0:
                    if div_idx == config.start_div_index:
                        break
                    break

                button = page.locator(f"xpath={button_xpath}").first
                if button.count() == 0:
                    continue

                vencimento, valor, situacao = extrair_dados_fatura(row)
                item = {
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
                faturas.append(item)
                self.logger.log(
                    "info",
                    "Fatura encontrada",
                    conta=conta,
                    vencimento=vencimento,
                    valor=valor,
                    situacao=situacao,
                )

        return faturas


class DownloadService:
    def __init__(self, logger: Logger, naming: NamingService) -> None:
        self.logger = logger
        self.naming = naming

    def baixar_faturas(
        self,
        page: Any,
        faturas: list[dict[str, Any]],
        config: AppConfig,
        runtime: dict[str, Any],
    ) -> None:
        if config.listar:
            self.logger.log("info", "Modo listar ativo, download desabilitado")
            return

        for item in faturas:
            runtime["tentativas_xpath"] = int(runtime["tentativas_xpath"]) + 1
            button_xpath = str(item.get("_xpath_botao", ""))
            link_xpath = str(item.get("_xpath_link", ""))

            try:
                opened_toggle = page.locator("div.toggle-dialog.dialog-icon.opened").first
                if opened_toggle.count() > 0 and BrowserActions.click_with_fallback(
                    opened_toggle, timeout_ms=3000
                ):
                    runtime["cliques_toggle_dialog_aberto"] = (
                        int(runtime["cliques_toggle_dialog_aberto"]) + 1
                    )
                    page.wait_for_timeout(250)

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
                link = page.locator(f"xpath={link_xpath}").first
                if link.count() == 0:
                    raise RuntimeError("Link de download nao encontrado")

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
                item["download_ok"] = True
                item["arquivo_download"] = str(target)
                runtime["downloads_ok"] = int(runtime["downloads_ok"]) + 1
                self.logger.log("ok", "Download concluido", arquivo=target.name)
            except Exception as exc:
                item["erro_download"] = str(exc)
                runtime["downloads_falhos"] = int(runtime["downloads_falhos"]) + 1
                self.logger.log(
                    "warn", "Falha no download", erro=str(exc), conta=item.get("conta", "")
                )

            page.wait_for_timeout(500)


class ResultService:
    def __init__(self, naming: NamingService) -> None:
        self.naming = naming

    def normalizar_faturas_saida(self, faturas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resultado: list[dict[str, Any]] = []
        for item in faturas:
            resultado.append(
                {
                    "conta": item.get("conta", ""),
                    "vencimento": item.get("vencimento", ""),
                    "valor": item.get("valor", ""),
                    "situacao": item.get("situacao", ""),
                    "download_ok": item.get("download_ok", False),
                    "arquivo_download": item.get("arquivo_download", ""),
                    "erro_download": item.get("erro_download", ""),
                    "codigo_de_barras": item.get("codigo_de_barras", ""),
                    "codigo_de_barras_sem_espaco": item.get("codigo_de_barras_sem_espaco", ""),
                    "coleta_data_hora": item.get("coleta_data_hora", ""),
                }
            )
        return resultado

    def salvar_resultado(self, response: Any, config: AppConfig, runtime: dict[str, Any]) -> Path:
        output = {
            "status": response.status,
            "url": response.url,
            "erro_execucao": runtime.get("erro_execucao", ""),
            "cnpj_cliente": str(runtime.get("cnpj_cliente", "") or config.cnpj_inicial),
            "coleta_data_hora": config.coleta_data_hora,
            "campo_senha_detectado": runtime["campo_senha_detectado"],
            "senha_enviada": runtime["senha_enviada"],
            "dashboard_detectado": runtime["dashboard_detectado"],
            "faturas_aberto": runtime["faturas_aberto"],
            "cliques_toggle_dialog_aberto": runtime["cliques_toggle_dialog_aberto"],
            "modo_listar": config.listar,
            "tentativas_xpath": runtime["tentativas_xpath"],
            "downloads_ok": runtime["downloads_ok"],
            "downloads_falhos": runtime["downloads_falhos"],
            "faturas_disponiveis": runtime["faturas_disponiveis"],
            "debug_ativado": config.debug,
            "debug_diretorio": str(config.debug_dir) if config.debug else "",
            "debug_paginas": runtime["debug_paginas"],
        }
        result_file = self.naming.montar_nome_resultado(
            config.download_dir,
            str(runtime.get("cnpj_cliente", "") or config.cnpj_inicial),
            config.coleta_dt,
        )
        result_file.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        return result_file


class VivoDownloadApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = Logger()
        self.debug_collector = DebugCollector(config, self.logger)
        self.auth_service = AuthService(self.logger)
        self.invoice_service = InvoiceService(self.logger)
        self.naming_service = NamingService()
        self.download_service = DownloadService(self.logger, self.naming_service)
        self.result_service = ResultService(self.naming_service)
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
        def page_action(page: Any) -> Any:
            self.logger.log("info", "Abertura da pagina inicial", url=self.config.url)
            page.wait_for_timeout(self.config.wait_ms)
            self.debug_collector.capture(page, "01_primeira_tela", self.runtime)

            if not self.auth_service.executar_login(page, self.config, self.runtime):
                return page

            self.debug_collector.capture(page, "02_apos_login", self.runtime)

            if not self.invoice_service.abrir_faturas(page, self.config, self.runtime):
                return page

            self.debug_collector.capture(page, "03_faturas", self.runtime)
            faturas = self.invoice_service.coletar_faturas(page, self.config)
            self.download_service.baixar_faturas(page, faturas, self.config, self.runtime)
            self.runtime["faturas_disponiveis"] = self.result_service.normalizar_faturas_saida(
                faturas
            )
            self.debug_collector.capture(page, "04_final", self.runtime)
            return page

        try:
            response = StealthyFetcher.fetch(
                url=self.config.url,
                headless=resolve_headless(self.config.mode),
                timeout=self.config.timeout_ms,
                wait=self.config.wait_ms,
                page_action=page_action,
                humanize=True,
            )
        except Exception as exc:
            self.runtime["erro_execucao"] = str(exc)
            self.logger.log("erro", "Falha de rede/executando browser", erro=str(exc))

            class FallbackResponse:
                status = 0
                url = self.config.url

            response = FallbackResponse()

        result_file = self.result_service.salvar_resultado(response, self.config, self.runtime)
        self.logger.log("ok", "Execucao finalizada", status=response.status, url=response.url)
        self.logger.log("ok", "Resumo", tentativas=self.runtime["tentativas_xpath"])
        self.logger.log("ok", "Resumo", downloads_ok=self.runtime["downloads_ok"])
        self.logger.log("ok", "Resumo", downloads_falhos=self.runtime["downloads_falhos"])
        self.logger.log("ok", "Resultado salvo", arquivo=result_file)
        return result_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Login Vivo e download por loop de XPath em /sec/invoices"
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--invoices-url", default=DEFAULT_INVOICES_URL)
    parser.add_argument("--cpf", default=os.getenv("VIVO_CPF", ""))
    parser.add_argument("--password", default=os.getenv("VIVO_PASSWORD", ""))
    parser.add_argument("--output-dir", default="screenshots/scrapling")
    parser.add_argument("--download-dir", default="downloads/vivo")
    parser.add_argument("--wait-ms", type=int, default=3500)
    parser.add_argument("--timeout-ms", type=int, default=90000)
    parser.add_argument("--start-div-index", type=int, default=2)
    parser.add_argument("--max-sections", type=int, default=12)
    parser.add_argument("--max-div-index", type=int, default=20)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Salva snapshots de pagina (HTML + screenshot) durante o fluxo",
    )
    parser.add_argument(
        "--listar",
        "--somente-listar",
        dest="listar",
        action="store_true",
        help="Somente lista faturas, sem baixar",
    )
    parser.add_argument(
        "--mode",
        choices=["headless", "headful", "virtual"],
        default="headless",
        help="Modo do navegador do Camoufox",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> AppConfig:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    download_dir = Path(args.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        url=args.url,
        dashboard_url=args.dashboard_url,
        invoices_url=args.invoices_url,
        cpf_ou_cnpj=normalize_document(args.cpf),
        password=args.password or "",
        output_dir=output_dir,
        download_dir=download_dir,
        wait_ms=args.wait_ms,
        timeout_ms=args.timeout_ms,
        start_div_index=args.start_div_index,
        max_sections=args.max_sections,
        max_div_index=args.max_div_index,
        debug=args.debug,
        listar=args.listar,
        mode=args.mode,
        coleta_dt=coleta_data_hora_gmt_menos3(),
    )


def main() -> None:
    carregar_env_arquivo(Path(".env"))
    args = parse_args()
    config = build_config(args)
    app = VivoDownloadApp(config)
    app.run()


if __name__ == "__main__":
    main()
