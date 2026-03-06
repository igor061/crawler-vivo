#!/usr/bin/env python3
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_LOGIN_URL = "https://mve.vivo.com.br/oauth?logout=true"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Abre a primeira tela da Vivo e tira screenshot"
    )
    parser.add_argument("--url", default=DEFAULT_LOGIN_URL)
    parser.add_argument("--output", default="screenshots/vivo_primeira_tela.png")
    parser.add_argument("--wait-ms", type=int, default=4000)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Executa sem abrir janela",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()

        print(f"[info] Abrindo: {args.url}")
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(args.wait_ms)

        page.screenshot(path=str(output), full_page=True)
        print(f"[ok] Screenshot salvo em: {output}")

        browser.close()


if __name__ == "__main__":
    main()
