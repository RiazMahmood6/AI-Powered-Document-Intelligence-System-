import io
import os
import shutil

import pytest

from models import Document, Invoice, db
from tests import helpers as H

needs_ocr = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def doc_of(resp):
    assert resp.status_code == 202, resp.get_json()
    return resp.get_json()["documents"][0]


def full(client, doc_id):
    return client.get(f"/api/documents/{doc_id}").get_json()


# ------------------------------------------------------------------ happy paths
def test_health(client):
    assert client.get("/api/health").get_json()["status"] == "ok"


def test_upload_pdf_end_to_end(client, upload, tmp_path):
    d = doc_of(upload(H.make_pdf(tmp_path / "inv.pdf")))
    detail = full(client, d["id"])
    assert detail["status"] == "completed" and detail["progress"] == 100
    inv = detail["invoices"][0]
    assert inv["invoice_number"] == "INV-2024-0042" and inv["total"] == 209.0
    assert inv["issues"] == [] and inv["review_status"] == "ok"
    assert [e["stage"] for e in detail["events"]][:2] == ["upload", "start"]
    assert client.get(f"/api/documents/{d['id']}/status").get_json()["status"] == "completed"


def test_upload_multiple_files_with_one_rejected(client, tmp_path):
    a, b = H.make_pdf(tmp_path / "a.pdf"), H.make_csv(str(tmp_path / "b.csv"))
    data = {"files": [(open(a, "rb"), "a.pdf"), (open(b, "rb"), "b.csv"), (io.BytesIO(b"x"), "evil.exe")]}
    r = client.post("/api/documents", data=data, content_type="multipart/form-data")
    body = r.get_json()
    assert r.status_code == 202 and len(body["documents"]) == 2
    assert body["rejected"][0]["filename"] == "evil.exe"


def test_xlsx_and_csv_pipelines(client, upload, tmp_path):
    csv_rows = H.sheet_rows({**H.SAMPLE, "invoice_number": "INV-CSV-1"})
    for path in (H.make_xlsx(str(tmp_path / "s.xlsx")), H.make_csv(str(tmp_path / "s.csv"), csv_rows)):
        d = doc_of(upload(path))
        inv = full(client, d["id"])["invoices"][0]
        assert inv["extraction_method"] == "spreadsheet" and len(inv["line_items"]) == 3
        assert inv["total"] == 209.0 and inv["review_status"] == "ok"


def test_same_invoice_in_two_spreadsheets_is_flagged_duplicate(client, upload, tmp_path):
    upload(H.make_xlsx(str(tmp_path / "a.xlsx")))
    d = doc_of(upload(H.make_csv(str(tmp_path / "b.csv"))))
    codes = {i["code"] for i in full(client, d["id"])["invoices"][0]["issues"]}
    assert "DUPLICATE_INVOICE" in codes


@needs_ocr
def test_scanned_pdf_and_image_pipelines(client, upload, tmp_path):
    for path in (H.make_scanned_pdf(str(tmp_path / "scan.pdf")), H.make_image(str(tmp_path / "pic.png"))):
        d = doc_of(upload(path))
        detail = full(client, d["id"])
        assert detail["status"] in ("completed", "needs_review")
        assert detail["pages"][0]["source"] == "ocr"
        assert detail["invoices"][0]["total"] == 209.0


def test_multi_invoice_pdf_is_split(client, upload, tmp_path):
    second = {**H.SAMPLE, "invoice_number": "INV-2024-0099"}
    pdf = H.make_pdf(tmp_path / "multi.pdf", pages=[H.invoice_lines(), H.invoice_lines(second)])
    detail = full(client, doc_of(upload(pdf))["id"])
    assert [i["invoice_number"] for i in detail["invoices"]] == ["INV-2024-0042", "INV-2024-0099"]
    assert [i["source_pages"] for i in detail["invoices"]] == ["1", "2"]


# ------------------------------------------------------------------ validation through the pipeline
def test_arithmetic_error_flags_needs_review(client, upload, tmp_path):
    bad = {**H.SAMPLE, "total": 250.0}
    d = doc_of(upload(H.make_pdf(tmp_path / "bad.pdf", rows=H.invoice_lines(bad))))
    detail = full(client, d["id"])
    assert detail["status"] == "needs_review"
    assert "TOTAL_MISMATCH" in {i["code"] for i in detail["invoices"][0]["issues"]}


def test_duplicate_invoice_detected_across_files(client, upload, tmp_path):
    upload(H.make_pdf(tmp_path / "one.pdf"))
    # same invoice, different bytes (extra blank page) so the file hash differs
    d2 = doc_of(upload(H.make_pdf(tmp_path / "two.pdf", pages=[H.invoice_lines(), [("memo",)]])))
    issues = full(client, d2["id"])["invoices"][0]["issues"]
    assert "DUPLICATE_INVOICE" in {i["code"] for i in issues}


def test_identical_file_upload_is_short_circuited(client, upload, tmp_path):
    p = H.make_pdf(tmp_path / "x.pdf")
    first = doc_of(upload(p))
    again = doc_of(upload(p))
    assert again["status"] == "duplicate" and again["duplicate_of"] == first["id"]


# ------------------------------------------------------------------ failures + reprocess
def test_corrupted_pdf_fails_cleanly_and_can_be_reprocessed(client, upload, tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4\n this is not a real pdf \x00\x00")
    d = doc_of(upload(str(bad)))
    detail = full(client, d["id"])
    assert detail["status"] == "failed" and detail["error"] and detail["attempts"] == 1
    r = client.post(f"/api/documents/{d['id']}/reprocess")
    assert r.status_code == 202
    assert full(client, d["id"])["attempts"] == 2


def test_reprocess_succeeds_after_file_is_fixed(client, upload, tmp_path, app):
    bad = tmp_path / "later.pdf"
    bad.write_bytes(b"%PDF-1.4\n junk")
    d = doc_of(upload(str(bad)))
    assert full(client, d["id"])["status"] == "failed"
    with app.app_context():                       # simulate the file being replaced
        doc = db.session.get(Document, d["id"])
        shutil.copy(H.make_pdf(tmp_path / "good.pdf"), doc.stored_path)
    client.post(f"/api/documents/{d['id']}/reprocess")
    assert full(client, d["id"])["status"] == "completed"


def test_reprocess_rejects_non_failed_without_force(client, upload, tmp_path):
    d = doc_of(upload(H.make_pdf(tmp_path / "ok.pdf")))
    assert client.post(f"/api/documents/{d['id']}/reprocess").status_code == 409
    assert client.post(f"/api/documents/{d['id']}/reprocess?force=true").status_code == 202


def test_reprocess_all_failed(client, upload, tmp_path):
    for n in range(2):
        p = tmp_path / f"b{n}.pdf"
        p.write_bytes(b"%PDF-1.4 junk " + str(n).encode())
        upload(str(p))
    r = client.post("/api/documents/reprocess-failed")
    assert r.get_json()["requeued"] == 2


def test_missing_ocr_engine_marks_failed_with_clear_message(client, upload, tmp_path, monkeypatch):
    import processing
    from processing import OCRUnavailableError

    def boom(cmd=None):
        raise OCRUnavailableError("Tesseract OCR engine not found")
    monkeypatch.setattr(processing, "_tesseract", boom)
    d = doc_of(upload(H.make_image(str(tmp_path / "i.png"))))
    detail = full(client, d["id"])
    assert detail["status"] == "failed" and "Tesseract" in detail["error"]


def test_blank_pdf_fails_with_reason(client, upload, tmp_path):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    doc = fitz.open()
    doc.new_page()
    doc.save(str(tmp_path / "blank.pdf"))
    d = doc_of(upload(str(tmp_path / "blank.pdf")))
    assert full(client, d["id"])["status"] == "failed"


# ------------------------------------------------------------------ upload hardening
def test_rejects_unsupported_extension(client):
    r = client.post("/api/documents", data={"files": (io.BytesIO(b"hello"), "notes.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and "Unsupported" in r.get_json()["rejected"][0]["error"]


def test_rejects_spoofed_extension_and_empty_files(client):
    r = client.post("/api/documents", data={"files": [(io.BytesIO(b"MZ not a pdf"), "a.pdf"),
                                                      (io.BytesIO(b""), "b.pdf")]},
                    content_type="multipart/form-data")
    errs = [x["error"] for x in r.get_json()["rejected"]]
    assert r.status_code == 400 and any("does not match" in e for e in errs) and any("empty" in e for e in errs)


def test_rejects_oversized_file(client, app, tmp_path):
    app.config["MAX_FILE_MB"] = 0
    r = client.post("/api/documents", data={"files": (open(H.make_pdf(tmp_path / "a.pdf"), "rb"), "a.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and "exceeds" in r.get_json()["rejected"][0]["error"]


def test_no_files(client):
    assert client.post("/api/documents").status_code == 400


def test_path_traversal_filename_is_neutralised(client, app, tmp_path):
    p = H.make_pdf(tmp_path / "a.pdf")
    r = client.post("/api/documents", data={"files": (open(p, "rb"), "../../etc/passwd.pdf")},
                    content_type="multipart/form-data")
    assert doc_of(r)["filename"] == "passwd.pdf"
    assert all(os.path.dirname(os.path.join(app.config["UPLOAD_DIR"], f)) == app.config["UPLOAD_DIR"]
               for f in os.listdir(app.config["UPLOAD_DIR"]))


def test_api_key_enforced(client, app, tmp_path):
    app.config["API_KEY"] = "s3cret"
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200


def test_security_headers(client):
    h = client.get("/").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in h["Content-Security-Policy"] and "unsafe-inline'" not in h["Content-Security-Policy"].split("style-src")[0]


def test_unknown_ids_return_404(client):
    assert client.get("/api/documents/nope").status_code == 404
    assert client.get("/api/invoices/99999").status_code == 404


# ------------------------------------------------------------------ corrections
def test_manual_correction_revalidates_and_clears_review(client, upload, tmp_path):
    bad = {**H.SAMPLE, "total": 250.0}
    d = doc_of(upload(H.make_pdf(tmp_path / "bad.pdf", rows=H.invoice_lines(bad))))
    inv = full(client, d["id"])["invoices"][0]
    r = client.patch(f"/api/invoices/{inv['id']}", json={"total": "209.00"})
    out = r.get_json()
    assert r.status_code == 200 and out["total"] == 209.0 and out["review_status"] == "ok"
    assert out["issues"] == [] and "total" in out["corrected_fields"]
    assert full(client, d["id"])["status"] == "completed"
    assert any(e["stage"] == "manual_correction" for e in full(client, d["id"])["events"])


def test_correction_validation_errors(client, upload, tmp_path):
    inv = full(client, doc_of(upload(H.make_pdf(tmp_path / "a.pdf")))["id"])["invoices"][0]
    r = client.patch(f"/api/invoices/{inv['id']}", json={"total": "abc", "invoice_date": "99/99/9999",
                                                          "currency": "DOLLARS"})
    assert r.status_code == 422 and set(r.get_json()["fields"]) == {"total", "invoice_date", "currency"}
    assert client.patch(f"/api/invoices/{inv['id']}", json={"id": 5}).status_code == 400
    assert client.patch(f"/api/invoices/{inv['id']}", data="nope").status_code == 400


def test_correction_that_breaks_arithmetic_is_flagged_and_approve_overrides(client, upload, tmp_path):
    inv = full(client, doc_of(upload(H.make_pdf(tmp_path / "a.pdf")))["id"])["invoices"][0]
    out = client.patch(f"/api/invoices/{inv['id']}", json={"tax": 50}).get_json()
    assert out["review_status"] == "needs_review"
    out = client.patch(f"/api/invoices/{inv['id']}", json={"approve": True}).get_json()
    assert out["review_status"] == "approved"


# ------------------------------------------------------------------ search
def test_search_modes_and_filters(client, upload, tmp_path):
    upload(H.make_pdf(tmp_path / "acme.pdf"))
    other = {**H.SAMPLE, "invoice_number": "ZZ-1", "supplier": "Zenith Fasteners Inc", "total": 1209.0,
             "subtotal": 1190.0, "items": [("Titanium rivets", 10, 119.0, 1190.0)]}
    upload(H.make_pdf(tmp_path / "zenith.pdf", rows=H.invoice_lines(other)))

    kw = client.get("/api/search?q=titanium&mode=keyword").get_json()
    assert kw["total"] == 1 and kw["results"][0]["document"]["filename"] == "zenith.pdf"
    assert "Titanium" in kw["results"][0]["snippet"]

    sem = client.get("/api/search?q=rivets fasteners&mode=semantic").get_json()
    assert sem["results"][0]["document"]["filename"] == "zenith.pdf"

    hyb = client.get("/api/search?q=safety gloves").get_json()
    assert hyb["results"][0]["document"]["filename"] == "acme.pdf"

    f = client.get("/api/search?q=invoice&supplier=zenith&min_total=1000").get_json()
    assert [r["document"]["filename"] for r in f["results"]] == ["zenith.pdf"]
    assert client.get("/api/search?q=invoice&min_total=5000").get_json()["total"] == 0
    assert client.get("/api/search?q=x&mode=bogus").status_code == 400


def test_search_is_safe_against_wildcards_and_injection(client, upload, tmp_path):
    upload(H.make_pdf(tmp_path / "a.pdf"))
    for q in ("%", "_", "'; DROP TABLE documents; --", "\\"):
        assert client.get("/api/search", query_string={"q": q, "mode": "keyword"}).status_code == 200
    assert client.get("/api/documents").status_code == 200


def test_filter_only_browse_and_invoice_list(client, upload, tmp_path):
    upload(H.make_pdf(tmp_path / "a.pdf"))
    assert client.get("/api/search?supplier=acme").get_json()["total"] == 1
    assert client.get("/api/invoices?q=globex").get_json()["total"] == 1
    assert client.get("/api/invoices?needs_review=true").get_json()["total"] == 0
    assert client.get("/api/invoices?date_from=2025-01-01").get_json()["total"] == 0


# ------------------------------------------------------------------ cross-check, stats, delete
def test_pdf_vs_excel_crosscheck(client, upload, tmp_path):
    a = doc_of(upload(H.make_pdf(tmp_path / "a.pdf")))
    changed = H.sheet_rows({**H.SAMPLE, "total": 209.0, "tax": 20.0, "subtotal": 189.0})
    b = doc_of(upload(H.make_xlsx(str(tmp_path / "b.xlsx"), changed)))
    r = client.post("/api/crosscheck", json={"document_a": a["id"], "document_b": b["id"]}).get_json()
    assert r["summary"]["compared"] == 1 and r["summary"]["with_differences"] == 1
    assert {d["field"] for d in r["matched"][0]["differences"]} == {"subtotal", "tax"}
    assert client.post("/api/crosscheck", json={}).status_code == 400


def test_pdf_vs_excel_identical(client, upload, tmp_path):
    a = doc_of(upload(H.make_pdf(tmp_path / "a.pdf")))
    b = doc_of(upload(H.make_csv(str(tmp_path / "b.csv"))))
    r = client.post("/api/crosscheck", json={"document_a": a["id"], "document_b": b["id"]}).get_json()
    assert r["summary"]["identical"] == 1


def test_stats_and_list_filters(client, upload, tmp_path):
    upload(H.make_pdf(tmp_path / "ok.pdf"))
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 junk")
    upload(str(bad))
    s = client.get("/api/stats").get_json()
    assert s["documents"] == 2 and s["success_rate"] == 0.5 and s["failure_rate"] == 0.5
    assert client.get("/api/documents?status=failed").get_json()["total"] == 1
    assert client.get("/api/documents?per_page=1&page=2").get_json()["items"][0]["filename"]
    assert client.get("/api/documents?page=abc").status_code == 400


def test_text_endpoint_and_download_and_delete(client, upload, tmp_path, app):
    d = doc_of(upload(H.make_pdf(tmp_path / "a.pdf", pages=[H.invoice_lines()] * 3)))
    t = client.get(f"/api/documents/{d['id']}/text?per_page=2").get_json()
    assert t["total"] == 3 and len(t["items"]) == 2
    assert client.get(f"/api/documents/{d['id']}/file").status_code == 200
    assert client.delete(f"/api/documents/{d['id']}").status_code == 204
    assert client.get(f"/api/documents/{d['id']}").status_code == 404
    with app.app_context():
        assert Invoice.query.count() == 0
    assert os.listdir(app.config["UPLOAD_DIR"]) == []


def test_stuck_jobs_are_requeued_on_startup(tmp_path):
    from app import create_app
    from models import db
    cfg = {"TESTING": True, "JOBS_SYNC": False, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path/'r.db'}",
           "UPLOAD_DIR": str(tmp_path / "up"), "EXTRACTION_MODE": "heuristic", "LLM_API_KEY": None}
    a1 = create_app(cfg)
    pdf = H.make_pdf(tmp_path / "a.pdf")
    os.makedirs(cfg["UPLOAD_DIR"], exist_ok=True)
    shutil.copy(pdf, os.path.join(cfg["UPLOAD_DIR"], "stuck.pdf"))
    with a1.app_context():
        db.session.add(Document(id="stuck", filename="a.pdf", stored_path=os.path.join(cfg["UPLOAD_DIR"], "stuck.pdf"),
                                ext=".pdf", file_type="pdf", status="processing"))
        db.session.commit()
    a1.extensions["executor"].shutdown(wait=True)
    a2 = create_app(cfg)                              # "restart"
    a2.extensions["executor"].shutdown(wait=True)
    with a2.app_context():
        assert db.session.get(Document, "stuck").status == "completed"


def test_react_bundle_is_served(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="root"' in html and "/static/app/app.js" in html
    assert client.get("/static/app/app.js").status_code == 200
    assert client.get("/static/app/app.css").status_code == 200
