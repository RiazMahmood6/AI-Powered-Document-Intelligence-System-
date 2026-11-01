import { useMemo, useState } from 'react';
import { api } from '../api.js';
import { FIELDS } from '../util.js';

const asStr = (v) => (v == null ? '' : String(v));
const ITEM_COLS = [['description', ''], ['quantity', 'num'], ['unit_price', 'num'], ['amount', 'num']];

export default function InvoiceSheet({ inv, idx, count, onHighlight, onSaved, toast }) {
  const [form, setForm] = useState(() => Object.fromEntries(FIELDS.map(([n]) => [n, asStr(inv[n])])));
  const [items, setItems] = useState(() => (inv.line_items || []).map((i) => ({
    description: asStr(i.description), quantity: asStr(i.quantity), unit_price: asStr(i.unit_price), amount: asStr(i.amount) })));
  const [itemsDirty, setItemsDirty] = useState(false);
  const [fieldErrors, setFieldErrors] = useState({});
  const [msg, setMsg] = useState('');

  const issuesBy = useMemo(() => {
    const m = {};
    (inv.issues || []).forEach((i) => (m[i.field] = m[i.field] || []).push(i));
    return m;
  }, [inv.issues]);
  const errors = (inv.issues || []).filter((i) => i.severity === 'error');
  const status = inv.review_status;

  const setItem = (n, k, v) => { setItems((rows) => rows.map((r, i) => (i === n ? { ...r, [k]: v } : r))); setItemsDirty(true); };

  const save = async (approve) => {
    const body = {};
    for (const [n] of FIELDS) {
      const v = form[n].trim();
      if (v !== asStr(inv[n])) body[n] = v === '' ? null : v;
    }
    if (itemsDirty) body.line_items = items.map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, v.trim() || null])));
    if (approve) body.approve = true;
    if (!Object.keys(body).length) { setMsg('Nothing changed.'); return; }
    setFieldErrors({}); setMsg('');
    try {
      await api(`/api/invoices/${inv.id}`, { method: 'PATCH', json: body });
      toast(approve ? 'Approved' : 'Saved and re-validated');
      onSaved();
    } catch (e) {
      if (e.status === 422) { setFieldErrors(e.data.fields || {}); setMsg(Object.entries(e.data.fields || {}).map(([k, v]) => `${k}: ${v}`).join('; ')); }
      else toast(e.message, true);
    }
  };

  return (
    <article className="invoice">
      <header>
        <h3>{count > 1 ? `Invoice ${idx + 1} of ${count}${inv.source_pages ? ` (${inv.source_pages})` : ''}` : 'Invoice'}</h3>
        <span className={'pill ' + (status === 'needs_review' ? 'needs_review' : 'completed')}>
          {status === 'needs_review' ? 'Needs review' : status === 'approved' ? 'Approved' : 'Looks good'}</span>
      </header>

      {inv.issues?.length > 0 && (
        <div className={'attention' + (errors.length ? '' : ' warn')}>
          <strong>{errors.length ? `${errors.length} problem${errors.length > 1 ? 's' : ''} to fix` : 'Worth a quick look'}</strong>
          <ul>{inv.issues.map((i) => <li key={i.id}>{i.severity === 'error' ? 'Error' : 'Warning'}: {i.message}</li>)}</ul>
        </div>
      )}
      {inv.notes?.length > 0 && <p className="sub">{inv.notes.join(' ')}</p>}

      <div className="grid">
        {FIELDS.map(([name, label, kind]) => {
          const c = inv.field_confidence?.[name];
          const hasErr = issuesBy[name]?.some((i) => i.severity === 'error') || fieldErrors[name];
          return (
            <div className="field" key={name}>
              <label htmlFor={`f${inv.id}-${name}`}>
                <span>{label}</span>
                {inv.corrected_fields?.includes(name) ? <span className="edited">edited</span>
                  : c == null || inv[name] == null ? null
                  : <span className={'conf num' + (c < 0.6 ? ' low' : '')}>{Math.round(c * 100)}% sure</span>}
              </label>
              <input id={`f${inv.id}-${name}`} type={kind === 'date' ? 'date' : 'text'} inputMode={kind === 'num' ? 'decimal' : undefined}
                className={(kind === 'num' ? 'num ' : '') + (hasErr ? 'bad' : '')} autoComplete="off" value={form[name]}
                onFocus={() => onHighlight(inv[name] ?? '')} onChange={(e) => setForm({ ...form, [name]: e.target.value })} />
              {(issuesBy[name] || []).map((i) => <div key={i.id} className={'msg ' + i.severity}>{i.message}</div>)}
            </div>
          );
        })}
      </div>

      <table className="items">
        <caption>Line items ({items.length})</caption>
        <thead><tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th><th /></tr></thead>
        <tbody>
          {items.map((r, n) => (
            <tr key={n}>
              {ITEM_COLS.map(([k, cls]) => (
                <td key={k} className={cls}><input type="text" autoComplete="off" aria-label={k.replace('_', ' ')} value={r[k]} onChange={(e) => setItem(n, k, e.target.value)} /></td>
              ))}
              <td><button className="btn ghost" type="button" aria-label="Remove line" onClick={() => { setItems(items.filter((_, i) => i !== n)); setItemsDirty(true); }}>×</button></td>
            </tr>
          ))}
        </tbody>
      </table>

      <footer>
        <button className="btn ghost" type="button" onClick={() => { setItems([...items, { description: '', quantity: '', unit_price: '', amount: '' }]); setItemsDirty(true); }}>Add line</button>
        <button className="btn" type="button" onClick={() => save(false)}>Save changes</button>
        <button className="btn ghost" type="button" onClick={() => save(true)}>{status === 'approved' ? 'Approved' : 'Approve as is'}</button>
        <span className="note" role="status">{msg}</span>
      </footer>
    </article>
  );
}
