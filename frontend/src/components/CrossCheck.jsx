import { useEffect, useState } from 'react';
import { api } from '../api.js';

export default function CrossCheck({ docs, toast }) {
  const ready = docs.filter((d) => ['completed', 'needs_review'].includes(d.status));
  const [a, setA] = useState('');
  const [b, setB] = useState('');
  const [result, setResult] = useState(null);

  useEffect(() => {
    if (!a && ready[0]) setA(ready[0].id);
    if (!b && ready[1]) setB(ready[1].id);
  }, [ready.length]);   // eslint-disable-line

  const go = async () => {
    if (a === b) return toast('Pick two different documents', true);
    try { setResult(await api('/api/crosscheck', { method: 'POST', json: { document_a: a, document_b: b } })); }
    catch (e) { toast(e.message, true); }
  };
  const opt = (d) => <option key={d.id} value={d.id}>{d.filename} ({d.invoice_count} invoice{d.invoice_count === 1 ? '' : 's'})</option>;
  const s = result?.summary;

  return (
    <>
      <p className="sub">Match invoices by number across two files (PDF against PDF, PDF against Excel) and list every field that disagrees.</p>
      {ready.length < 2 ? <p className="empty">Process at least two documents first.</p> : (
        <div className="sel2">
          <div><label htmlFor="x-a">Document A</label><select id="x-a" value={a} onChange={(e) => setA(e.target.value)}>{ready.map(opt)}</select></div>
          <div><label htmlFor="x-b">Document B</label><select id="x-b" value={b} onChange={(e) => setB(e.target.value)}>{ready.map(opt)}</select></div>
          <div><label className="sr" htmlFor="x-go">Run</label><button className="btn" id="x-go" type="button" onClick={go}>Compare</button></div>
        </div>
      )}
      <div aria-live="polite">
        {s && (
          <>
            <div className="sum">
              <span>Compared <b>{s.compared}</b></span><span>Identical <b>{s.identical}</b></span>
              <span>With differences <b>{s.with_differences}</b></span>
              <span>Only in {result.document_a.filename} <b>{s.only_in_a}</b></span>
              <span>Only in {result.document_b.filename} <b>{s.only_in_b}</b></span>
            </div>
            {result.matched.some((m) => !m.match) ? (
              <table className="plain">
                <thead><tr><th>Invoice</th><th>Field</th><th>{result.document_a.filename}</th><th>{result.document_b.filename}</th></tr></thead>
                <tbody>{result.matched.filter((m) => !m.match).flatMap((m) => m.differences.map((d, i) => (
                  <tr key={m.invoice_number + d.field + i}><td>{m.invoice_number}</td><td>{d.field}</td><td className="num">{String(d.a ?? '')}</td><td className="num">{String(d.b ?? '')}</td></tr>)))}</tbody>
              </table>
            ) : s.compared > 0 && <p className="sub">Every matched invoice agrees on all compared fields.</p>}
            {[[result.only_in_a, result.document_a.filename], [result.only_in_b, result.document_b.filename]].map(([list, name]) => list.length > 0 && (
              <details className="log" key={name}><summary>{list.length} invoice(s) only in {name}</summary>
                <ul>{list.map((i, n) => <li className="num" key={n}>{[i.invoice_number || '(no number)', i.supplier_name, i.total].filter((x) => x != null).join(', ')}</li>)}</ul></details>
            ))}
          </>
        )}
      </div>
    </>
  );
}
