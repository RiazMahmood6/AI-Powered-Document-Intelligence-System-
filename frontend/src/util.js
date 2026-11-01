export const STATUS = { queued: 'Queued', processing: 'Processing', completed: 'Done',
  needs_review: 'Needs review', failed: 'Failed', duplicate: 'Duplicate' };

export const FIELDS = [
  ['invoice_number', 'Invoice number', 'text'], ['invoice_date', 'Invoice date', 'date'],
  ['supplier_name', 'Supplier', 'text'], ['customer_name', 'Customer', 'text'],
  ['currency', 'Currency', 'text'], ['subtotal', 'Subtotal', 'num'], ['tax', 'Tax', 'num'],
  ['total', 'Total', 'num'],
];

export const fmtMs = (ms) => ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
export const fmtSize = (b) => b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`;
export const isActive = (d) => d.status === 'queued' || d.status === 'processing';
