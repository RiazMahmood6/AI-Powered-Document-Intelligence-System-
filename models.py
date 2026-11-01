"""Database schema (SQLAlchemy). Works on SQLite (default) and PostgreSQL."""
import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _uuid():
    return uuid.uuid4().hex


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat() if dt else None


class Document(db.Model):
    """One uploaded file (a PDF may contain many invoices, a workbook many sheets)."""
    __tablename__ = "documents"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    filename = db.Column(db.String(255), nullable=False)
    stored_path = db.Column(db.String(512), nullable=False)
    ext = db.Column(db.String(8), nullable=False)
    file_type = db.Column(db.String(16), nullable=False)  # pdf | image | spreadsheet
    size_bytes = db.Column(db.BigInteger, default=0)
    sha256 = db.Column(db.String(64), index=True)
    status = db.Column(db.String(16), default="queued", index=True)
    # queued | processing | completed | needs_review | failed | duplicate
    error = db.Column(db.Text)
    warnings = db.Column(db.JSON, default=list)
    page_count = db.Column(db.Integer, default=0)
    attempts = db.Column(db.Integer, default=0)
    duplicate_of = db.Column(db.String(32))
    progress = db.Column(db.Integer, default=0)  # 0-100
    stage = db.Column(db.String(32))
    processing_ms = db.Column(db.Integer)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)

    pages = db.relationship("ExtractedText", backref="document", cascade="all, delete-orphan",
                            lazy="dynamic")
    invoices = db.relationship("Invoice", backref="document", cascade="all, delete-orphan",
                               lazy="select", order_by="Invoice.id")
    events = db.relationship("ProcessingEvent", backref="document", cascade="all, delete-orphan",
                             lazy="dynamic", order_by="ProcessingEvent.id")

    def to_dict(self):
        return {
            "id": self.id, "filename": self.filename, "file_type": self.file_type,
            "size_bytes": self.size_bytes, "status": self.status, "error": self.error,
            "warnings": self.warnings or [], "page_count": self.page_count,
            "attempts": self.attempts, "duplicate_of": self.duplicate_of,
            "progress": self.progress, "stage": self.stage,
            "processing_ms": self.processing_ms,
            "created_at": _iso(self.created_at), "updated_at": _iso(self.updated_at),
        }


class ExtractedText(db.Model):
    """Text of one PDF page / image frame / spreadsheet chunk, plus its search vector."""
    __tablename__ = "extracted_text"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.String(32), db.ForeignKey("documents.id", ondelete="CASCADE"),
                            index=True, nullable=False)
    page_no = db.Column(db.Integer, nullable=False)
    label = db.Column(db.String(128))                # sheet name for spreadsheets
    source = db.Column(db.String(16))                # digital | ocr | sheet | error
    ocr_confidence = db.Column(db.Float)
    text = db.Column(db.Text, default="")
    embedding = db.Column(db.LargeBinary)            # float32 vector
    embedding_model = db.Column(db.String(64), index=True)


class Invoice(db.Model):
    """Structured result. Money columns are Numeric; validation uses Decimal."""
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.String(32), db.ForeignKey("documents.id", ondelete="CASCADE"),
                            index=True, nullable=False)
    source_kind = db.Column(db.String(16))           # document (pdf/image) | spreadsheet
    source_pages = db.Column(db.String(128))         # e.g. "3-5" or "Sheet1!2-9"
    extraction_method = db.Column(db.String(32))

    invoice_number = db.Column(db.String(128))
    invoice_number_norm = db.Column(db.String(128), index=True)
    invoice_date = db.Column(db.String(10), index=True)   # ISO yyyy-mm-dd
    supplier_name = db.Column(db.String(255))
    supplier_norm = db.Column(db.String(255), index=True)
    customer_name = db.Column(db.String(255))
    subtotal = db.Column(db.Numeric(18, 2, asdecimal=False))
    tax = db.Column(db.Numeric(18, 2, asdecimal=False))
    total = db.Column(db.Numeric(18, 2, asdecimal=False), index=True)
    currency = db.Column(db.String(8))
    line_items = db.Column(db.JSON, default=list)

    field_confidence = db.Column(db.JSON, default=dict)
    parse_errors = db.Column(db.JSON, default=list)
    notes = db.Column(db.JSON, default=list)
    corrected_fields = db.Column(db.JSON, default=list)
    review_status = db.Column(db.String(16), default="ok", index=True)  # ok | needs_review | approved
    created_at = db.Column(db.DateTime(timezone=True), default=_now)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)

    issues = db.relationship("ValidationIssue", backref="invoice", cascade="all, delete-orphan",
                             lazy="select", order_by="ValidationIssue.id")

    FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name",
              "subtotal", "tax", "total", "currency")

    def as_extraction(self):
        """Plain-dict view consumed by validation."""
        d = {f: getattr(self, f) for f in self.FIELDS}
        d["line_items"] = self.line_items or []
        d["confidence"] = self.field_confidence or {}
        d["parse_errors"] = self.parse_errors or []
        return d

    def to_dict(self, with_issues=True):
        d = {f: getattr(self, f) for f in self.FIELDS}
        d.update({
            "id": self.id, "document_id": self.document_id,
            "filename": self.document.filename if self.document else None,
            "line_items": self.line_items or [], "field_confidence": self.field_confidence or {},
            "source_pages": self.source_pages, "extraction_method": self.extraction_method,
            "review_status": self.review_status, "corrected_fields": self.corrected_fields or [],
            "notes": self.notes or [], "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        })
        if with_issues:
            d["issues"] = [i.to_dict() for i in self.issues]
        return d


class ValidationIssue(db.Model):
    __tablename__ = "validation_issues"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="CASCADE"),
                           index=True, nullable=False)
    code = db.Column(db.String(32), index=True)
    severity = db.Column(db.String(8))               # error | warning
    field = db.Column(db.String(32))
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)

    def to_dict(self):
        return {"id": self.id, "code": self.code, "severity": self.severity,
                "field": self.field, "message": self.message}


class ProcessingEvent(db.Model):
    """Audit trail: every pipeline stage, retry and manual correction."""
    __tablename__ = "processing_events"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.String(32), db.ForeignKey("documents.id", ondelete="CASCADE"),
                            index=True, nullable=False)
    invoice_id = db.Column(db.Integer)
    stage = db.Column(db.String(32))
    status = db.Column(db.String(16))                # ok | warning | error | info
    message = db.Column(db.Text)
    duration_ms = db.Column(db.Integer)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)

    def to_dict(self):
        return {"id": self.id, "invoice_id": self.invoice_id, "stage": self.stage,
                "status": self.status, "message": self.message,
                "duration_ms": self.duration_ms, "created_at": _iso(self.created_at)}
