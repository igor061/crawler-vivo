from datetime import UTC, datetime
from pathlib import Path

import pytest

from vivo_core import (
    NamingService,
    ano_mes_por_vencimento,
    buscar_pdf_existente,
    enriquecer_com_extrator,
    extrair_cnpj_da_pagina,
    format_coleta_data_hora,
    normalize_document,
    referencia_para_yyyymm,
)


def test_normalize_document_keeps_digits_only() -> None:
    assert normalize_document("10.755.237/0001-36") == "10755237000136"


def test_ano_mes_por_vencimento_returns_zeroes_for_invalid_date() -> None:
    assert ano_mes_por_vencimento("invalido") == ("0000", "00")


def test_ano_mes_por_vencimento_parses_dd_mm_yyyy() -> None:
    assert ano_mes_por_vencimento("15/02/2026") == ("2026", "02")


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
    target_1 = tmp_path / "vivo-movel-10755237000136-0466032796-2026-02.pdf"
    target_1.write_bytes(b"x")

    target_2 = NamingService.montar_nome_arquivo_padrao(
        cnpj_cliente="10.755.237/0001-36",
        conta="0466032796",
        vencimento="15/02/2026",
        download_dir=tmp_path,
    )

    assert target_2.name == "vivo-movel-10755237000136-0466032796-2026-02-2.pdf"


def test_referencia_para_yyyymm_fev_2026() -> None:
    assert referencia_para_yyyymm("Fev/2026") == "202602"


def test_referencia_para_yyyymm_short_year() -> None:
    assert referencia_para_yyyymm("Mar/26") == "202603"


def test_referencia_para_yyyymm_invalid_returns_empty() -> None:
    assert referencia_para_yyyymm("invalido") == ""


def test_buscar_pdf_existente_finds_matching_file(tmp_path: Path) -> None:
    pdf = tmp_path / "vivo-movel-12345678000190-9999-202602.pdf"
    pdf.write_bytes(b"x")

    result = buscar_pdf_existente(tmp_path, "vivo-movel", "12345678000190", "9999", "202602")
    assert result == pdf


def test_buscar_pdf_existente_returns_none_when_missing(tmp_path: Path) -> None:
    result = buscar_pdf_existente(tmp_path, "vivo-movel", "12345678000190", "9999", "202602")
    assert result is None


def test_enriquecer_com_extrator_enriches_item(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf = tmp_path / "fatura.pdf"
    pdf.write_bytes(b"pdf")

    def fake_extract(_path, verbose=False):
        return {
            "codigo_barras_digitavel": "846700000017 435001082021 601102511002 000420938712",
            "pix_copia_cola": "000201...",
            "emissor": "TELEFONICA BRASIL",
            "destinatario": "CLIENTE TESTE",
            "identificador_fatura": "0466032796",
            "telefone": "(11) 9999-8888",
            "numeros_vivo": ["11999998888"],
            "url_nfe": "https://dfe-portal.svrs.rs.gov.br/NFe/QRCode?chNFCom=12345",
            "data_emissao": "01/02/2026",
            "data_vencimento": "10/02/2026",
            "valor": "R$ 199,90",
        }

    monkeypatch.setattr("vivo_fatura_extrator.extrair_dados_fatura", fake_extract)

    item: dict = {
        "arquivo_download": str(pdf),
        "valor": "R$ 0,00",
    }
    result = enriquecer_com_extrator(item)

    assert result["codigo_de_barras"] == "846700000017 435001082021 601102511002 000420938712"
    assert result["codigo_de_barras_sem_espaco"] == "846700000017435001082021601102511002000420938712"
    assert result["pix_copia_cola"] == "000201..."
    assert result["emissor"] == "TELEFONICA BRASIL"
    assert result["identificador_fatura"] == "0466032796"
    assert result["valor"] == "R$ 199,90"
    assert result["url_nfe"].startswith("https://")


def test_enriquecer_com_extrator_skips_missing_file() -> None:
    item: dict = {"arquivo_download": "/nonexistent/path.pdf"}
    result = enriquecer_com_extrator(item)
    assert result is item
    assert "codigo_de_barras" not in result
