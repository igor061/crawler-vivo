from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from vivo_fixo import FixoConfig, FixoDownloadService, VivoFixoApp, _ref_yyyymm


def _config(tmp_path: Path, **kwargs: Any) -> FixoConfig:
    base = dict(
        url="https://exemplo", dashboard_url="https://exemplo/d",
        invoices_url="https://exemplo/f", cpf_ou_cnpj="99999999000191",
        password="segredo", output_dir=tmp_path, download_dir=tmp_path,
        wait_ms=0, timeout_ms=0, debug=False, listar=False, mode="headless",
        coleta_dt=datetime(2026, 8, 24, tzinfo=UTC), limite=None,
    )
    return FixoConfig(**{**base, **kwargs})


class _Noop:
    def log(self, *a: Any, **k: Any) -> None:
        pass

    def minimizar(self, page: Any) -> None:
        pass


class _Page:
    def wait_for_timeout(self, _ms: int) -> None:
        pass


class _Servico(FixoDownloadService):
    def __init__(self, opcoes: list[dict[str, Any]]) -> None:
        self.logger = _Noop()
        self.panel = _Noop()
        self._opcoes = opcoes
        self.chamadas: list[str] = []

    def _clicar_ver_detalhes(self, page: Any, sec_locator: Any) -> bool:
        return True

    def _obter_opcoes_no_slide(self, page: Any) -> list[dict[str, Any]]:
        return self._opcoes

    def _baixar_com_retry(self, page, toggle, codigo, cnpj, idx, referencia, situacao, config, runtime):
        self.chamadas.append(referencia)
        return self._resultado_falha(codigo, referencia, situacao, config, "x")


@pytest.mark.parametrize("entrada,esperado", [
    ("Ago/2026", "202608"), ("agosto/26", "202608"), ("21/09/2026", "202609"),
    ("202609", "202609"), ("", ""), ("xyz", ""),
])
def test_ref_yyyymm(entrada: str, esperado: str) -> None:
    assert _ref_yyyymm(entrada) == esperado


def _secoes(faturas: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [{"_sec_locator": object(), "codigo_cliente": "0439719185", "faturas": faturas or []}]


def test_baixar_todos_download_normaliza_e_guarda_portal(tmp_path: Path) -> None:
    s = _Servico([{"referencia": "Ago/2026", "toggle": object()}, {"referencia": "21/09/2026", "toggle": object()},
                  {"referencia": "lixo", "toggle": object()}])
    runtime: dict[str, Any] = {}
    r = s.baixar_todos(_Page(), _secoes(), _config(tmp_path), runtime)
    assert [x["referencia"] for x in r] == ["202608", "202609", "lixo"]
    assert [x["referencia_portal"] for x in r] == ["Ago/2026", "21/09/2026", "lixo"]
    assert s.chamadas == ["202608", "202609", "lixo"]  # download recebe a normalizada
    assert runtime["tentativas"] == 3


def test_baixar_todos_referencia_vem_da_fatura_quando_opcao_sem_ref(tmp_path: Path) -> None:
    s = _Servico([{"toggle": object()}])
    r = s.baixar_todos(_Page(), _secoes([{"referencia": "Set/2026"}]), _config(tmp_path), {})
    assert (r[0]["referencia"], r[0]["referencia_portal"]) == ("202609", "Set/2026")


def test_listar_nao_baixa_e_nao_conta_tentativas(tmp_path: Path) -> None:
    s = _Servico([{"referencia": "Ago/2026", "toggle": object()}, {"referencia": "21/09/2026", "toggle": object()}])
    runtime: dict[str, Any] = {}
    r = s.baixar_todos(_Page(), _secoes(), _config(tmp_path, listar=True), runtime)
    assert s.chamadas == []
    assert "tentativas" not in runtime
    assert [x["referencia"] for x in r] == ["202608", "202609"]
    assert [x["referencia_portal"] for x in r] == ["Ago/2026", "21/09/2026"]
    assert all(x["download_ok"] is False and x["erro_download"] == "" for x in r)


def test_popular_faturas_normaliza(tmp_path: Path) -> None:
    app = VivoFixoApp.__new__(VivoFixoApp)
    app.config = _config(tmp_path)
    app.runtime = {"faturas_disponiveis": []}
    app._popular_faturas([{"codigo_cliente": "1", "faturas": [{"referencia": "Ago/2026"}, {"referencia": "zzz"}]}])
    f = app.runtime["faturas_disponiveis"]
    assert [x["referencia"] for x in f] == ["202608", "zzz"]
    assert [x["referencia_portal"] for x in f] == ["Ago/2026", "zzz"]


def test_baixar_boleto_com_referencia_aaaamm_acha_pdf_existente(tmp_path: Path) -> None:
    pdf = tmp_path / "vivo-fixo-99999999000191-0439719185-202609.pdf"
    pdf.write_bytes(b"%PDF")
    s = _Servico([])
    runtime: dict[str, Any] = {}
    r = s._baixar_boleto_por_linha(_Page(), object(), "0439719185", "99999999000191", 1,
                                   "202609", "Aberta", _config(tmp_path), runtime)
    assert r["download_ok"] is True and r["arquivo_download"] == str(pdf)
    assert runtime["downloads_ok"] == 1
