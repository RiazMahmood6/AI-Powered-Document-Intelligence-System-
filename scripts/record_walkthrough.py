"""Drive the real UI with Playwright and record a captioned walkthrough video.

  python app.py &                                   # server on :5000 with an EMPTY database
  pip install playwright && playwright install chromium
  python scripts/record_walkthrough.py --base http://127.0.0.1:5000 --out walkthrough.webm
"""
import argparse
import glob
import os
import shutil
import tempfile

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTION_JS = """(t) => { let d = document.getElementById('cap');
  if (!d) { d = document.createElement('div'); d.id = 'cap';
    d.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);max-width:80vw;background:#111c;color:#fff;padding:10px 18px;border-radius:8px;font:600 17px system-ui;z-index:99999;text-align:center;pointer-events:none';
    document.body.append(d); } d.textContent = t; d.style.display = t ? 'block' : 'none'; }"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5000")
    ap.add_argument("--out", default="walkthrough.webm")
    a = ap.parse_args()
    S = lambda n: os.path.join(ROOT, "samples", n)
    vid_dir = tempfile.mkdtemp()
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1366, "height": 820}, record_video_dir=vid_dir,
                            record_video_size={"width": 1366, "height": 820})
        pg = ctx.new_page()
        cap = lambda t, ms=2600: (pg.evaluate(CAPTION_JS, t), pg.wait_for_timeout(ms))

        pg.goto(a.base); pg.wait_for_selector(".drop")
        cap("Document Desk: upload invoices, receipts, scans and spreadsheets in one go")
        cap("Uploading 7 files: digital PDF, scanned PDF, photo, Excel, CSV, a duplicate and a corrupted file", 800)
        pg.set_input_files("input[type=file]", [S(n) for n in sorted(os.listdir(os.path.join(ROOT, "samples"))) if not n.endswith(".png.tmp")])
        pg.wait_for_timeout(1200)
        cap("Jobs run in the background; progress is shown per document", 2200)
        for _ in range(180):                                   # poll (the app's CSP forbids string-eval waits)
            if pg.locator(".queue .pill").count() >= 7 and pg.locator(".bar").count() == 0:
                break
            pg.wait_for_timeout(500)
        cap("Done. Statuses: Done, Needs review and Failed (the corrupted file)", 3200)

        pg.locator(".queue button.row", has_text="1_acme").click(); pg.wait_for_selector(".invoice")
        cap("A clean digital PDF: every field extracted, line items checked, each with a confidence score", 3200)
        pg.locator("input[id$='-total']").first.focus()
        cap("Focusing a field highlights where its value came from in the source text", 3200)

        pg.locator(".queue button.row", has_text="2_zenith").click(); pg.wait_for_selector(".invoice")
        cap("A scanned PDF read with OCR; OCR confidence is shown beside the source text", 3200)

        pg.locator(".queue button.row", has_text="3_acme").click(); pg.wait_for_selector(".invoice")
        cap("An Excel export where subtotal + tax does not equal the total: flagged for manual review", 3600)
        pg.click("#t-cross")
        pg.select_option("#x-a", label=pg.locator("#x-a option", has_text="1_acme").first.inner_text())
        pg.select_option("#x-b", label=pg.locator("#x-b option", has_text="3_acme").first.inner_text())
        pg.click("#x-go"); pg.wait_for_selector(".sum")
        cap("PDF-to-Excel cross-check: same invoice number, the fields that disagree are listed side by side", 4200)
        pg.click("#t-review"); pg.wait_for_selector(".invoice")
        t = pg.locator("input[id$='-total']").first; t.fill("")
        cap("Review: the error is shown under the exact field. Correct the total and save…", 1600)
        t.fill("209.00")
        pg.locator("button:has-text('Save changes')").first.click(); pg.wait_for_timeout(1800)
        cap("Re-validated automatically: the invoice now passes", 2800)

        pg.locator(".queue button.row", has_text="7_acme").click(); pg.wait_for_selector(".invoice")
        cap("Re-sent copy of an invoice already processed: caught as a duplicate", 3200)

        pg.locator(".queue button.row", has_text="6_corrupted").click(); pg.wait_for_selector(".attention")
        cap("The corrupted file failed cleanly with a clear reason, and can be reprocessed later", 3200)

        pg.click("#t-search"); pg.fill("input[type=search]", "titanium rivets"); pg.click("form button[type=submit]")
        pg.wait_for_selector(".hit"); cap("Search: keyword + semantic, with filters for supplier, date and total", 3200)
        pg.fill("input[id=s-supplier]", "zenith"); pg.click("form button[type=submit]"); pg.wait_for_timeout(1500)
        cap("Filters narrow results by supplier, dates, totals, currency or review status", 2800)

        cap("Thanks for watching", 1500)
        ctx.close(); b.close()
    vid = glob.glob(os.path.join(vid_dir, "*.webm"))[0]
    shutil.move(vid, a.out)
    print("saved", a.out)


if __name__ == "__main__":
    main()
