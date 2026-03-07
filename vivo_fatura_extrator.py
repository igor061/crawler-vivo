#!/usr/bin/env python3
# Requisitos (runtime):
# - pypdf
# Requisitos opcionais para PIX via QR:
# - numpy
# - opencv-python-headless
# - pymupdf

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def log_event(level: str, message: str, **fields: Any) -> None:
    prefix_map = {
        "info": "[info]",
        "ok": "[ok]",
        "warn": "[warn]",
        "erro": "[erro]",
    }
    prefix = prefix_map.get(level, "[info]")
    if not fields:
        print(f"{prefix} {message}")
        return
    ordered = sorted(fields)
    details = " ".join(f"{k}={fields[k]}" for k in ordered)
    print(f"{prefix} {message} {details}")


@dataclass
class MetodoExtracao:
    texto_pdf: bool
    qr_decode: bool


@dataclass
class FaturaExtraida:
    arquivo_pdf: str
    pix_copia_cola: str
    codigo_barras_digitavel: str
    codigo_barras_digitavel_sem_espaco: str
    emissor: str
    destinatario: str
    identificador_fatura: str
    telefone: str
    numeros_vivo: list
    data_emissao: str
    data_vencimento: str
    valor: str
    metodo_extracao: MetodoExtracao

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["metodo_extracao"] = asdict(self.metodo_extracao)
        return data


class RegexChainExtractor:
    def __init__(self, patterns: list[str], wrap_prefix: str = "") -> None:
        self.patterns = patterns
        self.wrap_prefix = wrap_prefix

    def extract(self, text: str) -> str:
        for pattern in self.patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.group(1).strip()
            return f"{self.wrap_prefix}{value}" if self.wrap_prefix else value
        return ""


class BarcodeExtractor:
    def extract(self, text: str) -> str:
        raise NotImplementedError


class ArrecadacaoBarcodeExtractor(BarcodeExtractor):
    def extract(self, text: str) -> str:
        grouped_12 = re.compile(r"(?<!\d)(\d{12})\s+(\d{12})\s+(\d{12})\s+(\d{12})(?!\d)")
        for a, b, c, d in grouped_12.findall(text):
            full = a + b + c + d
            if full.startswith("8"):
                return f"{a} {b} {c} {d}"

        raw_48 = re.compile(r"(?<!\d)(\d{48})(?!\d)")
        for full in raw_48.findall(text):
            if full.startswith("8"):
                return f"{full[:12]} {full[12:24]} {full[24:36]} {full[36:]}"
        return ""


class Boleto47BarcodeExtractor(BarcodeExtractor):
    def extract(self, text: str) -> str:
        raw_47 = re.compile(r"(?<!\d)(\d{47})(?!\d)")
        match = raw_47.search(text)
        return match.group(1) if match else ""


class CompositeBarcodeExtractor(BarcodeExtractor):
    def __init__(self, strategies: list[BarcodeExtractor]) -> None:
        self.strategies = strategies

    def extract(self, text: str) -> str:
        for strategy in self.strategies:
            value = strategy.extract(text)
            if value:
                return value
        return ""


class PixQrExtractor:
    def extract(self, pdf_path: Path) -> str:
        try:
            import importlib

            cv2 = importlib.import_module("cv2")
            fitz = importlib.import_module("fitz")
            np = importlib.import_module("numpy")
        except Exception:
            return ""

        detector = cv2.QRCodeDetector()
        try:
            doc = fitz.open(pdf_path)
        except Exception:
            return ""

        for page in doc:
            for zoom in (2, 3, 4):
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                img = np.frombuffer(pix.samples, dtype=np.uint8)
                img = img.reshape(pix.height, pix.width, pix.n)
                if pix.n == 4:
                    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

                data, _pts, _ = detector.detectAndDecode(img)
                if data and data.startswith("000201"):
                    return data

                ok, infos, _pts2, _ = detector.detectAndDecodeMulti(img)
                if ok and infos:
                    for item in infos:
                        if item and item.startswith("000201"):
                            return item
        return ""


def ler_texto_pdf(pdf_path: Path) -> str:
    import importlib

    pypdf_module = importlib.import_module("pypdf")
    pdf_reader_cls = pypdf_module.PdfReader
    reader = pdf_reader_cls(str(pdf_path))
    paginas = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(paginas)


def extrair_telefone(texto: str) -> str:
    """Extrai o número da linha/telefone ao qual a fatura se refere.

    Prioriza números próximos a rótulos como 'Linha', 'Telefone', 'No da Linha'.
    Normaliza para o formato (XX) XXXXX-XXXX ou (XX) XXXX-XXXX.
    """
    # Padrões com rótulo explícito
    padroes = [
        # "No da Linha: (11) 99999-9999" / "Linha: 11 9999-9999"
        r"(?:N[oº°]\.?\s*da\s*[Ll]inha|[Ll]inha|[Tt]elefone|N[uú]mero\s*da\s*[Ll]inha)"
        r"\s*[:\-]?\s*\(?\s*(\d{2})\s*\)?\s*(\d{4,5})[\s\-\.](\d{4})",
        # Número em destaque no cabeçalho, ex: "55 11 99999-9999"
        r"(?:^|\s)55\s*(\d{2})\s*(\d{4,5})[\s\-\.](\d{4})(?:\s|$)",
    ]
    for pattern in padroes:
        m = re.search(pattern, texto, re.IGNORECASE | re.MULTILINE)
        if m:
            ddd, parte1, parte2 = m.group(1), m.group(2), m.group(3)
            return f"{ddd}-{parte1}-{parte2}"
    # Fallback: primeiro número da seção "Número Vivo"
    numeros = extrair_numeros_vivo(texto)
    return numeros[0] if numeros else ""


def _normalizar_numero(ddd: str, parte1: str, parte2: str) -> str:
    return f"{ddd}-{parte1}-{parte2}"


def extrair_numeros_vivo(texto: str) -> list[str]:
    """Extrai todos os números listados na seção 'Número Vivo' da fatura.

    Cobre o bloco 'VEJA OS NÚMEROS VIVO E PLANOS QUE COMPÕEM A SUA CONTA'
    onde os números aparecem no formato XX-XXXXX-XXXX ou (XX) XXXXX-XXXX.
    """
    numeros: list[str] = []
    vistos: set[str] = set()

    # Localiza a seção e extrai números no bloco dela
    secao = re.search(
        r"N[ÚU]MEROS?\s+(?:VIVO\s+)?E\s+PLANOS[^\n]*\n([\s\S]{0,3000}?)(?:\n\s*VEJA|\n\s*Total\s+N[úu]meros|$)",
        texto,
        re.IGNORECASE,
    )
    bloco = secao.group(1) if secao else texto

    # Padrão 1: XX-XXXXX-XXXX ou XX-XXXX-XXXX (formato do PDF)
    for m in re.finditer(r"(?<!\d)(\d{2})-(\d{4,5})-(\d{4})(?!\d)", bloco):
        num = _normalizar_numero(m.group(1), m.group(2), m.group(3))
        if num not in vistos:
            vistos.add(num)
            numeros.append(num)

    # Padrão 2: (XX) XXXXX-XXXX
    for m in re.finditer(r"\((\d{2})\)\s*(\d{4,5})-(\d{4})", bloco):
        num = _normalizar_numero(m.group(1), m.group(2), m.group(3))
        if num not in vistos:
            vistos.add(num)
            numeros.append(num)

    return numeros


def extrair_destinatario(texto: str) -> str:
    match = re.search(
        r"\n([A-Z][A-Z\s\-\.]{3,})\s+CPF/CNPJ\s*:\s*[0-9./-]{11,18}",
        texto,
    )
    if not match:
        return ""
    return " ".join(match.group(1).split())


def extrair_emissor(texto: str, pix: str) -> str:
    match_pix = re.search(r"59\d{2}([A-Z\s]{4,30})", (pix or "").upper())
    if match_pix:
        nome = " ".join(match_pix.group(1).split())
        if nome:
            return nome

    match_text = re.search(r"(TELEFONICA\s+BRASIL|VIVO)", texto.upper())
    return match_text.group(1) if match_text else ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extrai dados relevantes de uma fatura PDF")
    parser.add_argument("pdf", help="Caminho do PDF da fatura")
    parser.add_argument(
        "--salvar",
        action="store_true",
        help="Salva resultado em <pdf>_dados.json no mesmo diretorio",
    )
    return parser.parse_args()


def output_path_for(pdf_path: Path) -> Path:
    return (pdf_path.parent / f"{pdf_path.stem}_dados.json").resolve()


def extrair_dados_fatura(pdf_path: str | Path, verbose: bool = True) -> dict[str, Any]:
    pdf_resolvido = Path(pdf_path).expanduser().resolve()
    if not pdf_resolvido.exists():
        raise FileNotFoundError(f"PDF nao encontrado: {pdf_resolvido}")

    if verbose:
        log_event("info", "Lendo PDF", arquivo=pdf_resolvido)

    texto = ler_texto_pdf(pdf_resolvido)
    pix = PixQrExtractor().extract(pdf_resolvido)
    codigo = CompositeBarcodeExtractor(
        [ArrecadacaoBarcodeExtractor(), Boleto47BarcodeExtractor()]
    ).extract(texto)

    identificador_extractor = RegexChainExtractor(
        [
            r"N[ºo]\s*da\s*Conta\s*[:]?\s*(\d{8,})",
            r"N[uú]mero\s*da\s*Conta\s*[:]?\s*(\d{8,})",
        ]
    )
    emissao_extractor = RegexChainExtractor(
        [
            r"Data\s*de\s*emiss[aã]o\s*[:]?\s*(\d{2}/\d{2}/\d{4})",
            r"Emiss[aã]o\s*[:]?\s*(\d{2}/\d{2}/\d{4})",
            r"Data\s*de\s*gera[cç][aã]o\s*[:]?\s*(\d{2}/\d{2}/\d{4})",
        ]
    )
    vencimento_extractor = RegexChainExtractor(
        [
            r"Vencimento\s*[:]?\s*(\d{2}/\d{2}/\d{4})",
            r"Vence\s*em\s*(\d{2}/\d{2}/\d{4})",
        ]
    )
    valor_extractor = RegexChainExtractor(
        [
            r"Total\s*a\s*Pagar\s*[:]?\s*R\$\s*([\d\.,]+)",
            r"Total\s*a\s*Pagar\s*-\s*R\$\s*\d{2}/\d{2}/\d{4}\s*([\d\.,]+)",
            r"Vencimento\s*Total\s*a\s*Pagar\s*-\s*R\$\s*\d{2}/\d{2}/\d{4}\s*([\d\.,]+)",
            r"Valor\s*da\s*fatura\s*[:]?\s*R\$\s*([\d\.,]+)",
            r"Valor\s*do\s*documento\s*[:]?\s*R\$\s*([\d\.,]+)",
            r"Total\s*a\s*Pagar[\s\S]{0,120}?(\d{1,3}(?:\.\d{3})*,\d{2})",
        ],
        wrap_prefix="R$ ",
    )

    fatura = FaturaExtraida(
        arquivo_pdf=str(pdf_resolvido),
        pix_copia_cola=pix,
        codigo_barras_digitavel=codigo,
        codigo_barras_digitavel_sem_espaco=re.sub(r"\s+", "", codigo),
        emissor=extrair_emissor(texto, pix),
        destinatario=extrair_destinatario(texto),
        identificador_fatura=identificador_extractor.extract(texto),
        telefone=extrair_telefone(texto),
        numeros_vivo=extrair_numeros_vivo(texto),
        data_emissao=emissao_extractor.extract(texto),
        data_vencimento=vencimento_extractor.extract(texto),
        valor=valor_extractor.extract(texto),
        metodo_extracao=MetodoExtracao(
            texto_pdf=bool(texto.strip()),
            qr_decode=bool(pix),
        ),
    )
    return fatura.to_dict()


def salvar_resultado_json(resultado: dict[str, Any], output_path: str | Path) -> Path:
    destino = Path(output_path).expanduser().resolve()
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    return destino


def main() -> None:
    args = parse_args()
    resultado = extrair_dados_fatura(args.pdf, verbose=True)

    if args.salvar:
        pdf_path = Path(args.pdf).expanduser().resolve()
        output_path = output_path_for(pdf_path)
        destino = salvar_resultado_json(resultado, output_path)
        log_event("ok", "Extracao finalizada", output=destino)
    else:
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
        log_event("ok", "Extracao finalizada", output="stdout")

    log_event("ok", "Resumo", identificador=resultado["identificador_fatura"])
    log_event("ok", "Resumo", vencimento=resultado["data_vencimento"], valor=resultado["valor"])


if __name__ == "__main__":
    main()
