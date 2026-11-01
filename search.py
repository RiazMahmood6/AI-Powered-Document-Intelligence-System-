"""Task 5 - keyword filtering + semantic search.

Embedders:
  hashing  - HashingVectorizer (1-2 grams, L2-normalised). Deterministic, no model download,
             stateless so stored vectors never go stale. Matches on shared vocabulary.
  sbert    - sentence-transformers (true semantic similarity). Set EMBEDDING_BACKEND=sbert.
Vectors are stored per page/chunk in the DB and scored with one numpy matrix product.
For >1M chunks swap the scoring block for pgvector / Qdrant (same interface).
"""
import logging
import re

import numpy as np
from sqlalchemy import and_, or_

from models import ExtractedText, Invoice, db

log = logging.getLogger(__name__)
_CACHE = {}


class HashingEmbedder:
    dim = 1024
    name = "hashing-1024"

    def __init__(self):
        from sklearn.feature_extraction.text import HashingVectorizer
        self.vec = HashingVectorizer(n_features=self.dim, ngram_range=(1, 2), alternate_sign=False,
                                     norm="l2", stop_words="english",
                                     token_pattern=r"(?u)\b\w+\b")

    def embed(self, texts):
        return self.vec.transform(list(texts)).toarray().astype(np.float32)


class SBertEmbedder:
    def __init__(self, model):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model)
        self.name = f"sbert:{model}"
        self.dim = self.model.get_sentence_embedding_dimension()

    def embed(self, texts):
        return np.asarray(self.model.encode(list(texts), normalize_embeddings=True,
                                            batch_size=32, show_progress_bar=False),
                          dtype=np.float32)


def get_embedder(cfg):
    backend = (cfg.get("EMBEDDING_BACKEND") or "hashing").lower()
    key = (backend, cfg.get("SBERT_MODEL"))
    if key not in _CACHE:
        emb = None
        if backend == "sbert":
            try:
                emb = SBertEmbedder(cfg.get("SBERT_MODEL"))
            except Exception as e:
                log.warning("sbert unavailable (%s); falling back to hashing embedder", e)
        _CACHE[key] = emb or HashingEmbedder()
    return _CACHE[key]


# ----------------------------------------------------------------------------- helpers
def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def make_snippet(text, terms, width=160):
    low = (text or "").lower()
    pos = min((low.find(t.lower()) for t in terms if t and low.find(t.lower()) >= 0), default=0)
    start = max(0, pos - width // 3)
    snip = re.sub(r"\s+", " ", (text or "")[start:start + width]).strip()
    return ("…" if start else "") + snip + ("…" if start + width < len(text or "") else "")


def invoice_filter_clauses(args):
    """Translate query-string filters into SQLAlchemy clauses on Invoice."""
    cl = []

    def like(col, key):
        v = (args.get(key) or "").strip()
        if v:
            cl.append(col.ilike(f"%{_like_escape(v)}%", escape="\\"))

    like(Invoice.supplier_name, "supplier")
    like(Invoice.customer_name, "customer")
    like(Invoice.invoice_number, "invoice_number")
    if args.get("currency"):
        cl.append(Invoice.currency == args["currency"].upper())
    if args.get("date_from"):
        cl.append(Invoice.invoice_date >= args["date_from"])
    if args.get("date_to"):
        cl.append(Invoice.invoice_date <= args["date_to"])
    for key, op in (("min_total", "ge"), ("max_total", "le")):
        v = args.get(key)
        if v not in (None, ""):
            try:
                v = float(v)
            except ValueError:
                continue
            cl.append(Invoice.total >= v if op == "ge" else Invoice.total <= v)
    if args.get("review_status"):
        cl.append(Invoice.review_status == args["review_status"])
    if str(args.get("needs_review", "")).lower() in ("1", "true", "yes"):
        cl.append(Invoice.review_status == "needs_review")
    if args.get("document_id"):
        cl.append(Invoice.document_id == args["document_id"])
    return cl


def filtered_doc_ids(args):
    cl = invoice_filter_clauses(args)
    if not cl:
        return None
    return {r[0] for r in db.session.query(Invoice.document_id).filter(and_(*cl)).distinct()}


# ----------------------------------------------------------------------------- rankers
def keyword_hits(q, doc_ids, limit=300):
    terms = [t for t in re.split(r"\s+", q.strip()) if t]
    if not terms:
        return []
    clauses = [ExtractedText.text.ilike(f"%{_like_escape(t)}%", escape="\\") for t in terms]
    query = db.session.query(ExtractedText.document_id, ExtractedText.page_no, ExtractedText.text)
    query = query.filter(and_(*clauses))
    hits = []
    for doc_id, page_no, text in query.limit(limit * 3):
        if doc_ids is not None and doc_id not in doc_ids:
            continue
        low = text.lower()
        score = sum(low.count(t.lower()) for t in terms)
        hits.append({"document_id": doc_id, "page_no": page_no, "score": float(score),
                     "snippet": make_snippet(text, terms)})
    # invoice-field matches (supplier / customer / invoice number)
    f_clauses = []
    for t in terms:
        pat = f"%{_like_escape(t)}%"
        f_clauses.append(or_(Invoice.supplier_name.ilike(pat, escape="\\"),
                             Invoice.customer_name.ilike(pat, escape="\\"),
                             Invoice.invoice_number.ilike(pat, escape="\\")))
    for inv in db.session.query(Invoice).filter(and_(*f_clauses)).limit(limit):
        if doc_ids is not None and inv.document_id not in doc_ids:
            continue
        hits.append({"document_id": inv.document_id, "page_no": None, "score": 5.0 * len(terms),
                     "snippet": f"Invoice {inv.invoice_number or '?'} — {inv.supplier_name or '?'} "
                                f"→ {inv.customer_name or '?'}"})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def semantic_hits(q, cfg, doc_ids, limit=300, max_vectors=200_000):
    emb = get_embedder(cfg)
    qv = emb.embed([q])[0]
    if not np.any(qv):
        return []
    rows = (db.session.query(ExtractedText.id, ExtractedText.document_id, ExtractedText.page_no,
                             ExtractedText.embedding)
            .filter(ExtractedText.embedding.isnot(None), ExtractedText.embedding_model == emb.name)
            .limit(max_vectors).all())
    rows = [r for r in rows if doc_ids is None or r.document_id in doc_ids]
    if not rows:
        return []
    mat = np.vstack([np.frombuffer(r.embedding, dtype=np.float32) for r in rows])
    scores = mat @ qv
    order = np.argsort(-scores)[:limit]
    hits = []
    for i in order:
        if scores[i] <= 0.01:
            break
        hits.append({"document_id": rows[i].document_id, "page_no": rows[i].page_no,
                     "score": float(scores[i]), "page_id": rows[i].id})
    return hits


def _group_by_document(hits):
    seen, out = set(), []
    for h in hits:
        if h["document_id"] not in seen:
            seen.add(h["document_id"])
            out.append(h)
    return out


def search_documents(q, mode, cfg, args, limit=25):
    """Return ranked documents [{document_id, page_no, score, snippet, matched_by}]."""
    doc_ids = filtered_doc_ids(args)
    if doc_ids is not None and not doc_ids:
        return []
    kw = _group_by_document(keyword_hits(q, doc_ids)) if mode in ("keyword", "hybrid") else []
    sem = _group_by_document(semantic_hits(q, cfg, doc_ids)) if mode in ("semantic", "hybrid") else []
    if mode == "keyword":
        return [dict(h, matched_by=["keyword"]) for h in kw[:limit]]
    if mode == "semantic":
        results = []
        for h in sem[:limit]:
            pg = db.session.get(ExtractedText, h["page_id"])
            results.append({"document_id": h["document_id"], "page_no": h["page_no"],
                            "score": h["score"],
                            "snippet": make_snippet(pg.text, q.split()) if pg else "",
                            "matched_by": ["semantic"]})
        return results
    # hybrid: reciprocal-rank fusion
    fused = {}
    for tag, ranked in (("keyword", kw), ("semantic", sem)):
        for rank, h in enumerate(ranked):
            e = fused.setdefault(h["document_id"], {"score": 0.0, "by": [], "best": h})
            e["score"] += 1.0 / (60 + rank)
            e["by"].append(tag)
            if tag == "keyword" or "snippet" not in e["best"]:
                e["best"] = h
    results = []
    for doc_id, e in sorted(fused.items(), key=lambda kv: -kv[1]["score"])[:limit]:
        h = e["best"]
        snippet = h.get("snippet")
        if snippet is None and h.get("page_id"):
            pg = db.session.get(ExtractedText, h["page_id"])
            snippet = make_snippet(pg.text, q.split()) if pg else ""
        results.append({"document_id": doc_id, "page_no": h.get("page_no"),
                        "score": round(e["score"], 5), "snippet": snippet or "",
                        "matched_by": e["by"]})
    return results
