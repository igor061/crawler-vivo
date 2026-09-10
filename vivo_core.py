#!/usr/bin/env python3
"""vivo_core.py — Biblioteca compartilhada para automação do portal Vivo Empresas.

Fornece utilitários, serviços e config base usados por vivo_movel.py e vivo_fixo.py.
"""

import argparse
import asyncio
import getpass
import inspect
import json
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
    engine: str = "camoufox"
    spike_login: bool = False
    warmup_ms: int = 0

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
                "el => {"
                "  const ev = new MouseEvent('click', "
                "    {bubbles: true, cancelable: true, view: window})"
                "  el.dispatchEvent(ev)"
                "}"
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


class _FetchResponse:
    def __init__(self, url: str, status: int) -> None:
        self.status = status
        self.url = url


# Em Linux sem hardware de midia o `navigator.mediaDevices.enumerateDevices()` do
# Camoufox nunca resolve (o browserforge marca `multimediaDevices` como Unsupported).
# Scripts antifraude que consultam a API travam e classificam a sessao como bot.
_PATCH_MEDIA_DEVICES = """
(() => {
  const fake = [
    {deviceId: 'default', kind: 'audioinput',  label: '', groupId: 'grp-mic'},
    {deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'grp-mic'},
    {deviceId: 'cam0',    kind: 'videoinput',  label: '', groupId: 'grp-cam'},
  ];
  const build = () => fake.map(d => {
    const o = Object.create(window.MediaDeviceInfo ? MediaDeviceInfo.prototype : Object.prototype);
    Object.defineProperties(o, {
      deviceId: {value: d.deviceId, enumerable: true},
      kind: {value: d.kind, enumerable: true},
      label: {value: d.label, enumerable: true},
      groupId: {value: d.groupId, enumerable: true},
      toJSON: {value: () => d},
    });
    return o;
  });
  const md = navigator.mediaDevices;
  if (!md) return;
  const orig = md.enumerateDevices;
  Object.defineProperty(md, 'enumerateDevices', {
    configurable: true,
    writable: true,
    value: function enumerateDevices() { return Promise.resolve(build()); },
  });
  try {
    Object.defineProperty(md.enumerateDevices, 'toString', {
      value: () => (orig ? orig.toString()
                         : 'function enumerateDevices() {\\n    [native code]\\n}'),
    });
  } catch (e) {}
})();
"""


def aplicar_patch_media_devices(page: Any, logger: "Logger | None" = None) -> None:
    """Instala dispositivos de midia falsos quando rodando em Linux sem hardware.

    O `add_init_script` so vale a partir da proxima navegacao, entao recarrega a
    pagina para que a pagina de login ja veja a API corrigida.
    """
    modo = os.getenv("VIVO_FIX_MEDIA_DEVICES", "auto")
    if modo == "off":
        return
    if modo == "auto" and not sys.platform.startswith("linux"):
        return
    try:
        page.add_init_script(_PATCH_MEDIA_DEVICES)
        page.reload(wait_until="domcontentloaded")
        if logger:
            logger.log("info", "Patch de mediaDevices aplicado")
    except Exception as exc:
        if logger:
            logger.log("warn", "Falha ao aplicar patch de mediaDevices", erro=str(exc))


def _resolver_proxy() -> "str | None":
    return (
        os.getenv("VIVO_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or None
    )


def _camoufox_preset_resolvido() -> dict[str, Any] | bool | None:
    """Resolve fingerprint_preset do Camoufox (presets reais, Firefox >= 149).

    VIVO_CAMOUFOX_PRESET: off desliga; on/random sorteia preset; um numero
    (ex.: 51) fixa o preset macOS com aquele indice no arquivo v150. Em 'auto'
    fixa o preset 51 (Apple M1, compativel com a DB WebGL do Camoufox) quando
    VIVO_CAMOUFOX_OS forcou o OS. 'on/random' pode crashar com presets cujo
    vendor WebGL nao esta na DB ("No WebGL data found").
    """
    valor = (os.getenv("VIVO_CAMOUFOX_PRESET") or "auto").lower()
    if valor == "off":
        return None
    if valor in ("on", "random"):
        return True
    if valor == "auto":
        if not os.getenv("VIVO_CAMOUFOX_OS"):
            return None
        valor = "51"
    try:
        from camoufox.utils import launch_options  # type: ignore[import-not-found]

        if "fingerprint_preset" not in inspect.signature(launch_options).parameters:
            return None
    except Exception:
        return None
    if not valor.isdigit():
        return None
    try:
        import json

        import camoufox

        preset_file = Path(camoufox.__file__).parent / "fingerprint-presets-v150.json"
        if not preset_file.exists():
            return None
        data = json.loads(preset_file.read_text(encoding="utf-8"))
        macos = data.get("presets", {}).get("macos", [])
        indice = int(valor)
        if indice < 0 or indice >= len(macos):
            return None
        return macos[indice]
    except Exception:
        return None


def _camoufox_preset_habilitado() -> bool:
    return _camoufox_preset_resolvido() is not None


def _executar_fetch_camoufox(
    config: BaseConfig,
    page_action: Callable[[Any], Any],
    runtime: dict[str, Any],
) -> Any:
    """Executa StealthyFetcher (Camoufox) com a page_action fornecida."""
    proxy = _resolver_proxy()
    if proxy:
        Logger().log("info", "Usando proxy", proxy=proxy)
    extra: dict[str, Any] = {}
    os_fingerprint = os.getenv("VIVO_CAMOUFOX_OS")
    if os_fingerprint:
        extra["additional_arguments"] = {"os": os_fingerprint}
        Logger().log("info", "Fingerprint de OS forcado", os=os_fingerprint)
    preset = _camoufox_preset_resolvido()
    if preset is not None:
        extra.setdefault("additional_arguments", {})["fingerprint_preset"] = preset
        if preset is True:
            desc = "sorteio"
        elif isinstance(preset, dict) and preset.get("navigator"):
            desc = "pin (macos v150)"
        else:
            desc = "pin"
        Logger().log("info", "Fingerprint preset (v150) habilitado", modo=desc)
    try:
        return StealthyFetcher.fetch(
            url=config.url,
            headless=resolve_headless(config.mode),
            timeout=config.timeout_ms,
            wait=config.wait_ms,
            page_action=page_action,
            humanize=True,
            proxy=proxy,
            geoip=bool(proxy),
            **extra,
        )
    except Exception as exc:
        runtime["erro_execucao"] = str(exc)
        Logger().log("erro", "Falha na execucao do browser", erro=str(exc))
        return _FallbackResponse(config.url)


def _executar_fetch_patchright(
    config: BaseConfig,
    page_action: Callable[[Any], Any],
    runtime: dict[str, Any],
) -> Any:
    """Executa o fluxo via Patchright (Playwright com patches) usando Chrome real.

    `channel='chrome'` usa o Google Chrome instalado (TLS/JA3 de navegador real),
    com fallback para o Chromium do Playwright caso o Chrome nao exista.
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        runtime["erro_execucao"] = "patchright nao instalado (pip install patchright)"
        Logger().log("erro", "patchright nao instalado")
        return _FallbackResponse(config.url)

    proxy = _resolver_proxy()
    if proxy:
        Logger().log("info", "Usando proxy", proxy=proxy)
    headless = resolve_headless(config.mode) is True
    try:
        with sync_playwright() as p:
            launch_kwargs: dict[str, Any] = {"headless": headless}
            try:
                launch_kwargs["channel"] = "chrome"
                browser = p.chromium.launch(**launch_kwargs)
            except Exception:
                launch_kwargs.pop("channel", None)
                Logger().log("info", "Chrome nao disponivel, usando Chromium do Playwright")
                browser = p.chromium.launch(**launch_kwargs)
            context_kwargs: dict[str, Any] = {"accept_downloads": True, "locale": "pt-BR"}
            if proxy:
                context_kwargs["proxy"] = {"server": proxy}
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.goto(config.url, wait_until="domcontentloaded")
            page_action(page)
            url = page.url
            browser.close()
            return _FetchResponse(url, 200)
    except Exception as exc:
        runtime["erro_execucao"] = str(exc)
        Logger().log("erro", "Falha na execucao do patchright", erro=str(exc))
        return _FallbackResponse(config.url)


class _AsyncLoop:
    """Bridge síncrona -> assíncrona para APIs async (nodriver roda em loop próprio)."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._rodar, daemon=True)
        self.thread.start()

    def _rodar(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def chamar(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def encerrar(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass
        self.thread.join(timeout=2)


# Playwright `:has-text('X')` -> CSS + filtro por texto, para usar com nodriver.
_HAS_TEXT_RE = re.compile(
    r"^(?P<css>.*?):has-text\((?P<quote>['\"])(?P<text>[^'\"]+)(?P=quote)\)(?P<rest>.*)$"
)


class _NdElement:
    """Envolve um `nodriver.Element` com a API Playwright que o fluxo usa."""

    def __init__(self, bridge: _AsyncLoop, tab: Any, el: Any) -> None:
        self._bridge = bridge
        self._tab = tab
        self._el = el

    def count(self) -> int:
        return 1 if self._el is not None else 0

    def is_visible(self) -> bool:
        if self._el is None:
            return False
        try:
            return bool(self._bridge.chamar(self._el.apply("(e) => e.offsetParent !== null")))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        if self._el is None:
            return False
        try:
            return bool(self._bridge.chamar(self._el.apply("(e) => !e.disabled")))
        except Exception:
            return False

    def fill(self, value: str) -> None:
        if self._el is None:
            raise RuntimeError("Elemento nao encontrado")
        try:
            self._bridge.chamar(self._el.send_keys(value))
        except Exception:
            self._bridge.chamar(self._el.set_text(value))

    def click(self, timeout: "int | None" = None) -> None:
        if self._el is None:
            raise RuntimeError("Elemento nao encontrado")
        self._bridge.chamar(self._el.click())

    def inner_text(self, timeout: "int | None" = None) -> str:
        if self._el is None:
            return ""
        try:
            return str(
                self._bridge.chamar(self._el.apply("(e) => e.innerText || e.textContent || ''"))
                or ""
            )
        except Exception:
            return ""

    def get_attribute(self, name: str, timeout: "int | None" = None) -> "str | None":
        if self._el is None:
            return None
        try:
            val = self._bridge.chamar(self._el.apply(f"(e) => e.getAttribute('{name}')"))
            return str(val) if val is not None else None
        except Exception:
            return None

    def evaluate(self, js: str) -> Any:
        if self._el is None:
            return None
        try:
            return self._bridge.chamar(self._el.apply(js))
        except Exception:
            return None

    def scroll_into_view_if_needed(self, timeout: "int | None" = None) -> None:
        if self._el is None:
            return
        try:
            self._bridge.chamar(self._el.scroll_into_view())
        except Exception:
            pass

    def wait_for(self, state: str = "visible", timeout: "int | None" = None) -> None:
        deadline = time.monotonic() + (timeout or 5000) / 1000
        while time.monotonic() < deadline:
            if self.is_visible():
                return
            time.sleep(0.3)
        raise RuntimeError("Elemento nao ficou visivel")

    def locator(self, selector: str) -> "_NdLocator":
        return _NdLocator(self._bridge, self._tab, selector, parent=self._el)

    def all(self) -> list["_NdElement"]:
        return self.locator("*")._buscar_wrapped()


class _NdLocator:
    def __init__(
        self, bridge: _AsyncLoop, tab: Any, selector: str, parent: Any = None
    ) -> None:
        self._bridge = bridge
        self._tab = tab
        self._selector = selector
        self._parent = parent

    def _contem_texto(self, el: Any, texto: str) -> bool:
        try:
            txt = str(self._bridge.chamar(el.apply("(e) => e.textContent || ''")) or "")
            return texto.lower() in txt.lower()
        except Exception:
            return False

    def _query_css(self, css: str) -> list[Any]:
        if not css:
            return []
        try:
            if self._parent is not None:
                return list(self._bridge.chamar(self._parent.query_selector_all(css)))
            return list(self._bridge.chamar(self._tab.query_selector_all(css)))
        except Exception:
            return []

    def _buscar(self) -> list[Any]:
        match = _HAS_TEXT_RE.match(self._selector)
        if not match:
            return self._query_css(self._selector)
        css = (match.group("css") or "*").strip()
        texto = match.group("text")
        return [el for el in self._query_css(css) if self._contem_texto(el, texto)]

    def _buscar_wrapped(self) -> list[_NdElement]:
        return [_NdElement(self._bridge, self._tab, el) for el in self._buscar()]

    @property
    def first(self) -> _NdElement:
        els = self._buscar()
        return _NdElement(self._bridge, self._tab, els[0] if els else None)

    def count(self) -> int:
        return len(self._buscar())

    def all(self) -> list[_NdElement]:
        return self._buscar_wrapped()


class _NdKeyboard:
    def __init__(self, bridge: _AsyncLoop, tab: Any) -> None:
        self._bridge = bridge
        self._tab = tab

    def press(self, key: str) -> None:
        if key != "Enter":
            return
        try:
            self._bridge.chamar(
                self._tab.evaluate(
                    """(() => {
                        const el = document.activeElement;
                        if (!el || !el.form) return false;
                        try {
                            el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit();
                            return true;
                        }
                        catch (e) { return false; }
                    })()""",
                    return_by_value=True,
                )
            )
        except Exception:
            pass


class _NdTabAdapter:
    """Adapta `nodriver.Tab` para a API Playwright usada por AuthService/BaseVivoApp."""

    def __init__(self, bridge: _AsyncLoop, tab: Any) -> None:
        self._bridge = bridge
        self._tab = tab
        self.keyboard = _NdKeyboard(bridge, tab)

    @property
    def url(self) -> str:
        try:
            return str(
                self._bridge.chamar(self._tab.evaluate("location.href", return_by_value=True))
                or ""
            )
        except Exception:
            return ""

    def wait_for_timeout(self, ms: int) -> None:
        self._bridge.chamar(self._tab.wait(ms / 1000))

    def locator(self, selector: str) -> _NdLocator:
        return _NdLocator(self._bridge, self._tab, selector)

    def content(self) -> str:
        try:
            return str(self._bridge.chamar(self._tab.get_content()) or "")
        except Exception:
            return ""

    def screenshot(self, path: "str | None" = None, full_page: bool = False) -> None:
        try:
            if path:
                self._bridge.chamar(self._tab.save_screenshot(filename=path, full_page=full_page))
            else:
                self._bridge.chamar(
                    self._tab.save_screenshot(full_page=full_page, as_base64=True)
                )
        except Exception:
            pass

    def goto(self, url: str, wait_until: "str | None" = None) -> None:
        self._bridge.chamar(self._tab.get(url))

    def reload(self, wait_until: "str | None" = None) -> None:
        self._bridge.chamar(self._tab.reload())

    def add_init_script(self, script: str) -> None:
        return None

    def evaluate(self, expr: str) -> Any:
        try:
            return self._bridge.chamar(self._tab.evaluate(expr, return_by_value=True))
        except Exception:
            return None

    def wait_for_url(self, glob: str, timeout: int) -> bool:
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            if "sec/dashboard" in self.url:
                return True
            time.sleep(0.5)
        raise RuntimeError(f"URL nao atingida em {timeout}ms")

    def on(self, event: str, cb: Callable[..., Any]) -> None:
        return None

    def remove_listener(self, event: str, cb: Callable[..., Any]) -> None:
        return None

    def close(self) -> None:
        try:
            self._bridge.chamar(self._tab.close())
        except Exception:
            pass


def _executar_fetch_nodriver(
    config: BaseConfig,
    page_action: Callable[[Any], Any],
    runtime: dict[str, Any],
) -> Any:
    """Executa o fluxo via nodriver (Chrome real, CDP direto, sem shim de Playwright).

    Foco do spike: comparar a taxa de sucesso do LOGIN. Downloads nao sao
    suportados nessa engine — use --spike-login.
    """
    try:
        import nodriver
    except ImportError:
        runtime["erro_execucao"] = "nodriver nao instalado (pip install nodriver)"
        Logger().log("erro", "nodriver nao instalado")
        return _FallbackResponse(config.url)

    if not config.spike_login:
        Logger().log(
            "warn",
            "Engine nodriver: downloads nao suportados; use --spike-login p/ comparar o login",
        )

    proxy = _resolver_proxy()
    if proxy:
        Logger().log("info", "Usando proxy", proxy=proxy)
    bridge = _AsyncLoop()
    browser_args: list[str] = []
    if proxy:
        browser_args.append(f"--proxy-server={proxy}")

    try:
        browser = bridge.chamar(
            nodriver.start(
                headless=config.mode == "headless",
                lang="pt-BR",
                browser_args=browser_args or None,
            )
        )
        tab = bridge.chamar(browser.get(config.url))
        page = _NdTabAdapter(bridge, tab)
        page_action(page)
        url = page.url
        try:
            browser.stop()
        except Exception:
            pass
        return _FetchResponse(url, 200)
    except Exception as exc:
        runtime["erro_execucao"] = str(exc)
        Logger().log("erro", "Falha na execucao do nodriver", erro=str(exc))
        return _FallbackResponse(config.url)
    finally:
        bridge.encerrar()


def executar_fetch(
    config: BaseConfig,
    page_action: Callable[[Any], Any],
    runtime: dict[str, Any],
) -> Any:
    """Executa o fluxo com a engine selecionada (camoufox|patchright|nodriver).

    Para camoufox, se a senha foi enviada mas o antifraude rejeitou a sessao
    (falso OAM-2 / dashboard nao detectado), re-tenta com nova sessao/fingerprint
    (VIVO_LOGIN_RETRIES, default 3; intervalo VIVO_LOGIN_RETRY_BACKOFF, default 300s
    conforme o cooldown observado do antifraude ~5min).
    """
    engine = getattr(config, "engine", "camoufox")
    if engine == "patchright":
        return _executar_fetch_patchright(config, page_action, runtime)
    if engine == "nodriver":
        return _executar_fetch_nodriver(config, page_action, runtime)

    max_retries = max(0, int(os.getenv("VIVO_LOGIN_RETRIES", "3")))
    backoff = max(0.0, float(os.getenv("VIVO_LOGIN_RETRY_BACKOFF", "300")))
    result: Any = None
    for tentativa in range(max_retries + 1):
        if tentativa:
            Logger().log("warn", "Retry de login (nova sessao)", tentativa=tentativa)
            time.sleep(backoff)
        result = _executar_fetch_camoufox(config, page_action, runtime)
        if runtime.get("dashboard_detectado"):
            return result
        motivo = "falha de browser" if runtime.get("erro_execucao") else "falso OAM-2"
        Logger().log(
            "warn",
            "Login nao confirmado, re-tentando",
            tentativa=tentativa,
            motivo=motivo,
        )
    return result


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


def imprimir_resumo_pendentes(
    faturas: list[dict[str, Any]], titulo: str = "FATURAS EM ATRASO"
) -> None:
    """Imprime resumo das faturas com situação Atrasada, Aberta ou Vencida."""
    pendentes = [
        f for f in faturas
        if (f.get("situacao") or "").lower() in ("atrasada", "aberta", "vencida", "pendente")
    ]
    if not pendentes:
        print("\n[ok] Nenhuma fatura em atraso.")
        return

    print(f"\n{'='*100}")
    print(f"  {titulo}")
    print(f"{'='*100}")
    for f in pendentes:
        conta = f.get("codigo_cliente") or f.get("identificador_fatura") or "?"
        ref = f.get("referencia") or "-"
        sit = f.get("situacao") or "-"
        venc = f.get("data_vencimento") or f.get("vencimento") or "-"
        valor = f.get("valor") or "-"
        tel = f.get("telefone") or "(nao encontrado)"
        cb = (
            f.get("codigo_de_barras_sem_espaco")
            or f.get("codigo_barras_digitavel_sem_espaco")
            or "-"
        )
        pix = f.get("pix_copia_cola") or "-"

        print(f"\n  Conta:      {conta}")
        print(f"  Referência: {ref}")
        print(f"  Situação:   {sit}")
        print(f"  Vencimento: {venc}")
        print(f"  Valor:      {valor}")
        print(f"  Telefone:   {tel}")
        print(f"  Cód.Barras: {cb}")
        print(f"  PIX:        {pix}")
        print(f"  {'-'*96}")

    print(f"  Total em atraso: {len(pendentes)} fatura(s)")
    print(f"{'='*100}")


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
    parser.add_argument(
        "--engine",
        choices=["camoufox", "patchright", "nodriver"],
        default=os.getenv("VIVO_ENGINE", "camoufox"),
        help="Browser/fetcher a usar (default: camoufox)",
    )
    parser.add_argument(
        "--spike-login",
        action="store_true",
        help="Para apos validar o login, para comparar engines sem baixar faturas",
    )
    parser.add_argument(
        "--warmup-ms",
        type=int,
        default=int(os.getenv("VIVO_WARMUP_MS", "0")),
        help="Atraso apos carregar a pagina de login antes de preencher CPF/senha "
        "(warm-up do beacon antifraude, ex.: 20000)",
    )


def resolve_engine(args: argparse.Namespace) -> str:
    engine = getattr(args, "engine", "") or os.getenv("VIVO_ENGINE", "") or "camoufox"
    if engine not in ("camoufox", "patchright", "nodriver"):
        Logger().log("warn", "Engine desconhecida, usando camoufox", engine=engine)
        return "camoufox"
    return engine


def resolve_spike_login(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "spike_login", False))


def resolve_warmup_ms(args: argparse.Namespace) -> int:
    return max(0, int(getattr(args, "warmup_ms", 0) or 0))


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
    lines = [
        line
        for line in lines
        if not line.startswith("VIVO_CPF=") and not line.startswith("VIVO_PASSWORD=")
    ]
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
    print("  Credenciais do Vivo Empresas nao configuradas.")
    print(f"  Informe abaixo (timeout {timeout}s por campo, Ctrl+C cancela):")
    print()

    if not normalize_document(cpf):
        val = _ler_com_timeout(
            "  CPF ou CNPJ do Vivo Empresas (somente numeros): ", timeout=timeout
        )
        if not val or not normalize_document(val):
            print("\n[erro] Timeout ou valor invalido.")
            _imprimir_ajuda_credenciais()
            return "", ""
        cpf = val.strip()

    if not password:
        val = _ler_com_timeout("  Senha do Vivo Empresas: ", timeout=timeout, senha=True)
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
        # Lê o JSON salvo para ter todos os campos enriquecidos (PIX via QR, etc.)
        try:
            import json
            with open(result_file, encoding="utf-8") as fh:
                resultado_json = json.load(fh)
            faturas_json = resultado_json.get("faturas_disponiveis", [])
        except Exception:
            faturas_json = self.runtime["faturas_disponiveis"]
        imprimir_resumo_pendentes(
            faturas_json,
            titulo=f"FATURAS EM ATRASO — {self.TITULO_TABELA.replace('LISTAGEM DE FATURAS ', '')}",
        )
        self.logger.log("ok", "Resultado salvo", arquivo=result_file)
        return result_file

    def _page_action(self, page: Any) -> Any:
        aplicar_patch_media_devices(page, self.logger)
        self.logger.log("info", "Pagina inicial", url=self.config.url)
        warmup = getattr(self.config, "warmup_ms", 0) or 0
        if warmup > 0:
            self.logger.log("info", "Warm-up em pagina neutra", ms=warmup)
            try:
                page.goto("https://www.google.com", wait_until="domcontentloaded")
                page.wait_for_timeout(warmup)
            except Exception as exc:
                self.logger.log("warn", "Warm-up neutro falhou", erro=str(exc))
            page.goto(self.config.url, wait_until="domcontentloaded")
        page.wait_for_timeout(self.config.wait_ms)
        self.debug.capture(page, "01_login_page", self.runtime)

        if not self.auth_service.executar_login(page, self.config, self.runtime):
            self.logger.log("warn", "Login falhou")
            return page
        self.debug.capture(page, "02_apos_login", self.runtime)

        if getattr(self.config, "spike_login", False):
            self.logger.log("ok", "Spike de login concluido (parando apos o login)")
            return page

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
        print(
            fmt.format(
                r["conta"],
                r["ref"],
                r["vencimento"],
                r["situacao"],
                r["valor"],
                r["arquivo"],
            )
        )
        if r["codigo_barras"] != "-":
            print(f"|  Cód.Barras: {r['codigo_barras']}")
        if r["pix"] != "-":
            print(f"|  PIX:        {r['pix']}")
    print(sep)
    print(f"  Total: {len(linhas)} fatura(s)")
    print(f"{'='*len(sep)}\n")
