#!/usr/bin/env python3
"""vivo_core.py — Biblioteca compartilhada para automação do portal Vivo Empresas.

Fornece utilitários, serviços e config base usados por vivo_movel.py e vivo_fixo.py.
"""

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from scrapling.fetchers import StealthyFetcher


DEFAULT_URL = "https://mve.vivo.com.br/oauth?logout=true"
DEFAULT_DASHBOARD_URL = "https://mve.vivo.com.br/sec/dashboard"
DEFAULT_INVOICES_URL = "https://mve.vivo.com.br/sec/invoices"


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------

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


def resolve_headless(mode: str) -> "bool | str":
    if mode == "virtual":
        return "virtual"
    return mode == "headless"


def coleta_data_hora_gmt_menos3() -> datetime:
    return datetime.now(timezone(timedelta(hours=-3)))


def format_coleta_data_hora(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


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


def ano_mes_por_vencimento(vencimento: str) -> tuple[str, str]:
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", (vencimento or "").strip())
    if not match:
        return "0000", "00"
    return match.group(3), match.group(2)


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class Logger:
    def log(self, level: str, message: str, **fields: Any) -> None:
        prefix_map = {"info": "[info]", "ok": "[ok]", "warn": "[warn]", "erro": "[erro]"}
        prefix = prefix_map.get(level, "[info]")
        if not fields:
            print(f"{prefix} {message}")
            return
        details = " ".join(f"{k}={fields[k]}" for k in sorted(fields))
        print(f"{prefix} {message} {details}")


# ---------------------------------------------------------------------------
# Config base — campos comuns a Móvel e Fixo
# ---------------------------------------------------------------------------

@dataclass
class BaseConfig:
    url: str
    dashboard_url: str
    invoices_url: str
    cpf_ou_cnpj: str
    password: str
    output_dir: Path
    download_dir: Path
    wait_ms: int
    timeout_ms: int
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
        return self.output_dir / f"debug_{stamp}"


# ---------------------------------------------------------------------------
# BrowserActions
# ---------------------------------------------------------------------------

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
                """el => {
                  const ev = new MouseEvent('click', {bubbles: true, cancelable: true, view: window})
                  el.dispatchEvent(ev)
                }"""
            )
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# DebugCollector
# Timing-safe: sempre executa screenshot/html; só salva em disco com --debug.
# ---------------------------------------------------------------------------

class DebugCollector:
    def __init__(self, config: BaseConfig, logger: Logger) -> None:
        self.config = config
        self.logger = logger
        self.counter = 0
        if config.debug:
            config.debug_dir.mkdir(parents=True, exist_ok=True)

    def capture(self, page: Any, etapa: str, runtime: dict[str, Any]) -> None:
        self.counter += 1
        base = f"{self.counter:02d}_{etapa}"
        if self.config.debug:
            screenshot = self.config.debug_dir / f"{base}.png"
            html_path = self.config.debug_dir / f"{base}.html"
            try:
                page.screenshot(path=str(screenshot), full_page=True)
            except Exception:
                pass
            try:
                html_path.write_text(page.content() or "", encoding="utf-8")
            except Exception:
                pass
            runtime["debug_paginas"] = int(runtime.get("debug_paginas", 0)) + 1
            self.logger.log("info", "Snapshot debug coletado", etapa=etapa, html=html_path.name)
        else:
            try:
                page.screenshot(full_page=True)
            except Exception:
                pass
            try:
                page.content()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# AuthService
# ---------------------------------------------------------------------------

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
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                return True
        return False

    def _fill_first_visible(self, page: Any, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                loc.fill(value)
                return True
        return False

    def _click_first_enabled(self, page: Any, selectors: list[str], timeout_ms: int = 3000) -> bool:
        for selector in selectors:
            btn = page.locator(selector).first
            if btn.count() > 0 and btn.is_visible() and btn.is_enabled():
                try:
                    btn.click(timeout=timeout_ms)
                    return True
                except Exception:
                    continue
        return False

    def _avancar_ate_senha(self, page: Any, config: BaseConfig) -> None:
        if self._has_password_field(page):
            return
        for _ in range(4):
            if not self._click_first_enabled(page, self.CONTINUE_SELECTORS):
                page.keyboard.press("Enter")
            page.wait_for_timeout(config.wait_ms)
            if self._has_password_field(page):
                return

    def _preencher_e_submeter_senha(self, page: Any, config: BaseConfig) -> bool:
        if not config.password:
            self.logger.log("warn", "Senha nao informada")
            return False
        if not self._fill_first_visible(page, self.PASSWORD_SELECTORS, config.password):
            self.logger.log("warn", "Nao foi possivel preencher senha")
            return False
        if not self._click_first_enabled(page, self.ENTER_SELECTORS):
            page.keyboard.press("Enter")
        return True

    def _validar_dashboard(self, page: Any, config: BaseConfig) -> bool:
        try:
            page.wait_for_url("**/sec/dashboard*", timeout=30000)
            return True
        except Exception:
            return config.dashboard_url in (page.url or "")

    def executar_login(self, page: Any, config: BaseConfig, runtime: dict[str, Any]) -> bool:
        if not config.cpf_ou_cnpj:
            self.logger.log("warn", "CPF/CNPJ nao informado")
            return False
        if not self._fill_first_visible(page, self.CPF_SELECTORS, config.cpf_ou_cnpj):
            self.logger.log("warn", "Campo de CPF/CNPJ nao encontrado")
            return False
        self.logger.log("ok", "CPF/CNPJ preenchido")
        page.wait_for_timeout(config.wait_ms)
        self._avancar_ate_senha(page, config)
        runtime["campo_senha_detectado"] = self._has_password_field(page)
        if not runtime["campo_senha_detectado"]:
            self.logger.log("warn", "Campo de senha nao detectado")
            return False
        if not self._preencher_e_submeter_senha(page, config):
            return False
        runtime["senha_enviada"] = True
        self.logger.log("ok", "Senha enviada")
        page.wait_for_timeout(config.wait_ms)
        runtime["dashboard_detectado"] = self._validar_dashboard(page, config)
        self.logger.log("ok", "Dashboard verificado", detectado=runtime["dashboard_detectado"])
        return True


# ---------------------------------------------------------------------------
# Enriquecimento com dados do PDF
# ---------------------------------------------------------------------------

def enriquecer_com_extrator(item: dict[str, Any]) -> dict[str, Any]:
    """Enriquece dict de fatura com código de barras, PIX, datas e valor extraídos do PDF."""
    arquivo = str(item.get("arquivo_download", "")).strip()
    if not arquivo:
        return item
    pdf_path = Path(arquivo).expanduser().resolve()
    if not pdf_path.exists():
        return item
    try:
        from vivo_fatura_extrator import extrair_dados_fatura
        dados = extrair_dados_fatura(pdf_path, verbose=False)
    except Exception:
        return item

    codigo = str(dados.get("codigo_barras_digitavel", "")).strip()
    if codigo:
        item["codigo_de_barras"] = codigo
        item["codigo_de_barras_sem_espaco"] = re.sub(r"\s+", "", codigo)
    item["pix_copia_cola"] = str(dados.get("pix_copia_cola", "")).strip()
    item["emissor"] = str(dados.get("emissor", "")).strip()
    item["destinatario"] = str(dados.get("destinatario", "")).strip()
    item["identificador_fatura"] = str(dados.get("identificador_fatura", "")).strip()
    item["telefone"] = str(dados.get("telefone", "")).strip()
    item["numeros_vivo"] = dados.get("numeros_vivo") or []
    item["data_emissao"] = str(dados.get("data_emissao", "")).strip()
    data_venc = str(dados.get("data_vencimento", "")).strip()
    if data_venc:
        item["data_vencimento"] = data_venc
    valor_extraido = str(dados.get("valor", "")).strip()
    if valor_extraido:
        item["valor"] = valor_extraido
    return item


# ---------------------------------------------------------------------------
# ResultService — salva o JSON de resultado com campos comuns
# ---------------------------------------------------------------------------

class ResultService:
    """Salva resultado da execução em JSON.

    Campos comuns (status, cnpj, login, downloads, debug) são preenchidos
    automaticamente a partir de response/config/runtime. Campos específicos
    de cada script são passados via `extra`.
    """

    def __init__(self, naming: "NamingService") -> None:
        self.naming = naming

    def salvar_resultado(
        self,
        response: Any,
        config: BaseConfig,
        runtime: dict[str, Any],
        extra: dict[str, Any] | None = None,
        prefixo: str = "vivo_resultado",
    ) -> Path:
        cnpj = str(runtime.get("cnpj_cliente", "") or config.cnpj_inicial)
        output: dict[str, Any] = {
            "status": response.status,
            "url": response.url,
            "erro_execucao": runtime.get("erro_execucao", ""),
            "cnpj_cliente": cnpj,
            "coleta_data_hora": config.coleta_data_hora,
            "campo_senha_detectado": runtime.get("campo_senha_detectado", False),
            "senha_enviada": runtime.get("senha_enviada", False),
            "dashboard_detectado": runtime.get("dashboard_detectado", False),
            "faturas_aberto": runtime.get("faturas_aberto", False),
            "modo_listar": config.listar,
            "downloads_ok": runtime.get("downloads_ok", 0),
            "downloads_falhos": runtime.get("downloads_falhos", 0),
            "faturas_disponiveis": runtime.get("faturas_disponiveis", []),
            "debug_ativado": config.debug,
            "debug_diretorio": str(config.debug_dir) if config.debug else "",
            "debug_paginas": runtime.get("debug_paginas", 0),
        }
        if extra:
            output.update(extra)
        result_file = self.naming.montar_nome_resultado(
            config.download_dir, cnpj, config.coleta_dt, prefixo=prefixo
        )
        result_file.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        return result_file


# ---------------------------------------------------------------------------
# NamingService
# ---------------------------------------------------------------------------

class NamingService:
    @staticmethod
    def montar_nome_arquivo_padrao(
        cnpj_cliente: str,
        conta: str,
        vencimento: str,
        download_dir: Path,
        prefixo: str = "vivo-movel",
    ) -> Path:
        ano, mes = ano_mes_por_vencimento(vencimento)
        cnpj_seguro = normalize_document(cnpj_cliente) or "semcnpj"
        conta_segura = normalize_document(conta) or "semconta"
        base = f"{prefixo}-{cnpj_seguro}-{conta_segura}-{ano}-{mes}"
        target = download_dir / f"{base}.pdf"
        contador = 2
        while target.exists():
            target = download_dir / f"{base}-{contador}.pdf"
            contador += 1
        return target

    @staticmethod
    def montar_nome_resultado(
        download_dir: Path,
        cnpj_cliente: str,
        coleta_dt: datetime,
        prefixo: str = "vivo_resultado",
    ) -> Path:
        cnpj_seguro = normalize_document(cnpj_cliente) or "semcnpj"
        stamp = coleta_dt.strftime("%Y%m%d_%H%M%S")
        return download_dir / f"{prefixo}_{cnpj_seguro}_{stamp}.json"


# ---------------------------------------------------------------------------
# StealthyFetcher runner
# ---------------------------------------------------------------------------

class _FallbackResponse:
    def __init__(self, url: str) -> None:
        self.status = 0
        self.url = url


def executar_fetch(
    config: BaseConfig,
    page_action: Callable[[Any], Any],
    runtime: dict[str, Any],
) -> Any:
    """Executa StealthyFetcher com a page_action fornecida. Retorna o response."""
    try:
        return StealthyFetcher.fetch(
            url=config.url,
            headless=resolve_headless(config.mode),
            timeout=config.timeout_ms,
            wait=config.wait_ms,
            page_action=page_action,
            humanize=True,
        )
    except Exception as exc:
        runtime["erro_execucao"] = str(exc)
        Logger().log("erro", "Falha na execucao do browser", erro=str(exc))
        return _FallbackResponse(config.url)


# ---------------------------------------------------------------------------
# Resumo de telefones por conta
# ---------------------------------------------------------------------------

def imprimir_resumo_telefones(faturas: list[dict[str, Any]], logger: Logger) -> None:
    """Imprime os telefones encontrados por identificador de conta."""
    if not faturas:
        return
    logger.log("info", "Resumo de telefones por conta")
    for item in faturas:
        ident = (
            item.get("identificador_fatura")
            or item.get("codigo_cliente")
            or item.get("conta")
            or "?"
        )
        tel = item.get("telefone") or "(nao encontrado)"
        ref = item.get("referencia") or item.get("vencimento") or ""
        extras = {"telefone": tel}
        if ref:
            extras["referencia"] = ref
        logger.log("ok", f"  conta={ident}", **extras)


# ---------------------------------------------------------------------------
# Helpers de CLI compartilhados
# ---------------------------------------------------------------------------

def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Adiciona argumentos CLI compartilhados por movel e fixo."""
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--invoices-url", default=DEFAULT_INVOICES_URL)
    parser.add_argument("--cpf", default=os.getenv("VIVO_CPF", ""))
    parser.add_argument("--password", default=os.getenv("VIVO_PASSWORD", ""))
    parser.add_argument("--output-dir", default="screenshots/scrapling")
    parser.add_argument("--download-dir", default="downloads/vivo")
    parser.add_argument("--wait-ms", type=int, default=5000)
    parser.add_argument("--timeout-ms", type=int, default=90000)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Salva snapshots HTML + screenshot por etapa",
    )
    parser.add_argument(
        "--listar", "--somente-listar",
        dest="listar",
        action="store_true",
        help="Somente lista faturas, sem baixar PDFs",
    )
    parser.add_argument(
        "--show",
        dest="show",
        action="store_true",
        help="Exibe a janela do navegador (equivale a --mode headful)",
    )
    parser.add_argument(
        "--mode",
        choices=["headless", "headful", "virtual"],
        default="headless",
        help="Modo do navegador (Camoufox)",
    )


def build_common_dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    download_dir = Path(args.download_dir).expanduser().resolve()
    download_dir.mkdir(parents=True, exist_ok=True)
    return output_dir, download_dir


def resolve_mode(args: argparse.Namespace) -> str:
    return "headful" if getattr(args, "show", False) else args.mode
