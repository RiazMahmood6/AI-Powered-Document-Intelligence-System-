"""Background job: parse -> extract -> validate -> index. Runs inside a worker thread."""
import logging
import threading
import time

import numpy as np

import extraction as ex
from models import Document, ExtractedText, Invoice, ProcessingEvent, ValidationIssue, db
from processing import OCRUnavailableError, ProcessingError, parse_file
from search import get_embedder
from validation import needs_review, validate_invoice
from decimal import Decimal

log = logging.getLogger(__name__)
_COMMIT_LOCK = threading.Lock()      # makes duplicate detection consistent across workers
_LLM = {}


def get_llm(cfg):
    key = cfg.get("LLM_API_KEY")
    if not key or cfg.get("EXTRACTION_MODE") == "heuristic":
        return None
    ident = (cfg.get("LLM_PROVIDER"), key, cfg.get("LLM_MODEL"), cfg.get("LLM_BASE_URL"))
    if ident not in _LLM:
        try:
            _LLM[ident] = ex.LLMExtractor(cfg.get("LLM_PROVIDER"), key, cfg.get("LLM_MODEL"),
                                          cfg.get("LLM_BASE_URL"))
        except Exception as e:
            log.warning("LLM disabled: %s", e)
            _LLM[ident] = None
    return _LLM[ident]


def event(doc, stage, status, message="", ms=None, invoice_id=None):
    db.session.add(ProcessingEvent(document_id=doc.id, stage=stage, status=status,
                                   message=message[:2000], duration_ms=ms, invoice_id=invoice_id))


def set_progress(doc, stage, pct):
    doc.stage, doc.progress = stage, pct
    db.session.commit()


def revalidate(inv, cfg):
    """(Re)run validation for one invoice and store the issues + review status."""
    dups = []
    if inv.invoice_number_norm:
        for o in (Invoice.query.filter(Invoice.invoice_number_norm == inv.invoice_number_norm,
                                       Invoice.source_kind == inv.source_kind,
                                       Invoice.id < inv.id).all()):
            dups.append({"id": o.id, "supplier_name": o.supplier_name, "total": o.total,
                         "filename": o.document.filename if o.document else None})
    issues = validate_invoice(inv.as_extraction(), dups,
                              tol=Decimal(str(cfg.get("AMOUNT_TOLERANCE", "0.02"))),
                              min_conf=float(cfg.get("MIN_FIELD_CONFIDENCE", 0.6)))
    ValidationIssue.query.filter_by(invoice_id=inv.id).delete()
    for i in issues:
        db.session.add(ValidationIssue(invoice_id=inv.id, **i))
    inv.review_status = "needs_review" if needs_review(issues) else "ok"
    return issues


def refresh_document_status(doc):
    if doc.status in ("failed", "duplicate", "queued", "processing"):
        return
    doc.status = ("needs_review" if any(i.review_status == "needs_review" for i in doc.invoices)
                  else "completed")


def _store_invoice(doc, res, source_kind, source_pages):
    f = res["fields"]
    inv = Invoice(
        document_id=doc.id, source_kind=source_kind,
        source_pages=res.get("source_pages") or source_pages,
        extraction_method=res["method"],
        invoice_number=f.get("invoice_number"), invoice_number_norm=ex.norm_number(f.get("invoice_number")),
        invoice_date=f.get("invoice_date"), supplier_name=f.get("supplier_name"),
        supplier_norm=ex.norm_name(f.get("supplier_name")), customer_name=f.get("customer_name"),
        subtotal=f.get("subtotal"), tax=f.get("tax"), total=f.get("total"), currency=f.get("currency"),
        line_items=res.get("line_items") or [], field_confidence=res.get("confidence") or {},
        parse_errors=res.get("parse_errors") or [], notes=res.get("notes") or [],
    )
    db.session.add(inv)
    db.session.flush()
    return inv


def process_document(app, doc_id):
    with app.app_context():
        try:
            _process(app.config, doc_id)
        except Exception:                      # last-resort safety net
            log.exception("pipeline crashed for %s", doc_id)
            db.session.rollback()
            doc = db.session.get(Document, doc_id)
            if doc:
                doc.status, doc.error = "failed", "Internal error while processing (see server log)"
                db.session.commit()


def _process(cfg, doc_id):
    doc = db.session.get(Document, doc_id)
    if doc is None:
        return
    t_start = time.perf_counter()
    doc.status, doc.error, doc.warnings = "processing", None, []
    doc.attempts = (doc.attempts or 0) + 1
    # delete old invoices through the ORM so issues cascade
    for inv in list(doc.invoices):
        db.session.delete(inv)
    ExtractedText.query.filter_by(document_id=doc.id).delete()
    event(doc, "start", "info", f"attempt {doc.attempts}")
    set_progress(doc, "parsing", 5)

    try:
        t = time.perf_counter()
        parsed = parse_file(doc.stored_path, doc.ext, cfg)
        doc.page_count = len(parsed.pages)
        doc.warnings = list(parsed.warnings)[:50]
        ocr_pages = sum(p.source == "ocr" for p in parsed.pages)
        event(doc, "parse", "warning" if parsed.warnings else "ok",
              f"{len(parsed.pages)} page(s), {ocr_pages} via OCR"
              + (f"; {len(parsed.warnings)} warning(s)" if parsed.warnings else ""),
              int((time.perf_counter() - t) * 1000))
        for p in parsed.pages:
            db.session.add(ExtractedText(document_id=doc.id, page_no=p.page_no, label=p.label,
                                         source=p.source, ocr_confidence=p.confidence, text=p.text))
        set_progress(doc, "extracting", 30)

        # ---------------- extraction
        t = time.perf_counter()
        results = []
        if parsed.file_type == "spreadsheet":
            results, warns = ex.extract_from_sheets(parsed.sheets)
            doc.warnings = (doc.warnings or []) + warns
            kind = "spreadsheet"
        else:
            kind = "document"
            llm = get_llm(cfg)
            budget = ex.LLMBudget(int(cfg.get("LLM_MAX_CALLS_PER_DOC", 150)))
            segments = ex.segment_pages(parsed.pages)
            for n, seg in enumerate(segments, 1):
                text = "\n".join(p.text for p in seg["pages"])
                ocr_confs = [p.confidence for p in seg["pages"] if p.source == "ocr" and p.confidence]
                scale = min(1.0, max(0.4, (sum(ocr_confs) / len(ocr_confs)) / 0.85)) if ocr_confs else 1.0
                res = ex.extract_invoice(text, llm, cfg.get("EXTRACTION_MODE", "hybrid"), scale, budget)
                a, b = seg["pages"][0].page_no, seg["pages"][-1].page_no
                res["source_pages"] = f"{a}" if a == b else f"{a}-{b}"
                results.append(res)
                if n % 10 == 0:
                    set_progress(doc, "extracting", 30 + int(40 * n / len(segments)))
        event(doc, "extract", "ok", f"{len(results)} invoice record(s)",
              int((time.perf_counter() - t) * 1000))
        if not results:
            doc.warnings = (doc.warnings or []) + ["No invoice data found in this file"]
            results = [{"fields": {k: None for k in Invoice.FIELDS}, "line_items": [],
                        "confidence": {}, "method": "none", "notes": ["Nothing extractable"],
                        "parse_errors": []}]
        set_progress(doc, "validating", 75)

        # ---------------- validation (serialised so duplicates are detected deterministically)
        t = time.perf_counter()
        with _COMMIT_LOCK:
            flagged = 0
            for res in results:
                inv = _store_invoice(doc, res, kind, None)
                revalidate(inv, cfg)
                flagged += inv.review_status == "needs_review"
            db.session.commit()
        event(doc, "validate", "warning" if flagged else "ok",
              f"{flagged} of {len(results)} record(s) need manual review",
              int((time.perf_counter() - t) * 1000))
        set_progress(doc, "indexing", 88)

        # ---------------- embeddings (failure here must not fail the document)
        t = time.perf_counter()
        try:
            emb = get_embedder(cfg)
            pages = ExtractedText.query.filter_by(document_id=doc.id).all()
            for i in range(0, len(pages), 64):
                batch = pages[i:i + 64]
                vecs = emb.embed([(p.text or "")[:6000] for p in batch])
                for p, v in zip(batch, vecs):
                    p.embedding, p.embedding_model = np.asarray(v, dtype=np.float32).tobytes(), emb.name
            event(doc, "index", "ok", f"{len(pages)} page vector(s) via {emb.name}",
                  int((time.perf_counter() - t) * 1000))
        except Exception as e:
            log.exception("embedding failed")
            event(doc, "index", "warning", f"semantic index skipped: {e}")

        doc.status = "needs_review" if flagged else "completed"
        doc.progress, doc.stage = 100, "done"

    except OCRUnavailableError as e:
        _fail(doc, str(e), "ocr_unavailable")
    except ProcessingError as e:
        _fail(doc, str(e), "processing_error")
    except Exception as e:
        log.exception("unexpected failure for %s", doc.id)
        db.session.rollback()
        doc = db.session.get(Document, doc_id)
        _fail(doc, f"Unexpected error: {type(e).__name__}: {e}", "unexpected")
    finally:
        doc.processing_ms = int((time.perf_counter() - t_start) * 1000)
        db.session.commit()


def _fail(doc, message, code):
    db.session.rollback()
    doc = db.session.get(Document, doc.id)
    doc.status, doc.error, doc.progress, doc.stage = "failed", message[:1000], 100, "failed"
    event(doc, "failed", "error", f"[{code}] {message}")
    db.session.commit()
