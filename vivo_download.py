#!/usr/bin/env python3
import argparse
import json
import os
import re
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


def log_event(level: str, message: str, **fields: Any) -> None:
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
    details = " ".join(f"{key}={fields[key]}" for key in ordered_keys)
    print(f"{prefix} {message} {details}")


def normalize_document(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def resolve_headless(mode: str):
    if mode == "virtual":
        return "virtual"
    return mode == "headless"


def coleta_data_hora_gmt_menos3() -> datetime:
    tz = timezone(timedelta(hours=-3))
    return datetime.now(tz)


def format_coleta_data_hora(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


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


def has_password_field(page: Any, password_selectors: list[str]) -> bool:
    for selector in password_selectors:
        locator = page.locator(selector).first
        if locator.count() > 0 and locator.is_visible():
            return True
    return False


def fill_first_visible(page: Any, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count() > 0 and locator.is_visible():
            locator.fill(value)
            return True
    return False


def click_first_enabled(page: Any, selectors: list[str], timeout_ms: int = 3000) -> bool:
    for selector in selectors:
        button = page.locator(selector).first
        if button.count() > 0 and button.is_visible() and button.is_enabled():
            try:
                button.click(timeout=timeout_ms)
                return True
            except Exception:
                continue
    return False


def montar_xpaths(section_idx: int, div_idx: int) -> tuple[str, str, str]:
    section_xpath = (
        f"/html/body/main/div/div/div/div/div[2]/div[2]/div/div[1]/section[{section_idx}]"
    )
    row_xpath = f"{section_xpath}/div/div[{div_idx}]"
    button_xpath = f"{row_xpath}/div[4]/div/div/div[2]/div/button"
    link_xpath = f"{row_xpath}/div[4]/div/div/div[2]/div/div/ul/li[1]/a"
    return row_xpath, button_xpath, link_xpath


def extrair_dados_fatura(row: Any) -> tuple[str, str, str]:
    due_date = ""
    value = ""
    status = ""

    try:
        due = row.locator(".data-card-section__thirdColumn p.data-card-cell__description").first
        if due.count() > 0:
            due_text = due.inner_text(timeout=1500)
            due_match = re.search(r"(\d{2}/\d{2}/\d{4})", due_text)
            due_date = due_match.group(1) if due_match else ""
    except Exception:
        pass

    try:
        value_node = row.locator(
            ".data-card-section__secondColumn p.data-card-cell__description"
        ).first
        if value_node.count() > 0:
            value = value_node.inner_text(timeout=1500).replace("\xa0", " ").strip()
    except Exception:
        pass

    try:
        status_node = row.locator(".badge p").first
        if status_node.count() > 0:
            status = status_node.inner_text(timeout=1500).strip()
    except Exception:
        pass

    return due_date, value, status


def ano_mes_por_vencimento(vencimento: str) -> tuple[str, str]:
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", (vencimento or "").strip())
    if not match:
        return "0000", "00"
    return match.group(3), match.group(2)


def montar_nome_arquivo_padrao(
    cnpj_cliente: str, conta: str, vencimento: str, download_dir: Path
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


def montar_nome_resultado(output_dir: Path, cnpj_cliente: str, coleta_dt: datetime) -> Path:
    cnpj_seguro = normalize_document(cnpj_cliente) or "semcnpj"
    stamp = coleta_dt.strftime("%Y%m%d_%H%M%S")
    return output_dir / f"xpath_loop_resultado_{cnpj_seguro}_{stamp}.json"


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


def main() -> None:
    carregar_env_arquivo(Path(".env"))
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    download_dir = Path(args.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    cpf_ou_cnpj = normalize_document(args.cpf)
    cnpj_cliente = cpf_ou_cnpj if len(cpf_ou_cnpj) == 14 else ""
    password = args.password or ""
    coleta_dt = coleta_data_hora_gmt_menos3()
    coleta_data_hora = format_coleta_data_hora(coleta_dt)
    coleta_stamp = coleta_dt.strftime("%Y%m%d_%H%M%S")

    debug_dir = output_dir / f"debug_xpath_loop_{coleta_stamp}"
    if args.debug:
        debug_dir.mkdir(parents=True, exist_ok=True)

    metadata_file = output_dir / "xpath_loop_resultado_tmp.json"

    runtime: dict[str, Any] = {
        "campo_senha_detectado": False,
        "senha_enviada": False,
        "dashboard_detectado": False,
        "faturas_aberto": False,
        "cliques_toggle_dialog_aberto": 0,
        "tentativas_xpath": 0,
        "downloads_ok": 0,
        "downloads_falhos": 0,
        "faturas_disponiveis": [],
        "cnpj_cliente": cnpj_cliente,
        "debug_paginas": 0,
    }

    debug_counter = {"value": 0}

    def salvar_snapshot_debug(page: Any, etapa: str) -> None:
        if not args.debug:
            return
        debug_counter["value"] += 1
        idx = debug_counter["value"]
        nome_base = f"{idx:02d}_{etapa}"
        screenshot_path = debug_dir / f"{nome_base}.png"
        html_path = debug_dir / f"{nome_base}.html"
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        try:
            html_path.write_text(page.content() or "", encoding="utf-8")
        except Exception:
            pass
        runtime["debug_paginas"] = int(runtime["debug_paginas"]) + 1
        log_event("info", "Snapshot debug salvo", etapa=etapa, arquivo=nome_base)

    def page_action(page: Any):
        log_event("info", "Abertura da pagina inicial", url=args.url)
        page.wait_for_timeout(args.wait_ms)
        salvar_snapshot_debug(page, "01_primeira_tela")

        if not cpf_ou_cnpj:
            log_event("warn", "CPF/CNPJ nao informado")
            return page

        cpf_selectors = [
            "input[name*='cpf' i]",
            "input[id*='cpf' i]",
            "input[placeholder*='cpf' i]",
            "input[type='tel']",
            "input[type='text']",
        ]
        continue_selectors = [
            "button:has-text('Continuar')",
            "button:has-text('Avancar')",
            "button:has-text('Próximo')",
            "button:has-text('Proximo')",
            "button:has-text('Entrar')",
            "button[type='submit']",
            "input[type='submit']",
        ]
        password_selectors = [
            "input[type='password']",
            "input[name*='senha' i]",
            "input[id*='senha' i]",
            "input[placeholder*='senha' i]",
        ]

        if not fill_first_visible(page, cpf_selectors, cpf_ou_cnpj):
            log_event("warn", "Campo de CPF/CNPJ nao encontrado")
            return page

        log_event("ok", "CPF/CNPJ preenchido")
        page.wait_for_timeout(args.wait_ms)
        salvar_snapshot_debug(page, "02_apos_cpf")

        if not has_password_field(page, password_selectors):
            for _ in range(4):
                if not click_first_enabled(page, continue_selectors, timeout_ms=3000):
                    page.keyboard.press("Enter")
                page.wait_for_timeout(args.wait_ms)
                if has_password_field(page, password_selectors):
                    break

        runtime["campo_senha_detectado"] = has_password_field(page, password_selectors)
        if not runtime["campo_senha_detectado"]:
            log_event("warn", "Campo de senha nao detectado")
            return page
        if not password:
            log_event("warn", "Senha nao informada")
            return page

        if not fill_first_visible(page, password_selectors, password):
            log_event("warn", "Nao foi possivel preencher senha")
            return page

        enter_selectors = [
            "button:has-text('Entrar')",
            "button[type='submit']",
            "input[type='submit']",
        ]
        if not click_first_enabled(page, enter_selectors, timeout_ms=3000):
            page.keyboard.press("Enter")

        runtime["senha_enviada"] = True
        log_event("ok", "Senha enviada")
        page.wait_for_timeout(args.wait_ms)
        salvar_snapshot_debug(page, "03_apos_senha")

        try:
            page.wait_for_url("**/sec/dashboard*", timeout=30000)
            runtime["dashboard_detectado"] = True
        except Exception:
            runtime["dashboard_detectado"] = args.dashboard_url in (page.url or "")
        log_event("ok", "Dashboard verificado", detectado=runtime["dashboard_detectado"])

        try:
            page.goto(args.invoices_url, wait_until="domcontentloaded")
            runtime["faturas_aberto"] = "/sec/invoices" in (page.url or "")
            page.wait_for_timeout(args.wait_ms)
            salvar_snapshot_debug(page, "04_faturas")
        except Exception:
            runtime["faturas_aberto"] = False
            log_event("erro", "Falha ao abrir pagina de faturas")
            return page

        log_event("ok", "Pagina de faturas aberta", url=page.url)
        cnpj_pagina = extrair_cnpj_da_pagina(page.content())
        if cnpj_pagina:
            runtime["cnpj_cliente"] = cnpj_pagina
            log_event("ok", "CNPJ extraido da pagina", cnpj=cnpj_pagina)

        faturas_internas: list[dict[str, Any]] = []

        for section_idx in range(1, args.max_sections + 1):
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

            for div_idx in range(args.start_div_index, args.max_div_index + 1):
                row_xpath, button_xpath, link_xpath = montar_xpaths(section_idx, div_idx)
                row = page.locator(f"xpath={row_xpath}").first
                if row.count() == 0:
                    if div_idx == args.start_div_index:
                        break
                    break

                button = page.locator(f"xpath={button_xpath}").first
                if button.count() == 0:
                    continue

                vencimento, valor, situacao = extrair_dados_fatura(row)
                item = {
                    "_secao": section_idx,
                    "_div": div_idx,
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
                    "coleta_data_hora": coleta_data_hora,
                }
                faturas_internas.append(item)
                log_event(
                    "info",
                    "Fatura encontrada",
                    conta=conta,
                    vencimento=vencimento,
                    valor=valor,
                    situacao=situacao,
                )

        if not args.listar:
            for item in faturas_internas:
                runtime["tentativas_xpath"] = int(runtime["tentativas_xpath"]) + 1
                button_xpath = str(item.get("_xpath_botao", ""))
                link_xpath = str(item.get("_xpath_link", ""))

                try:
                    opened_toggle = page.locator("div.toggle-dialog.dialog-icon.opened").first
                    if opened_toggle.count() > 0 and click_with_fallback(
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
                    if not click_with_fallback(button, timeout_ms=5000):
                        raise RuntimeError("Falha ao clicar no botao")

                    page.wait_for_timeout(300)
                    link = page.locator(f"xpath={link_xpath}").first
                    if link.count() == 0:
                        raise RuntimeError("Link de download nao encontrado")

                    with page.expect_download(timeout=45000) as dl_info:
                        if not click_with_fallback(link, timeout_ms=5000):
                            raise RuntimeError("Falha ao clicar no link de download")

                    download = dl_info.value
                    target = montar_nome_arquivo_padrao(
                        str(runtime.get("cnpj_cliente", "") or cnpj_cliente),
                        str(item.get("conta", "")),
                        str(item.get("vencimento", "")),
                        download_dir,
                    )
                    download.save_as(str(target))
                    item["download_ok"] = True
                    item["arquivo_download"] = str(target)
                    runtime["downloads_ok"] = int(runtime["downloads_ok"]) + 1
                    log_event("ok", "Download concluido", arquivo=target.name)
                except Exception as exc:
                    item["erro_download"] = str(exc)
                    runtime["downloads_falhos"] = int(runtime["downloads_falhos"]) + 1
                    log_event(
                        "warn", "Falha no download", erro=str(exc), conta=item.get("conta", "")
                    )

                page.wait_for_timeout(500)
        else:
            log_event("info", "Modo listar ativo, download desabilitado")

        faturas_limpo: list[dict[str, Any]] = []
        for item in faturas_internas:
            faturas_limpo.append(
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
                    "coleta_data_hora": item.get("coleta_data_hora", coleta_data_hora),
                }
            )

        runtime["faturas_disponiveis"] = faturas_limpo
        salvar_snapshot_debug(page, "05_final")
        return page

    response = StealthyFetcher.fetch(
        url=args.url,
        headless=resolve_headless(args.mode),
        timeout=args.timeout_ms,
        wait=args.wait_ms,
        page_action=page_action,
        humanize=True,
    )

    resultado = {
        "status": response.status,
        "url": response.url,
        "cnpj_cliente": str(runtime.get("cnpj_cliente", "") or cnpj_cliente),
        "coleta_data_hora": coleta_data_hora,
        "campo_senha_detectado": runtime["campo_senha_detectado"],
        "senha_enviada": runtime["senha_enviada"],
        "dashboard_detectado": runtime["dashboard_detectado"],
        "faturas_aberto": runtime["faturas_aberto"],
        "cliques_toggle_dialog_aberto": runtime["cliques_toggle_dialog_aberto"],
        "modo_listar": args.listar,
        "tentativas_xpath": runtime["tentativas_xpath"],
        "downloads_ok": runtime["downloads_ok"],
        "downloads_falhos": runtime["downloads_falhos"],
        "faturas_disponiveis": runtime["faturas_disponiveis"],
        "debug_ativado": args.debug,
        "debug_diretorio": str(debug_dir) if args.debug else "",
        "debug_paginas": runtime["debug_paginas"],
    }

    metadata_file = montar_nome_resultado(
        output_dir,
        str(runtime.get("cnpj_cliente", "") or cnpj_cliente),
        coleta_dt,
    )
    metadata_file.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")

    log_event("ok", "Execucao finalizada", status=response.status, url=response.url)
    log_event("ok", "Resumo", tentativas=runtime["tentativas_xpath"])
    log_event("ok", "Resumo", downloads_ok=runtime["downloads_ok"])
    log_event("ok", "Resumo", downloads_falhos=runtime["downloads_falhos"])
    log_event("ok", "Resultado salvo", arquivo=metadata_file)


if __name__ == "__main__":
    main()
