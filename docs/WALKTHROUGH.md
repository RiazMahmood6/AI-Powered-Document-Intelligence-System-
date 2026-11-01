# Walkthrough script (for your own voice-over recording)

`walkthrough.mp4` in the submission is an automated, captioned screen recording of the real application
(made with `scripts/record_walkthrough.py`). If the reviewers expect your voice, record yourself with OBS / Loom /
the Windows Game Bar (Win+G) following this script (about 5 minutes).

**Before recording:** start from an empty database (`rm instance/*.db`), run `python app.py`, open http://127.0.0.1:5000,
have the files from `samples/` ready in a folder.

| Time | Do | Say |
|---|---|---|
| 0:00 | Show the repo tree and README | "This is a Flask + React document-intelligence system: upload, OCR, AI extraction, validation, search." |
| 0:30 | Show `docs/architecture.png` | "Uploads are validated, saved, queued; a background pipeline parses, extracts, validates and indexes; everything is stored in SQL." |
| 1:00 | Select all 7 files in `samples/` and drop them | "PDF, scanned PDF, photo, Excel, CSV, a re-sent duplicate and a corrupted file, all at once." |
| 1:30 | Watch progress bars, then the statuses | "Jobs run in the background. Done, Needs review, Failed." |
| 2:00 | Open `1_acme_invoice.pdf`, click the Total field | "All fields with confidence; clicking a field highlights its source text." |
| 2:30 | Open `2_zenith_scanned_invoice.pdf` | "This was a scan, read with Tesseract OCR; the OCR confidence is shown." |
| 3:00 | Open `3_acme_export_with_wrong_total.xlsx` | "Subtotal + tax does not equal the total, so it is flagged." Fix Total to 209, Save: "Re-validated, now it passes." |
| 3:40 | Cross-check tab: file 1 vs file 3 (do this *before* fixing the total) | "PDF-to-Excel cross-verification shows exactly which field disagrees." |
| 4:00 | Open `7_acme_invoice_resent.pdf` and `6_corrupted.pdf` | "Duplicate invoice caught; corrupted file fails cleanly and can be reprocessed." |
| 4:30 | Search tab: "titanium rivets", then filter supplier "zenith" | "Keyword plus semantic search with filters." |
| 5:00 | Show `evaluation/report.md` and `pytest -q` | "Measured accuracy, speed, success/failure rates; 102 automated tests." |
