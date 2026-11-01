import { memo, useCallback, useEffect, useState } from 'react';
import { api, downloadFile } from '../api.js';
import { STATUS, fmtMs, isActive } from '../util.js';
import InvoiceSheet from './InvoiceSheet.jsx';

export default function Review({ docId, summary, toast, reload, onGone }) {
  const [detail, setDetail] = useState(null);
  const [highlight, setHighlight] = useState('');

  const load = useCallback(async () => {
    if (!docId) { setDetail(null); return; }
    try { setDetail(await api(`/api/documents/${docId}`)); }
    catch (e) { if (e.status === 404) onGone(); else toast(e.message, true); }
  }, [docId, toast, onGone]);

  // Reload when the selection changes, when the status changes, and while it is processing.
  useEffect(() => { setHighlight(''); load(); }, [docId, summary?.status, summary?.progress]);   // eslint-disable-line

  if (!docId) return <p className="empty">Pick a document from the list to inspect what was extracted.</p>;
  if (!detail || detail.id !== docId) return <p className="empty">Loading…</p>;

  const d = detail;
  const act = async (path, method, ok, gone) => {
    try { await api(path, { method }); toast(ok); if (gone) onGone(); reload(); load(); }
    catch (e) { toast(e.message, true); }
  };

  return (
    <>
      <div className="docbar">
        <div>
          <h2>{d.filename}</h2>
          <div className="sub">{[STATUS[d.status] || d.status, d.page_count ? `${d.page_count} page${d.page_count > 1 ? 's' : ''}` : null,
            `processed in ${fmtMs(d.processing_ms)}`, `attempt ${d.attempts}`].filter(Boolean).join(', ')}</div>
        </div>
        <div className="actions">
          {d.status === 'failed' && <button className="btn" type="button" onClick={() => act(`/api/documents/${d.id}/reprocess`, 'POST', 'Re-queued')}>Reprocess</button>}
          {d.status === 'duplicate' && <button className="btn ghost" type="button" onClick={() => act(`/api/documents/${d.id}/reprocess?force=true`, 'POST', 'Re-queued')}>Process anyway</button>}
          <button className="btn ghost" type="button" onClick={() => downloadFile(`/api/documents/${d.id}/file`, d.filename).catch((e) => toast(e.message, true))}>Download original</button>
          <button className="btn danger" type="button" onClick={() => window.confirm('Delete this document and its extracted data?') && act(`/api/documents/${d.id}`, 'DELETE', 'Deleted', true)}>Delete</button>
        </div>
      </div>

      <div className="split">
        <div className="sheetwrap">
          {d.status === 'failed' && (
            <div className="attention"><strong>Processing failed. </strong>{d.error || 'Unknown error'}
              <div>Fix the cause (for example install Tesseract or re-export the file) and reprocess.</div></div>
          )}
          {d.status === 'duplicate' && <div className="attention warn">{d.error}</div>}
          {isActive(d) && <p className="empty">{d.stage || 'Queued'}… {d.progress || 0}%. Results appear here when processing finishes.</p>}
          {d.warnings?.length > 0 && (
            <div className="attention warn"><strong>File warnings</strong>
              <ul>{d.warnings.slice(0, 8).map((w, i) => <li key={i}>{w}</li>)}</ul></div>
          )}
          {d.invoices.map((inv, i) => (
            <InvoiceSheet key={`${inv.id}-${inv.updated_at}`} inv={inv} idx={i} count={d.invoices.length}
              onHighlight={setHighlight} toast={toast} onSaved={() => { reload(); load(); }} />
          ))}
          <details className="log">
            <summary>Processing history ({d.events.length})</summary>
            <table className="plain">
              <thead><tr>{['When', 'Stage', 'Result', 'Detail', 'Time'].map((h) => <th key={h}>{h}</th>)}</tr></thead>
              <tbody>{d.events.map((e) => (
                <tr key={e.id}><td>{new Date(e.created_at).toLocaleTimeString()}</td><td>{e.stage}</td>
                  <td><span className={'pill ' + e.status}>{e.status}</span></td><td>{e.message}</td><td className="num">{fmtMs(e.duration_ms)}</td></tr>
              ))}</tbody>
            </table>
          </details>
        </div>
        {d.page_count > 0 && <SourcePane key={`${d.id}-${d.status}`} docId={d.id} highlight={highlight} toast={toast} />}
      </div>
    </>
  );
}

// ---- source text with the focused field's value highlighted
const SourcePane = memo(function SourcePane({ docId, highlight, toast }) {
  const [pages, setPages] = useState([]);
  const [total, setTotal] = useState(0);
  const PER = 4;

  const load = useCallback(async (page) => {
    try {
      const r = await api(`/api/documents/${docId}/text?per_page=${PER}&page=${page}`);
      setPages((p) => (page === 1 ? r.items : [...p, ...r.items]));
      setTotal(r.total);
    } catch (e) { toast(e.message, true); }
  }, [docId, toast]);
  useEffect(() => { load(1); }, [load]);

  const needles = highlight === '' ? [] : [String(highlight),
    ...(!isNaN(highlight) ? [Number(highlight).toFixed(2), Number(highlight).toLocaleString('en-US', { minimumFractionDigits: 2 })] : [])];

  return (
    <aside className="source" aria-label="Source text">
      <h3>Source text</h3>
      {pages.map((p) => (
        <div key={p.page_no}>
          <div className="pg num">{p.label || `Page ${p.page_no}`} — {p.source}
            {p.source === 'ocr' && p.ocr_confidence != null ? ` (${Math.round(p.ocr_confidence * 100)}% OCR confidence)` : ''}</div>
          <pre><Marked text={p.text || '(no text on this page)'} needles={needles} /></pre>
        </div>
      ))}
      {pages.length < total && (
        <button className="btn ghost" type="button" onClick={() => load(Math.floor(pages.length / PER) + 1)}>Load more pages ({total - pages.length} left)</button>
      )}
    </aside>
  );
});

function Marked({ text, needles }) {
  const low = text.toLowerCase();
  for (const n of needles) {
    const i = low.indexOf(n.toLowerCase());
    if (i >= 0) {
      return <>{text.slice(0, i)}<mark ref={(el) => el && el.scrollIntoView({ block: 'nearest' })}>{text.slice(i, i + n.length)}</mark>{text.slice(i + n.length)}</>;
    }
  }
  return text;
}
