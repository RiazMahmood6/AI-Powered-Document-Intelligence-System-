# Document Desk — AI-Powered Document Intelligence (Flask)

Upload invoices, receipts, scans and spreadsheets. The system extracts structured data, validates the
arithmetic, flags anything doubtful for a human, and makes everything searchable (keyword + semantic).
Backend: **Flask + SQLAlchemy**. Frontend: **React** (Vite), compiled into `static/app/` and served by Flask from `templates/index.html`.

```
docintel/
├── app.py            Flask app factory, REST API, security headers, job queue
├── models.py         Database schema
├── processing.py     Task 1: PDF text, OCR, images, XLSX/XLS/CSV parsing
├── extraction.py     Task 2: rules + LLM extraction + anti-hallucination grounding
├── validation.py     Task 3: validation rules + PDF/Excel cross-verification
├── pipeline.py       Background job: parse → extract → validate → index
├── search.py         Task 5: keyword filters + semantic search
├── templates/index.html   HTML shell that mounts the React app
├── frontend/         Task 6: React source (Vite). Build output goes to static/app/
├── static/app/       Compiled React bundle (committed, so Node is NOT needed to run the app)
├── tests/            Task 7: 102 automated tests + results.txt
├── evaluation/       Task 7: evaluate.py + measured report.md / report.json
├── docs/             architecture + schema diagrams, AI prompts/disclosure, walkthrough script
├── samples/          7 demo files used in the walkthrough
├── scripts/record_walkthrough.py   regenerates the walkthrough video
├── walkthrough.mp4   recorded walkthrough of the running app
└── requirements.txt  .env.example  .env
```

## Quick start

Requires Python 3.10+ and the **Tesseract OCR** binary (needed only for scans/images).

```bash
# 1. Tesseract:  Ubuntu: sudo apt install tesseract-ocr | macOS: brew install tesseract
#                Windows: https://github.com/UB-Mannheim/tesseract/wiki  (then set TESSERACT_CMD in .env)
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # then edit (see below)
python app.py                                            # http://127.0.0.1:5000
```

The React UI is already compiled into `static/app/`. Only if you change files in `frontend/`:
`cd frontend && npm install && npm run build` (or `npm run dev` for hot reload on :5173, proxying the API on :5000).

Production: `gunicorn -w 2 --threads 8 'app:create_app()'` behind nginx/Caddy (TLS), with `DATABASE_URL`
pointing to PostgreSQL. Use `-w 1` with the default in-process job queue, or move jobs to Celery/RQ (below).

**Without an LLM key the app still works** (rules-only extraction). Add a key to enable the LLM
fallback. Everything in the test-suite runs without a key or network.

### Key settings (`.env`)

| Variable | Meaning |
|---|---|
| `EXTRACTION_MODE` | `hybrid` (rules first, LLM only when rules are weak — default), `llm` (always), `heuristic` (never) |
| `LLM_PROVIDER`, `OPENAI_API_KEY` / `GROQ_API_KEY` / `ANTHROPIC_API_KEY`, `LLM_MODEL` | LLM access (`openai` default `gpt-4o-mini`; `groq` default `llama-3.3-70b-versatile`). `LLM_MAX_CALLS_PER_DOC` caps cost on 1000-page PDFs |
| `API_KEY` | If set, every `/api/*` call needs header `X-API-Key` (the UI asks for it) |
| `DATABASE_URL` | `sqlite:///docintel.db` (default) or `postgresql+psycopg2://user:pass@host/db` |
| `DATE_DAYFIRST` | How to read `03/04/2024`. Ambiguous dates are also flagged low-confidence |
| `AMOUNT_TOLERANCE` | Rounding tolerance for arithmetic checks (default 0.02) |
| `EMBEDDING_BACKEND` | `hashing` (built in) or `sbert` (`pip install sentence-transformers`) |

## Architecture

![Architecture](docs/architecture.png)

Source: `docs/architecture.mmd` (Mermaid). The same diagram as live text:

```mermaid
flowchart LR
  UI[Browser UI<br/>templates/index.html] -->|multipart upload / REST| API[Flask API<br/>app.py]
  API -->|validate ext + magic bytes + size, sha256| FS[(uploads/)]
  API -->|insert Document: queued| DB[(SQL database)]
  API -->|submit| Q[ThreadPool job queue]
  Q --> P1[processing.py<br/>digital text / OCR / sheets]
  P1 --> P2[extraction.py<br/>rules → LLM → grounding]
  P2 --> P3[validation.py<br/>arithmetic, dupes, confidence]
  P3 --> P4[search.py<br/>page embeddings]
  P1 & P2 & P3 & P4 -->|pages, invoices, issues, events| DB
  UI -->|poll status, PATCH corrections, search| API
```

**Pipeline per document.** (1) Magic-byte check at upload. (2) *Parse*: digital PDF pages are read with PyMuPDF and
rebuilt row-by-row so table cells stay on one line; pages without a text layer are rendered at 200 DPI and OCR'd;
images are auto-rotated, upscaled, contrast-stretched, denoised and OCR'd (multi-frame TIFF supported); every sheet of
XLSX/XLS and CSVs (encoding + delimiter sniffed) are read. (3) *Segment*: a multi-invoice PDF is split into invoices
wherever a new invoice number appears; spreadsheets are grouped by invoice number into header + line items.
(4) *Extract*, (5) *Validate*, (6) *Index* (one vector per page/chunk). Each stage is timed and logged to `processing_events`.

**Failure handling.** Corrupt/password-protected/blank files, missing Tesseract and unreadable pages all end in
status `failed` with a plain-language reason and an audit event; one bad page never kills a 1000-page file. Failed
jobs are re-run with `POST /api/documents/<id>/reprocess` or `/reprocess-failed`. Jobs interrupted by a crash are re-queued at startup.

## How it avoids inventing values (Task 2)

1. Rules extract what they can; anything not found stays `null` with confidence 0.
2. The LLM (OpenAI, Groq or Anthropic, in JSON mode) is asked for JSON with `null` for unknowns and told never to compute or guess.
3. **Grounding check:** every string, number, date and currency the LLM returns must literally appear in the source text
   (tolerating one OCR slip); otherwise it is discarded and noted. Line items are checked the same way.
4. When rules and LLM disagree on a field, the LLM value is kept but confidence drops to 55%, which triggers review.
5. OCR confidence scales field confidence down. Fields below `MIN_FIELD_CONFIDENCE` (0.6) force manual review.

## Validation rules (Task 3)

Missing required fields (number, date, supplier, total = error; customer, currency, subtotal, tax = warning) ·
non-numeric / negative / zero amounts · invalid or future dates · bad currency codes ·
`qty × unit price ≠ line amount` · `Σ lines ≠ subtotal` · `subtotal + tax ≠ total` (all with `Decimal` math and a tolerance) ·
**duplicate invoices** (same number + supplier + total = error; same number, different details = warning) ·
low confidence. Any error, possible duplicate or low-confidence field sets `review_status = needs_review`.
Duplicates are matched within the same source kind (PDF/image vs spreadsheet) so that the *same* invoice appearing
once in a PDF and once in Excel is **not** a duplicate. That pair is what `/api/crosscheck` compares.

## REST API

| Method & path | Purpose |
|---|---|
| `POST /api/documents` | Upload many files (`files` field). Returns 202 with queued docs + per-file rejections |
| `GET /api/documents?status=&file_type=&filename=&page=&per_page=` | List with filters |
| `GET /api/documents/<id>` · `/status` · `/text?page=` · `/file` | Detail (invoices, issues, history) · progress · paged text · original file |
| `POST /api/documents/<id>/reprocess[?force=true]` · `POST /api/documents/reprocess-failed` | Re-run failed jobs |
| `DELETE /api/documents/<id>` | Remove document, data and file |
| `GET /api/invoices?supplier=&customer=&invoice_number=&currency=&date_from=&date_to=&min_total=&max_total=&needs_review=&q=` | Filter structured results |
| `PATCH /api/invoices/<id>` | Manual correction (`{"total": 209, "approve": true}`); re-validates, logs history |
| `GET /api/search?q=&mode=keyword\|semantic\|hybrid&<invoice filters>` | Search + filters (no `q` = filter-only) |
| `POST /api/crosscheck` `{document_a, document_b}` | PDF↔PDF or PDF↔Excel field-by-field comparison |
| `GET /api/stats` · `GET /api/health` | Success/failure rate, average time · liveness |

## Database schema

![Schema](docs/schema.png)

Source: `docs/schema.mmd`. The same diagram as live text:

```mermaid
erDiagram
  documents ||--o{ extracted_text : has
  documents ||--o{ invoices : yields
  documents ||--o{ processing_events : logs
  invoices  ||--o{ validation_issues : has
  documents { string id PK
    string filename
    string sha256
    string status
    int page_count
    int attempts
    text error
    int processing_ms }
  extracted_text { int id PK
    string document_id FK
    int page_no
    string source
    float ocr_confidence
    text text
    blob embedding }
  invoices { int id PK
    string document_id FK
    string invoice_number
    string invoice_date
    string supplier_name
    string customer_name
    numeric subtotal
    numeric tax
    numeric total
    string currency
    json line_items
    json field_confidence
    string review_status }
  validation_issues { int id PK
    int invoice_id FK
    string code
    string severity
    string field
    text message }
  processing_events { int id PK
    string document_id FK
    string stage
    string status
    text message
    int duration_ms }
```

Indexes: `documents(status, sha256, created_at)`, `invoices(invoice_number_norm, supplier_norm, invoice_date, total, review_status)`,
`extracted_text(document_id, embedding_model)`.

## Design decisions & trade-offs

- **Flask + threads instead of NestJS + Redis/BullMQ.** You asked for Flask; a `ThreadPoolExecutor` is enough for a
  single node (Tesseract runs as a subprocess, PyMuPDF releases the GIL for most work) and has zero infrastructure. The
  only coupling is `app.submit_job(doc_id)`: replacing it with `celery.delay(doc_id)` / `rq.enqueue` makes workers horizontal.
- **Hybrid extraction.** Rules are free and instant; the LLM is used only for weak results. This keeps a 1000-page batch fast
  and cheap, and `LLM_MAX_CALLS_PER_DOC` bounds cost.
- **Semantic search.** The default `hashing` embedder (1–2-gram feature hashing) needs no download and is stateless, but it
  matches shared *vocabulary*, not true meaning. For real semantic recall set `EMBEDDING_BACKEND=sbert`. Vectors are scored
  with one numpy matrix product (fine to ~100k pages); beyond that swap in pgvector/Qdrant in `search.semantic_hits`.
- **Keyword search** uses escaped `ILIKE`; on PostgreSQL add a GIN `to_tsvector` index for large volumes.
- **React (Vite) served by Flask**, as the assignment requires, while keeping `templates/index.html` as the shell. One origin means no CORS
  and a strict CSP (`script-src 'self'`, no inline scripts). Next.js was not used because it would need a separate Node server.
- **Money** is stored as `NUMERIC`, validated with `Decimal`.

## Security

Extension whitelist **and** magic-byte sniffing · per-file and per-request size limits · random server-side filenames
(client names are never used as paths) · decompression-bomb guard on images · parameterised queries and escaped `LIKE`
(tested against injection strings) · optional constant-time API-key auth · CSP (`script-src 'self'`), `nosniff`,
`X-Frame-Options: DENY`, `no-store` on API responses · React escapes all rendered text (no `innerHTML`) · error messages don't expose server paths.
Still to add for production: TLS, per-user auth (not just a shared key), rate limiting (e.g. Flask-Limiter), virus scanning
of uploads, and encryption at rest.

## Tests and evaluation (Task 7)

```bash
pytest -q                          # 101 tests: parsing, OCR, spreadsheets, extraction, grounding, validation, API, failures
python evaluation/evaluate.py      # builds a labelled set, runs the real pipeline, writes evaluation/report.md|json
python evaluation/evaluate.py --dataset path/to/your_data   # score YOUR labelled files (format in the script header)
```

`evaluation/report.md` contains the measured accuracy, processing time, success and failure rates.
**Important:** the bundled report uses a *synthetic* dataset generated by the script (rendered PDFs, scans, noisy images,
spreadsheets, injected errors, corrupted files). Those layouts are cleaner than real-world invoices, so treat the numbers as a
regression baseline and a demonstration of the method, **not** a claim about production accuracy. Run it on a few hundred of the company's own
labelled documents (ideally with an LLM key set) and report those figures for the submission.

## Known limitations

- Rules handle English labels and common layouts; unusual layouts rely on the LLM fallback.
- Segmentation of multi-invoice PDFs relies on a printed invoice number per page; invoices spanning pages without a repeated number may be split or merged incorrectly (visible in the review UI, correctable).
- The React UI lists up to 100 documents at a time (the API paginates beyond that).
- `EXTRACTION_MODE=llm` on very large PDFs is slow/costly; hybrid mode is the recommended setting.
- Job queue is in-process. Jobs interrupted by a restart are re-queued; for multi-server deployments use Celery/RQ.

## AI disclosure and prompts

Full prompts and disclosure: **`docs/AI_PROMPTS.md`**.

This project was developed with AI assistance (Claude): architecture, code, tests and this README were drafted with it and
then run and verified (tests, OCR on generated scans, a live server, a browser check). Review all code before production use.

**Runtime prompt** (in `extraction.py`, `LLM_SYSTEM` + `LLM_PROMPT`): the system message instructs the model to extract only values
explicitly present, never guess, infer, calculate or invent, return `null` for missing/ambiguous fields, and answer with a single JSON
object. The user message gives the JSON schema (8 fields, `line_items`, per-field `confidence`, `notes`), rules (plain numbers,
copy text exactly, do not compute totals) and the document text. Model output is never trusted directly: it passes the grounding check above.
