from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vivo_movel import MovelConfig, MovelDownloadService


def _config(**kwargs: Any) -> MovelConfig:
    base = dict(
        url="https://exemplo", dashboard_url="https://exemplo/d",
        invoices_url="https://exemplo/f", cpf_ou_cnpj="99999999000191",
        password="segredo", output_dir=Path("/tmp"), download_dir=Path("/tmp"),
        wait_ms=0, timeout_ms=0, debug=False, listar=False, mode="headless",
        coleta_dt=datetime(2026, 8, 24, tzinfo=UTC),
    )
    return MovelConfig(**{**base, **kwargs})


class _FakePage:
    def wait_for_timeout(self, _ms: int) -> None:
        pass


class _ServicoEspiao(MovelDownloadService):
    """Instrumenta o servico para registrar qualquer tentativa de download."""

    def __init__(self, opcoes: list[dict[str, Any]]) -> None:
        self._opcoes = opcoes
        self.downloads = 0
        self.slides_fechados = 0

    def _clicar_exibir_detalhes(self, page: Any, sec_locator: Any) -> bool:
        return True

    def _obter_opcoes_no_slide(self, page: Any) -> list[dict[str, Any]]:
        return self._opcoes

    def _fechar_slide(self, page: Any) -> None:
        self.slides_fechados += 1

    def _processar_com_retry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.downloads += 1
        raise AssertionError("--listar nao pode baixar fatura")

    @property
    def panel(self) -> Any:
        return self

    def minimizar(self, page: Any) -> None:
        pass

    @property
    def logger(self) -> Any:
        return self

    def log(self, *args: Any, **kwargs: Any) -> None:
        pass


def _opcao(vencimento: str) -> dict[str, Any]:
    return {"vencimento": vencimento, "situacao": "Paga", "toggle": object(), "row": object()}


def test_listar_nao_baixa_nenhuma_fatura() -> None:
    opcoes = [_opcao("25/06/2026"), _opcao("25/07/2026"), _opcao("25/08/2026")]
    servico = _ServicoEspiao(opcoes)
    config = _config(listar=True, limite=1)
    secoes = [{"_sec_locator": object(), "codigo_cliente": "0439719185"}]

    resultados = servico.baixar_todos(_FakePage(), secoes, config, {})

    assert servico.downloads == 0
    # lista as 3 apesar de --limite 1: limite so vale para download
    assert len(resultados) == 3
    assert [r["referencia"] for r in resultados] == ["202606", "202607", "202608"]
    assert all(r["download_ok"] is False for r in resultados)
    assert all(r["arquivo_download"] == "" for r in resultados)
