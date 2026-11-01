"""AI Document Intelligence System - Flask backend (Tasks 4, 5 and the web UI host).

Run:   flask --app app run --debug        (dev)
       gunicorn -w 2 --threads 8 'app:create_app()'   (prod)
"""
import hashlib
import hmac
import logging
import os
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, render_template, request, send_file
from sqlalchemy import case, func
from werkzeug.exceptions import HTTPException

load_dotenv()

import extraction as ex                                    # noqa: E402
from models import Document, ExtractedText, Invoice, ProcessingEvent, db   # noqa: E402
from pipeline import event, process_document, refresh_document_status, revalidate   # noqa: E402
from processing import ALLOWED_EXTENSIONS, sniff_ok         # noqa: E402
from search import invoice_filter_clauses, search_documents  # noqa: E402
from validation import cross_verify                         # noqa: E402

log = logging.getLogger("docintel")
EDITABLE_TEXT = ("invoice_number", "supplier_name", "customer_name")
EDITABLE_MONEY = ("subtotal", "tax", "total")


def _env(name, default=None):
    v = os.getenv(name)
    return default if v is None or v == "" else v


def llm_config():
    """LLM_PROVIDER = openai | groq | anthropic. Key comes from LLM_API_KEY or the provider's own variable."""
    provider = _env("LLM_PROVIDER", "openai").lower()
    key_var = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
    model = {"openai": "gpt-4o-mini", "groq": "llama-3.3-70b-versatile",
             "anthropic": "claude-sonnet-5-5"}.get(provider, "gpt-4o-mini")
    base = {"groq": "https://api.groq.com/openai/v1"}.get(provider)
    return {
        "LLM_PROVIDER": provider,
        "LLM_API_KEY": _env("LLM_API_KEY") or _env(key_var.get(provider, "OPENAI_API_KEY")),
        "LLM_MODEL": _env("LLM_MODEL", model),
        "LLM_BASE_URL": _env("LLM_BASE_URL", base),
    }


def default_config():
    base = os.path.dirname(os.path.abspath(__file__))
    return {
        "SECRET_KEY": _env("SECRET_KEY", secrets.token_hex(16)),
        "API_KEY": _env("API_KEY"),
        "SQLALCHEMY_DATABASE_URI": _env("DATABASE_URL", f"sqlite:///{os.path.join(base, 'docintel.db')}"),
        "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True},
        "UPLOAD_DIR": os.path.abspath(_env("UPLOAD_DIR", os.path.join(base, "uploads"))),
        "MAX_FILE_MB": int(_env("MAX_FILE_MB", 50)),
        "MAX_CONTENT_LENGTH": int(_env("MAX_REQUEST_MB", 500)) * 1024 * 1024,
        "WORKER_THREADS": int(_env("WORKER_THREADS", 4)),
        "JOBS_SYNC": False,                       # tests run jobs inline
        "OCR_LANG": _env("OCR_LANG", "eng"),
        "OCR_DPI": int(_env("OCR_DPI", 200)),
        "OCR_PSM": int(_env("OCR_PSM", 6)),
        "TESSERACT_CMD": _env("TESSERACT_CMD"),
        "EXTRACTION_MODE": _env("EXTRACTION_MODE", "hybrid").lower(),
        **llm_config(),
        "LLM_MAX_CALLS_PER_DOC": int(_env("LLM_MAX_CALLS_PER_DOC", 150)),
        "DATE_DAYFIRST": _env("DATE_DAYFIRST", "true").lower() in ("1", "true", "yes"),
        "AMOUNT_TOLERANCE": _env("AMOUNT_TOLERANCE", "0.02"),
        "MIN_FIELD_CONFIDENCE": float(_env("MIN_FIELD_CONFIDENCE", 0.6)),
        "EMBEDDING_BACKEND": _env("EMBEDDING_BACKEND", "hashing"),
        "SBERT_MODEL": _env("SBERT_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
    }


def create_app(test_config=None):
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = Flask(__name__)
    app.config.update(default_config())
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    ex.configure(dayfirst=app.config["DATE_DAYFIRST"])
    db.init_app(app)
    executor = ThreadPoolExecutor(max_workers=app.config["WORKER_THREADS"], thread_name_prefix="job")
    app.extensions["executor"] = executor

    def submit_job(doc_id):
        if app.config["JOBS_SYNC"]:
            process_document(app, doc_id)
        else:
            executor.submit(process_document, app, doc_id)

    app.submit_job = submit_job

    try:                                      # pre-warm so the first upload isn't slow
        from search import get_embedder
        get_embedder(app.config).embed(["warm up"])
    except Exception:
        log.warning("embedder warm-up skipped", exc_info=True)

    with app.app_context():
        db.create_all()
        stuck = Document.query.filter(Document.status.in_(("queued", "processing"))).all()
        for d in stuck:                       # crash recovery: re-queue interrupted jobs
            d.status = "queued"
        db.session.commit()
        if not app.config["JOBS_SYNC"]:
            for d in stuck:
                submit_job(d.id)

    # ------------------------------------------------------------------ security
    @app.before_request
    def _auth():
        key = app.config.get("API_KEY")
        if key and request.path.startswith("/api/") and request.path != "/api/health":
            if not hmac.compare_digest(request.headers.get("X-API-Key", ""), key):
                return jsonify(error="Invalid or missing API key"), 401

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(Exception)
    def _errors(e):
        if isinstance(e, HTTPException):
            return jsonify(error=e.description, code=e.code), e.code
        log.exception("unhandled error")
        db.session.rollback()
        return jsonify(error="Internal server error"), 500

    # ------------------------------------------------------------------ helpers
    def page_args(default=25):
        try:
            page = max(1, int(request.args.get("page", 1)))
            per = min(200, max(1, int(request.args.get("per_page", default))))
        except ValueError:
            abort(400, "page and per_page must be integers")
        return page, per

    def get_doc(doc_id):
        doc = db.session.get(Document, doc_id)
        if not doc:
            abort(404, "Document not found")
        return doc

    def summaries(docs):
        ids = [d.id for d in docs]
        counts = {}
        if ids:
            review_expr = func.sum(case((Invoice.review_status == "needs_review", 1), else_=0))
            for doc_id, n, review in (db.session.query(
                    Invoice.document_id, func.count(Invoice.id), review_expr)
                    .filter(Invoice.document_id.in_(ids)).group_by(Invoice.document_id)):
                counts[doc_id] = (n, int(review or 0))
        out = []
        for d in docs:
            row = d.to_dict()
            row["invoice_count"], row["review_count"] = counts.get(d.id, (0, 0))
            out.append(row)
        return out

    # ------------------------------------------------------------------ UI + health
    @app.get("/")
    def index():
        return render_template("index.html", auth_required=bool(app.config["API_KEY"]))

    @app.get("/api/health")
    def health():
        return jsonify(status="ok", time=datetime.now(timezone.utc).isoformat(),
                       llm_enabled=bool(app.config["LLM_API_KEY"]
                                        and app.config["EXTRACTION_MODE"] != "heuristic"),
                       extraction_mode=app.config["EXTRACTION_MODE"], llm_provider=app.config["LLM_PROVIDER"],
                       embedding_backend=app.config["EMBEDDING_BACKEND"])

    # ------------------------------------------------------------------ documents
    @app.post("/api/documents")
    def upload():
        files = request.files.getlist("files") or request.files.getlist("file")
        if not files:
            abort(400, "No files received. Send multipart/form-data with field 'files'.")
        max_bytes = app.config["MAX_FILE_MB"] * 1024 * 1024
        accepted, rejected = [], []
        for f in files:
            name = os.path.basename(f.filename or "unnamed")[:255]
            ext = os.path.splitext(name)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                rejected.append({"filename": name, "error": f"Unsupported type '{ext or 'none'}'. "
                                 f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"})
                continue
            doc_id = uuid.uuid4().hex
            path = os.path.join(app.config["UPLOAD_DIR"], f"{doc_id}{ext}")
            sha, size = hashlib.sha256(), 0
            too_big = False
            with open(path, "wb") as out:
                while chunk := f.stream.read(1 << 20):
                    size += len(chunk)
                    if size > max_bytes:
                        too_big = True
                        break
                    sha.update(chunk)
                    out.write(chunk)
            if too_big or size == 0 or not sniff_ok(path, ext):
                os.remove(path)
                msg = (f"File exceeds {app.config['MAX_FILE_MB']} MB" if too_big else
                       "File is empty" if size == 0 else "File content does not match its extension")
                rejected.append({"filename": name, "error": msg})
                continue
            digest = sha.hexdigest()
            twin = (Document.query.filter(Document.sha256 == digest, Document.status != "failed",
                                          Document.status != "duplicate").first())
            doc = Document(id=doc_id, filename=name, stored_path=path, ext=ext,
                           file_type=ALLOWED_EXTENSIONS[ext], size_bytes=size, sha256=digest)
            if twin:
                doc.status, doc.duplicate_of, doc.progress = "duplicate", twin.id, 100
                doc.error = f"Identical file already uploaded as '{twin.filename}'"
            db.session.add(doc)
            db.session.flush()
            event(doc, "upload", "warning" if twin else "ok",
                  doc.error or f"{size} bytes, sha256 {digest[:12]}")
            db.session.commit()
            if not twin:
                app.submit_job(doc.id)
            accepted.append(doc.id)
        status = 202 if accepted else 400
        docs = [db.session.get(Document, i) for i in accepted]
        return jsonify(documents=summaries(docs), rejected=rejected), status

    @app.get("/api/documents")
    def list_documents():
        page, per = page_args()
        q = Document.query
        if request.args.get("status"):
            q = q.filter(Document.status.in_(request.args["status"].split(",")))
        if request.args.get("file_type"):
            q = q.filter(Document.file_type == request.args["file_type"])
        if request.args.get("filename"):
            like = request.args["filename"].replace("%", r"\%").replace("_", r"\_")
            q = q.filter(Document.filename.ilike(f"%{like}%", escape="\\"))
        total = q.count()
        docs = q.order_by(Document.created_at.desc()).offset((page - 1) * per).limit(per).all()
        return jsonify(items=summaries(docs), page=page, per_page=per, total=total)

    @app.get("/api/documents/<doc_id>")
    def get_document(doc_id):
        doc = get_doc(doc_id)
        d = summaries([doc])[0]
        d["invoices"] = [i.to_dict() for i in doc.invoices]
        d["events"] = [e.to_dict() for e in doc.events]
        d["pages"] = [{"page_no": p.page_no, "label": p.label, "source": p.source,
                       "ocr_confidence": p.ocr_confidence, "chars": len(p.text or "")}
                      for p in doc.pages.order_by(ExtractedText.page_no).limit(2000)]
        return jsonify(d)

    @app.get("/api/documents/<doc_id>/status")
    def document_status(doc_id):
        doc = get_doc(doc_id)
        return jsonify(id=doc.id, status=doc.status, stage=doc.stage, progress=doc.progress,
                       error=doc.error, attempts=doc.attempts, processing_ms=doc.processing_ms)

    @app.get("/api/documents/<doc_id>/text")
    def document_text(doc_id):
        get_doc(doc_id)
        page, per = page_args(default=20)
        q = ExtractedText.query.filter_by(document_id=doc_id).order_by(ExtractedText.page_no)
        total = q.count()
        rows = q.offset((page - 1) * per).limit(per).all()
        return jsonify(items=[{"page_no": r.page_no, "label": r.label, "source": r.source,
                               "ocr_confidence": r.ocr_confidence, "text": r.text} for r in rows],
                       page=page, per_page=per, total=total)

    @app.get("/api/documents/<doc_id>/file")
    def download(doc_id):
        doc = get_doc(doc_id)
        if not os.path.exists(doc.stored_path):
            abort(404, "File no longer on disk")
        return send_file(doc.stored_path, as_attachment=True, download_name=doc.filename)

    def _reprocess(doc, force=False):
        if doc.status == "failed" or (force and doc.status not in ("queued", "processing")):
            doc.status, doc.error, doc.progress, doc.stage = "queued", None, 0, "queued"
            event(doc, "reprocess", "info", "re-queued by user")
            db.session.commit()
            app.submit_job(doc.id)
            return True
        return False

    @app.post("/api/documents/<doc_id>/reprocess")
    def reprocess(doc_id):
        doc = get_doc(doc_id)
        force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
        if not _reprocess(doc, force):
            abort(409, "Only failed documents can be reprocessed (use ?force=true to override; "
                       "manual corrections will be lost)")
        return jsonify(summaries([db.session.get(Document, doc_id)])[0]), 202

    @app.post("/api/documents/reprocess-failed")
    def reprocess_failed():
        ids = [d.id for d in Document.query.filter_by(status="failed").all()]
        for i in ids:
            _reprocess(db.session.get(Document, i))
        return jsonify(requeued=len(ids), ids=ids), 202

    @app.delete("/api/documents/<doc_id>")
    def delete_document(doc_id):
        doc = get_doc(doc_id)
        path = doc.stored_path
        db.session.delete(doc)
        db.session.commit()
        if not Document.query.filter_by(stored_path=path).first() and os.path.exists(path):
            os.remove(path)
        return "", 204

    # ------------------------------------------------------------------ invoices
    @app.get("/api/invoices")
    def list_invoices():
        page, per = page_args()
        q = Invoice.query.filter(*invoice_filter_clauses(request.args))
        term = (request.args.get("q") or "").strip()
        if term:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            q = q.filter(db.or_(Invoice.supplier_name.ilike(like, escape="\\"),
                                Invoice.customer_name.ilike(like, escape="\\"),
                                Invoice.invoice_number.ilike(like, escape="\\")))
        total = q.count()
        rows = q.order_by(Invoice.id.desc()).offset((page - 1) * per).limit(per).all()
        return jsonify(items=[i.to_dict() for i in rows], page=page, per_page=per, total=total)

    @app.get("/api/invoices/<int:inv_id>")
    def get_invoice(inv_id):
        inv = db.session.get(Invoice, inv_id)
        if not inv:
            abort(404, "Invoice not found")
        return jsonify(inv.to_dict())

    @app.patch("/api/invoices/<int:inv_id>")
    def correct_invoice(inv_id):
        inv = db.session.get(Invoice, inv_id)
        if not inv:
            abort(404, "Invoice not found")
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            abort(400, "JSON body required")
        approve = bool(body.pop("approve", False))
        allowed = set(EDITABLE_TEXT + EDITABLE_MONEY + ("invoice_date", "currency", "line_items"))
        unknown = set(body) - allowed
        if unknown:
            abort(400, f"Unknown or read-only fields: {', '.join(sorted(unknown))}")

        changes, errors = {}, {}
        for f, v in body.items():
            if f in EDITABLE_TEXT:
                v = None if ex._blank(v) else str(v).strip()
                if v and len(v) > 255:
                    errors[f] = "too long (max 255)"
                changes[f] = v
            elif f in EDITABLE_MONEY:
                if ex._blank(v):
                    changes[f] = None
                else:
                    d = ex.parse_amount(v)
                    if d is None:
                        errors[f] = "must be a number"
                    else:
                        changes[f] = ex.to_float(d)
            elif f == "invoice_date":
                if ex._blank(v):
                    changes[f] = None
                else:
                    iso, _ = ex.normalize_date(v)
                    if iso is None:
                        errors[f] = "not a valid date (use YYYY-MM-DD)"
                    changes[f] = iso
            elif f == "currency":
                v = None if ex._blank(v) else str(v).strip().upper()
                if v and len(v) != 3:
                    errors[f] = "must be a 3-letter ISO code"
                changes[f] = v
            elif f == "line_items":
                if not isinstance(v, list) or len(v) > 2000:
                    errors[f] = "must be a list (max 2000 items)"
                    continue
                items = []
                for n, it in enumerate(v, 1):
                    if not isinstance(it, dict):
                        errors[f] = f"item {n} must be an object"
                        break
                    row = {"description": None if ex._blank(it.get("description"))
                           else str(it["description"]).strip()[:255]}
                    for k in ("quantity", "unit_price", "amount"):
                        raw = it.get(k)
                        d = None if ex._blank(raw) else ex.parse_amount(raw)
                        if not ex._blank(raw) and d is None:
                            errors[f"line_items[{n}].{k}"] = "must be a number"
                        row[k] = ex.to_float(d, 4)
                    items.append(row)
                changes[f] = items
        if errors:
            return jsonify(error="Validation failed", fields=errors), 422

        before = {f: getattr(inv, f) for f in changes}
        conf = dict(inv.field_confidence or {})
        corrected = set(inv.corrected_fields or [])
        for f, v in changes.items():
            setattr(inv, f, v)
            if f != "line_items":
                conf[f] = 1.0 if v is not None else 0.0
            corrected.add(f)
            inv.parse_errors = [p for p in (inv.parse_errors or [])
                                if p.get("field") != f
                                and not (f == "line_items" and str(p.get("field", "")).startswith("line_items"))]
        inv.field_confidence, inv.corrected_fields = conf, sorted(corrected)
        inv.invoice_number_norm = ex.norm_number(inv.invoice_number)
        inv.supplier_norm = ex.norm_name(inv.supplier_name)
        db.session.flush()
        issues = revalidate(inv, app.config)
        if approve:
            inv.review_status = "approved"
        doc = inv.document
        refresh_document_status(doc)
        event(doc, "manual_correction", "info",
              f"fields={sorted(changes)} approve={approve} before={before}", invoice_id=inv.id)
        db.session.commit()
        return jsonify(inv.to_dict())

    # ------------------------------------------------------------------ search / stats / compare
    @app.get("/api/search")
    def search():
        q = (request.args.get("q") or "").strip()
        mode = request.args.get("mode", "hybrid")
        if mode not in ("keyword", "semantic", "hybrid"):
            abort(400, "mode must be keyword, semantic or hybrid")
        limit = min(100, int(request.args.get("limit", 25)))
        if not q:                                    # filter-only browsing
            page, per = page_args()
            qry = Invoice.query.filter(*invoice_filter_clauses(request.args))
            return jsonify(mode="filter", total=qry.count(),
                           results=[{"document": i.document.to_dict(), "invoice": i.to_dict(False),
                                     "score": None, "snippet": "", "page_no": None,
                                     "matched_by": ["filter"]}
                                    for i in qry.order_by(Invoice.id.desc())
                                    .offset((page - 1) * per).limit(per)])
        started = time.perf_counter()
        hits = search_documents(q, mode, app.config, request.args, limit)
        out = []
        for h in hits:
            doc = db.session.get(Document, h["document_id"])
            if not doc:
                continue
            invs = [i.to_dict(False) for i in doc.invoices[:5]]
            out.append({"document": doc.to_dict(), "invoices": invs, **{k: h[k] for k in
                        ("score", "snippet", "page_no", "matched_by")}})
        return jsonify(mode=mode, query=q, total=len(out), results=out,
                       took_ms=int((time.perf_counter() - started) * 1000))

    @app.get("/api/stats")
    def stats():
        by_status = dict(db.session.query(Document.status, func.count(Document.id))
                         .group_by(Document.status).all())
        ok = by_status.get("completed", 0) + by_status.get("needs_review", 0)
        failed = by_status.get("failed", 0)
        attempted = ok + failed
        avg_ms = db.session.query(func.avg(Document.processing_ms)).filter(
            Document.status.in_(("completed", "needs_review"))).scalar()
        pages = db.session.query(func.sum(Document.page_count)).scalar() or 0
        n_inv = Invoice.query.count()
        n_review = Invoice.query.filter_by(review_status="needs_review").count()
        return jsonify(
            documents=sum(by_status.values()), by_status=by_status, invoices=n_inv,
            invoices_needing_review=n_review, pages=int(pages),
            success_rate=round(ok / attempted, 4) if attempted else None,
            failure_rate=round(failed / attempted, 4) if attempted else None,
            avg_processing_ms=round(avg_ms) if avg_ms else None)

    @app.post("/api/crosscheck")
    def crosscheck():
        body = request.get_json(silent=True) or {}
        a, b = body.get("document_a"), body.get("document_b")
        if not a or not b:
            abort(400, "document_a and document_b are required")
        da, db_ = get_doc(a), get_doc(b)
        tol = float(body.get("tolerance", app.config["AMOUNT_TOLERANCE"]))
        res = cross_verify([i.to_dict(False) for i in da.invoices],
                           [i.to_dict(False) for i in db_.invoices], tol)
        res["document_a"], res["document_b"] = da.to_dict(), db_.to_dict()
        return jsonify(res)

    return app


if __name__ == "__main__":
    create_app().run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", 5000)),
                     debug=os.getenv("FLASK_DEBUG", "0") == "1")
