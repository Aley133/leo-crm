const SESSION_KEY = "leo_workspace_session";
const token = localStorage.getItem(SESSION_KEY);
if (!token) window.location.replace("/login");

const headers = () => ({Authorization: `Bearer ${token || ""}`});
const body = document.querySelector("#products-body");
const empty = document.querySelector("#empty");
const message = document.querySelector("#message");
const refresh = document.querySelector("#refresh");
const filters = document.querySelector("#filters");

const money = (value, currency = "KZT") => `${Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits: 2})} ${currency}`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));

const readError = async (response) => {
  try { const payload = await response.json(); return typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`; }
  catch (_) { return `HTTP ${response.status}`; }
};

const queryString = () => {
  const params = new URLSearchParams({limit:"500"});
  const q = document.querySelector("#search").value.trim();
  const status = document.querySelector("#status").value;
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  return params.toString();
};

const render = (items) => {
  body.innerHTML = items.map((item) => `<tr>
    <td><strong>${escapeHtml(item.name)}</strong><span class="muted">Kaspi ID ${escapeHtml(item.kaspi_product_id)}${item.merchant_sku ? ` · SKU ${escapeHtml(item.merchant_sku)}` : ""}${item.brand ? ` · ${escapeHtml(item.brand)}` : ""}</span></td>
    <td><span class="badge">${escapeHtml(item.status)}</span></td>
    <td><strong>${Number(item.units_sold || 0).toLocaleString("ru-RU")}</strong><span class="muted">${Number(item.orders_count || 0)} заказов</span></td>
    <td><strong>${money(item.revenue_kzt)}</strong></td>
    <td><strong>${Number(item.supplier_count || 0)}</strong><span class="muted">доступно ${Number(item.available_offer_count || 0)}</span></td>
    <td><strong>${item.best_supplier_price == null ? "—" : money(item.best_supplier_price, item.best_supplier_currency || "KZT")}</strong><span class="muted">${escapeHtml(item.best_supplier_name || "Поставщик не выбран")}</span></td>
    <td><strong>${Number(item.active_monitor_count || 0)}</strong><span class="muted">ошибок ${Number(item.failed_monitor_count || 0)}</span></td>
    <td>${dateTime(item.last_checked_at)}</td>
  </tr>`).join("");
  empty.classList.toggle("hidden", items.length > 0);
  document.querySelector("#summary-products").textContent = items.length.toLocaleString("ru-RU");
  document.querySelector("#summary-units").textContent = items.reduce((sum, item) => sum + Number(item.units_sold || 0), 0).toLocaleString("ru-RU");
  document.querySelector("#summary-revenue").textContent = money(items.reduce((sum, item) => sum + Number(item.revenue_kzt || 0), 0));
  document.querySelector("#summary-unbound").textContent = items.filter((item) => Number(item.supplier_count || 0) === 0).length.toLocaleString("ru-RU");
  document.querySelector("#summary-errors").textContent = items.filter((item) => Number(item.failed_monitor_count || 0) > 0).length.toLocaleString("ru-RU");
  document.querySelector("#rows-label").textContent = `Показано товаров: ${items.length}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit"})}`;
};

const loadContext = async () => {
  const [meResponse, kaspiResponse] = await Promise.all([
    fetch("/api/auth/me", {headers:headers(), cache:"no-store"}),
    fetch("/api/workspace/kaspi", {headers:headers(), cache:"no-store"}),
  ]);
  if (meResponse.status === 401 || kaspiResponse.status === 401) {
    localStorage.removeItem(SESSION_KEY);
    window.location.replace("/login");
    return false;
  }
  if (meResponse.ok) {
    const me = await meResponse.json();
    document.querySelector("#workspace-label").textContent = me.username || "Workspace";
  }
  if (kaspiResponse.ok) {
    const kaspi = await kaspiResponse.json();
    if (kaspi.configured) document.querySelector("#shop-title").textContent = `Товары — ${kaspi.shop_name}`;
  }
  return true;
};

const load = async () => {
  refresh.disabled = true;
  message.textContent = "";
  try {
    if (!(await loadContext())) return;
    const response = await fetch(`/api/workspace/products?${queryString()}`, {headers:headers(), cache:"no-store"});
    if (response.status === 401) {
      localStorage.removeItem(SESSION_KEY);
      window.location.replace("/login");
      return;
    }
    if (!response.ok) throw new Error(await readError(response));
    render(await response.json());
  } catch (error) {
    message.textContent = error.message || "Не удалось загрузить товары.";
  } finally {
    refresh.disabled = false;
  }
};

filters.addEventListener("submit", (event) => { event.preventDefault(); load(); });
document.querySelector("#reset").addEventListener("click", () => { filters.reset(); load(); });
refresh.addEventListener("click", load);
document.querySelector("#logout").addEventListener("click", async () => {
  try { await fetch("/api/auth/logout", {method:"POST", headers:headers()}); } catch (_) {}
  localStorage.removeItem(SESSION_KEY);
  window.location.replace("/login");
});

load();
