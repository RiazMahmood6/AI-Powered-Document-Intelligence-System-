"""Task 7 - measure real extraction accuracy, speed, success and failure rates.

  python evaluation/evaluate.py                       # builds a synthetic labelled set and scores it
  python evaluation/evaluate.py --n 120 --seed 7
  python evaluation/evaluate.py --dataset my_data/    # your own files + my_data/labels.json

labels.json format (one entry per file; a PDF with many invoices lists them all):
  {"inv1.pdf": [{"invoice_number": "A-1", "invoice_date": "2024-03-15", "supplier_name": "...",
                 "customer_name": "...", "subtotal": 190.0, "tax": 19.0, "total": 209.0,
                 "currency": "USD", "line_items": [{"description": "...", "amount": 45.0}]}],
   "broken.pdf": "expect_failure"}
Use null for a field that is genuinely absent: predicting a value there counts as a hallucination.
"""
import argparse
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import create_app                    # noqa: E402
from extraction import norm_name, norm_number  # noqa: E402
from models import Document, db               # noqa: E402
from tests import helpers as H                # noqa: E402

FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name",
          "subtotal", "tax", "total", "currency"]
SUPPLIERS = ["Acme Industrial Supplies Ltd", "Northwind Traders", "Zenith Fasteners Inc",
             "Blue Harbor Logistics LLC", "Kestrel Office Products Ltd", "Orion Packaging Co",
             "Delta Tools GmbH", "Summit Catering Services"]
CUSTOMERS = ["Globex Corporation", "Initech Ltd", "Umbrella Holdings", "Hooli Inc", "Stark Retail Co"]
ITEMS = ["Steel bolts M8", "Hex nuts M8", "Safety gloves", "Cardboard boxes", "Printer paper A4",
         "Packing tape", "LED bulbs", "Cable ties", "Hand sanitizer", "Wooden pallets", "Toner cartridge"]
NUM_LABELS = ["Invoice No:", "Invoice #:", "Invoice Number:"]
TAX_LABELS = [("Tax (10%)", .10), ("VAT 20%", .20), ("GST 5%", .05)]
CURRENCIES = ["USD", "EUR", "GBP"]
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def make_label(rng, i):
    items = []
    for _ in range(rng.randint(1, 6)):
        q, p = rng.randint(1, 50), round(rng.uniform(1, 150), 2)
        items.append((rng.choice(ITEMS), q, p, round(q * p, 2)))
    sub = round(sum(x[3] for x in items), 2)
    tl, rate = rng.choice(TAX_LABELS)
    tax = round(sub * rate, 2)
    return {"invoice_number": f"INV-{2024}-{i:04d}", "invoice_date": f"2024-{rng.randint(1,12):02d}-{rng.randint(13,28):02d}",
            "supplier_name": rng.choice(SUPPLIERS), "customer_name": rng.choice(CUSTOMERS),
            "subtotal": sub, "tax": tax, "total": round(sub + tax, 2), "currency": rng.choice(CURRENCIES),
            "line_items": [{"description": d, "quantity": q, "unit_price": p, "amount": a} for d, q, p, a in items],
            "_tax_label": tl}


def to_rows(lab, rng, date_style):
    y, m, d = lab["invoice_date"].split("-")
    shown = {"iso": lab["invoice_date"], "text": f"{int(d)} {MONTHS[int(m)-1]} {y}", "us": f"{m}/{d}/{y}"}[date_style]
    data = {"invoice_number": lab["invoice_number"], "invoice_date": shown, "supplier": lab["supplier_name"],
            "customer": lab["customer_name"] or "-", "currency": lab["currency"], "subtotal": lab["subtotal"],
            "tax": lab["tax"] or 0.0, "total": lab["total"],
            "items": [(i["description"], i["quantity"], i["unit_price"], i["amount"]) for i in lab["line_items"]]}
    rows = H.invoice_lines(data, rng.choice(NUM_LABELS), lab["_tax_label"])
    if lab.get("customer_name") is None:
        rows = [r for r in rows if not (r and r[0] == "Bill To:")]
    if lab.get("tax") is None:
        rows = [r for r in rows if not (r and r[0].startswith(("Tax", "VAT", "GST")))]
    return rows


def sheet_rows(lab):
    d = {**H.SAMPLE, **{k: lab[k] for k in ("invoice_number", "invoice_date", "customer_name", "subtotal", "tax", "total", "currency")},
         "supplier": lab["supplier_name"], "customer": lab["customer_name"],
         "items": [(i["description"], i["quantity"], i["unit_price"], i["amount"]) for i in lab["line_items"]]}
    return H.sheet_rows(d)


def build_dataset(out, n, seed):
    rng = random.Random(seed)
    os.makedirs(out, exist_ok=True)
    labels, kinds = {}, {}
    mix = ["pdf"] * 35 + ["scan"] * 15 + ["img"] * 15 + ["xlsx"] * 15 + ["csv"] * 10 + ["noisy"] * 10
    for i in range(1, n + 1):
        kind = mix[(i - 1) % len(mix)]
        lab = make_label(rng, i)
        if i % 9 == 0:
            lab["customer_name"] = None            # missing customer must stay null
        if i % 11 == 0:
            lab["tax"] = None
            lab["total"] = lab["subtotal"]
        style = rng.choice(["iso", "text", "us"]) if kind in ("pdf", "scan", "img", "noisy") else "iso"
        name = f"{kind}_{i:03d}"
        if kind == "pdf":
            H.make_pdf(f"{out}/{name}.pdf", rows=to_rows(lab, rng, style)); fn = f"{name}.pdf"
        elif kind == "scan":
            H.make_scanned_pdf(f"{out}/{name}.pdf", rows=to_rows(lab, rng, style)); fn = f"{name}.pdf"
        elif kind in ("img", "noisy"):
            H.make_image(f"{out}/{name}.png", rows=to_rows(lab, rng, style), noise=kind == "noisy"); fn = f"{name}.png"
        elif kind == "xlsx":
            H.make_xlsx(f"{out}/{name}.xlsx", sheet_rows(lab)); fn = f"{name}.xlsx"
        else:
            H.make_csv(f"{out}/{name}.csv", sheet_rows(lab)); fn = f"{name}.csv"
        lab = {k: v for k, v in lab.items() if not k.startswith("_")}
        labels[fn], kinds[fn] = [lab], kind
    # injected arithmetic errors: the system must flag these for review
    for j in range(1, 7):
        lab = make_label(rng, 900 + j)
        shown = {**lab, "total": round(lab["total"] + rng.choice([5, 10, 25.5]), 2)}
        fn = f"badmath_{j}.pdf"
        H.make_pdf(f"{out}/{fn}", rows=to_rows(shown, rng, "iso"))
        labels[fn] = {"expect_flag": True, "invoices": [{k: v for k, v in shown.items() if not k.startswith("_")}]}
        kinds[fn] = "badmath"
    # corrupted files: must fail cleanly
    for j in range(1, 7):
        fn = f"corrupt_{j}." + ["pdf", "pdf", "png", "xlsx", "csv", "pdf"][j - 1]
        data = {"pdf": b"%PDF-1.4\n garbage \x00\x01", "png": b"\x89PNG\r\n\x1a\n nope", "xlsx": b"PK\x03\x04 broken",
                "csv": b"\x00\x01\x02 binary"}[fn.rsplit(".", 1)[1]]
        open(f"{out}/{fn}", "wb").write(data + bytes([j]))
        labels[fn], kinds[fn] = "expect_failure", "corrupt"
    json.dump(labels, open(f"{out}/labels.json", "w"), indent=1)
    json.dump(kinds, open(f"{out}/kinds.json", "w"))
    return labels, kinds


def same(field, pred, true):
    if true is None or pred is None:
        return true is None and pred is None
    if field in ("subtotal", "tax", "total"):
        return abs(float(pred) - float(true)) <= 0.01
    if field == "supplier_name" or field == "customer_name":
        return norm_name(pred) == norm_name(true)
    if field == "invoice_number":
        return norm_number(pred) == norm_number(true)
    return str(pred).strip().upper() == str(true).strip().upper()


def run(dataset, labels, kinds, tag):
    tmp = tempfile.mkdtemp()
    app = create_app({"JOBS_SYNC": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp}/eval.db",
                      "UPLOAD_DIR": f"{tmp}/up", "TESTING": True})
    client = app.test_client()
    rows = []
    for fn in sorted(labels):
        t0 = time.perf_counter()
        with open(os.path.join(dataset, fn), "rb") as f:
            r = client.post("/api/documents", data={"files": (f, fn)}, content_type="multipart/form-data")
        wall = time.perf_counter() - t0
        body = r.get_json()
        if not body["documents"]:
            rows.append({"file": fn, "status": "rejected", "wall": wall, "invoices": [], "kind": kinds.get(fn, "?")})
            continue
        doc = client.get(f"/api/documents/{body['documents'][0]['id']}").get_json()
        rows.append({"file": fn, "status": doc["status"], "ms": doc["processing_ms"], "wall": wall,
                     "invoices": doc["invoices"], "pages": doc["page_count"], "kind": kinds.get(fn, "?"),
                     "error": doc["error"]})
    shutil.rmtree(tmp, ignore_errors=True)

    hit, tot = {f: 0 for f in FIELDS}, {f: 0 for f in FIELDS}
    halluc = {"opportunities": 0, "invented": 0}
    line_ok = line_tot = 0
    exact_docs = docs_scored = 0
    by_kind = {}
    flagged_ok = flagged_tot = 0
    fail_ok = fail_tot = 0
    for r in rows:
        lab = labels[r["file"]]
        if lab == "expect_failure":
            fail_tot += 1
            fail_ok += r["status"] in ("failed", "rejected")
            continue
        exp = lab["invoices"] if isinstance(lab, dict) else lab
        if isinstance(lab, dict) and lab.get("expect_flag"):
            flagged_tot += 1
            flagged_ok += any(i["review_status"] == "needs_review" for i in r["invoices"])
        if r["status"] in ("failed", "rejected"):
            for e in exp:
                for f in FIELDS:
                    tot[f] += 1
                    by_kind.setdefault(r["kind"], [0, 0])[1] += 1
            continue
        preds = {norm_number(p["invoice_number"]): p for p in r["invoices"]}
        for k, e in enumerate(exp):
            p = preds.get(norm_number(e["invoice_number"])) or (r["invoices"][k] if k < len(r["invoices"]) else {})
            all_ok = True
            for f in FIELDS:
                tot[f] += 1
                ok = same(f, p.get(f), e.get(f))
                hit[f] += ok
                all_ok &= ok
                bk = by_kind.setdefault(r["kind"], [0, 0]); bk[0] += ok; bk[1] += 1
                if e.get(f) is None:
                    halluc["opportunities"] += 1
                    halluc["invented"] += p.get(f) is not None
            docs_scored += 1
            exact_docs += all_ok
            pl = p.get("line_items") or []
            for i, el in enumerate(e.get("line_items") or []):
                line_tot += 1
                line_ok += i < len(pl) and abs((pl[i].get("amount") or -1) - el["amount"]) <= 0.01
    processed = [r for r in rows if r["status"] in ("completed", "needs_review")]
    valid_rows = [r for r in rows if labels[r["file"]] != "expect_failure"]
    ok_valid = [r for r in valid_rows if r["status"] in ("completed", "needs_review")]
    times = [r["ms"] for r in processed if r.get("ms") is not None]
    per_kind_time = {}
    for r in processed:
        per_kind_time.setdefault(r["kind"], []).append(r["ms"])
    pct = lambda a, b: round(100 * a / b, 1) if b else None
    pctl = lambda xs, q: sorted(xs)[min(len(xs) - 1, int(len(xs) * q))] if xs else None
    return {
        "tag": tag, "files": len(rows), "valid_files": len(valid_rows),
        "document_success_rate_pct": pct(len(ok_valid), len(valid_rows)),
        "document_failure_rate_pct": pct(len(valid_rows) - len(ok_valid), len(valid_rows)),
        "corrupted_files_failed_cleanly_pct": pct(fail_ok, fail_tot),
        "field_accuracy_pct": {f: pct(hit[f], tot[f]) for f in FIELDS},
        "overall_field_accuracy_pct": pct(sum(hit.values()), sum(tot.values())),
        "invoices_fully_correct_pct": pct(exact_docs, docs_scored),
        "line_item_amount_accuracy_pct": pct(line_ok, line_tot),
        "hallucination_rate_pct": pct(halluc["invented"], halluc["opportunities"]),
        "injected_errors_flagged_pct": pct(flagged_ok, flagged_tot),
        "accuracy_by_source_pct": {k: pct(a, b) for k, (a, b) in sorted(by_kind.items())},
        "processing_ms": {"mean": round(statistics.mean(times)) if times else None,
                          "median": round(statistics.median(times)) if times else None,
                          "p95": pctl(times, .95), "max": max(times) if times else None},
        "processing_ms_mean_by_source": {k: round(statistics.mean(v)) for k, v in sorted(per_kind_time.items())},
        "failures": [{"file": r["file"], "status": r["status"], "error": r.get("error")}
                     for r in rows if r["status"] in ("failed", "rejected") and labels[r["file"]] != "expect_failure"],
        "misses": [],
    }


def markdown(m, n_label):
    L = [f"# Evaluation report", "",
         f"Dataset: {n_label}. Extraction mode: `{m['tag']}`. All numbers below were measured by `evaluation/evaluate.py`.", "",
         "## Headline", "",
         "| Metric | Value |", "|---|---|",
         f"| Files evaluated (valid / total) | {m['valid_files']} / {m['files']} |",
         f"| Document success rate | {m['document_success_rate_pct']}% |",
         f"| Document failure rate | {m['document_failure_rate_pct']}% |",
         f"| Corrupted files failed cleanly | {m['corrupted_files_failed_cleanly_pct']}% |",
         f"| Overall field accuracy | {m['overall_field_accuracy_pct']}% |",
         f"| Invoices with every field correct | {m['invoices_fully_correct_pct']}% |",
         f"| Line-item amount accuracy | {m['line_item_amount_accuracy_pct']}% |",
         f"| Hallucination rate (value given where truth is null) | {m['hallucination_rate_pct']}% |",
         f"| Injected arithmetic errors flagged for review | {m['injected_errors_flagged_pct']}% |",
         f"| Processing time mean / median / p95 / max | {m['processing_ms']['mean']} / {m['processing_ms']['median']} / {m['processing_ms']['p95']} / {m['processing_ms']['max']} ms |",
         "", "## Accuracy per field", "", "| Field | Accuracy |", "|---|---|"]
    L += [f"| {f} | {v}% |" for f, v in m["field_accuracy_pct"].items()]
    L += ["", "## Accuracy and mean time by source", "", "| Source | Field accuracy | Mean time |", "|---|---|---|"]
    for k, v in m["accuracy_by_source_pct"].items():
        L.append(f"| {k} | {v}% | {m['processing_ms_mean_by_source'].get(k, '—')} ms |")
    if m["failures"]:
        L += ["", "## Unexpected failures", ""] + [f"- `{f['file']}`: {f['status']} {f['error'] or ''}" for f in m["failures"]]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset"), ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42), ap.add_argument("--out", default=os.path.join(ROOT, "evaluation"))
    a = ap.parse_args()
    if a.dataset:
        labels = json.load(open(os.path.join(a.dataset, "labels.json")))
        kinds_p = os.path.join(a.dataset, "kinds.json")
        kinds = json.load(open(kinds_p)) if os.path.exists(kinds_p) else {k: os.path.splitext(k)[1][1:] for k in labels}
        desc = f"{len(labels)} user-supplied labelled files"
    else:
        ds = os.path.join(a.out, "dataset")
        shutil.rmtree(ds, ignore_errors=True)
        labels, kinds = build_dataset(ds, a.n, a.seed)
        a.dataset = ds
        desc = f"{len(labels)} synthetic files (seed {a.seed}): digital PDF, scanned PDF, PNG, noisy PNG, XLSX, CSV, 6 injected arithmetic errors, 6 corrupted files"
    metrics = run(a.dataset, labels, kinds, os.getenv("EXTRACTION_MODE", "hybrid") + (" + LLM" if os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY") else " (rules only)"))
    json.dump(metrics, open(os.path.join(a.out, "report.json"), "w"), indent=2)
    open(os.path.join(a.out, "report.md"), "w").write(markdown(metrics, desc))
    print(markdown(metrics, desc))


if __name__ == "__main__":
    main()
