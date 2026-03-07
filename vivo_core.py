#!/usr/bin/env python3
"""vivo_core.py — Biblioteca compartilhada para automação do portal Vivo Empresas.

Fornece utilitários, serviços e config base usados por vivo_movel.py e vivo_fixo.py.
"""

import argparse
import getpass
import json
import os
import re
import signal
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
    item["url_nfe"] = str(dados.get("url_nfe", "")).strip()
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


def _ler_com_timeout(prompt: str, timeout: int, senha: bool = False) -> "str | None":
    """Lê input do usuario com timeout via SIGALRM (Unix). Retorna None se expirar."""
    resultado: list[str] = []

    def _handler(signum: Any, frame: Any) -> None:
        raise TimeoutError

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout)
    try:
        valor = getpass.getpass(prompt) if senha else input(prompt)
        resultado.append(valor)
    except (TimeoutError, EOFError, KeyboardInterrupt):
        pass
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)

    return resultado[0] if resultado else None


def _imprimir_ajuda_credenciais() -> None:
    print()
    print("  Configure de uma das formas a seguir:")
    print()
    print("  1) Arquivo .env no diretorio atual:")
    print("       echo 'VIVO_CPF=seu_cpf_ou_cnpj' >> .env")
    print("       echo 'VIVO_PASSWORD=sua_senha'  >> .env")
    print()
    print("  2) Variaveis de ambiente:")
    print("       export VIVO_CPF=seu_cpf_ou_cnpj")
    print("       export VIVO_PASSWORD=sua_senha")
    print()
    print("  3) Argumentos na linha de comando:")
    print("       vivo-movel --cpf seu_cpf_ou_cnpj --password sua_senha")
    print("       vivo-fixo  --cpf seu_cpf_ou_cnpj --password sua_senha")
    print()


def _salvar_env(cpf: str, password: str) -> None:
    env_path = Path(".env")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    lines = [l for l in lines if not l.startswith("VIVO_CPF=") and not l.startswith("VIVO_PASSWORD=")]
    lines += [f"VIVO_CPF={cpf}", f"VIVO_PASSWORD={password}"]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [ok] Salvo em {env_path.resolve()}")


def validar_credenciais(cpf: str, password: str, timeout: int = 30) -> "tuple[str, str]":
    """Verifica credenciais; se ausentes, solicita interativamente com timeout de 30s.

    Retorna (cpf, password) prontos para uso, ou ("", "") se nao fornecidos.
    """
    faltando = []
    if not normalize_document(cpf):
        faltando.append("CPF/CNPJ")
    if not password:
        faltando.append("senha")

    if not faltando:
        return cpf, password

    print()
    print(f"[info] Credencial(is) ausente(s): {', '.join(faltando)}")
    print(f"[info] Voce tem {timeout}s para preencher cada campo. Ctrl+C para cancelar.")
    print()

    if not normalize_document(cpf):
        val = _ler_com_timeout("  CPF ou CNPJ (somente numeros): ", timeout=timeout)
        if not val or not normalize_document(val):
            print("\n[erro] Timeout ou valor invalido.")
            _imprimir_ajuda_credenciais()
            return "", ""
        cpf = val.strip()

    if not password:
        val = _ler_com_timeout("  Senha: ", timeout=timeout, senha=True)
        if not val:
            print("\n[erro] Timeout ou senha em branco.")
            _imprimir_ajuda_credenciais()
            return "", ""
        password = val

    print()
    salvar = _ler_com_timeout("  Salvar em .env para proximas execucoes? [s/N]: ", timeout=timeout)
    if salvar and salvar.strip().lower() in ("s", "sim", "y", "yes"):
        _salvar_env(cpf, password)

    return cpf, password


def build_common_dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    download_dir = Path(args.download_dir).expanduser().resolve()
    download_dir.mkdir(parents=True, exist_ok=True)
    return output_dir, download_dir


def resolve_mode(args: argparse.Namespace) -> str:
    return "headful" if getattr(args, "show", False) else args.mode


# ---------------------------------------------------------------------------
# BaseVivoApp — template de fluxo compartilhado por Móvel e Fixo
# ---------------------------------------------------------------------------

class BaseVivoApp:
    """Classe base para apps de automação Vivo (Móvel e Fixo).

    Implementa o fluxo completo via template method:
      login → _pos_login() → invoices → _coletar_secoes() → _popular_faturas()
      → _baixar_ou_listar() → enriquecimento PDF → salvar JSON

    Subclasses sobrescrevem os hooks e as constantes de classe.
    """

    PREFIXO_RESULTADO: str = "vivo_resultado"
    PDF_PREFIXO: str = "vivo"
    TITULO_TABELA: str = "FATURAS VIVO"

    def __init__(self, config: BaseConfig) -> None:
        self.config = config
        self.logger = Logger()
        self.debug = DebugCollector(config, self.logger)
        self.auth_service = AuthService(self.logger)
        self.result_service = ResultService(NamingService())
        self.runtime: dict[str, Any] = {
            "campo_senha_detectado": False,
            "senha_enviada": False,
            "dashboard_detectado": False,
            "faturas_aberto": False,
            "tentativas": 0,
            "downloads_ok": 0,
            "downloads_falhos": 0,
            "faturas_disponiveis": [],
            "cnpj_cliente": config.cnpj_inicial,
            "erro_execucao": "",
            "debug_paginas": 0,
        }

    # --- hooks para subclasses ---

    def _extra_result(self) -> dict[str, Any]:
        """Campos extras para o JSON de resultado (específicos de cada script)."""
        return {}

    def _pos_login(self, page: Any) -> None:
        """Executado após login e antes de navegar para /sec/invoices.
        Ex: troca de contexto Móvel → Fixo.
        """
        pass

    def _coletar_secoes(self, page: Any) -> list[dict[str, Any]]:
        """Coleta metadados das seções/contas na grade de faturas."""
        raise NotImplementedError

    def _popular_faturas(self, secoes: list[dict[str, Any]]) -> None:
        """Preenche runtime['faturas_disponiveis'] com dados resumidos das seções."""
        raise NotImplementedError

    def _baixar_ou_listar(
        self, page: Any, secoes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Executa o download (ou listagem) e retorna os resultados."""
        raise NotImplementedError

    # --- fluxo principal ---

    def run(self) -> Path:
        response = executar_fetch(self.config, self._page_action, self.runtime)
        result_file = self.result_service.salvar_resultado(
            response,
            self.config,
            self.runtime,
            extra={"tentativas": self.runtime["tentativas"], **self._extra_result()},
            prefixo=self.PREFIXO_RESULTADO,
        )
        self.logger.log("ok", "Execucao finalizada", status=response.status)
        self.logger.log("ok", "Resumo", downloads_ok=self.runtime["downloads_ok"])
        self.logger.log("ok", "Resumo", downloads_falhos=self.runtime["downloads_falhos"])
        imprimir_resumo_telefones(self.runtime["faturas_disponiveis"], self.logger)
        if self.config.listar:
            imprimir_tabela_listagem(
                self.runtime["faturas_disponiveis"], titulo=self.TITULO_TABELA
            )
        self.logger.log("ok", "Resultado salvo", arquivo=result_file)
        return result_file

    def _page_action(self, page: Any) -> Any:
        self.logger.log("info", "Pagina inicial", url=self.config.url)
        page.wait_for_timeout(self.config.wait_ms)
        self.debug.capture(page, "01_login_page", self.runtime)

        if not self.auth_service.executar_login(page, self.config, self.runtime):
            self.logger.log("warn", "Login falhou")
            return page
        self.debug.capture(page, "02_apos_login", self.runtime)

        self._pos_login(page)

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
        self.debug.capture(page, "03_faturas", self.runtime)

        secoes = self._coletar_secoes(page)
        self._popular_faturas(secoes)

        resultados = self._baixar_ou_listar(page, secoes)

        cnpj_str = str(self.runtime.get("cnpj_cliente", "") or self.config.cnpj_inicial or "")
        for item in resultados:
            if not item.get("arquivo_download"):
                ref = item.get("referencia", "")
                # Suporta tanto YYYYMM (Móvel) quanto "Fev/2026" (Fixo)
                ref_key = referencia_para_yyyymm(ref) or ref
                if ref_key:
                    pdf = buscar_pdf_existente(
                        self.config.download_dir,
                        self.PDF_PREFIXO,
                        cnpj_str,
                        item.get("codigo_cliente", ""),
                        ref_key,
                    )
                    if pdf:
                        item["arquivo_download"] = str(pdf)
                        item["download_ok"] = True
            enriquecer_com_extrator(item)

        if resultados:
            self.runtime["faturas_disponiveis"] = resultados

        self.debug.capture(page, "04_final", self.runtime)
        return page


# ---------------------------------------------------------------------------
# Referência → YYYYMM
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
# buscar_pdf_existente
# ---------------------------------------------------------------------------

def buscar_pdf_existente(
    download_dir: Path,
    prefixo: str,
    cnpj: str,
    conta: str,
    referencia: str,
) -> "Path | None":
    """Procura PDF já baixado para a combinação prefixo/cnpj/conta/referencia."""
    cnpj_s = re.sub(r"\D", "", cnpj)
    conta_s = re.sub(r"\D", "", conta)
    padrao = f"{prefixo}-{cnpj_s}-{conta_s}-{referencia}*.pdf"
    candidatos = sorted(download_dir.glob(padrao))
    return candidatos[0] if candidatos else None


# ---------------------------------------------------------------------------
# DownloadPanelService — operações compartilhadas no painel de download
# ---------------------------------------------------------------------------

class DownloadPanelService:
    """Gerencia o painel flutuante de downloads do portal Vivo Empresas."""

    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def cancelar_dialog_se_visivel(self, page: Any) -> None:
        """Fecha o dialog 'Cancelar download' se aparecer."""
        try:
            btn = page.locator(
                "[data-test-close-dialog], button[aria-label*='Cancelar download']"
            ).first
            if btn.count() > 0 and btn.is_visible():
                BrowserActions.click_with_fallback(btn, timeout_ms=3000)
                page.wait_for_timeout(500)
                self.logger.log("info", "Dialog cancelar clicado")
                for sel in [
                    "button:has-text('Sim, cancelar')",
                    "button:has-text('Sim')",
                    "[data-test-confirm-cancel]",
                ]:
                    b = page.locator(sel).first
                    if b.count() > 0 and b.is_visible():
                        BrowserActions.click_with_fallback(b, timeout_ms=3000)
                        page.wait_for_timeout(500)
                        break
        except Exception:
            pass

    def minimizar(self, page: Any) -> None:
        """Cancela dialog pendente e minimiza o painel de download."""
        self.cancelar_dialog_se_visivel(page)
        try:
            btn = page.locator("[data-test-minimize-dialog]").first
            if btn.count() > 0:
                cls = btn.get_attribute("class") or ""
                if "opened" in cls:
                    BrowserActions.click_with_fallback(btn, timeout_ms=3000)
                    page.wait_for_timeout(300)
                    self.logger.log("info", "Painel minimizado")
        except Exception:
            pass
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
        except Exception:
            pass

    def tem_falha(self, page: Any) -> bool:
        """Verifica se o painel mostra 'Tentar novamente' (falha do servidor)."""
        try:
            return (
                page.locator(
                    "li.download-item:has-text('Tentar novamente'), "
                    "li.download-item:has-text('Falha no download')"
                ).count()
                > 0
            )
        except Exception:
            return False

    def aguardar_download(
        self,
        page: Any,
        btn: Any,
        label: str = "",
        timeout_ms: int = 60000,
    ) -> "tuple[Path | None, str]":
        """Clica no botão e monitora download via polling a cada 2s.

        Detecta falha no painel rapidamente para não esperar o timeout inteiro.
        """
        downloaded: list[Any] = []

        def on_download(dl: Any) -> None:
            downloaded.append(dl)

        page.on("download", on_download)
        try:
            BrowserActions.click_with_fallback(btn, timeout_ms=5000)
        except Exception as exc:
            page.remove_listener("download", on_download)
            return None, f"Falha ao clicar: {exc}"

        elapsed = 0
        poll_ms = 2000
        while elapsed < timeout_ms:
            if downloaded:
                page.remove_listener("download", on_download)
                if label:
                    self.logger.log("ok", "Download capturado", label=label)
                return Path(downloaded[0].path()), ""
            if self.tem_falha(page):
                page.remove_listener("download", on_download)
                self.logger.log("warn", "Falha detectada no painel", label=label)
                return None, "Falha detectada no painel (Tentar novamente)"
            page.wait_for_timeout(poll_ms)
            elapsed += poll_ms

        page.remove_listener("download", on_download)
        return None, f"Timeout {timeout_ms}ms aguardando download"


# ---------------------------------------------------------------------------
# imprimir_tabela_listagem
# ---------------------------------------------------------------------------

def imprimir_tabela_listagem(
    faturas: list[dict[str, Any]],
    titulo: str = "FATURAS VIVO",
) -> None:
    """Imprime tabela formatada usando dados já enriquecidos nos itens."""
    linhas = []
    for f in faturas:
        conta   = f.get("codigo_cliente", "")
        ref     = f.get("referencia", "")
        venc    = f.get("vencimento", "") or f.get("data_vencimento", "") or "-"
        sit     = f.get("situacao", "")
        valor   = f.get("valor", "") or "-"
        arquivo = f.get("arquivo_download", "") or "-"
        cb = (
            f.get("codigo_de_barras_sem_espaco")
            or f.get("codigo_barras_digitavel_sem_espaco")
            or ""
        )
        pix = (f.get("pix_copia_cola") or "")[:40]
        linhas.append({
            "conta": conta, "ref": ref, "vencimento": venc,
            "situacao": sit, "valor": valor, "arquivo": arquivo,
            "codigo_barras": cb or "-", "pix": pix or "-",
        })

    if not linhas:
        print("\n[info] Nenhuma fatura encontrada para listar.")
        return

    col_conta = max(len("Conta"),      max(len(r["conta"])      for r in linhas))
    col_ref   = max(len("Referência"), max(len(r["ref"])        for r in linhas))
    col_venc  = max(len("Vencimento"), max(len(r["vencimento"]) for r in linhas))
    col_sit   = max(len("Situação"),   max(len(r["situacao"])   for r in linhas))
    col_valor = max(len("Valor"),      max(len(r["valor"])      for r in linhas))
    col_arq   = max(len("Arquivo"),    max(len(r["arquivo"])    for r in linhas))

    sep = (
        f"+{'-'*(col_conta+2)}+{'-'*(col_ref+2)}+{'-'*(col_venc+2)}"
        f"+{'-'*(col_sit+2)}+{'-'*(col_valor+2)}+{'-'*(col_arq+2)}+"
    )
    fmt = (
        f"| {{:<{col_conta}}} | {{:<{col_ref}}} | {{:<{col_venc}}}"
        f" | {{:<{col_sit}}} | {{:<{col_valor}}} | {{:<{col_arq}}} |"
    )

    print(f"\n{'='*len(sep)}")
    print(f"  {titulo}")
    print(sep)
    print(fmt.format("Conta", "Referência", "Vencimento", "Situação", "Valor", "Arquivo"))
    print(sep)
    for r in linhas:
        print(fmt.format(r["conta"], r["ref"], r["vencimento"], r["situacao"], r["valor"], r["arquivo"]))
        if r["codigo_barras"] != "-":
            print(f"|  Cód.Barras: {r['codigo_barras']}")
        if r["pix"] != "-":
            print(f"|  PIX:        {r['pix']}")
    print(sep)
    print(f"  Total: {len(linhas)} fatura(s)")
    print(f"{'='*len(sep)}\n")
