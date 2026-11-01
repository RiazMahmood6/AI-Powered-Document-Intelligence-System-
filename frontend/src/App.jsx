import { useCallback, useEffect, useRef, useState } from 'react';
import { api, setApiKey, hasApiKey } from './api.js';
import { fmtMs } from './util.js';
import Intake from './components/Intake.jsx';
import Review from './components/Review.jsx';
import Search from './components/Search.jsx';
import CrossCheck from './components/CrossCheck.jsx';

const TABS = [['review', 'Review'], ['search', 'Search'], ['cross', 'Cross-check']];

export default function App({ authRequired }) {
  const [docs, setDocs] = useState([]);
  const [stats, setStats] = useState(null);
  const [selected, setSelected] = useState(null);
  const [tab, setTab] = useState('review');
  const [statusFilter, setStatusFilter] = useState('');
  const [toasts, setToasts] = useState([]);
  const [needKey, setNeedKey] = useState(authRequired && !hasApiKey());
  const [tick, setTick] = useState(0);                       // bump to force a refresh
  const [searchState, setSearchState] = useState({ q: '', mode: 'hybrid', filters: {}, results: null });
  const toastId = useRef(0);

  const toast = useCallback((message, error = false) => {
    const id = ++toastId.current;
    setToasts((t) => [...t, { id, message, error }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), error ? 7000 : 3500);
  }, []);
  const reload = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    const onAuth = () => setNeedKey(true);
    window.addEventListener('auth-required', onAuth);
    return () => window.removeEventListener('auth-required', onAuth);
  }, []);

  // Poll: fast while anything is processing, slow otherwise.
  useEffect(() => {
    let timer, stopped = false;
    const loop = async () => {
      let busy = false;
      try {
        const [list, s] = await Promise.all([
          api('/api/documents?per_page=100' + (statusFilter ? `&status=${statusFilter}` : '')),
          api('/api/stats'),
        ]);
        if (stopped) return;
        setDocs(list.items); setStats(s);
        busy = list.items.some((d) => d.status === 'queued' || d.status === 'processing');
      } catch (e) { /* auth handled via event; transient errors retry */ }
      if (!stopped) timer = setTimeout(loop, busy ? 1500 : 10000);
    };
    loop();
    return () => { stopped = true; clearTimeout(timer); };
  }, [statusFilter, tick]);

  const upload = async (files) => {
    if (!files.length) return;
    const fd = new FormData();
    [...files].forEach((f) => fd.append('files', f, f.name));
    toast(`Uploading ${files.length} file${files.length > 1 ? 's' : ''}…`);
    try {
      const r = await api('/api/documents', { method: 'POST', body: fd });
      r.rejected.forEach((x) => toast(`${x.filename}: ${x.error}`, true));
      if (r.documents.length) { setSelected(r.documents[0].id); setTab('review'); }
    } catch (e) {
      const rej = e.data?.rejected;
      if (rej) rej.forEach((x) => toast(`${x.filename}: ${x.error}`, true)); else toast(e.message, true);
    }
    reload();
  };

  const retryFailed = async () => {
    try { const r = await api('/api/documents/reprocess-failed', { method: 'POST' }); toast(`${r.requeued} failed document(s) re-queued`); reload(); }
    catch (e) { toast(e.message, true); }
  };

  const pick = (id) => { setSelected(id); setTab('review'); };
  const selectedDoc = docs.find((d) => d.id === selected) || null;

  return (
    <>
      <header className="top">
        <h1>Document Desk</h1>
        <div className="tally" aria-live="polite">
          {stats && <>
            <span>Documents <b>{stats.documents}</b></span>
            <span>Invoices <b>{stats.invoices}</b></span>
            <span>To review <b>{stats.invoices_needing_review}</b></span>
            <span>Success <b>{stats.success_rate == null ? '—' : Math.round(stats.success_rate * 100) + '%'}</b></span>
            <span>Avg time <b>{fmtMs(stats.avg_processing_ms)}</b></span>
          </>}
        </div>
        {needKey && (
          <form className="keybar show" onSubmit={(e) => { e.preventDefault(); setApiKey(e.target.key.value.trim()); setNeedKey(false); reload(); }}>
            <label htmlFor="apikey">API key</label>
            <input type="password" id="apikey" name="key" autoComplete="off" placeholder="X-API-Key" />
            <button className="btn" type="submit">Save key</button>
          </form>
        )}
      </header>

      <main>
        <Intake docs={docs} selected={selected} onPick={pick} onUpload={upload}
                statusFilter={statusFilter} onFilter={setStatusFilter} onRetryFailed={retryFailed} />
        <section aria-label="Workspace">
          <nav className="tabs" role="tablist">
            {TABS.map(([id, label]) => (
              <button key={id} role="tab" id={`t-${id}`} aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>
            ))}
          </nav>
          {tab === 'review' && <Review docId={selected} summary={selectedDoc} toast={toast} reload={reload} onGone={() => setSelected(null)} />}
          {tab === 'search' && <Search state={searchState} setState={setSearchState} toast={toast} onOpen={pick} />}
          {tab === 'cross' && <CrossCheck docs={docs} toast={toast} />}
        </section>
      </main>

      <div id="toasts" role="status" aria-live="polite">
        {toasts.map((t) => <div key={t.id} className={'toast' + (t.error ? ' err' : '')}>{t.message}</div>)}
      </div>
    </>
  );
}
