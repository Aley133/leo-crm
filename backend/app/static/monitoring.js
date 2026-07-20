const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#monitoring");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const onlyErrors = document.querySelector("#only-errors");
const jobStatus = document.querySelector("#job-status");
const jobSource = document.querySelector("#job-source");
const attemptSource = document.querySelector("#attempt-source");
const attemptPeriod = document.querySelector("#attempt-period");

let cachedJobs = [];
let cachedAttempts = [];

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const when = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "—";
const badge = (value) => {
  const text = escapeHtml(value || "unknown");
  const cls = ["succeeded","success","healthy","active"].includes(value) ? "ok" : ["failed","blocked","captcha_required","degraded","auth_required"].includes(value) ? "bad" : ["queued","leased","rate_limited"].includes(value) ? "warn" : "";
  return `<span class="badge ${cls}">${text}</span>`;
};
const setText = (id, value) => { const el = document.querySelector(`#${id}`); if (el) el.textContent = String(value ?? 0); };
const api = async (path, token) => {
  const response = await fetch(path, {headers:{Authorization:`Bearer ${token}`}, cache:"no-store"});
  if (response.status === 401) { localStorage.removeItem(storageKey); throw new Error("Токен не принят."); }
  if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
  return response.json();
};
const setLoading = (loading) => {
  page.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  refreshButton.textContent = loading ? "Обновление…" : "Обновить";
};
const formatDuration = (milliseconds) => {
  if (milliseconds == null) return "—";
  const value = Number(milliseconds);
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 1000) return `${Math.round(value)} мс`;
  if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} с`;
  const minutes = Math.floor(value / 60000);
  const seconds = Math.round((value % 60000) / 1000);
  if (minutes < 60) return `${minutes} мин ${seconds} с`;
  const hours = Math.floor(minutes / 60);
  return `${hours} ч ${minutes % 60} мин`;
};
const elapsed = (value) => {
  if (!value) return "—";
  return formatDuration(Math.max(0, Date.now() - new Date(value).getTime()));
};
const errorCell = (code, text) => {
  if (!code && !text) return "—";
  const safeCode = escapeHtml(code || "Ошибка");
  const safeText = escapeHtml(text || "Нет подробностей");
  return `<details class="error-details"><summary>${safeCode}</summary><div>${safeText}</div></details>`;
};

const filteredJobs = () => {
  const status = jobStatus.value;
  const source = jobSource.value.trim().toLowerCase();
  return cachedJobs.filter((row) => (!status || row.status === status) && (!source || String(row.supplier_code || row.supplier_name || "").toLowerCase().includes(source)));
};
const filteredAttempts = () => {
  const source = attemptSource.value.trim().toLowerCase();
  const since = Date.now() - 24 * 60 * 60 * 1000;
  return cachedAttempts.filter((row) => {
    if (onlyErrors.checked && !row.error_code) return false;
    if (source && !String(row.supplier_code || row.adapter_code || "").toLowerCase().includes(source)) return false;
    if (attemptPeriod.value === "24h" && new Date(row.started_at).getTime() < since) return false;
    return true;
  });
};

const renderLeased = () => {
  const rows = cachedJobs.filter((row) => row.status === "leased");
  const body = document.querySelector("#leased-body");
  const table = document.querySelector("#leased-table");
  const empty = document.querySelector("#leased-empty");
  body.innerHTML = rows.map((row) => `<tr><td><strong>#${row.id}</strong></td><td>${row.product_id ? `<a href="/crm/products/${row.product_id}">${escapeHtml(row.product_name || row.kaspi_product_id)}</a>` : "—"}<span class="muted">${escapeHtml(row.kaspi_product_id || "")}</span></td><td><a href="${escapeHtml(row.supplier_product_url)}" target="_blank" rel="noreferrer">${escapeHtml(row.supplier_name || row.supplier_code || "Источник")}</a></td><td>${escapeHtml(row.lease_owner || "—")}</td><td>${when(row.lease_until)}</td><td>${elapsed(row.updated_at || row.created_at)}</td></tr>`).join("");
  table.classList.toggle("hidden", rows.length === 0);
  empty.classList.toggle("hidden", rows.length > 0);
  setText("leased-label", `${rows.length} активных`);
};
const renderJobs = () => {
  const rows = filteredJobs();
  document.querySelector("#jobs-body").innerHTML = rows.map((row) => `<tr><td><strong>#${row.id}</strong></td><td>${badge(row.status)}</td><td>${row.product_id ? `<a href="/crm/products/${row.product_id}">${escapeHtml(row.product_name || row.kaspi_product_id)}</a>` : "—"}<span class="muted">${escapeHtml(row.kaspi_product_id || "")}</span></td><td><a href="${escapeHtml(row.supplier_product_url)}" target="_blank" rel="noreferrer">${escapeHtml(row.supplier_name || row.supplier_code || "Источник")}</a></td><td>${escapeHtml(row.lease_owner || "—")}<span class="muted">до ${when(row.lease_until)}</span></td><td>${errorCell(row.error_code, row.error_message)}</td><td>${when(row.created_at)}</td></tr>`).join("");
};
const renderAttempts = () => {
  const rows = filteredAttempts();
  document.querySelector("#attempts-body").innerHTML = rows.map((row) => `<tr><td>${badge(row.outcome)}</td><td>${row.product_id ? `<a href="/crm/products/${row.product_id}">${escapeHtml(row.product_name || row.kaspi_product_id)}</a>` : "—"}<span class="muted">${escapeHtml(row.kaspi_product_id || "")}</span></td><td>${escapeHtml(row.supplier_code || row.adapter_code)}</td><td>${formatDuration(row.duration_ms)}</td><td>${row.http_status ?? "—"}</td><td>${errorCell(row.error_code, row.error_message)}</td><td>${when(row.started_at)}</td></tr>`).join("");
  setText("attempts-label", `${rows.length} проверок`);
};
const renderSources = (rows) => {
  document.querySelector("#sources-body").innerHTML = rows.map((row) => `<tr><td>${escapeHtml(row.supplier_name)}<span class="muted">${escapeHtml(row.supplier_code)}</span></td><td>${escapeHtml(row.access_strategy)}</td><td>${badge(row.status)}</td><td>${row.consecutive_failures}</td><td>${when(row.last_success_at)}</td><td>${when(row.last_failure_at)}<span class="muted">${escapeHtml(row.last_error_code || "")}</span></td></tr>`).join("");
};
const renderAttemptMetrics = () => {
  const since = Date.now() - 24 * 60 * 60 * 1000;
  const recent = cachedAttempts.filter((row) => new Date(row.started_at).getTime() >= since);
  const successful = recent.filter((row) => !row.error_code && ["success","succeeded"].includes(row.outcome)).length;
  const failed = recent.filter((row) => Boolean(row.error_code) || row.outcome === "failed").length;
  const durations = cachedAttempts.map((row) => Number(row.duration_ms)).filter((value) => Number.isFinite(value) && value >= 0);
  const average = durations.length ? durations.reduce((sum, value) => sum + value, 0) / durations.length : null;
  setText("attempts-success-24h", successful);
  setText("attempts-failed-24h", failed);
  setText("attempts-average", formatDuration(average));
  setText("last-attempt", cachedAttempts.length ? when(cachedAttempts[0].started_at) : "—");
};

const rerender = () => { renderLeased(); renderJobs(); renderAttempts(); renderAttemptMetrics(); };
const loadMonitoring = async () => {
  const token = localStorage.getItem(storageKey);
  if (!token) { authPanel.classList.remove("hidden"); page.classList.add("hidden"); return; }
  setLoading(true);
  message.textContent = "";
  try {
    const [summary, jobs, attempts, sources] = await Promise.all([
      api("/api/monitoring-center/summary", token),
      api("/api/monitoring-center/jobs?limit=100", token),
      api("/api/monitoring-center/attempts?limit=100", token),
      api("/api/monitoring-center/sources", token),
    ]);
    cachedJobs = jobs;
    cachedAttempts = attempts;
    setText("targets-total", summary.targets_total);
    setText("targets-active", summary.targets_active);
    setText("jobs-queued", summary.jobs_queued);
    setText("jobs-leased", summary.jobs_leased);
    rerender();
    renderSources(sources);
    document.querySelector("#jobs-updated").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
    authPanel.classList.add("hidden");
    page.classList.remove("hidden");
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось загрузить мониторинг.";
  } finally { setLoading(false); }
};

tokenForm.addEventListener("submit", (event) => { event.preventDefault(); const token = tokenInput.value.trim(); if (!token) return; localStorage.setItem(storageKey, token); tokenInput.value = ""; loadMonitoring(); });
refreshButton.addEventListener("click", loadMonitoring);
[onlyErrors, jobStatus, jobSource, attemptSource, attemptPeriod].forEach((control) => control.addEventListener(control.tagName === "INPUT" && control.type === "search" ? "input" : "change", rerender));
loadMonitoring();
