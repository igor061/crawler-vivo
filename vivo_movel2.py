#!/usr/bin/env python3
"""vivo_movel2.py — Download de contas detalhadas Vivo Móvel.

Fluxo: login → /sec/invoices → por conta: Exibir detalhes →
       por fatura: Opções → Conta detalhada e nota fiscal (.pdf)

Baseado em vivo_fixo.py. Usa seletores CSS em vez de XPath.
Padrão de execução: headful (janela visível).
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
from vivo_core import ano_mes_por_vencimento
from vivo_fixo import referencia_para_yyyymm


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class MovelDetalheConfig(BaseConfig):
    limite: int | None = 2
    force: bool = False

    @property
    def debug_dir(self) -> Path:
        stamp = self.coleta_dt.strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"debug_movel2_{stamp}"


# ---------------------------------------------------------------------------
# Coleta de metadados das seções Móvel
# ---------------------------------------------------------------------------

def _extrair_secoes_movel(page: Any, config: MovelDetalheConfig, logger: Logger) -> list[dict[str, Any]]:
    """Coleta metadados de todas as seções/contas da grade Vivo Móvel.

    Estrutura confirmada via HTML real:
      section[data-test-invoices-line-grid]
        [data-test-detail-button] button  → "Exibir detalhes"
        h4[aria-label^="Conta:"]          → número da conta
        .data-card-section__secondColumn .data-card-cell__description → valor
        .data-card-section__thirdColumn  .data-card-cell__description → vencimento
        .badge p                          → situacao
    """
    secoes: list[dict[str, Any]] = []

    try:
        page.locator("[data-test-invoices-line-grid]").first.wait_for(state="visible", timeout=15000)
    except Exception:
        logger.log("warn", "Grid data-test-invoices-line-grid nao encontrado")

    sections = page.locator("section[data-test-invoices-line-grid]").all()
    logger.log("info", f"Secoes encontradas: {len(sections)}")

    for sec in sections:
        # Número da conta — ex: h4 aria-label="Conta: 0466032796"
        codigo_cliente = ""
        try:
            el = sec.locator("h4[aria-label^='Conta:']").first
            if el.count() > 0:
                texto = el.inner_text(timeout=1500).strip()
                m = re.search(r"Conta:\s*(\S+)", texto)
                codigo_cliente = m.group(1) if m else texto
        except Exception:
            pass

        # Valor da fatura
        valor = ""
        try:
            el = sec.locator(".data-card-section__secondColumn .data-card-cell__description").first
            if el.count() > 0:
                valor = el.inner_text(timeout=1500).replace("\xa0", " ").strip()
        except Exception:
            pass

        # Vencimento — formato dd/MM/yyyy
        vencimento = ""
        try:
            el = sec.locator(".data-card-section__thirdColumn .data-card-cell__description").first
            if el.count() > 0:
                vencimento = el.inner_text(timeout=1500).strip()
        except Exception:
            pass

        # Situação
        situacao = ""
        try:
            el = sec.locator(".badge p").first
            if el.count() > 0:
                situacao = el.inner_text(timeout=1500).strip()
        except Exception:
            pass

        # Converte vencimento dd/MM/yyyy → YYYYMM, com fallback para texto "Fev/2026"
        ano, mes = ano_mes_por_vencimento(vencimento)
        referencia_yyyymm = f"{ano}{mes}" if ano != "0000" else referencia_para_yyyymm(vencimento)

        secoes.append({
            "codigo_cliente": codigo_cliente,
            "linha_titulo": codigo_cliente,
            "telefone_conta": "",
            "valor": valor,
            "vencimento": vencimento,
            "referencia": referencia_yyyymm,
            "situacao": situacao,
            "faturas": [{"valor": valor, "referencia": referencia_yyyymm, "situacao": situacao}],
            "_sec_locator": sec,
        })

    return secoes


# ---------------------------------------------------------------------------
# DownloadService Móvel (Exibir detalhes → Opções → Conta detalhada)
# ---------------------------------------------------------------------------

class MovelDetalheDownloadService:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def _cancelar_download_se_existir(self, page: Any) -> None:
        """Fecha o slide/dialog 'Cancelar download' se ele aparecer, clicando em 'Sim, cancelar'."""
        try:
            btn_cancelar = page.locator("[data-test-close-dialog], button[aria-label*='Cancelar download']").first
            if btn_cancelar.count() > 0 and btn_cancelar.is_visible():
                BrowserActions.click_with_fallback(btn_cancelar, timeout_ms=3000)
                page.wait_for_timeout(500)
                self.logger.log("info", "Dialog cancelar download clicado")
                # Confirma: "Sim, cancelar"
                for seletor in [
                    "button:has-text('Sim, cancelar')",
                    "button:has-text('Sim')",
                    "[data-test-confirm-cancel]",
                ]:
                    btn_sim = page.locator(seletor).first
                    if btn_sim.count() > 0 and btn_sim.is_visible():
                        BrowserActions.click_with_fallback(btn_sim, timeout_ms=3000)
                        page.wait_for_timeout(500)
                        self.logger.log("info", "Confirmado: Sim cancelar")
                        break
        except Exception:
            pass

    def _minimizar_painel(self, page: Any) -> None:
        self._cancelar_download_se_existir(page)
        try:
            btn = page.locator('[data-test-minimize-dialog]').first
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

    def _clicar_exibir_detalhes(self, page: Any, sec_locator: Any) -> bool:
        """Clica em 'Exibir detalhes' para expandir a conta.

        Seletor confirmado via HTML: [data-test-detail-button] button
        aria-label="Exibir detalhes da conta"
        """
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
            # Aguarda o slide abrir — espera pelos botões "Baixar fatura" aparecerem
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

    def _painel_tem_falha(self, page: Any) -> bool:
        """Verifica se o painel de downloads mostra 'Tentar novamente' (falha do servidor)."""
        try:
            loc = page.locator(
                "li.download-item:has-text('Tentar novamente'), "
                "li.download-item:has-text('Falha no download')"
            )
            return loc.count() > 0
        except Exception:
            return False

    def _aguardar_download_monitorando(
        self,
        page: Any,
        btn_detalhe: Any,
        idx_linha: int,
        timeout_ms: int = 60000,
    ) -> "tuple[Path | None, str]":
        """Clica no botão e monitora o download a cada 2s sem sleep longo.

        Detecta 'Tentar novamente' no painel rapidamente para não esperar o timeout inteiro.
        Usa page.on('download') para capturar o evento sem bloquear o polling.
        """
        downloaded: list[Any] = []

        def on_download(dl: Any) -> None:
            downloaded.append(dl)

        page.on("download", on_download)
        try:
            BrowserActions.click_with_fallback(btn_detalhe, timeout_ms=5000)
        except Exception as exc:
            page.remove_listener("download", on_download)
            return None, f"Falha ao clicar: {exc}"

        elapsed = 0
        poll_ms = 2000
        while elapsed < timeout_ms:
            if downloaded:
                page.remove_listener("download", on_download)
                self.logger.log("ok", "Download capturado", linha=idx_linha)
                return Path(downloaded[0].path()), ""

            if self._painel_tem_falha(page):
                page.remove_listener("download", on_download)
                self.logger.log("warn", "Falha detectada no painel", linha=idx_linha)
                return None, "Falha detectada no painel (Tentar novamente)"

            page.wait_for_timeout(poll_ms)
            elapsed += poll_ms

        page.remove_listener("download", on_download)
        return None, f"Timeout {timeout_ms}ms aguardando download"

    def _recarregar_e_abrir_secao(
        self,
        page: Any,
        config: "MovelDetalheConfig",
        codigo_cliente: str,
    ) -> "tuple[Any | None, list[dict[str, Any]]]":
        """Recarrega /sec/invoices, re-encontra a conta e abre o slide de detalhes."""
        self.logger.log("info", "Recarregando pagina para retry", conta=codigo_cliente)
        try:
            page.goto(config.invoices_url, wait_until="domcontentloaded")
            page.wait_for_timeout(config.wait_ms)
        except Exception as exc:
            self.logger.log("warn", "Erro ao recarregar pagina", erro=str(exc))
            return None, []

        sec_locator = None
        try:
            sections = page.locator("section[data-test-invoices-line-grid]").all()
            for s in sections:
                try:
                    el = s.locator("h4[aria-label^='Conta:']").first
                    if el.count() > 0 and codigo_cliente in (el.inner_text(timeout=1500) or ""):
                        sec_locator = s
                        break
                except Exception:
                    pass
        except Exception as exc:
            self.logger.log("warn", "Erro ao re-encontrar secao", erro=str(exc))
            return None, []

        if not sec_locator:
            self.logger.log("warn", "Secao nao encontrada apos reload", conta=codigo_cliente)
            return None, []

        self._minimizar_painel(page)
        if not self._clicar_exibir_detalhes(page, sec_locator):
            return None, []

        page.wait_for_timeout(config.wait_ms // 2)
        opcoes = self._obter_opcoes_no_slide(page)
        return sec_locator, opcoes

    def _fechar_slide(self, page: Any) -> None:
        """Fecha o slide de detalhes clicando no botão X."""
        try:
            btn = page.locator("[data-test-slider-close-button]").first
            if btn.count() > 0:
                BrowserActions.click_with_fallback(btn, timeout_ms=3000)
                page.wait_for_timeout(500)
                self.logger.log("info", "Slide fechado")
        except Exception:
            pass

    def _obter_opcoes_no_slide(self, page: Any) -> list[dict[str, Any]]:
        """
        Retorna lista de {toggle, vencimento, row_locator} das linhas de fatura
        que têm botão 'Opções' dentro do slide aberto.

        Estrutura confirmada via HTML:
          [data-slider-root]
            datacardsection[data-test-card-section-content]
              p[aria-label="DD/MM/YYYY"]              → vencimento
              [data-test-dropdown-root]
                button[data-test-open-dropdown-button] → "Opções"
                ul[data-test-dropdown-list]
                  a[data-test-dropdown-list-item-link] → opções do menu
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
            # Vencimento: aria-label no formato DD/MM/YYYY
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
            # Status: sobe no DOM até encontrar o .data-card-badge do grupo pai
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

        self.logger.log("info", "Opcoes com toggle encontrados", total=len(opcoes))
        return opcoes

    def _baixar_conta_detalhada(
        self,
        page: Any,
        toggle: Any,
        row_locator: Any,
        codigo_cliente: str,
        cnpj: str,
        idx_linha: int,
        referencia: str,
        config: MovelDetalheConfig,
        runtime: dict[str, Any],
        situacao: str = "",
    ) -> dict[str, Any]:
        """
        Clica no dropdown 'Opções' de uma linha de fatura,
        seleciona 'Conta detalhada e nota fiscal (.pdf)' e baixa o arquivo.
        """
        # Verifica se o PDF já existe (pula download se --force não foi passado)
        pdf_existente = _buscar_pdf_existente(config.download_dir, cnpj, codigo_cliente, referencia)
        if pdf_existente and not config.force:
            self.logger.log("info", "PDF ja existe, pulando download", arquivo=pdf_existente.name, linha=idx_linha)
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

        self._minimizar_painel(page)
        try:
            toggle.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        texto_toggle = ""
        try:
            texto_toggle = toggle.inner_text(timeout=1000).strip()
        except Exception:
            pass
        self.logger.log("info", "Abrindo dropdown", texto=texto_toggle, linha=idx_linha)

        if not BrowserActions.click_with_fallback(toggle, timeout_ms=5000):
            return self._resultado_falha(
                codigo_cliente, idx_linha, referencia, config,
                "Falha ao abrir dropdown Opcoes"
            )
        page.wait_for_timeout(600)

        # Busca o link dentro do [data-test-dropdown-root] desta linha específica.
        # NÃO chamamos _minimizar_painel aqui para não fechar o dropdown aberto.
        btn_detalhe = None
        try:
            dropdown_root = row_locator.locator("[data-test-dropdown-root]").first
            for seletor in [
                "a[data-test-dropdown-list-item-link]:has-text('Conta detalhada')",
                "a[data-test-dropdown-list-item-link]:has-text('nota fiscal')",
                "[data-test-dropdown-list-item]:nth-child(2) a",
            ]:
                el = dropdown_root.locator(seletor).first
                if el.count() == 0:
                    continue
                texto_el = el.inner_text(timeout=1000).strip()
                self.logger.log("info", "Opcao encontrada", texto=texto_el)
                btn_detalhe = el
                break
        except Exception as exc:
            self.logger.log("warn", "Erro ao buscar opcao no dropdown", erro=str(exc))

        if btn_detalhe is None:
            return self._resultado_falha(
                codigo_cliente, idx_linha, referencia, config,
                "Botao Conta detalhada nao encontrado no dropdown"
            )

        self.logger.log("info", "Solicitando Conta detalhada e NF (.pdf)", linha=idx_linha)
        tmp, erro = self._aguardar_download_monitorando(page, btn_detalhe, idx_linha, timeout_ms=60000)

        if not tmp or erro:
            runtime["downloads_falhos"] = int(runtime.get("downloads_falhos", 0)) + 1
            return self._resultado_falha(
                codigo_cliente, idx_linha, referencia, config,
                erro or "Download nao capturado"
            )

        # Salva PDF com nome padronizado
        cnpj_seguro = normalize_document(cnpj) or "semcnpj"
        cod_seguro = re.sub(r"\D", "", codigo_cliente) or "semconta"
        # referencia já vem como YYYYMM (convertido em _extrair_secoes_movel)
        ref_segura = referencia if referencia else f"linha{idx_linha:02d}"
        base = f"vivo-movel-{cnpj_seguro}-{cod_seguro}-{ref_segura}"
        target = config.download_dir / f"{base}.pdf"
        target.write_bytes(tmp.read_bytes())
        runtime["downloads_ok"] = int(runtime.get("downloads_ok", 0)) + 1
        self.logger.log("ok", "PDF salvo", arquivo=target.name)
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
        idx_linha: int,
        referencia: str,
        config: MovelDetalheConfig,
        erro: str,
        situacao: str = "",
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

    def baixar_todos(
        self,
        page: Any,
        secoes: list[dict[str, Any]],
        config: MovelDetalheConfig,
        runtime: dict[str, Any],
    ) -> list[dict[str, Any]]:
        resultados: list[dict[str, Any]] = []
        cnpj = str(runtime.get("cnpj_cliente", "") or config.cnpj_inicial or "semcnpj")

        if config.listar:
            self.logger.log("info", "Modo listar: abrindo slides para coletar todas as faturas")
            for sec in secoes:
                sec_locator = sec["_sec_locator"]
                codigo_cliente = sec.get("codigo_cliente", "")
                self._minimizar_painel(page)
                if not self._clicar_exibir_detalhes(page, sec_locator):
                    continue
                page.wait_for_timeout(config.wait_ms // 2)
                opcoes = self._obter_opcoes_no_slide(page)
                for idx, opcao in enumerate(opcoes, start=1):
                    vencimento = opcao["vencimento"]
                    situacao = opcao.get("situacao", "")
                    ano, mes = ano_mes_por_vencimento(vencimento)
                    referencia = f"{ano}{mes}" if ano != "0000" else referencia_para_yyyymm(vencimento)

                    # Baixa automaticamente faturas sem PDF local
                    pdf_existente = _buscar_pdf_existente(config.download_dir, cnpj, codigo_cliente, referencia)
                    if not pdf_existente or config.force:
                        self.logger.log("info", "Sem PDF, baixando", referencia=referencia, conta=codigo_cliente)
                        runtime["tentativas"] = int(runtime.get("tentativas", 0)) + 1
                        resultado = self._baixar_conta_detalhada(
                            page, opcao["toggle"], opcao["row"],
                            codigo_cliente, cnpj, idx, referencia, config, runtime, situacao,
                        )
                        tentativa_retry = 1
                        while not resultado["download_ok"] and tentativa_retry <= 3:
                            self.logger.log("warn", f"Retry {tentativa_retry}/3", referencia=referencia, conta=codigo_cliente)
                            _, opcoes_retry = self._recarregar_e_abrir_secao(page, config, codigo_cliente)
                            if not opcoes_retry:
                                break
                            opcao_retry = None
                            for o in opcoes_retry:
                                v = o["vencimento"]
                                a, m = ano_mes_por_vencimento(v)
                                ref = f"{a}{m}" if a != "0000" else referencia_para_yyyymm(v)
                                if ref == referencia:
                                    opcao_retry = o
                                    break
                            if not opcao_retry:
                                break
                            resultado = self._baixar_conta_detalhada(
                                page, opcao_retry["toggle"], opcao_retry["row"],
                                codigo_cliente, cnpj, idx, referencia, config, runtime,
                                opcao_retry.get("situacao", situacao),
                            )
                            tentativa_retry += 1
                        resultados.append(resultado)
                        continue

                    resultados.append({
                        "codigo_cliente": codigo_cliente,
                        "referencia": referencia,
                        "situacao": situacao,
                        "vencimento": vencimento,
                        "download_ok": False,
                        "arquivo_download": "",
                        "erro_download": "",
                        "coleta_data_hora": config.coleta_data_hora,
                    })
                self._fechar_slide(page)
            self.logger.log("info", "Listagem concluida", total=len(resultados))
            return resultados

        for sec in secoes:
            sec_locator = sec["_sec_locator"]
            codigo_cliente = sec.get("codigo_cliente", "")
            faturas_sec = sec.get("faturas", [])

            self.logger.log("info", "Processando conta", conta=codigo_cliente)
            self._minimizar_painel(page)

            if not self._clicar_exibir_detalhes(page, sec_locator):
                resultados.append(self._resultado_falha(
                    codigo_cliente, 0, "", config,
                    "Falha ao expandir Exibir detalhes"
                ))
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
                resultados.append(self._resultado_falha(
                    codigo_cliente, 0, "", config,
                    "Nenhum botao Opcoes encontrado no slide"
                ))
                self._fechar_slide(page)
                continue

            for idx, opcao in enumerate(opcoes_para_baixar, start=1):
                toggle = opcao["toggle"]
                vencimento = opcao["vencimento"]
                row_locator = opcao["row"]
                situacao = opcao.get("situacao", "")
                ano, mes = ano_mes_por_vencimento(vencimento)
                referencia = f"{ano}{mes}" if ano != "0000" else referencia_para_yyyymm(vencimento)
                runtime["tentativas"] = int(runtime.get("tentativas", 0)) + 1

                resultado = self._baixar_conta_detalhada(
                    page, toggle, row_locator, codigo_cliente, cnpj, idx, referencia, config, runtime, situacao
                )

                # Retry até 3 vezes: fecha painel, recarrega página, refaz o fluxo normal
                tentativa_retry = 1
                while not resultado["download_ok"] and tentativa_retry <= 3:
                    self.logger.log(
                        "warn", f"Retry {tentativa_retry}/3: recarregando pagina",
                        referencia=referencia, conta=codigo_cliente,
                    )
                    _, opcoes_retry = self._recarregar_e_abrir_secao(page, config, codigo_cliente)
                    if not opcoes_retry:
                        break

                    opcao_retry = None
                    for o in opcoes_retry:
                        v = o["vencimento"]
                        a, m = ano_mes_por_vencimento(v)
                        ref = f"{a}{m}" if a != "0000" else referencia_para_yyyymm(v)
                        if ref == referencia:
                            opcao_retry = o
                            break

                    if not opcao_retry:
                        self.logger.log("warn", "Opcao nao encontrada no slide apos reload", referencia=referencia)
                        break

                    resultado = self._baixar_conta_detalhada(
                        page, opcao_retry["toggle"], opcao_retry["row"],
                        codigo_cliente, cnpj, idx, referencia, config, runtime,
                        opcao_retry.get("situacao", situacao),
                    )
                    tentativa_retry += 1

                resultados.append(resultado)
                page.wait_for_timeout(500)

            self._fechar_slide(page)
            page.wait_for_timeout(500)

        return resultados


# ---------------------------------------------------------------------------
# Tabela de listagem
# ---------------------------------------------------------------------------

def _buscar_pdf_existente(download_dir: Path, cnpj: str, conta: str, referencia: str) -> Path | None:
    """Procura o PDF já baixado para a combinação cnpj/conta/referencia."""
    cnpj_s = re.sub(r"\D", "", cnpj)
    conta_s = re.sub(r"\D", "", conta)
    padrao = f"vivo-movel-{cnpj_s}-{conta_s}-{referencia}*.pdf"
    candidatos = sorted(download_dir.glob(padrao))
    return candidatos[0] if candidatos else None


def _imprimir_tabela_listagem(faturas: list[dict[str, Any]], download_dir: Path, cnpj: str) -> None:
    """Imprime tabela formatada das faturas listadas.

    Para cada fatura, verifica se o PDF já existe e, se sim, extrai dados dele.
    """
    try:
        from vivo_fatura_extrator import extrair_dados_fatura
        extrator_ok = True
    except Exception:
        extrator_ok = False

    linhas = []
    for f in faturas:
        conta = f.get("codigo_cliente", "")
        ref = f.get("referencia", "")
        venc = f.get("vencimento", "")
        sit = f.get("situacao", "")

        pdf = _buscar_pdf_existente(download_dir, cnpj, conta, ref)
        arquivo_str = str(pdf) if pdf else "-"

        valor = "-"
        codigo_barras = "-"
        pix = "-"
        if pdf and extrator_ok:
            try:
                dados = extrair_dados_fatura(str(pdf), verbose=False)
                valor = dados.get("valor") or "-"
                codigo_barras = dados.get("codigo_barras_digitavel_sem_espaco") or "-"
                pix = (dados.get("pix_copia_cola") or "")[:40] or "-"
            except Exception:
                pass

        linhas.append({
            "conta": conta,
            "ref": ref,
            "vencimento": venc,
            "situacao": sit,
            "valor": valor,
            "arquivo": arquivo_str,
            "codigo_barras": codigo_barras,
            "pix": pix,
        })

    if not linhas:
        print("\n[info] Nenhuma fatura encontrada para listar.")
        return

    # Larguras das colunas
    col_conta  = max(len("Conta"),      max(len(r["conta"])      for r in linhas))
    col_ref    = max(len("Referência"), max(len(r["ref"])        for r in linhas))
    col_venc   = max(len("Vencimento"), max(len(r["vencimento"]) for r in linhas))
    col_sit    = max(len("Situação"),   max(len(r["situacao"])   for r in linhas))
    col_valor  = max(len("Valor"),      max(len(r["valor"])      for r in linhas))
    col_arq    = max(len("Arquivo"),    max(len(r["arquivo"])    for r in linhas))

    sep = (
        f"+{'-'*(col_conta+2)}+{'-'*(col_ref+2)}+{'-'*(col_venc+2)}"
        f"+{'-'*(col_sit+2)}+{'-'*(col_valor+2)}+{'-'*(col_arq+2)}+"
    )
    fmt = (
        f"| {{:<{col_conta}}} | {{:<{col_ref}}} | {{:<{col_venc}}}"
        f" | {{:<{col_sit}}} | {{:<{col_valor}}} | {{:<{col_arq}}} |"
    )

    print(f"\n{'='*len(sep)}")
    print("  LISTAGEM DE FATURAS VIVO MOVEL")
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


# ---------------------------------------------------------------------------
# App principal
# ---------------------------------------------------------------------------

class VivoMovelDetalheApp:
    def __init__(self, config: MovelDetalheConfig) -> None:
        self.config = config
        self.logger = Logger()
        self.debug = DebugCollector(config, self.logger)
        self.auth_service = AuthService(self.logger)
        self.download_service = MovelDetalheDownloadService(self.logger)
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

    def run(self) -> Path:
        response = executar_fetch(self.config, self._page_action, self.runtime)
        result_file = self.result_service.salvar_resultado(
            response, self.config, self.runtime,
            extra={"tentativas": self.runtime["tentativas"]},
            prefixo="vivo_movel2_resultado",
        )
        self.logger.log("ok", "Execucao finalizada", status=response.status)
        self.logger.log("ok", "Resumo", downloads_ok=self.runtime["downloads_ok"])
        self.logger.log("ok", "Resumo", downloads_falhos=self.runtime["downloads_falhos"])
        imprimir_resumo_telefones(self.runtime["faturas_disponiveis"], self.logger)
        if self.config.listar:
            _imprimir_tabela_listagem(
                self.runtime["faturas_disponiveis"],
                self.config.download_dir,
                str(self.runtime.get("cnpj_cliente", "") or ""),
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

        secoes = _extrair_secoes_movel(page, self.config, self.logger)
        self.logger.log("info", "Secoes movel coletadas", total=len(secoes))

        for sec in secoes:
            self.runtime["faturas_disponiveis"].append({
                "codigo_cliente": sec.get("codigo_cliente", ""),
                "valor": sec.get("valor", ""),
                "vencimento": sec.get("vencimento", ""),
                "referencia": sec.get("referencia", ""),
                "situacao": sec.get("situacao", ""),
                "coleta_data_hora": self.config.coleta_data_hora,
            })

        resultados = self.download_service.baixar_todos(
            page, secoes, self.config, self.runtime
        )

        cnpj_str = str(self.runtime.get("cnpj_cliente", "") or self.config.cnpj_inicial or "")
        for item in resultados:
            # Em modo listar, arquivo_download está vazio — tenta localizar o PDF existente
            if not item.get("arquivo_download"):
                pdf = _buscar_pdf_existente(
                    self.config.download_dir,
                    cnpj_str,
                    item.get("codigo_cliente", ""),
                    item.get("referencia", ""),
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
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download de contas detalhadas Vivo Movel (Opcoes → Conta detalhada e NF)"
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
        help="Força re-download mesmo se o PDF ja existir",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> MovelDetalheConfig:
    output_dir, download_dir = build_common_dirs(args)
    limite = None if getattr(args, "todas", False) else args.limite
    return MovelDetalheConfig(
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
    )


def main() -> None:
    carregar_env_arquivo(Path(".env"))
    args = parse_args()
    config = build_config(args)
    app = VivoMovelDetalheApp(config)
    app.run()


if __name__ == "__main__":
    main()
