"""Task 1 - Document processing.

PDF   -> digital text (PyMuPDF, rebuilt row-by-row so tables stay aligned);
         pages with no text layer are rendered and OCR'd.
Image -> preprocessing + Tesseract OCR (multi-frame TIFF supported).
Sheet -> every sheet of XLSX/XLS, or CSV with encoding/delimiter sniffing.
"""
import csv
import logging
import os
from dataclasses import dataclass, field

import pandas as pd
from PIL import Image, ImageFilter, ImageOps, ImageSequence

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 120_000_000  # decompression-bomb guard

ALLOWED_EXTENSIONS = {
    ".pdf": "pdf", ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".tif": "image", ".tiff": "image",
    ".xlsx": "spreadsheet", ".xls": "spreadsheet", ".csv": "spreadsheet",
}
MIN_DIGITAL_CHARS = 25       # below this a PDF page is treated as scanned
SHEET_CHUNK_ROWS = 100       # rows per searchable text chunk
MAX_TIFF_FRAMES = 200


class ProcessingError(Exception):
    """Expected, user-explainable failure (document marked failed)."""


class CorruptedFileError(ProcessingError):
    pass


class OCRUnavailableError(ProcessingError):
    pass


@dataclass
class PageText:
    page_no: int
    text: str
    source: str                      # digital | ocr | sheet | error
    confidence: float | None = None  # 0..1 (OCR only)
    label: str | None = None


@dataclass
class ParsedDocument:
    file_type: str
    pages: list = field(default_factory=list)
    sheets: dict = field(default_factory=dict)   # name -> raw DataFrame (header=None)
    warnings: list = field(default_factory=list)


# ----------------------------------------------------------------------------- sniffing
def sniff_ok(path, ext):
    """Check magic bytes so a renamed .exe cannot masquerade as a PDF."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if not head:
        return False
    if ext == ".pdf":
        return b"%PDF" in head[:1024]
    if ext in (".jpg", ".jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in (".tif", ".tiff"):
        return head[:4] in (b"II*\x00", b"MM\x00*")
    if ext == ".xlsx":
        return head[:4] == b"PK\x03\x04"
    if ext == ".xls":
        return head[:4] == b"\xd0\xcf\x11\xe0" or head.lstrip()[:5].lower() in (b"<html", b"<?xml")
    if ext == ".csv":
        return b"\x00" not in head
    return False


# ----------------------------------------------------------------------------- OCR
def _tesseract(cmd=None):
    try:
        import pytesseract
    except ImportError as e:  # pragma: no cover
        raise OCRUnavailableError("pytesseract is not installed") from e
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        pytesseract.get_tesseract_version()
    except Exception as e:
        raise OCRUnavailableError(
            "Tesseract OCR engine not found. Install it and/or set TESSERACT_CMD.") from e
    return pytesseract


def preprocess_image(img):
    """Rotate by EXIF, grayscale, upscale small scans, stretch contrast, denoise."""
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    longest = max(img.size)
    if longest < 1600:
        scale = 1600 / longest
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    return img.filter(ImageFilter.MedianFilter(3))


def ocr_image(img, cfg):
    """Return (text, mean_confidence_0_to_1)."""
    tess = _tesseract(cfg.get("TESSERACT_CMD"))
    img = preprocess_image(img)
    config = f"--oem 3 --psm {cfg.get('OCR_PSM', 6)}"
    data = tess.image_to_data(img, lang=cfg.get("OCR_LANG", "eng"), config=config,
                              output_type=tess.Output.DICT)
    lines, confs = {}, []
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        confs.append(conf)
    text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
    return text, (sum(confs) / len(confs) / 100.0 if confs else 0.0)


# ----------------------------------------------------------------------------- PDF
def _page_rows(page):
    """Rebuild text row-by-row from word boxes so table cells stay on one line."""
    words = page.get_text("words")
    if not words:
        return ""
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows, cur, cur_y, cur_h = [], [], None, 0
    for w in words:
        yc, h = (w[1] + w[3]) / 2, max(w[3] - w[1], 1)
        if cur and abs(yc - cur_y) > 0.6 * max(cur_h, h):
            rows.append(cur)
            cur = []
        if not cur:
            cur_y, cur_h = yc, h
        cur.append(w)
    if cur:
        rows.append(cur)
    out = []
    for row in rows:
        row.sort(key=lambda w: w[0])
        parts, prev_x1 = [], None
        for w in row:
            if prev_x1 is not None:
                parts.append("   " if w[0] - prev_x1 > 14 else " ")
            parts.append(w[4])
            prev_x1 = w[2]
        out.append("".join(parts))
    return "\n".join(out)


def parse_pdf(path, cfg):
    try:
        try:
            import pymupdf as fitz  # PyMuPDF >= 1.24.3
        except ImportError:
            import fitz
    except ImportError as e:  # pragma: no cover
        raise ProcessingError("PyMuPDF is not installed") from e
    try:
        doc = fitz.open(path)
    except Exception as e:
        log.info("pdf open failed: %s", e)
        raise CorruptedFileError("PDF could not be opened: the file is corrupted or not a valid PDF") from e
    try:
        if doc.needs_pass:
            raise ProcessingError("PDF is password-protected")
        if doc.page_count == 0:
            raise CorruptedFileError("PDF has no pages")
        parsed = ParsedDocument("pdf")
        dpi = int(cfg.get("OCR_DPI", 200))
        bad_pages = 0
        for i in range(doc.page_count):
            try:
                page = doc.load_page(i)
                text = _page_rows(page) or page.get_text("text") or ""
                if len(text.strip()) >= MIN_DIGITAL_CHARS:
                    parsed.pages.append(PageText(i + 1, text, "digital", 1.0))
                    continue
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                text, conf = ocr_image(img, cfg)
                parsed.pages.append(PageText(i + 1, text, "ocr", conf))
                if conf < 0.5 and text.strip():
                    parsed.warnings.append(f"page {i + 1}: low OCR confidence ({conf:.0%})")
            except OCRUnavailableError:
                raise
            except Exception as e:  # one bad page must not kill a 1000-page file
                bad_pages += 1
                parsed.warnings.append(f"page {i + 1}: could not be read ({e})")
                parsed.pages.append(PageText(i + 1, "", "error", 0.0))
        if bad_pages == doc.page_count:
            raise CorruptedFileError("No page of the PDF could be read")
        if not any(p.text.strip() for p in parsed.pages):
            raise ProcessingError("No text could be extracted from the PDF")
        return parsed
    finally:
        doc.close()


# ----------------------------------------------------------------------------- images
def parse_image(path, cfg):
    try:
        with Image.open(path) as im:
            im.verify()
    except Exception as e:
        log.info("image verify failed: %s", e)
        raise CorruptedFileError("Image is corrupted or unreadable") from e
    parsed = ParsedDocument("image")
    try:
        with Image.open(path) as im:
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                if i >= MAX_TIFF_FRAMES:
                    parsed.warnings.append(f"only the first {MAX_TIFF_FRAMES} frames were read")
                    break
                text, conf = ocr_image(frame.copy(), cfg)
                parsed.pages.append(PageText(i + 1, text, "ocr", conf))
                if conf < 0.5:
                    parsed.warnings.append(f"frame {i + 1}: poor image quality (OCR {conf:.0%})")
    except OCRUnavailableError:
        raise
    except Exception as e:
        raise CorruptedFileError(f"Image could not be decoded: {e}") from e
    if not any(p.text.strip() for p in parsed.pages):
        raise ProcessingError("OCR found no text in the image (blank or too low quality)")
    return parsed


# ----------------------------------------------------------------------------- spreadsheets
def _read_csv(path):
    last = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        for sep in (None, ","):
            try:
                return pd.read_csv(path, sep=sep, engine="python", header=None, dtype=object,
                                   encoding=enc, on_bad_lines="skip", skip_blank_lines=True)
            except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError,
                    csv.Error) as e:
                last = e
    raise CorruptedFileError(f"CSV could not be parsed: {last}")


def _sheet_text(name, df):
    """Cut a sheet into searchable chunks of SHEET_CHUNK_ROWS rows."""
    rows = []
    for r in df.itertuples(index=False):
        cells = ["" if pd.isna(c) else str(c).strip() for c in r]
        if any(cells):
            rows.append(" | ".join(cells))
    return [(name, "\n".join(rows[i:i + SHEET_CHUNK_ROWS]))
            for i in range(0, len(rows), SHEET_CHUNK_ROWS)]


def parse_spreadsheet(path, ext, cfg=None):
    parsed = ParsedDocument("spreadsheet")
    try:
        if ext == ".csv":
            sheets = {"csv": _read_csv(path)}
        else:
            engine = "openpyxl" if ext == ".xlsx" else "xlrd"
            try:
                sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object,
                                       engine=engine)
            except ImportError as e:
                raise ProcessingError(f"Missing spreadsheet engine '{engine}': {e}") from e
    except ProcessingError:
        raise
    except Exception as e:
        log.info("spreadsheet read failed: %s", e)
        raise CorruptedFileError("Spreadsheet could not be read: the file is corrupted or protected") from e

    page_no = 0
    for name, df in sheets.items():
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            parsed.warnings.append(f"sheet '{name}' is empty and was skipped")
            continue
        parsed.sheets[str(name)] = df.reset_index(drop=True)
        for label, text in _sheet_text(str(name), df):
            page_no += 1
            parsed.pages.append(PageText(page_no, text, "sheet", 1.0, label))
    if not parsed.sheets:
        raise ProcessingError("Spreadsheet contains no data")
    return parsed


# ----------------------------------------------------------------------------- entry point
def parse_file(path, ext, cfg):
    kind = ALLOWED_EXTENSIONS.get(ext)
    if kind is None:
        raise ProcessingError(f"Unsupported file type '{ext}'")
    if not os.path.exists(path):
        raise ProcessingError("Stored file is missing from disk")
    if kind == "pdf":
        return parse_pdf(path, cfg)
    if kind == "image":
        return parse_image(path, cfg)
    return parse_spreadsheet(path, ext, cfg)
