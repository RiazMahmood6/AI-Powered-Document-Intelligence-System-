"""Builders for realistic test documents (digital PDF, scanned PDF, image, XLSX, CSV)."""
import io

try:
    import pymupdf as fitz
except ImportError:
    import fitz
import openpyxl
from PIL import Image, ImageDraw, ImageFont

SAMPLE = {
    "invoice_number": "INV-2024-0042", "invoice_date": "2024-03-15",
    "supplier": "Acme Industrial Supplies Ltd", "customer": "Globex Corporation",
    "currency": "USD",
    "items": [("Steel bolts M8", 100, 0.45, 45.00), ("Hex nuts M8", 100, 0.20, 20.00),
              ("Safety gloves", 10, 12.50, 125.00)],
    "subtotal": 190.00, "tax": 19.00, "total": 209.00,
}


def invoice_lines(d=SAMPLE, number_label="Invoice No:", tax_label="Tax (10%)"):
    rows = [(d["supplier"],), ("INVOICE",), (number_label, d["invoice_number"]),
            ("Invoice Date:", d["invoice_date"]), ("Bill To:", d["customer"]), (),
            ("Description", "Qty", "Unit Price", "Amount")]
    for desc, q, p, a in d["items"]:
        rows.append((desc, str(q), f"{p:.2f}", f"{a:.2f}"))
    rows += [(), ("Subtotal", f"{d['subtotal']:.2f}"), (tax_label, f"{d['tax']:.2f}"),
             ("Total", f"{d['total']:.2f} {d['currency']}")]
    return rows


COLS = [50, 300, 380, 470]


def draw_rows(draw_text, rows, y0=60, step=22):
    y = y0
    for row in rows:
        if row:
            xs = COLS if len(row) == 4 else ([50, 190] if len(row) == 2 else [50])
            if len(row) == 2 and row[0] in ("Subtotal", "Total") or (len(row) == 2 and row[0].startswith("Tax")):
                xs = [380, 470]
            for x, cell in zip(xs, row):
                draw_text((x, y), cell)
        y += step


def make_pdf(path, pages=None, rows=None):
    """Digital PDF. `pages` = list of row-lists (one per page)."""
    pages = pages or [rows or invoice_lines()]
    doc = fitz.open()
    for rws in pages:
        page = doc.new_page()
        draw_rows(lambda xy, t: page.insert_text(xy, t, fontsize=11), rws)
    doc.save(path)
    doc.close()
    return path


def make_image(path, rows=None, size=(900, 700), noise=False):
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    draw_rows(lambda xy, t: d.text((xy[0] * 1.5, xy[1]), t, fill="black", font=font),
              rows or invoice_lines(), y0=40, step=34)
    if noise:
        import random
        random.seed(1)
        px = img.load()
        for _ in range(size[0] * size[1] // 60):
            x, y = random.randrange(size[0]), random.randrange(size[1])
            px[x, y] = (random.randrange(256),) * 3
    img.save(path)
    return path


def make_scanned_pdf(path, rows=None):
    """PDF whose only content is a raster image -> forces the OCR branch."""
    img_path = path + ".png"
    make_image(img_path, rows, size=(1240, 900))
    doc = fitz.open()
    page = doc.new_page(width=620, height=450)
    page.insert_image(page.rect, filename=img_path)
    doc.save(path)
    doc.close()
    import os
    os.remove(img_path)
    return path


SHEET_HEADER = ["Invoice Number", "Invoice Date", "Supplier", "Customer", "Description",
                "Qty", "Unit Price", "Line Total", "Subtotal", "Tax", "Total", "Currency"]


def sheet_rows(d=SAMPLE):
    rows = []
    for n, (desc, q, p, a) in enumerate(d["items"]):
        rows.append([d["invoice_number"], d["invoice_date"], d["supplier"], d["customer"],
                     desc, q, p, a, d["subtotal"], d["tax"], d["total"], d["currency"]]
                    if n == 0 else [None, None, None, None, desc, q, p, a, None, None, None, None])
    return rows


def make_xlsx(path, rows=None, extra_sheet=True):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices"
    ws.append(["ACME monthly export"])          # junk title row above the header
    ws.append([])
    ws.append(SHEET_HEADER)
    for r in rows or sheet_rows():
        ws.append(r)
    if extra_sheet:
        ws2 = wb.create_sheet("Notes")
        ws2.append(["nothing", "to see"])
    wb.save(path)
    return path


def make_csv(path, rows=None, delimiter=","):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(SHEET_HEADER)
        w.writerows(rows or sheet_rows())
    return path
