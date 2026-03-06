from pathlib import Path

from vivo_fatura_extrator import (
    ArrecadacaoBarcodeExtractor,
    Boleto47BarcodeExtractor,
    CompositeBarcodeExtractor,
    RegexChainExtractor,
    extrair_dados_fatura,
    extrair_destinatario,
    extrair_emissor,
    output_path_for,
    salvar_resultado_json,
)


def test_regex_chain_extractor_uses_first_match_with_wrap_prefix() -> None:
    extractor = RegexChainExtractor(
        [r"Total\s*[:]??\s*R\$\s*([\d\.,]+)", r"Valor\s*[:]??\s*R\$\s*([\d\.,]+)"],
        wrap_prefix="R$ ",
    )

    text = "Total: R$ 123,45\nValor: R$ 999,99"

    assert extractor.extract(text) == "R$ 123,45"


def test_arrecadacao_extractor_prioritizes_48_digit_format() -> None:
    text = "846700000017 435001082021 601102511002 000420938712"

    value = ArrecadacaoBarcodeExtractor().extract(text)

    assert value == "846700000017 435001082021 601102511002 000420938712"


def test_composite_barcode_falls_back_to_boleto_47() -> None:
    text = "Linha digitavel 23793381286008320607782005000106389870000020999"

    value = CompositeBarcodeExtractor(
        [ArrecadacaoBarcodeExtractor(), Boleto47BarcodeExtractor()]
    ).extract(text)

    assert value == "23793381286008320607782005000106389870000020999"


def test_extrair_destinatario_from_text() -> None:
    texto = "\nEMPRESA CLIENTE XYZ LTDA CPF/CNPJ : 12.345.678/0001-90"

    assert extrair_destinatario(texto) == "EMPRESA CLIENTE XYZ LTDA"


def test_extrair_emissor_prioritizes_pix_payload() -> None:
    texto = "Documento VIVO"
    pix = "0002010102125907VIVO SA6009SAO PAULO"

    assert extrair_emissor(texto, pix) == "VIVO SA"


def test_output_path_for_uses_pdf_stem(tmp_path: Path) -> None:
    pdf = tmp_path / "fatura.pdf"

    assert output_path_for(pdf) == (tmp_path / "fatura_dados.json").resolve()


def test_salvar_resultado_json_creates_parent_and_writes_file(tmp_path: Path) -> None:
    output = tmp_path / "subdir" / "resultado.json"
    payload = {"valor": "R$ 10,00", "ok": True}

    saved = salvar_resultado_json(payload, output)

    assert saved == output.resolve()
    assert output.exists()
    assert '"valor": "R$ 10,00"' in output.read_text(encoding="utf-8")


def test_extrair_dados_fatura_usa_texto_qr_e_regex(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "fatura.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    texto = "\n".join(
        [
            "TELEFONICA BRASIL S.A.",
            "No da Conta: 12345678",
            "Data de emissao: 01/02/2026",
            "Vencimento: 10/02/2026",
            "Total a Pagar: R$ 199,90",
            "846700000017 435001082021 601102511002 000420938712",
            "CLIENTE FULANO LTDA CPF/CNPJ : 12.345.678/0001-90",
        ]
    )

    monkeypatch.setattr("vivo_fatura_extrator.ler_texto_pdf", lambda _pdf: texto)
    monkeypatch.setattr(
        "vivo_fatura_extrator.PixQrExtractor.extract",
        lambda self, _pdf: "0002010102125907VIVO SA6009SAO PAULO",
    )

    resultado = extrair_dados_fatura(pdf, verbose=False)

    assert resultado["arquivo_pdf"] == str(pdf.resolve())
    assert resultado["identificador_fatura"] == "12345678"
    assert resultado["data_emissao"] == "01/02/2026"
    assert resultado["data_vencimento"] == "10/02/2026"
    assert resultado["valor"] == "R$ 199,90"
    assert (
        resultado["codigo_barras_digitavel"]
        == "846700000017 435001082021 601102511002 000420938712"
    )
    assert (
        resultado["codigo_barras_digitavel_sem_espaco"]
        == "846700000017435001082021601102511002000420938712"
    )
    assert resultado["metodo_extracao"] == {"texto_pdf": True, "qr_decode": True}
