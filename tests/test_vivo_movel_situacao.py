"""Situacao so da fatura mais recente (badge) + captura debug do slide.

HTML sintetico com a mesma estrutura do slide real do portal Vivo:
.data-card > [.data-card-badge, datacardsection*]. Playwright real.
"""
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from vivo_movel import MovelDownloadService  # noqa: E402
from test_vivo_movel_listar import _config, _FakePage, _ServicoEspiao  # noqa: E402


def _linha(venc: str) -> str:
    return (
        '<datacardsection data-test-card-section-content class="data-card-section">'
        f'<p aria-label="{venc}">{venc}</p>'
        '<button data-test-open-dropdown-button>Opcoes</button></datacardsection>'
    )


def _slide(linhas: list[str], badge: str = "") -> str:
    return (
        '<div data-slider-root><div class="data-card">' + badge
        + "".join(_linha(v) for v in linhas) + "</div></div>"
    )


BADGE_ARIA = '<div class="badge data-card-badge success" aria-label="Paga"><p>Paga</p></div>'
BADGE_TEXTO = '<div class="badge data-card-badge"><p>Em aberto</p></div>'
VENCS = ["25/08/2026", "25/07/2026", "25/06/2026"]


class _Log:
    def log(self, *a: Any, **k: Any) -> None:
        pass


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            # revisao do chromium instalada pode divergir da esperada pelo playwright
            exe = sorted(Path.home().glob(
                ".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
            if not exe:
                pytest.skip("chromium indisponivel")
            b = p.chromium.launch(executable_path=str(exe[-1]))
        yield b
        b.close()


def _opcoes(browser: Any, html: str) -> list[dict[str, Any]]:
    page = browser.new_page()
    try:
        page.set_content(html)
        svc = MovelDownloadService.__new__(MovelDownloadService)
        svc.logger = _Log()
        return svc._obter_opcoes_no_slide(page)
    finally:
        page.close()


def test_so_primeira_linha_recebe_situacao(browser: Any) -> None:
    ops = _opcoes(browser, _slide(VENCS, BADGE_ARIA))
    assert [o["vencimento"] for o in ops] == VENCS
    assert [o["situacao"] for o in ops] == ["Paga", "", ""]


def test_situacao_via_inner_text_sem_aria_label(browser: Any) -> None:
    ops = _opcoes(browser, _slide(VENCS, BADGE_TEXTO))
    assert [o["situacao"] for o in ops] == ["Em aberto", "", ""]


def test_sem_badge_todas_vazias(browser: Any) -> None:
    ops = _opcoes(browser, _slide(VENCS))
    assert [o["situacao"] for o in ops] == ["", "", ""]


def test_badge_aninhado_nao_e_lido(browser: Any) -> None:
    # badge que nao e filho direto do .data-card pai da linha deve ser ignorado
    html = (
        '<div data-slider-root><div class="data-card"><div class="x">'
        + BADGE_ARIA + "</div>" + _linha(VENCS[0]) + "</div></div>"
    )
    assert _opcoes(browser, html)[0]["situacao"] == ""


def test_linha_sem_toggle_nao_consome_o_slot_da_primeira(browser: Any) -> None:
    # linha sem botao e ignorada; a primeira linha COM opcao recebe o badge
    html = (
        '<div data-slider-root><div class="data-card">' + BADGE_ARIA
        + '<datacardsection data-test-card-section-content><p aria-label="x">x</p></datacardsection>'
        + _linha(VENCS[0]) + _linha(VENCS[1]) + "</div></div>"
    )
    assert [o["situacao"] for o in _opcoes(browser, html)] == ["Paga", ""]


class _Debug:
    def __init__(self) -> None:
        self.etapas: list[str] = []

    def capture(self, page: Any, etapa: str, runtime: dict[str, Any]) -> None:
        self.etapas.append(etapa)


class _ServicoDebug(_ServicoEspiao):
    def __init__(self, debug: Any) -> None:
        super().__init__([])
        self.debug = debug
        self.ordem: list[str] = []

    def _obter_opcoes_no_slide(self, page: Any) -> list[dict[str, Any]]:
        self.ordem.append("opcoes")
        return []


def _rodar(debug: Any) -> _ServicoDebug:
    svc = _ServicoDebug(debug)
    if debug is not None:
        orig = debug.capture
        debug.capture = lambda p, e, r: (svc.ordem.append("capture"), orig(p, e, r))[1]
    secoes = [
        {"_sec_locator": object(), "codigo_cliente": "0439719185"},
        {"_sec_locator": object(), "codigo_cliente": "0439719186"},
    ]
    svc.baixar_todos(_FakePage(), secoes, _config(), {})
    return svc


def test_debug_captura_slide_de_cada_conta_antes_das_opcoes() -> None:
    dbg = _Debug()
    svc = _rodar(dbg)
    assert dbg.etapas == ["slide_0439719185", "slide_0439719186"]
    assert svc.ordem == ["capture", "opcoes", "capture", "opcoes"]


def test_sem_debug_nao_quebra() -> None:
    assert _rodar(None).ordem == ["opcoes", "opcoes"]


def test_app_injeta_debug_no_servico() -> None:
    import inspect
    from vivo_movel import VivoMovelApp
    assert "self.debug" in inspect.getsource(VivoMovelApp.__init__)
