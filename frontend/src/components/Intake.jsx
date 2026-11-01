import { useRef, useState } from 'react';
import { STATUS, fmtSize, isActive } from '../util.js';

export default function Intake({ docs, selected, onPick, onUpload, statusFilter, onFilter, onRetryFailed }) {
  const [over, setOver] = useState(false);
  const input = useRef(null);

  return (
    <section className="intake" aria-label="Intake">
      <label className={'drop' + (over ? ' over' : '')}
        onDragEnter={(e) => { e.preventDefault(); setOver(true); }}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={(e) => { e.preventDefault(); setOver(false); }}
        onDrop={(e) => { e.preventDefault(); setOver(false); onUpload(e.dataTransfer.files); }}>
        <strong>Add documents</strong>
        <small>Drop files or click. PDF, JPG, PNG, TIFF, XLSX, XLS, CSV.</small>
        <input ref={input} type="file" multiple accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv"
          onChange={(e) => { onUpload(e.target.files); e.target.value = ''; }} />
      </label>

      <div className="filters">
        <label className="sr" htmlFor="statusFilter">Filter by status</label>
        <select id="statusFilter" value={statusFilter} onChange={(e) => onFilter(e.target.value)}>
          <option value="">All documents</option>
          <option value="needs_review">Needs review</option>
          <option value="failed">Failed</option>
          <option value="processing,queued">In progress</option>
          <option value="completed">Completed</option>
        </select>
        <button className="btn ghost" type="button" onClick={onRetryFailed} title="Re-queue every failed document">Retry failed</button>
      </div>

      <ul className="queue" aria-label="Documents">
        {!docs.length && <li className="empty">No documents yet. Add invoices, receipts, scans or spreadsheets above.</li>}
        {docs.map((d) => {
          const meta = [d.file_type, d.page_count ? `${d.page_count} p` : null, fmtSize(d.size_bytes),
            d.invoice_count ? `${d.invoice_count} invoice${d.invoice_count > 1 ? 's' : ''}` : null,
            d.review_count ? `${d.review_count} to review` : null].filter(Boolean).join(', ');
          const active = isActive(d);
          return (
            <li key={d.id} className={d.id === selected ? 'sel' : ''}>
              <button className="row" type="button" aria-current={d.id === selected ? 'true' : undefined} onClick={() => onPick(d.id)}>
                <div className="name">
                  <span title={d.filename}>{d.filename}</span>
                  <span className={'pill ' + d.status}>{STATUS[d.status] || d.status}</span>
                </div>
                <div className="meta">{active ? `${d.stage || 'queued'}… ${d.progress || 0}%` : meta}</div>
                {active && <div className="bar" role="progressbar" aria-valuenow={d.progress || 0} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${d.progress || 0}%` }} /></div>}
                {d.status === 'failed' && <div className="meta">{d.error}</div>}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
