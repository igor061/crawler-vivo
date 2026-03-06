#!/usr/bin/env python3
import argparse
import os
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_LOGIN_URL = "https://mve.vivo.com.br/oauth?logout=true"

KEYWORDS = ("pdf", "fatura", "2via", "2-via", "conta", "download", "boleto")


def slugify(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", text).strip("_")
    return value or "vivo_bill"


def unique_keep_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in values:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def normalize_cpf(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def guess_login_fields(page, cpf: str, password: str) -> bool:
    user_selectors = [
        "input[name*='cpf' i]",
        "input[id*='cpf' i]",
        "input[name*='user' i]",
        "input[id*='user' i]",
        "input[type='email']",
        "input[type='tel']",
        "input[type='text']",
    ]
    pass_selectors = [
        "input[type='password']",
        "input[name*='senha' i]",
        "input[id*='senha' i]",
        "input[name*='password' i]",
        "input[id*='password' i]",
    ]

    user_filled = False
    pass_filled = False

    for selector in user_selectors:
        locator = page.locator(selector).first
        if locator.count() > 0 and locator.is_visible():
            locator.fill(cpf)
            user_filled = True
            break

    for selector in pass_selectors:
        locator = page.locator(selector).first
        if locator.count() > 0 and locator.is_visible():
            locator.fill(password)
            pass_filled = True
            break

    if user_filled and pass_filled:
        for selector in [
            "button:has-text('Entrar')",
            "button:has-text('Acessar')",
            "button:has-text('Login')",
            "button[type='submit']",
            "input[type='submit']",
        ]:
            button = page.locator(selector).first
            if button.count() > 0 and button.is_visible():
                button.click()
                return True

    return False


def extract_candidate_links(page) -> list[str]:
    candidates: list[str] = []

    for frame in page.frames:
        base_url = frame.url or page.url
        try:
            links = frame.eval_on_selector_all(
                "a[href]",
                """
                anchors => anchors
                  .map(a => {
                    const href = a.getAttribute('href') || ''
                    const text = (a.textContent || '').toLowerCase()
                    return { href, text }
                  })
                  .filter(item => item.href)
                """,
            )
        except Exception:
            continue

        for item in links:
            href = str(item.get("href", ""))
            text = str(item.get("text", ""))
            joined = urljoin(base_url, href)
            searchable = f"{href.lower()} {text.lower()} {joined.lower()}"
            if any(keyword in searchable for keyword in KEYWORDS):
                candidates.append(joined)

    return unique_keep_order(candidates)


def save_download_object(download, output_dir: Path) -> Path:
    suggested = download.suggested_filename or "fatura_vivo.pdf"
    file_name = slugify(suggested)
    if not file_name.lower().endswith(".pdf"):
        file_name += ".pdf"
    target = output_dir / file_name
    download.save_as(str(target))
    return target


def click_and_capture_downloads(page, output_dir: Path, timeout_ms: int) -> int:
    selectors = [
        "a:has-text('Baixar')",
        "a:has-text('Download')",
        "a:has-text('2 via')",
        "a:has-text('2a via')",
        "a:has-text('Fatura')",
        "button:has-text('Baixar')",
        "button:has-text('Download')",
        "button:has-text('2 via')",
        "button:has-text('2a via')",
        "button:has-text('Fatura')",
    ]

    saved = 0
    clicked_labels = set()

    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        for i in range(count):
            item = locator.nth(i)
            if not item.is_visible() or not item.is_enabled():
                continue

            label = (item.inner_text(timeout=1500) or "").strip().lower()
            if not label:
                label = selector

            if label in clicked_labels:
                continue

            try:
                with page.expect_download(timeout=timeout_ms) as dl_info:
                    item.click(timeout=3000)
                download = dl_info.value
                target = save_download_object(download, output_dir)
                print(f"[ok] Download via clique: {target}")
                clicked_labels.add(label)
                saved += 1
            except PlaywrightTimeoutError:
                continue
            except Exception as exc:
                print(f"[warn] Falha ao clicar em '{label}': {exc}")

    return saved


def save_links_as_pdf_requests(context, links: list[str], output_dir: Path) -> int:
    saved = 0
    for idx, link in enumerate(links, start=1):
        try:
            response = context.request.get(link, timeout=30000)
            if not response.ok:
                continue
            content_type = (response.header_value("content-type") or "").lower()
            body = response.body()
            if "pdf" not in content_type and not body.startswith(b"%PDF"):
                continue

            file_name = slugify(link.split("/")[-1] or f"fatura_{idx}.pdf")
            if not file_name.lower().endswith(".pdf"):
                file_name += ".pdf"
            target = output_dir / file_name
            target.write_bytes(body)
            saved += 1
            print(f"[ok] Salvo: {target}")
        except Exception as exc:
            print(f"[warn] Falha ao baixar {link}: {exc}")
    return saved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Baixa faturas da Vivo apos login no Meu Vivo"
    )
    parser.add_argument("--login-url", default=DEFAULT_LOGIN_URL)
    parser.add_argument("--bills-url", default="")
    parser.add_argument("--cpf", default=os.getenv("VIVO_CPF", ""))
    parser.add_argument("--password", default=os.getenv("VIVO_PASSWORD", ""))
    parser.add_argument("--download-dir", default="downloads/vivo")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Executa sem abrir janela do navegador",
    )
    parser.add_argument(
        "--pause-for-manual-login",
        action="store_true",
        help="Sempre pausa para voce concluir login manual",
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=5000,
        help="Tempo de espera apos login e navegacao (ms)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.download_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print(f"[info] Abrindo login em: {args.login_url}")
        page.goto(args.login_url, wait_until="domcontentloaded")

        auto_login_attempted = False
        cpf = normalize_cpf(args.cpf)

        if cpf and args.password and not args.pause_for_manual_login:
            try:
                page.wait_for_timeout(1500)
                auto_login_attempted = guess_login_fields(page, cpf, args.password)
                if auto_login_attempted:
                    print("[info] Login automatico enviado")
                else:
                    print("[warn] Nao encontrei campos para login automatico")
            except Exception as exc:
                print(f"[warn] Falha no login automatico: {exc}")

        if args.pause_for_manual_login or not auto_login_attempted:
            input("Conclua login manualmente no navegador e pressione Enter...")
        else:
            try:
                page.wait_for_timeout(args.wait_ms)
            except PlaywrightTimeoutError:
                pass

        if args.bills_url:
            print(f"[info] Abrindo pagina de faturas: {args.bills_url}")
            page.goto(args.bills_url, wait_until="domcontentloaded")
            page.wait_for_timeout(args.wait_ms)

        print("[info] Tentando baixar por clique em botoes/links...")
        clicked_saved = click_and_capture_downloads(page, output_dir, timeout_ms=7000)

        links = extract_candidate_links(page)
        if not links:
            if clicked_saved > 0:
                print(f"[done] PDFs salvos: {clicked_saved}")
            else:
                print("[warn] Nao encontrei links de fatura nesta pagina")
                print("[tip] Navegue ate a tela de faturas e rode novamente")
            browser.close()
            return

        print(f"[info] Links candidatos encontrados: {len(links)}")
        saved_by_link = save_links_as_pdf_requests(context, links, output_dir)
        total_saved = clicked_saved + saved_by_link
        print(f"[done] PDFs salvos: {total_saved}")
        browser.close()


if __name__ == "__main__":
    main()
