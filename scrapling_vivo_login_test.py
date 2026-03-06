#!/usr/bin/env python3
import argparse
import json
import os
import re
from pathlib import Path

from scrapling.fetchers import StealthyFetcher

DEFAULT_URL = "https://mve.vivo.com.br/oauth?logout=true"
INVOICES_URL = "https://mve.vivo.com.br/sec/invoices"
INVOICES_MENU_BUTTON_XPATH = (
    "/html/body/main/div/div/div/div/div[2]/div[2]/div/div[1]/section[1]/"
    "div/div[3]/div[4]/div/div/div[2]/div/button"
)
INVOICES_DIRECT_CLICK_XPATH = (
    "/html/body/main/div/div/div/div/div[2]/div[2]/div/div[1]/section[1]/"
    "div/div[3]/div[4]/div/div/div[2]/div/div/ul/li[1]/a"
)


def normalize_cpf(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def normalize_period(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{2})[/-](\d{4})$", text)
    if not match:
        raise ValueError("Formato de periodo invalido. Use MM/AAAA")
    month, year = match.groups()
    mm = int(month)
    if mm < 1 or mm > 12:
        raise ValueError("Mes invalido no periodo. Use MM entre 01 e 12")
    return f"{month}/{year}"


def normalize_account_orders(value: str) -> list[int]:
    text = (value or "").strip()
    if not text:
        return []
    tokens = [part.strip() for part in text.split(",") if part.strip()]
    orders: list[int] = []
    for token in tokens:
        if not token.isdigit():
            raise ValueError(
                "Formato invalido em --account-order. Use numeros separados por virgula"
            )
        index = int(token)
        if index < 1:
            raise ValueError("--account-order usa posicoes iniciando em 1")
        if index not in orders:
            orders.append(index)
    return orders


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Teste de acesso ao login da Vivo com Scrapling")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--cpf", default=os.getenv("VIVO_CPF", ""))
    parser.add_argument("--password", default=os.getenv("VIVO_PASSWORD", ""))
    parser.add_argument("--account-number", default="")
    parser.add_argument(
        "--account-order",
        default="",
        help="Posicao da conta na pagina (1-based). Ex.: 1 ou 1,3",
    )
    parser.add_argument("--period", default="", help="Periodo MM/AAAA")
    parser.add_argument("--month", type=int, default=0, help="Mes da fatura (1-12)")
    parser.add_argument("--year", type=int, default=0, help="Ano da fatura (AAAA)")
    parser.add_argument(
        "--list-available",
        action="store_true",
        help="Lista faturas disponiveis no CLI",
    )
    parser.add_argument(
        "--only-list",
        action="store_true",
        help="Somente lista faturas, sem baixar",
    )
    parser.add_argument("--output-dir", default="screenshots/scrapling")
    parser.add_argument("--download-dir", default="downloads/vivo")
    parser.add_argument("--wait-ms", type=int, default=3500)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument(
        "--mode",
        choices=["headless", "headful", "virtual"],
        default="headless",
        help="Modo do navegador do Camoufox",
    )
    return parser.parse_args()


def resolve_headless(mode: str):
    if mode == "virtual":
        return "virtual"
    return mode == "headless"


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    download_dir = Path(args.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    cpf = normalize_cpf(args.cpf)
    password = args.password or ""
    account_number = re.sub(r"\D+", "", args.account_number or "")
    account_orders = normalize_account_orders(args.account_order)
    account_orders_set = set(account_orders)
    period = normalize_period(args.period)
    if not period and args.month and args.year:
        if args.month < 1 or args.month > 12:
            raise ValueError("Mes invalido. Use --month entre 1 e 12")
        period = f"{args.month:02d}/{args.year}"

    first_screen = output_dir / "01_primeira_tela.png"
    second_screen = output_dir / "02_apos_cpf.png"
    third_screen = output_dir / "03_tela_seguinte.png"
    fourth_screen = output_dir / "04_apos_senha.png"
    fifth_screen = output_dir / "05_faturas.png"
    sixth_screen = output_dir / "06_download_resultado.png"
    final_html = output_dir / "final_page.html"
    metadata_file = output_dir / "resultado.json"

    runtime = {
        "password_field_detected": False,
        "password_submitted": False,
        "contas_clicked": False,
        "faturas_clicked": False,
        "download_started": False,
        "retry_clicked": False,
        "download_file": "",
        "invoices_url_opened": False,
        "manual_xpath_button_clicked": False,
        "manual_xpath_clicked": False,
        "manual_xpath_downloaded": False,
        "manual_xpath_attempted": False,
        "downloads": [],
        "available_invoices": [],
        "account_filter": account_number,
        "account_order_filter": account_orders,
        "period_filter": period,
        "submission_attempts": 0,
    }

    def page_action(page):
        page.wait_for_timeout(args.wait_ms)
        page.screenshot(path=str(first_screen), full_page=True)

        if not cpf:
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

        def has_password_field() -> bool:
            for selector in password_selectors:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return True
            return False

        cpf_filled = False
        for selector in cpf_selectors:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.fill(cpf)
                cpf_filled = True
                break

        if not cpf_filled:
            return page

        page.wait_for_timeout(args.wait_ms)
        page.screenshot(path=str(second_screen), full_page=True)

        if has_password_field():
            runtime["password_field_detected"] = True
            page.screenshot(path=str(third_screen), full_page=True)
            return page

        for _ in range(4):
            clicked = False
            for selector in continue_selectors:
                button = page.locator(selector).first
                if button.count() > 0 and button.is_visible() and button.is_enabled():
                    button.click(timeout=3000)
                    clicked = True
                    runtime["submission_attempts"] += 1
                    break

            if not clicked:
                page.keyboard.press("Enter")
                runtime["submission_attempts"] += 1

            page.wait_for_timeout(args.wait_ms)
            if has_password_field():
                runtime["password_field_detected"] = True
                break

        page.screenshot(path=str(third_screen), full_page=True)

        if runtime["password_field_detected"] and password:
            pass_selectors = [
                "input[type='password']",
                "input[name*='senha' i]",
                "input[id*='senha' i]",
                "input[placeholder*='senha' i]",
            ]
            enter_selectors = [
                "button:has-text('Entrar')",
                "button[type='submit']",
                "input[type='submit']",
            ]

            pass_filled = False
            for selector in pass_selectors:
                field = page.locator(selector).first
                if field.count() > 0 and field.is_visible():
                    field.fill(password)
                    pass_filled = True
                    break

            if pass_filled:
                # Alguns fluxos validam eventos de teclado; por isso usamos digitacao real.
                for selector in pass_selectors:
                    field = page.locator(selector).first
                    if field.count() > 0 and field.is_visible():
                        field.click(timeout=3000)
                        field.fill("")
                        field.type(password, delay=120)
                        break

                clicked_enter = False
                for selector in enter_selectors:
                    btn = page.locator(selector).first
                    if btn.count() > 0 and btn.is_visible() and btn.is_enabled():
                        btn.click(timeout=3000)
                        clicked_enter = True
                        break

                if not clicked_enter:
                    page.keyboard.press("Enter")

                runtime["password_submitted"] = True
                page.wait_for_timeout(args.wait_ms)
                page.screenshot(path=str(fourth_screen), full_page=True)

                try:
                    page.goto(INVOICES_URL, wait_until="domcontentloaded")
                    runtime["invoices_url_opened"] = True
                    runtime["contas_clicked"] = True
                    runtime["faturas_clicked"] = True
                    page.wait_for_timeout(args.wait_ms)
                except Exception:
                    pass

                if not runtime["faturas_clicked"]:
                    try:
                        page.wait_for_selector("[data-e2e-header-menu-invoices]", timeout=15000)
                    except Exception:
                        pass

                    contas_targets = [
                        "[data-e2e-header-menu-invoices]",
                        "span[data-nav-item-caption]:has-text('Contas')",
                    ]
                    contas_item = None
                    for selector in contas_targets:
                        item = page.locator(selector).first
                        if item.count() > 0 and item.is_visible():
                            contas_item = item
                            break

                    if contas_item is not None:
                        try:
                            contas_item.hover(timeout=5000)
                        except Exception:
                            pass
                        try:
                            contas_item.click(timeout=5000)
                        except Exception:
                            try:
                                contas_item.click(timeout=5000, force=True)
                            except Exception:
                                pass
                        runtime["contas_clicked"] = True
                        page.wait_for_timeout(args.wait_ms)

                    faturas_targets = [
                        "[data-e2e-header-menu-detalhes-contas-pagamentos]",
                        "[data-nav-menu-dropdown-item='invoices']",
                        "span[data-nav-menu-dropdown-item-label]:has-text('Acessar faturas')",
                    ]
                    for selector in faturas_targets:
                        item = page.locator(selector).first
                        if item.count() > 0 and item.is_visible():
                            try:
                                item.click(timeout=5000)
                            except Exception:
                                try:
                                    item.click(timeout=5000, force=True)
                                except Exception:
                                    continue
                            runtime["faturas_clicked"] = True
                            page.wait_for_timeout(args.wait_ms)
                            break

                if runtime["faturas_clicked"]:
                    if "/sec/invoices" in (page.url or ""):
                        runtime["manual_xpath_attempted"] = True
                        try:
                            button_selector = f"xpath={INVOICES_MENU_BUTTON_XPATH}"
                            button_target = page.locator(button_selector).first
                            if button_target.count() == 0:
                                try:
                                    page.wait_for_selector(button_selector, timeout=3000)
                                except Exception:
                                    pass
                                button_target = page.locator(button_selector).first

                            if button_target.count() > 0:
                                try:
                                    button_target.scroll_into_view_if_needed(timeout=3000)
                                except Exception:
                                    pass
                                try:
                                    button_target.click(timeout=5000)
                                    runtime["manual_xpath_button_clicked"] = True
                                except Exception:
                                    try:
                                        button_target.click(timeout=5000, force=True)
                                        runtime["manual_xpath_button_clicked"] = True
                                    except Exception:
                                        pass
                                page.wait_for_timeout(400)

                            selector = f"xpath={INVOICES_DIRECT_CLICK_XPATH}"
                            direct_target = page.locator(selector).first
                            if direct_target.count() == 0:
                                try:
                                    page.wait_for_selector(selector, timeout=3000)
                                except Exception:
                                    pass
                                direct_target = page.locator(selector).first

                            if direct_target.count() > 0:
                                try:
                                    direct_target.scroll_into_view_if_needed(timeout=3000)
                                except Exception:
                                    pass
                                if not args.only_list:
                                    try:
                                        with page.expect_download(timeout=40000) as dl_info:
                                            direct_target.click(timeout=5000)
                                        runtime["manual_xpath_clicked"] = True
                                        download = dl_info.value
                                        suggested = download.suggested_filename or "fatura.pdf"
                                        target = download_dir / f"manual_xpath_{suggested}"
                                        download.save_as(str(target))
                                        runtime["manual_xpath_downloaded"] = True
                                        runtime["download_started"] = True
                                        runtime["download_file"] = str(target)
                                        runtime["downloads"].append(
                                            {
                                                "button_index": -1,
                                                "success": True,
                                                "retry_clicked": False,
                                                "file": str(target),
                                                "attempts": 1,
                                                "account_order": 0,
                                                "account": "",
                                                "due_date": "",
                                                "filtered_out": False,
                                                "source": "manual_xpath",
                                            }
                                        )
                                    except Exception:
                                        try:
                                            direct_target.click(timeout=5000, force=True)
                                            runtime["manual_xpath_clicked"] = True
                                        except Exception:
                                            pass
                                else:
                                    try:
                                        direct_target.click(timeout=5000)
                                        runtime["manual_xpath_clicked"] = True
                                    except Exception:
                                        try:
                                            direct_target.click(timeout=5000, force=True)
                                            runtime["manual_xpath_clicked"] = True
                                        except Exception:
                                            pass
                                page.wait_for_timeout(600)
                        except Exception:
                            pass

                    if account_number:
                        try:
                            search_input = page.locator("#account-search-input").first
                            if search_input.count() > 0 and search_input.is_visible():
                                search_input.fill(account_number)
                                search_input.press("Enter")
                                page.wait_for_timeout(args.wait_ms)
                        except Exception:
                            pass

                        try:
                            detail_btn = page.locator("button:has-text('Exibir detalhes')").first
                            if detail_btn.count() > 0 and detail_btn.is_visible():
                                detail_btn.click(timeout=7000)
                                page.wait_for_timeout(args.wait_ms)
                        except Exception:
                            pass

                    def collapse_download_panel() -> None:
                        try:
                            toggle = page.locator("[data-toggle-dialog]").first
                            if toggle.count() > 0 and toggle.is_visible():
                                toggle.click(timeout=1500)
                                page.wait_for_timeout(300)
                        except Exception:
                            pass

                    collapse_download_panel()

                    try:
                        page.wait_for_selector("button:has-text('Baixar fatura')", timeout=20000)
                    except Exception:
                        pass

                    invoice_rows = page.locator("div.data-card-sections")
                    total_invoice_rows = invoice_rows.count()
                    account_order_map: dict[str, int] = {}
                    next_account_order = 1
                    for row_idx in range(total_invoice_rows):
                        row = invoice_rows.nth(row_idx)
                        row_account = ""
                        row_due = ""
                        row_value = ""
                        row_status = ""

                        try:
                            title = (
                                row.locator("xpath=ancestor::*[contains(@class,'data-card')]")
                                .first.locator("h4[aria-label*='Conta']")
                                .first
                            )
                            if title.count() > 0:
                                title_text = title.inner_text(timeout=1500)
                                account_match = re.search(r"(\d{8,})", title_text)
                                row_account = account_match.group(1) if account_match else ""
                        except Exception:
                            pass

                        try:
                            due = row.locator(
                                ".data-card-section__thirdColumn p.data-card-cell__description"
                            ).first
                            if due.count() > 0:
                                due_text = due.inner_text(timeout=1500)
                                due_match = re.search(r"(\d{2}/\d{2}/\d{4})", due_text)
                                row_due = due_match.group(1) if due_match else ""
                        except Exception:
                            pass

                        try:
                            value = row.locator(
                                ".data-card-section__secondColumn p.data-card-cell__description"
                            ).first
                            if value.count() > 0:
                                row_value = value.inner_text(timeout=1500).strip()
                        except Exception:
                            pass

                        try:
                            status = row.locator(".badge p").first
                            if status.count() > 0:
                                row_status = status.inner_text(timeout=1500).strip()
                        except Exception:
                            pass

                        has_download = False
                        try:
                            dl_btn = row.locator("button:has-text('Baixar fatura')").first
                            has_download = dl_btn.count() > 0 and dl_btn.is_visible()
                        except Exception:
                            pass

                        if row_account:
                            if row_account not in account_order_map:
                                account_order_map[row_account] = next_account_order
                                next_account_order += 1
                            account_order = account_order_map[row_account]
                        else:
                            account_order = 0

                        runtime["available_invoices"].append(
                            {
                                "row_index": row_idx,
                                "account_order": account_order,
                                "account": row_account,
                                "due_date": row_due,
                                "value": row_value,
                                "status": row_status,
                                "download_available": has_download,
                            }
                        )

                    baixar_buttons = page.locator("button:has-text('Baixar fatura')")
                    total_botoes = baixar_buttons.count()

                    target_indexes = []
                    rows = page.locator("div.data-card-sections")
                    total_rows = rows.count()
                    for row_idx in range(total_rows):
                        row = rows.nth(row_idx)
                        row_account = ""
                        row_due = ""
                        try:
                            title = (
                                row.locator("xpath=ancestor::*[contains(@class,'data-card')]")
                                .first.locator("h4[aria-label*='Conta']")
                                .first
                            )
                            if title.count() > 0:
                                account_text = title.inner_text(timeout=1500)
                                account_match = re.search(r"(\d{8,})", account_text)
                                row_account = account_match.group(1) if account_match else ""
                        except Exception:
                            pass

                        try:
                            due = row.locator(
                                ".data-card-section__thirdColumn p.data-card-cell__description"
                            ).first
                            if due.count() > 0:
                                due_text = due.inner_text(timeout=1500)
                                due_match = re.search(r"(\d{2}/\d{2}/\d{4})", due_text)
                                row_due = due_match.group(1) if due_match else ""
                        except Exception:
                            pass

                        account_order = 0
                        for item in runtime["available_invoices"]:
                            if not isinstance(item, dict):
                                continue
                            if item.get("row_index") != row_idx:
                                continue
                            order_value = item.get("account_order", 0)
                            if isinstance(order_value, int):
                                account_order = order_value
                            break

                        if account_number and row_account != account_number:
                            continue
                        if account_orders_set and account_order not in account_orders_set:
                            continue
                        if period and row_due and row_due[3:] != period:
                            continue
                        target_indexes.append(row_idx)

                    if (
                        not target_indexes
                        and total_botoes > 0
                        and not account_number
                        and not account_orders_set
                        and not period
                    ):
                        target_indexes = list(range(total_botoes))

                    if (
                        total_botoes > 0
                        and not args.only_list
                        and not runtime["manual_xpath_downloaded"]
                    ):
                        for idx in target_indexes:
                            item_result = {
                                "button_index": idx,
                                "success": False,
                                "retry_clicked": False,
                                "file": "",
                                "attempts": 0,
                                "account_order": 0,
                                "account": "",
                                "due_date": "",
                                "filtered_out": False,
                            }

                            def make_target(download_obj, button_index: int):
                                suggested = download_obj.suggested_filename or "fatura.pdf"
                                return download_dir / f"{button_index + 1:02d}_{suggested}"

                            for _attempt in range(3):
                                item_result["attempts"] += 1
                                try:
                                    collapse_download_panel()

                                    section_rows = page.locator("div.data-card-sections")
                                    if section_rows.count() <= idx:
                                        break

                                    row = section_rows.nth(idx)
                                    btn = row.locator("button:has-text('Baixar fatura')").first
                                    btn.scroll_into_view_if_needed(timeout=7000)
                                    if not btn.is_visible() or not btn.is_enabled():
                                        break

                                    section = row
                                    card = row.locator(
                                        "xpath=ancestor::*[contains(@class,'data-card')]"
                                    ).first

                                    account_text = ""
                                    try:
                                        title = card.locator("h4[aria-label*='Conta']").first
                                        if title.count() > 0:
                                            account_text = title.inner_text(timeout=1500)
                                    except Exception:
                                        pass
                                    account_match = re.search(r"(\d{8,})", account_text)
                                    current_account = (
                                        account_match.group(1) if account_match else ""
                                    )
                                    item_result["account"] = current_account

                                    current_account_order = 0
                                    for available in runtime["available_invoices"]:
                                        if not isinstance(available, dict):
                                            continue
                                        if available.get("row_index") != idx:
                                            continue
                                        order_value = available.get("account_order", 0)
                                        if isinstance(order_value, int):
                                            current_account_order = order_value
                                        break
                                    item_result["account_order"] = current_account_order

                                    due_text = ""
                                    try:
                                        due = section.locator(
                                            ".data-card-section__thirdColumn "
                                            "p.data-card-cell__description"
                                        ).first
                                        if due.count() > 0:
                                            due_text = due.inner_text(timeout=1500)
                                    except Exception:
                                        pass
                                    due_match = re.search(r"(\d{2}/\d{2}/\d{4})", due_text)
                                    current_due = due_match.group(1) if due_match else ""
                                    item_result["due_date"] = current_due

                                    if account_number and current_account != account_number:
                                        item_result["filtered_out"] = True
                                        break

                                    if (
                                        account_orders_set
                                        and current_account_order not in account_orders_set
                                    ):
                                        item_result["filtered_out"] = True
                                        break

                                    if period and current_due:
                                        if current_due[3:] != period:
                                            item_result["filtered_out"] = True
                                            break

                                    btn.click(timeout=7000)
                                    page.wait_for_timeout(600)

                                    pdf_option = row.locator(
                                        "a[data-test-dropdown-list-item-link]"
                                        ":has-text('Conta detalhada e nota fiscal (.pdf)')"
                                    ).first

                                    if pdf_option.count() > 0 and pdf_option.is_visible():
                                        try:
                                            with page.expect_download(timeout=30000) as dl_info:
                                                pdf_option.click(timeout=7000)
                                            download = dl_info.value
                                            target = make_target(download, idx)
                                            download.save_as(str(target))
                                            item_result["success"] = True
                                            item_result["file"] = str(target)
                                            runtime["download_started"] = True
                                            runtime["download_file"] = str(target)
                                        except Exception:
                                            pass

                                    if (
                                        not item_result["success"]
                                        and current_account
                                        and current_due
                                    ):
                                        try:
                                            items = page.locator("[data-download-item]")
                                            for i in range(items.count()):
                                                row = items.nth(i)
                                                info = row.locator("[data-download-info]").first
                                                if info.count() == 0:
                                                    continue
                                                info_text = info.inner_text(timeout=1500)
                                                if current_account not in info_text:
                                                    continue
                                                if current_due not in info_text:
                                                    continue
                                                icon = row.locator(
                                                    "[data-download-available]"
                                                ).first
                                                if icon.count() == 0 or not icon.is_visible():
                                                    continue
                                                with page.expect_download(timeout=40000) as dl_info:
                                                    icon.click(timeout=7000)
                                                download = dl_info.value
                                                target = make_target(download, idx)
                                                download.save_as(str(target))
                                                item_result["success"] = True
                                                item_result["file"] = str(target)
                                                runtime["download_started"] = True
                                                runtime["download_file"] = str(target)
                                                break
                                        except Exception:
                                            pass

                                    if not item_result["success"]:
                                        retry_btn = page.locator("text=Tentar novamente").first
                                        if retry_btn.count() > 0 and retry_btn.is_visible():
                                            runtime["retry_clicked"] = True
                                            item_result["retry_clicked"] = True
                                            try:
                                                with page.expect_download(timeout=20000) as dl_info:
                                                    retry_btn.click(timeout=7000)
                                                download = dl_info.value
                                                target = make_target(download, idx)
                                                download.save_as(str(target))
                                                item_result["success"] = True
                                                item_result["file"] = str(target)
                                                runtime["download_started"] = True
                                                runtime["download_file"] = str(target)
                                            except Exception:
                                                pass

                                    if not item_result["success"]:
                                        try:
                                            ready_icons = page.locator("[data-download-available]")
                                            if ready_icons.count() > 0:
                                                with page.expect_download(timeout=20000) as dl_info:
                                                    ready_icons.last.click(timeout=7000)
                                                download = dl_info.value
                                                target = make_target(download, idx)
                                                download.save_as(str(target))
                                                item_result["success"] = True
                                                item_result["file"] = str(target)
                                                runtime["download_started"] = True
                                                runtime["download_file"] = str(target)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass

                                if item_result["success"]:
                                    break

                                page.wait_for_timeout(900)

                            runtime["downloads"].append(item_result)
                            page.wait_for_timeout(1200)

                        page.wait_for_timeout(args.wait_ms)
                        page.screenshot(path=str(sixth_screen), full_page=True)

                page.screenshot(path=str(fifth_screen), full_page=True)

        return page

    print(f"[info] Acessando: {args.url}")
    response = StealthyFetcher.fetch(
        url=args.url,
        headless=resolve_headless(args.mode),
        timeout=args.timeout_ms,
        wait=args.wait_ms,
        page_action=page_action,
        humanize=True,
    )

    html = response.html_content or ""
    final_html.write_text(html, encoding="utf-8")

    title_node = response.css_first("title")
    title = ""
    if title_node:
        try:
            title = title_node.text  # type: ignore[union-attr]
        except Exception:
            title = str(title_node)
    html_lower = html.lower()
    challenge_detected = (
        "verify you are human" in html_lower
        or "just a moment" in html_lower
        or "cloudflare" in html_lower
    )

    metadata = {
        "status": response.status,
        "url": response.url,
        "title": title,
        "challenge_detected": challenge_detected,
        "first_screenshot": str(first_screen),
        "second_screenshot": str(second_screen),
        "third_screenshot": str(third_screen),
        "fourth_screenshot": str(fourth_screen),
        "fifth_screenshot": str(fifth_screen),
        "sixth_screenshot": str(sixth_screen),
        "password_field_detected": runtime["password_field_detected"],
        "password_submitted": runtime["password_submitted"],
        "contas_clicked": runtime["contas_clicked"],
        "faturas_clicked": runtime["faturas_clicked"],
        "invoices_url_opened": runtime["invoices_url_opened"],
        "download_started": runtime["download_started"],
        "retry_clicked": runtime["retry_clicked"],
        "download_file": runtime["download_file"],
        "manual_xpath_button_clicked": runtime["manual_xpath_button_clicked"],
        "manual_xpath_clicked": runtime["manual_xpath_clicked"],
        "manual_xpath_downloaded": runtime["manual_xpath_downloaded"],
        "manual_xpath_attempted": runtime["manual_xpath_attempted"],
        "downloads": runtime["downloads"],
        "available_invoices": runtime["available_invoices"],
        "account_filter": runtime["account_filter"],
        "account_order_filter": runtime["account_order_filter"],
        "period_filter": runtime["period_filter"],
        "submission_attempts": runtime["submission_attempts"],
        "final_html": str(final_html),
    }
    metadata_file.write_text(
        json.dumps(metadata, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )

    print(f"[ok] Status: {response.status}")
    print(f"[ok] URL final: {response.url}")
    print(f"[ok] Titulo: {title}")
    print(f"[ok] Challenge detectado: {challenge_detected}")
    print(f"[ok] Screenshot 1: {first_screen}")
    print(f"[ok] Screenshot 2: {second_screen}")
    print(f"[ok] Screenshot 3: {third_screen}")
    print(f"[ok] Screenshot 4: {fourth_screen}")
    print(f"[ok] Screenshot 5: {fifth_screen}")
    print(f"[ok] Screenshot 6: {sixth_screen}")
    print(f"[ok] Campo de senha detectado: {runtime['password_field_detected']}")
    print(f"[ok] Senha enviada: {runtime['password_submitted']}")
    print(f"[ok] Clique em Contas: {runtime['contas_clicked']}")
    print(f"[ok] Clique em Acessar faturas: {runtime['faturas_clicked']}")
    print(f"[ok] URL de faturas aberta direto: {runtime['invoices_url_opened']}")
    print(f"[ok] Download iniciado: {runtime['download_started']}")
    print(f"[ok] Clique em Tentar novamente: {runtime['retry_clicked']}")
    print(f"[ok] Tentativa de clique manual no XPath: {runtime['manual_xpath_attempted']}")
    print(f"[ok] Clique no botao manual XPath: {runtime['manual_xpath_button_clicked']}")
    print(f"[ok] Clique manual no XPath: {runtime['manual_xpath_clicked']}")
    print(f"[ok] Download via XPath manual: {runtime['manual_xpath_downloaded']}")
    print(f"[ok] Arquivo baixado: {runtime['download_file']}")
    print(f"[ok] Filtro conta: {runtime['account_filter']}")
    print(f"[ok] Filtro ordem da conta: {runtime['account_order_filter']}")
    print(f"[ok] Filtro periodo: {runtime['period_filter']}")
    available_value = runtime.get("available_invoices", [])
    available_count = len(available_value) if isinstance(available_value, list) else 0
    print(f"[ok] Faturas visiveis na pagina: {available_count}")
    if args.list_available or args.only_list:
        print("[info] Lista de contas disponiveis:")
        account_lines: list[str] = []
        account_seen = set()
        for item in available_value:
            if not isinstance(item, dict):
                continue
            account = str(item.get("account", ""))
            if not account or account in account_seen:
                continue
            account_seen.add(account)
            order_value = item.get("account_order", 0)
            account_order = order_value if isinstance(order_value, int) else 0
            if account_order > 0:
                account_lines.append(f" - [{account_order}] conta={account}")
            else:
                account_lines.append(f" - conta={account}")
        for line in account_lines:
            print(line)

        print("[info] Lista de faturas disponiveis:")
        for item in available_value:
            if not isinstance(item, dict):
                continue
            order_value = item.get("account_order", 0)
            account_order = order_value if isinstance(order_value, int) else 0
            print(
                " - "
                f"ordem={account_order} "
                f"conta={item.get('account', '')} "
                f"venc={item.get('due_date', '')} "
                f"valor={item.get('value', '')} "
                f"status={item.get('status', '')} "
                f"baixar={item.get('download_available', False)}"
            )
    downloads_value = runtime.get("downloads", [])
    downloads_count = len(downloads_value) if isinstance(downloads_value, list) else 0
    print(f"[ok] Total de itens processados: {downloads_count}")
    print(f"[ok] Tentativas de envio: {runtime['submission_attempts']}")
    print(f"[ok] HTML: {final_html}")
    print(f"[ok] Metadados: {metadata_file}")


if __name__ == "__main__":
    main()
