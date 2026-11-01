import shutil

import pandas as pd
import pytest

import processing as P
from tests import helpers as H

CFG = {"OCR_DPI": 200, "OCR_LANG": "eng", "OCR_PSM": 6}
needs_ocr = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def test_digital_pdf_text_and_table_rows(tmp_path):
    parsed = P.parse_pdf(H.make_pdf(tmp_path / "a.pdf"), CFG)
    assert parsed.pages[0].source == "digital"
    text = parsed.pages[0].text
    assert "INV-2024-0042" in text
    assert any("Steel bolts M8" in ln and "45.00" in ln for ln in text.splitlines())  # row kept together


def test_multipage_pdf(tmp_path):
    pages = [H.invoice_lines() for _ in range(5)]
    parsed = P.parse_pdf(H.make_pdf(tmp_path / "m.pdf", pages=pages), CFG)
    assert [p.page_no for p in parsed.pages] == [1, 2, 3, 4, 5]


@needs_ocr
def test_scanned_pdf_uses_ocr(tmp_path):
    parsed = P.parse_pdf(H.make_scanned_pdf(str(tmp_path / "s.pdf")), CFG)
    assert parsed.pages[0].source == "ocr"
    assert "INV-2024-0042" in parsed.pages[0].text.replace(" ", "")
    assert parsed.pages[0].confidence > 0.6


@needs_ocr
def test_image_ocr(tmp_path):
    parsed = P.parse_image(H.make_image(str(tmp_path / "i.png")), CFG)
    assert "209.00" in parsed.pages[0].text


@needs_ocr
def test_noisy_image_still_readable_or_flagged(tmp_path):
    parsed = P.parse_image(H.make_image(str(tmp_path / "n.png"), noise=True), CFG)
    assert parsed.pages[0].text.strip()
    assert 0 <= parsed.pages[0].confidence <= 1


def test_corrupted_pdf(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\n garbage garbage \x00\x01")
    with pytest.raises(P.ProcessingError):
        P.parse_pdf(str(bad), CFG)


def test_corrupted_image(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"nonsense")
    with pytest.raises(P.CorruptedFileError):
        P.parse_image(str(bad), CFG)


def test_ocr_missing_engine_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_tesseract", lambda cmd=None: (_ for _ in ()).throw(P.OCRUnavailableError("x")))
    with pytest.raises(P.OCRUnavailableError):
        P.parse_image(H.make_image(str(tmp_path / "i.png")), CFG)


def test_xlsx_multiple_sheets(tmp_path):
    parsed = P.parse_spreadsheet(H.make_xlsx(str(tmp_path / "x.xlsx")), ".xlsx")
    assert set(parsed.sheets) == {"Invoices", "Notes"}
    assert any(p.label == "Invoices" for p in parsed.pages)


def test_xlsx_empty_sheet_skipped(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["a", "b"])
    wb.create_sheet("Empty")
    wb.save(tmp_path / "e.xlsx")
    parsed = P.parse_spreadsheet(str(tmp_path / "e.xlsx"), ".xlsx")
    assert "Empty" not in parsed.sheets and any("empty" in w for w in parsed.warnings)


@pytest.mark.parametrize("delim", [",", ";", "\t"])
def test_csv_delimiters(tmp_path, delim):
    parsed = P.parse_spreadsheet(H.make_csv(str(tmp_path / "c.csv"), delimiter=delim), ".csv")
    assert parsed.sheets["csv"].shape[1] == len(H.SHEET_HEADER)


def test_csv_latin1_encoding(tmp_path):
    p = tmp_path / "l.csv"
    p.write_bytes("Invoice Number,Supplier\nA1,Café Müller\n".encode("latin-1"))
    parsed = P.parse_spreadsheet(str(p), ".csv")
    assert "Müller" in parsed.pages[0].text


def test_corrupted_xlsx(tmp_path):
    p = tmp_path / "bad.xlsx"
    p.write_bytes(b"PK\x03\x04 definitely not a workbook")
    with pytest.raises(P.CorruptedFileError):
        P.parse_spreadsheet(str(p), ".xlsx")


def test_magic_byte_sniffing(tmp_path):
    exe = tmp_path / "evil.pdf"
    exe.write_bytes(b"MZ\x90\x00 not a pdf")
    assert not P.sniff_ok(str(exe), ".pdf")
    assert P.sniff_ok(H.make_pdf(tmp_path / "ok.pdf"), ".pdf")


def test_unsupported_extension(tmp_path):
    with pytest.raises(P.ProcessingError):
        P.parse_file(str(tmp_path / "x.docx"), ".docx", CFG)
