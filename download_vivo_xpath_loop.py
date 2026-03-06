#!/usr/bin/env python3
import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from scrapling.fetchers import StealthyFetcher

DEFAULT_URL = "https://mve.vivo.com.br/oauth?logout=true"
DEFAULT_DASHBOARD_URL = "https://mve.vivo.com.br/sec/dashboard"
DEFAULT_INVOICES_URL = "https://mve.vivo.com.br/sec/invoices"


def normalize_cpf(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def slugify_filename(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", value or "").strip("_")
    return clean or "fatura.pdf"


def resolve_headless(mode: str):
    if mode == "virtual":
        return "virtual"
    return mode == "headless"


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
        "--mode",
        choices=["headless", "headful", "virtual"],
        default="headless",
        help="Modo do navegador do Camoufox",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    download_dir = Path(args.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    cpf = normalize_cpf(args.cpf)
    password = args.password or ""

    first_screen = output_dir / "xpath_loop_01_primeira_tela.png"
    second_screen = output_dir / "xpath_loop_02_apos_cpf.png"
    third_screen = output_dir / "xpath_loop_03_apos_senha.png"
    fourth_screen = output_dir / "xpath_loop_04_invoices.png"
    fifth_screen = output_dir / "xpath_loop_05_final.png"
    metadata_file = output_dir / "xpath_loop_resultado.json"

    runtime: dict[str, Any] = {
        "password_field_detected": False,
        "password_submitted": False,
        "dashboard_detected": False,
        "invoices_opened": False,
        "toggle_dialog_clicked": 0,
        "xpath_attempts": 0,
        "downloads_ok": 0,
        "downloads_fail": 0,
        "downloads": [],
        "available_invoices": [],
    }

    def page_action(page):
        def click_with_fallback(locator, timeout: int = 5000) -> bool:
            try:
                locator.click(timeout=timeout)
                return True
            except Exception:
                pass

            try:
                locator.click(timeout=timeout, force=True)
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

        if not has_password_field():
            for _ in range(4):
                clicked = False
                for selector in continue_selectors:
                    button = page.locator(selector).first
                    if button.count() > 0 and button.is_visible() and button.is_enabled():
                        button.click(timeout=3000)
                        clicked = True
                        break
                if not clicked:
                    page.keyboard.press("Enter")
                page.wait_for_timeout(args.wait_ms)
                if has_password_field():
                    break

        if has_password_field():
            runtime["password_field_detected"] = True

        if not runtime["password_field_detected"] or not password:
            return page

        pass_filled = False
        for selector in password_selectors:
            field = page.locator(selector).first
            if field.count() > 0 and field.is_visible():
                field.fill(password)
                pass_filled = True
                break

        if not pass_filled:
            return page

        enter_selectors = [
            "button:has-text('Entrar')",
            "button[type='submit']",
            "input[type='submit']",
        ]
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
        page.screenshot(path=str(third_screen), full_page=True)

        try:
            page.wait_for_url("**/sec/dashboard*", timeout=30000)
            runtime["dashboard_detected"] = True
        except Exception:
            runtime["dashboard_detected"] = args.dashboard_url in (page.url or "")

        try:
            page.goto(args.invoices_url, wait_until="domcontentloaded")
            runtime["invoices_opened"] = "/sec/invoices" in (page.url or "")
            page.wait_for_timeout(args.wait_ms)
            page.screenshot(path=str(fourth_screen), full_page=True)
        except Exception:
            runtime["invoices_opened"] = False
            return page

        available_invoices: list[dict[str, Any]] = []

        for section_idx in range(1, args.max_sections + 1):
            section_xpath = (
                f"/html/body/main/div/div/div/div/div[2]/div[2]/div/div[1]/section[{section_idx}]"
            )
            section_locator = page.locator(f"xpath={section_xpath}").first
            if section_locator.count() == 0:
                if section_idx == 1:
                    continue
                break

            section_account = ""
            try:
                section_title = page.locator(
                    f"xpath={section_xpath}//h4[contains(@aria-label,'Conta')]"
                ).first
                if section_title.count() > 0:
                    title_text = section_title.inner_text(timeout=1500)
                    account_match = re.search(r"(\d{8,})", title_text)
                    if account_match:
                        section_account = account_match.group(1)
            except Exception:
                pass

            for div_idx in range(args.start_div_index, args.max_div_index + 1):
                row_xpath = f"{section_xpath}/div/div[{div_idx}]"
                button_xpath = f"{row_xpath}/div[4]/div/div/div[2]/div/button"
                link_xpath = f"{row_xpath}/div[4]/div/div/div[2]/div/div/ul/li[1]/a"

                row = page.locator(f"xpath={row_xpath}").first
                if row.count() == 0:
                    if div_idx == args.start_div_index:
                        break
                    break

                button = page.locator(f"xpath={button_xpath}").first
                if button.count() == 0:
                    continue

                due_date = ""
                value = ""
                status = ""

                try:
                    due = row.locator(
                        ".data-card-section__thirdColumn p.data-card-cell__description"
                    ).first
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

                available_invoices.append(
                    {
                        "section": section_idx,
                        "div": div_idx,
                        "account": section_account,
                        "due_date": due_date,
                        "value": value,
                        "status": status,
                        "download_ok": False,
                        "download_file": "",
                        "download_error": "",
                    }
                )

        for invoice in available_invoices:
            runtime["xpath_attempts"] = int(runtime["xpath_attempts"]) + 1
            section_idx = int(invoice.get("section", 0))
            div_idx = int(invoice.get("div", 0))
            section_xpath = (
                f"/html/body/main/div/div/div/div/div[2]/div[2]/div/div[1]/section[{section_idx}]"
            )
            button_xpath = f"{section_xpath}/div/div[{div_idx}]/div[4]/div/div/div[2]/div/button"
            link_xpath = (
                f"{section_xpath}/div/div[{div_idx}]/div[4]/div/div/div[2]/div/div/ul/li[1]/a"
            )

            download_item: dict[str, Any] = {
                "section": section_idx,
                "div": div_idx,
                "account": str(invoice.get("account", "")),
                "due_date": str(invoice.get("due_date", "")),
                "button_xpath": button_xpath,
                "link_xpath": link_xpath,
                "success": False,
                "file": "",
                "error": "",
            }

            try:
                opened_toggle = page.locator("div.toggle-dialog.dialog-icon.opened").first
                if opened_toggle.count() > 0:
                    if click_with_fallback(opened_toggle, timeout=3000):
                        runtime["toggle_dialog_clicked"] = int(runtime["toggle_dialog_clicked"]) + 1
                        page.wait_for_timeout(250)

                button = page.locator(f"xpath={button_xpath}").first
                if button.count() == 0:
                    raise RuntimeError("Botao nao encontrado")

                try:
                    button.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass

                if not click_with_fallback(button, timeout=5000):
                    raise RuntimeError("Falha ao clicar no botao")

                page.wait_for_timeout(300)

                link = page.locator(f"xpath={link_xpath}").first
                if link.count() == 0:
                    raise RuntimeError("Link de download nao encontrado")

                with page.expect_download(timeout=45000) as dl_info:
                    if not click_with_fallback(link, timeout=5000):
                        raise RuntimeError("Falha ao clicar no link de download")

                download = dl_info.value
                suggested = download.suggested_filename or "fatura.pdf"
                target_name = slugify_filename(f"s{section_idx:02d}_d{div_idx:02d}_{suggested}")
                if not target_name.lower().endswith(".pdf"):
                    target_name += ".pdf"
                target = download_dir / target_name
                download.save_as(str(target))

                download_item["success"] = True
                download_item["file"] = str(target)
                runtime["downloads_ok"] = int(runtime["downloads_ok"]) + 1

                invoice["download_ok"] = True
                invoice["download_file"] = str(target)
            except Exception as exc:
                runtime["downloads_fail"] = int(runtime["downloads_fail"]) + 1
                message = str(exc)
                download_item["error"] = message
                invoice["download_error"] = message

            runtime["downloads"].append(download_item)
            page.wait_for_timeout(500)

        runtime["available_invoices"] = available_invoices

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

    metadata = {
        "status": response.status,
        "url": response.url,
        "password_field_detected": runtime["password_field_detected"],
        "password_submitted": runtime["password_submitted"],
        "dashboard_detected": runtime["dashboard_detected"],
        "invoices_opened": runtime["invoices_opened"],
        "toggle_dialog_clicked": runtime["toggle_dialog_clicked"],
        "xpath_attempts": runtime["xpath_attempts"],
        "downloads_ok": runtime["downloads_ok"],
        "downloads_fail": runtime["downloads_fail"],
        "downloads": runtime["downloads"],
        "available_invoices": runtime["available_invoices"],
        "first_screenshot": str(first_screen),
        "second_screenshot": str(second_screen),
        "third_screenshot": str(third_screen),
        "fourth_screenshot": str(fourth_screen),
        "fifth_screenshot": str(fifth_screen),
    }
    metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[ok] Status: {response.status}")
    print(f"[ok] URL final: {response.url}")
    print(f"[ok] Campo de senha detectado: {runtime['password_field_detected']}")
    print(f"[ok] Senha enviada: {runtime['password_submitted']}")
    print(f"[ok] Dashboard detectado: {runtime['dashboard_detected']}")
    print(f"[ok] Invoices aberto: {runtime['invoices_opened']}")
    print(f"[ok] Cliques em toggle-dialog aberto: {runtime['toggle_dialog_clicked']}")
    print(f"[ok] Tentativas XPath: {runtime['xpath_attempts']}")
    print(f"[ok] Downloads OK: {runtime['downloads_ok']}")
    print(f"[ok] Downloads falhos: {runtime['downloads_fail']}")
    print("[info] Lista de faturas encontradas:")
    invoices_value = runtime.get("available_invoices", [])
    if isinstance(invoices_value, list):
        for item in invoices_value:
            if not isinstance(item, dict):
                continue
            print(
                " - "
                f"conta={item.get('account', '')} "
                f"venc={item.get('due_date', '')} "
                f"valor={item.get('value', '')} "
                f"status={item.get('status', '')} "
                f"download_ok={item.get('download_ok', False)} "
                f"arquivo={item.get('download_file', '')}"
            )
    print(f"[ok] Metadados: {metadata_file}")


if __name__ == "__main__":
    main()
