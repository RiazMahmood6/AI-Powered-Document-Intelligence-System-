// Thin fetch wrapper: adds the API key (if any) and turns errors into ApiError.
let apiKey = sessionStorage.getItem('apikey') || '';

export const setApiKey = (k) => { apiKey = k; sessionStorage.setItem('apikey', k); };
export const hasApiKey = () => !!apiKey;

export class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data; }
}

export async function api(path, { method = 'GET', json, body, headers = {} } = {}) {
  const h = { ...headers };
  if (apiKey) h['X-API-Key'] = apiKey;
  if (json !== undefined) { h['Content-Type'] = 'application/json'; body = JSON.stringify(json); }
  const res = await fetch(path, { method, headers: h, body });
  if (res.status === 401) {
    window.dispatchEvent(new Event('auth-required'));
    throw new ApiError('API key required', 401, {});
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || res.statusText, res.status, data);
  return data;
}

export async function downloadFile(path, filename) {
  const res = await fetch(path, { headers: apiKey ? { 'X-API-Key': apiKey } : {} });
  if (!res.ok) throw new ApiError('Download failed', res.status, {});
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement('a'), { href: url, download: filename });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
