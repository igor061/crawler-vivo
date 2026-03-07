#!/usr/bin/env python3
# Requisitos (runtime):
# - scrapling
# - playwright

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from scrapling.fetchers import StealthyFetcher

from vivo_download import (
    AppConfig,
    AuthService,
    BrowserActions,
    Logger,
    carregar_env_arquivo,
    extrair_cnpj_da_pagina,
    normalize_document,
    resolve_headless,
)

DEFAULT_URL = "https://mve.vivo.com.br/oauth?logout=true"
DEFAULT_DASHBOARD_URL = "https://mve.vivo.com.br/sec/dashboard"
DEFAULT_INVOICES_URL = "https://mve.vivo.com.br/sec/invoices"


_MESES_PT: dict[str, str] = {
    "jan": "01", "fev": "02", "mar": "03", "abr": "04",
    "mai": "05", "jun": "06", "jul": "07", "ago": "08",
    "set": "09", "out": "10", "nov": "11", "dez": "12",
}


def referencia_para_yyyymm(referencia: str) -> str:
    """Converte 'Fev/2026' ou 'Fevereiro/2026' → '202602'. Retorna '' se não parsear."""
    m = re.match(r"([A-Za-zÀ-ú]+)[/\-](\d{4})", referencia.strip())
    if not m:
        return ""
    mes_str = m.group(1)[:3].lower()
    # normaliza acentos simples
    mes_str = mes_str.replace("á", "a").replace("ã", "a").replace("é", "e").replace("ê", "e")
    ano = m.group(2)
    mes_num = _MESES_PT.get(mes_str, "")
    if not mes_num:
        return ""
    return f"{ano}{mes_num}"


def coleta_data_hora_gmt_menos3() -> datetime:
    return datetime.now(timezone(timedelta(hours=-3)))


def format_coleta_data_hora(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class FixoConfig:
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
    limite: int | None
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
        return self.output_dir / f"debug_fixo_{stamp}"


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------

class DebugCollector:
    def __init__(self, config: FixoConfig, logger: Logger) -> None:
        self.config = config
        self.logger = logger
        self.counter = 0
        if config.debug:
            config.debug_dir.mkdir(parents=True, exist_ok=True)

    def capture(self, page: Any, etapa: str, runtime: dict[str, Any]) -> None:
        self.counter += 1
        base = f"{self.counter:02d}_{etapa}"
        # Sempre executa screenshot e captura de HTML para garantir timing consistente.
        # Só salva em disco quando --debug está ativo.
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
            runtime["debug_paginas"] = int(runtime["debug_paginas"]) + 1
            self.logger.log("info", "Snapshot debug coletado", etapa=etapa, html=html_path.name)
        else:
            # Executa sem salvar — mantém o mesmo timing que o modo debug
            try:
                page.screenshot(full_page=True)
            except Exception:
                pass
            try:
                page.content()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Troca de contexto Móvel → Fixo
# Seletores confirmados via análise do HTML real:
#   - Botão header: li#service-select-desktop[data-e2e-context-mobile-menu]
#   - Item Vivo Fixo no slider: li[data-service-id="WIR"]
# ---------------------------------------------------------------------------

class ContextSwitchService:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def selecionar_vivo_fixo(self, page: Any, config: FixoConfig) -> bool:
        self.logger.log("info", "Clicando no menu de servicos no header")

        # Botão "Vivo Móvel ∨" no header
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

        # "Vivo Fixo" no slider lateral — seletor estável: data-service-id="WIR"
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

        # Aguarda o contexto mudar (typeService passa para WIRELINE)
        page.wait_for_timeout(config.wait_ms)
        self.logger.log("ok", "Contexto Vivo Fixo selecionado")
        return True


# ---------------------------------------------------------------------------
# Coleta de metadados das faturas Fixo
# Estrutura confirmada via HTML real:
#   section.mve-grid → p[data-test-line-title], [data-test-secondary-info]
#   [data-test-invoices].invoice → [data-test-invoice-amount], [data-test-invoice-due-date]
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
        # Código do cliente (ex: 899933550000)
        codigo_cliente = ""
        try:
            el = sec.locator("[data-test-secondary-info] span").first
            if el.count() > 0:
                codigo_cliente = el.inner_text(timeout=1500).strip()
        except Exception:
            pass

        # Endereço/linha title
        linha_titulo = ""
        try:
            el = sec.locator("[data-test-line-title]").first
            if el.count() > 0:
                linha_titulo = el.inner_text(timeout=1500).strip()
        except Exception:
            pass

        # Faturas dentro desta seção
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
                    # classe contém PAID, OPEN, etc.
                    cls = el.get_attribute("class") or ""
                    if "PAID" in cls.upper():
                        situacao = "Paga"
                    elif "OPEN" in cls.upper() or "DUE" in cls.upper():
                        situacao = "Aberta"
            except Exception:
                pass
            faturas.append({
                "valor": valor,
                "referencia": referencia,
                "situacao": situacao,
            })

        secoes.append({
            "codigo_cliente": codigo_cliente,
            "linha_titulo": linha_titulo,
            "faturas": faturas,
            # referência ao locator da seção para clicar no dropdown depois
            "_sec_locator": sec,
        })

    return secoes


# ---------------------------------------------------------------------------
# Download via painel "Download de arquivos"
# Estrutura confirmada via HTML real:
#   [data-test-drop-down] button.dropdown-toggle  → "Baixar agora"
#   button[data-e2e-download-bills=""]             → "Outros formatos" (abre painel PDF)
#   li.download-item[data-download-item]           → cada item no painel
#   i[data-download-available][aria-label="Baixar arquivo"] → botão de download
# ---------------------------------------------------------------------------

class FixoDownloadService:
    """
    Baixa faturas Vivo Fixo clicando em cada linha da grade:
      dropdown 'Baixar agora' → 'Todas em boleto (.zip)'
    Isso dispara um download direto de arquivo ZIP contendo o(s) PDF(s).
    Os PDFs são extraídos do ZIP e salvos em download_dir.
    O painel 'Download de arquivos' NÃO é usado — é apenas um status/advisor.
    """

    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def _clicar_ver_detalhes(self, page: Any, sec_locator: Any) -> bool:
        """Clica em 'Ver detalhes' para expandir a seção antes de interagir."""
        btn = sec_locator.locator('button[data-test-detail-button]').first
        if btn.count() == 0:
            return True  # seção já expandida ou botão não presente
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

    def _clicar_baixar_agora(self, page: Any, sec_locator: Any) -> bool:
        """Abre o dropdown 'Baixar agora' na seção."""
        toggle = sec_locator.locator("[data-test-drop-down] button.dropdown-toggle").first
        if toggle.count() == 0:
            toggle = sec_locator.locator("button.dropdown-toggle").first
        if toggle.count() == 0:
            self.logger.log("warn", "Dropdown toggle nao encontrado")
            return False
        try:
            toggle.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        if not BrowserActions.click_with_fallback(toggle, timeout_ms=5000):
            self.logger.log("warn", "Falha ao clicar dropdown toggle")
            return False
        page.wait_for_timeout(400)
        return True

    def _minimizar_painel(self, page: Any) -> None:
        """Minimiza o painel 'Download de arquivos' se estiver aberto e rola a página para o topo."""
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
        """Re-abre o painel 'Download de arquivos' se estiver minimizado."""
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

    def _solicitar_zip(self, page: Any) -> bool:
        """
        Clica em 'Todas em boleto (.zip)' para enfileirar o download no servidor.
        O botão NÃO dispara download direto — adiciona item ao painel 'Download de arquivos'.
        """
        btn = page.locator('button[data-e2e-download-bills="invoice"]').first
        try:
            btn.wait_for(state="visible", timeout=4000)
        except Exception:
            pass
        if btn.count() == 0:
            self.logger.log("warn", "Botao Todas em boleto nao encontrado")
            return False
        BrowserActions.click_with_fallback(btn, timeout_ms=5000)
        page.wait_for_timeout(500)
        self.logger.log("info", "Download enfileirado no servidor")
        return True

    def _aguardar_e_baixar_do_painel(
        self,
        page: Any,
        codigo_cliente: str,
        tipo: str = "Boleto",
        timeout_ms: int = 120000,
    ) -> "tuple[Path | None, str]":
        """
        Aguarda item do painel 'Download de arquivos' ficar disponível e baixa.
        Filtra por código_cliente + tipo (ex: 'Boleto', 'Todas em boleto').
        """
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
                    tmp = Path(dl.path())
                    return tmp, ""
                except Exception as exc:
                    return None, str(exc)
            page.wait_for_timeout(3000)

        return None, f"Timeout aguardando download-available ({tipo}) para {codigo_cliente}"

    def _extrair_pdfs_do_zip(
        self,
        zip_path: Path,
        codigo_cliente: str,
        cnpj: str,
        referencia: str,
        download_dir: Path,
    ) -> list[Path]:
        """Extrai PDFs do ZIP e os salva com nome padronizado."""
        import zipfile
        salvos: list[Path] = []
        cnpj_seguro = normalize_document(cnpj) or "semcnpj"
        cod_seguro = re.sub(r"\D", "", codigo_cliente) or "semconta"
        ref_segura = re.sub(r"[/\\]", "-", referencia) if referencia else "0000-00"

        with zipfile.ZipFile(zip_path) as zf:
            pdfs = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
            for idx, nome in enumerate(pdfs, start=1):
                sufixo = f"-{idx}" if idx > 1 else ""
                base = f"vivo-fixo-{cnpj_seguro}-{cod_seguro}-{ref_segura}{sufixo}"
                target = download_dir / f"{base}.pdf"
                counter = 2
                while target.exists():
                    target = download_dir / f"{base}-{counter}.pdf"
                    counter += 1
                data = zf.read(nome)
                target.write_bytes(data)
                salvos.append(target)
        return salvos

    def _obter_toggles_por_linha(self, page: Any) -> list[Any]:
        """
        Retorna os toggles 'Baixar' por linha (exclui 'Baixar agora' da seção).
        Deve ser chamado APÓS clicar em 'Ver detalhes'.
        As linhas de detalhe são renderizadas fora de section.mve-grid,
        por isso buscamos no escopo da página inteira.
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

    def _baixar_boleto_por_linha(
        self,
        page: Any,
        toggle: Any,
        codigo_cliente: str,
        cnpj: str,
        idx_linha: int,
        referencia: str,
        config: "FixoConfig",
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Clica no dropdown 'Baixar' de uma linha, seleciona 'Boleto (.pdf)',
        aguarda o item no painel e baixa o arquivo.
        """
        # Minimizar antes de cada interação com a tela
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

        # Clica em 'Boleto (.pdf)' dentro do dropdown aberto (classe 'show')
        # O dropdown aberto tem class 'show'; filtrar para não clicar no botão
        # do dropdown de seção ('Baixar agora') que está fechado.
        self._minimizar_painel(page)
        btn_boleto = page.locator('div.dropdown.show button[data-e2e-download-bills="invoice"]').first
        try:
            btn_boleto.wait_for(state="visible", timeout=4000)
        except Exception:
            # Fallback: qualquer botão invoice visível na página
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

        self.logger.log("info", "Solicitando Boleto (.pdf)", linha=idx_linha)

        # Tenta download direto primeiro (expect_download com 30s)
        tmp: Path | None = None
        erro = ""
        try:
            with page.expect_download(timeout=30000) as dl_info:
                BrowserActions.click_with_fallback(btn_boleto, timeout_ms=5000)
            dl = dl_info.value
            tmp = Path(dl.path())
            self.logger.log("ok", "Download direto capturado", linha=idx_linha)
        except Exception:
            # Boleto (.pdf) pode ir para o painel em vez de download direto
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

        # Salva PDF com nome padronizado
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
        config: "FixoConfig",
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

            # 1. Minimizar painel + clicar Ver detalhes para expandir as linhas
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

            # Aguarda as linhas renderizarem após a expansão
            page.wait_for_timeout(config.wait_ms // 2)

            # 2. Coleta os toggles 'Baixar' por linha (excluindo 'Baixar agora')
            toggles = self._obter_toggles_por_linha(page)
            self.logger.log("info", "Linhas para download", total=len(toggles), conta=codigo_cliente)

            if not toggles:
                resultados.append({
                    "codigo_cliente": codigo_cliente,
                    "download_ok": False,
                    "arquivo_download": "",
                    "erro_download": "Nenhuma linha de download encontrada apos Ver detalhes",
                    "coleta_data_hora": config.coleta_data_hora,
                })
                continue

            # 3. Para cada linha, baixa o boleto individual (respeitando limite)
            faturas_sec = sec.get("faturas", [])
            limite = config.limite
            toggles_para_baixar = toggles if limite is None else toggles[:limite]
            self.logger.log(
                "info",
                "Faturas para download",
                total_disponiveis=len(toggles),
                baixando=len(toggles_para_baixar),
            )

            for idx, toggle in enumerate(toggles_para_baixar, start=1):
                referencia = faturas_sec[idx - 1].get("referencia", "") if idx - 1 < len(faturas_sec) else ""
                runtime["tentativas"] = int(runtime.get("tentativas", 0)) + 1
                resultado = self._baixar_boleto_por_linha(
                    page, toggle, codigo_cliente, cnpj, idx, referencia, config, runtime
                )
                resultados.append(resultado)
                page.wait_for_timeout(500)

        return resultados


# ---------------------------------------------------------------------------
# Enriquecimento pós-download via extrator de PDF

# ---------------------------------------------------------------------------

def _enriquecer_com_extrator(item: dict[str, Any]) -> dict[str, Any]:
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
    item["data_emissao"] = str(dados.get("data_emissao", "")).strip()
    data_venc = str(dados.get("data_vencimento", "")).strip()
    if data_venc:
        item["data_vencimento"] = data_venc
    valor_extraido = str(dados.get("valor", "")).strip()
    if valor_extraido:
        item["valor"] = valor_extraido
    return item


# ---------------------------------------------------------------------------
# App principal
# ---------------------------------------------------------------------------

class VivoDownloadFixoApp:
    def __init__(self, config: FixoConfig) -> None:
        self.config = config
        self.logger = Logger()
        self.debug = DebugCollector(config, self.logger)
        self.auth_service = AuthService(self.logger)
        self.context_service = ContextSwitchService(self.logger)
        self.download_service = FixoDownloadService(self.logger)
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

    def _page_action(self, page: Any) -> Any:
        self.logger.log("info", "Pagina inicial", url=self.config.url)
        page.wait_for_timeout(self.config.wait_ms)
        self.debug.capture(page, "01_login_page", self.runtime)

        if not self.auth_service.executar_login(
            page, cast(AppConfig, self.config), self.runtime
        ):
            self.logger.log("warn", "Login falhou")
            return page

        # Garante que chegamos ao dashboard antes de tentar trocar o contexto.
        # O AuthService pode retornar True mesmo sem dashboard_detectado=True
        # (timeout no wait_for_url), então navegamos explicitamente se necessário.
        if self.config.dashboard_url not in (page.url or ""):
            self.logger.log("info", "Navegando ao dashboard explicitamente")
            try:
                page.goto(self.config.dashboard_url, wait_until="domcontentloaded")
                page.wait_for_timeout(self.config.wait_ms)
            except Exception as exc:
                self.logger.log("warn", "Falha ao navegar ao dashboard", erro=str(exc))

        # Espera o botão de troca de serviço estar disponível
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

        # Armazena metadados (modo listar)
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

        # Enriquece com dados do PDF
        for item in resultados:
            _enriquecer_com_extrator(item)

        # Mescla metadados de download nos faturas_disponiveis
        if resultados:
            self.runtime["faturas_disponiveis"] = resultados

        self.debug.capture(page, "05_final", self.runtime)
        return page

    def run(self) -> Path:
        status = 0
        url = self.config.url
        try:
            response = StealthyFetcher.fetch(
                url=self.config.url,
                headless=resolve_headless(self.config.mode),
                timeout=self.config.timeout_ms,
                wait=self.config.wait_ms,
                page_action=self._page_action,
                humanize=True,
            )
            status = response.status
            url = response.url
        except Exception as exc:
            self.runtime["erro_execucao"] = str(exc)
            self.logger.log("erro", "Falha na execucao", erro=str(exc))

        output: dict[str, Any] = {
            "status": status,
            "url": url,
            "erro_execucao": self.runtime["erro_execucao"],
            "coleta_data_hora": self.config.coleta_data_hora,
            "cnpj_cliente": self.runtime["cnpj_cliente"],
            "campo_senha_detectado": self.runtime["campo_senha_detectado"],
            "senha_enviada": self.runtime["senha_enviada"],
            "dashboard_detectado": self.runtime["dashboard_detectado"],
            "contexto_fixo_ok": self.runtime["contexto_fixo_ok"],
            "faturas_aberto": self.runtime["faturas_aberto"],
            "modo_listar": self.config.listar,
            "tentativas": self.runtime["tentativas"],
            "downloads_ok": self.runtime["downloads_ok"],
            "downloads_falhos": self.runtime["downloads_falhos"],
            "faturas_disponiveis": self.runtime["faturas_disponiveis"],
            "debug_ativado": self.config.debug,
            "debug_diretorio": str(self.config.debug_dir) if self.config.debug else "",
            "debug_paginas": self.runtime["debug_paginas"],
        }

        cnpj = self.runtime["cnpj_cliente"] or "semcnpj"
        stamp = self.config.coleta_dt.strftime("%Y%m%d_%H%M%S")
        result_file = self.config.download_dir / f"vivo_fixo_resultado_{cnpj}_{stamp}.json"
        result_file.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

        self.logger.log("ok", "Execucao finalizada", status=status)
        self.logger.log("ok", "Resumo", downloads_ok=self.runtime["downloads_ok"])
        self.logger.log("ok", "Resumo", downloads_falhos=self.runtime["downloads_falhos"])
        self.logger.log("ok", "Resultado salvo", arquivo=result_file)
        return result_file


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download de faturas Vivo Fixo via portal Vivo Empresas"
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--invoices-url", default=DEFAULT_INVOICES_URL)
    parser.add_argument("--cpf", default=os.getenv("VIVO_CPF", ""))
    parser.add_argument("--password", default=os.getenv("VIVO_PASSWORD", ""))
    parser.add_argument("--output-dir", default="screenshots/scrapling")
    parser.add_argument("--download-dir", default="downloads/vivo")
    parser.add_argument("--wait-ms", type=int, default=5000)
    parser.add_argument("--timeout-ms", type=int, default=90000)
    parser.add_argument("--debug", action="store_true",
                        help="Salva snapshots HTML + screenshot por etapa")
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
        help="Modo do navegador (Camoufox); use --show para atalho headful",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=2,
        metavar="N",
        help="Numero maximo de faturas para baixar por conta (default: 2)",
    )
    parser.add_argument(
        "--todas",
        action="store_true",
        help="Baixar todas as faturas disponiveis (ignora --limite)",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> FixoConfig:
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    download_dir = Path(args.download_dir).expanduser().resolve()
    download_dir.mkdir(parents=True, exist_ok=True)
    mode = "headful" if getattr(args, "show", False) else args.mode
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
        mode=mode,
        limite=limite,
        coleta_dt=coleta_data_hora_gmt_menos3(),
    )


def main() -> None:
    carregar_env_arquivo(Path(".env"))
    args = parse_args()
    config = build_config(args)
    app = VivoDownloadFixoApp(config)
    app.run()


if __name__ == "__main__":
    main()
