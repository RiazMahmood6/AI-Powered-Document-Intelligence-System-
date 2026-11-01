import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { STATUS } from '../util.js';

const FILTERS = [['supplier', 'Supplier'], ['customer', 'Customer'], ['invoice_number', 'Invoice number'], ['currency', 'Currency'],
  ['date_from', 'From date', 'date'], ['date_to', 'To date', 'date'], ['min_total', 'Min total'], ['max_total', 'Max total']];

export default function Search({ state, setState, toast, onOpen }) {
  const [q, setQ] = useState(state.q);
  const [mode, setMode] = useState(state.mode);
  const [filters, setFilters] = useState(state.filters);
  const [busy, setBusy] = useState(false);

  const run = async (e) => {
    e?.preventDefault();
    const p = new URLSearchParams();
    if (q.trim()) p.set('q', q.trim());
    p.set('mode', mode);
    Object.entries(filters).forEach(([k, v]) => { if (v) p.set(k, v === true ? 'true' : String(v).trim()); });
    setBusy(true);
    try {
      const r = await api('/api/search?' + p);
      setState({ q, mode, filters, results: r });
    } catch (err) { toast(err.message, true); }
    setBusy(false);
  };
  useEffect(() => { if (state.results === null && (state.q || Object.keys(state.filters).length)) run(); }, []);   // eslint-disable-line

  const r = state.results;
  return (
    <>
      <form onSubmit={run}>
        <div className="searchform">
          <input type="search" aria-label="Search" value={q} onChange={(e) => setQ(e.target.value)}
            placeholder="Words, a supplier, an invoice number, or what the document is about" />
          <select aria-label="Search mode" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="hybrid">Keyword + meaning</option>
            <option value="keyword">Keyword only</option>
            <option value="semantic">Meaning only</option>
          </select>
          <button className="btn" type="submit" disabled={busy}>{busy ? 'Searching…' : 'Search'}</button>
        </div>
        <div className="advanced">
          {FILTERS.map(([k, label, type]) => (
            <div key={k}><label htmlFor={`s-${k}`}>{label}</label>
              <input id={`s-${k}`} type={type || 'text'} value={filters[k] || ''} onChange={(e) => setFilters({ ...filters, [k]: e.target.value })} /></div>
          ))}
          <div><label htmlFor="s-rev">Only needs review</label>
            <input id="s-rev" type="checkbox" checked={!!filters.needs_review} onChange={(e) => setFilters({ ...filters, needs_review: e.target.checked })} /></div>
        </div>
      </form>

      <div aria-live="polite">
        {r && <p className="sub">{r.total} result{r.total === 1 ? '' : 's'}{r.took_ms != null ? ` in ${r.took_ms} ms` : ''}</p>}
        {r && !r.results.length && <p className="empty">Nothing matched. Try fewer words, switch to “Keyword + meaning”, or loosen the filters.</p>}
        {r?.results.map((h, i) => {
          const inv = (h.invoices || [h.invoice])[0];
          return (
            <button key={h.document.id + i} className="hit" type="button" onClick={() => onOpen(h.document.id)}>
              <div className="name" style={{ display: 'flex', justifyContent: 'space-between', gap: '.75rem' }}>
                <strong>{h.document.filename}</strong><span className={'pill ' + h.document.status}>{STATUS[h.document.status]}</span>
              </div>
              {inv && <div className="sub num">{[inv.invoice_number, inv.supplier_name, inv.total != null ? `${inv.total} ${inv.currency || ''}` : null].filter(Boolean).join(', ')}</div>}
              {h.snippet && <div className="snip">{h.snippet}</div>}
              <div className="sub">{[h.page_no ? `page ${h.page_no}` : null, (h.matched_by || []).join(' + ')].filter(Boolean).join(', ')}</div>
            </button>
          );
        })}
      </div>
    </>
  );
}
