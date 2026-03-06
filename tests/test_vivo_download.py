from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from vivo_download import (
    AppConfig,
    InvoiceService,
    Logger,
    NamingService,
    ResultService,
    ano_mes_por_vencimento,
    extrair_cnpj_da_pagina,
    format_coleta_data_hora,
    montar_xpaths,
    normalize_document,
)


def test_normalize_document_keeps_digits_only() -> None:
    assert normalize_document("10.755.237/0001-36") == "10755237000136"


def test_ano_mes_por_vencimento_returns_zeroes_for_invalid_date() -> None:
    assert ano_mes_por_vencimento("invalido") == ("0000", "00")


def test_montar_xpaths_builds_expected_paths() -> None:
    row, button, link = montar_xpaths(3, 7)

    assert "/section[3]/div/div[7]" in row
    assert button.endswith("/div[4]/div/div/div[2]/div/button")
    assert link.endswith("/div[4]/div/div/div[2]/div/div/ul/li[1]/a")


def test_extrair_cnpj_da_pagina_prefers_dashboard_document_number() -> None:
    html = """
    <script>window.dashboardDocumentNumber = '10755237000136';</script>
    <div>00000000000000</div>
    """

    assert extrair_cnpj_da_pagina(html) == "10755237000136"


def test_format_coleta_data_hora_uses_seconds_precision() -> None:
    dt = datetime(2026, 3, 6, 12, 0, 5, 999999, tzinfo=UTC)

    assert format_coleta_data_hora(dt) == "2026-03-06T12:00:05+00:00"


def test_naming_service_appends_counter_when_file_exists(tmp_path: Path) -> None:
    target_1 = tmp_path / "vivo-10755237000136-0466032796-2026-02.pdf"
    target_1.write_bytes(b"x")

    target_2 = NamingService.montar_nome_arquivo_padrao(
        cnpj_cliente="10.755.237/0001-36",
        conta="0466032796",
        vencimento="15/02/2026",
        download_dir=tmp_path,
    )

    assert target_2.name == "vivo-10755237000136-0466032796-2026-02-2.pdf"


def test_invoice_service_deduplicates_collected_items() -> None:
    service = InvoiceService(Logger())

    class FakeStrategy:
        def collect(self, page, config):
            return [
                {
                    "conta": "1",
                    "vencimento": "10/01/2026",
                    "valor": "R$ 1,00",
                    "situacao": "Aberta",
                },
                {
                    "conta": "1",
                    "vencimento": "10/01/2026",
                    "valor": "R$ 1,00",
                    "situacao": "Aberta",
                },
            ]

    service.strategies = [FakeStrategy()]

    result = service.coletar_faturas(page=None, config=cast(AppConfig, object()))

    assert len(result) == 1


def test_result_service_enriches_downloaded_invoice(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "fatura.pdf"
    pdf.write_bytes(b"pdf")

    def fake_extract(_path, verbose=False):
        assert verbose is False
        return {
            "codigo_barras_digitavel": "846700000017 435001082021 601102511002 000420938712",
            "pix_copia_cola": "000201...",
            "emissor": "TELEFONICA BRASIL",
            "destinatario": "CLIENTE TESTE",
            "identificador_fatura": "0466032796",
            "data_emissao": "01/02/2026",
            "data_vencimento": "10/02/2026",
            "valor": "R$ 199,90",
        }

    monkeypatch.setattr("vivo_fatura_extrator.extrair_dados_fatura", fake_extract)

    item = {
        "conta": "0466032796",
        "vencimento": "10/02/2026",
        "valor": "R$ 0,00",
        "situacao": "Aberta",
        "download_ok": True,
        "arquivo_download": str(pdf),
        "erro_download": "",
        "codigo_de_barras": "",
        "codigo_de_barras_sem_espaco": "",
        "coleta_data_hora": "2026-03-06T12:00:00-03:00",
    }

    result = ResultService(NamingService()).normalizar_faturas_saida([item])[0]

    assert result["codigo_de_barras"] == "846700000017 435001082021 601102511002 000420938712"
    assert (
        result["codigo_de_barras_sem_espaco"] == "846700000017435001082021601102511002000420938712"
    )
    assert result["pix_copia_cola"] == "000201..."
    assert result["emissor"] == "TELEFONICA BRASIL"
    assert result["identificador_fatura"] == "0466032796"
    assert result["valor"] == "R$ 199,90"
