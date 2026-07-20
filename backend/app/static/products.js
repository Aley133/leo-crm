const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const productsPage = document.querySelector("#products");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const importButton = document.querySelector("#import-xml");
const xmlFileInput = document.querySelector("#xml-file");
const xmlDialog = document.querySelector("#xml-dialog");
const xmlPreview = document.querySelector("#xml-preview");
const xmlWarnings = document.querySelector("#xml-warnings");
const confirmImportButton = document.querySelector("#confirm-import");
const filters = document.querySelector("#filters");
const resetButton = document.querySelector("#reset");
const body = document.querySelector("#products-body");
const empty = document.querySelector("#empty");
let selectedXmlFile = null;

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency = "KZT") => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency}`;
const checkedAt = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "Никогда";
const statusLabel = (status) => ({active:"Активен",draft:"Черновик",paused:"Приостановлен",archived:"Архив"}[status] || status || "—");
const statusClass = (status) => status === "active" ? "ok" : status === "paused" ? "warn" : status === "archived" ? "bad" : "";

const setLoading = (loading) => {
  productsPage.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  refreshButton.textContent = loading ? "Обновление…" : "Обновить";
};

const queryString = () => {
  const params = new URLSearchParams({limit:"200"});
  const q = document.querySelector("#search").value.trim();
  const status = document.querySelector("#status").value;
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (document.querySelector("#only-unbound").checked) params.set("only_without_supplier", "true");
  if (document.querySelector("#only-failures").checked) params.set("only_failures", "true");
  if (document.querySelector("#only-monitored").checked) params.set("only_monitored", "true");
  return params.toString();
};

const monitoringBadge = (row) => {
  if (row.failed_monitor_count > 0) return '<span class="badge bad">Есть ошибки</span>';
  if (row.active_monitor_count > 0) return '<span class="badge ok">Активен</span>';
  return '<span class="badge">Не настроен</span>';
};

const renderSummary = (rows) => {
  const units = rows.reduce((sum, row) => sum + Number(row.units_sold || 0), 0);
  const revenue = rows.reduce((sum, row) => sum + Number(row.revenue_kzt || 0), 0);
  document.querySelector("#summary-products").textContent = rows.length.toLocaleString("ru-RU");
  document.querySelector("#summary-units").textContent = units.toLocaleString("ru-RU");
  document.querySelector("#summary-revenue").textContent = money(revenue);
  document.querySelector("#summary-unbound").textContent = rows.filter((row) => row.supplier_count === 0).length;
  document.querySelector("#summary-errors").textContent = rows.filter((row) => row.failed_monitor_count > 0).length;
};

const render = (rows) => {
  renderSummary(rows);
  body.innerHTML = rows.map((row) => `
    <tr>
      <td><a class="product-title" href="/crm/products/${row.product_id}">${escapeHtml(row.name)}</a><span class="muted">Kaspi ${escapeHtml(row.kaspi_product_id)}${row.merchant_sku ? ` · SKU ${escapeHtml(row.merchant_sku)}` : ""}${row.brand ? ` · ${escapeHtml(row.brand)}` : ""}</span></td>
      <td><span class="badge ${statusClass(row.status)}">${escapeHtml(statusLabel(row.status))}</span></td>
      <td><strong>${Number(row.units_sold || 0).toLocaleString("ru-RU")}</strong><span class="muted">строк заказов: ${Number(row.orders_count || 0).toLocaleString("ru-RU")}</span></td>
      <td><strong>${money(row.revenue_kzt)}</strong></td>
      <td><strong>${row.supplier_count}</strong><span class="muted">доступно: ${row.available_offer_count}</span></td>
      <td>${row.best_supplier_name ? `<strong>${escapeHtml(row.best_supplier_name)}</strong>` : '<span class="badge bad">Нет поставщика</span>'}</td>
      <td>${money(row.best_supplier_price, row.best_supplier_currency || "KZT")}</td>
      <td>${monitoringBadge(row)}<span class="muted">активных целей: ${row.active_monitor_count}</span></td>
      <td>${checkedAt(row.last_checked_at)}</td>
    </tr>`).join("");
  empty.classList.toggle("hidden", rows.length > 0);
  document.querySelector("#rows-label").textContent = `Показано товаров: ${rows.length}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
  authPanel.classList.add("hidden");
  productsPage.classList.remove("hidden");
};

const loadProducts = async () => {
  const token = localStorage.getItem(storageKey);
  if (!token) { authPanel.classList.remove("hidden"); productsPage.classList.add("hidden"); return; }
  setLoading(true); message.textContent = "";
  try {
    const response = await fetch(`/api/product-registry/products?${queryString()}`, {headers:{Authorization:`Bearer ${token}`},cache:"no-store"});
    if (response.status === 401) { localStorage.removeItem(storageKey); authPanel.classList.remove("hidden"); productsPage.classList.add("hidden"); message.textContent = "Токен не принят."; return; }
    if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
    render(await response.json());
  } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось загрузить товары."; }
  finally { setLoading(false); }
};

const xmlRequest = async (action, file) => {
  const token = localStorage.getItem(storageKey);
  const response = await fetch(`/api/product-registry/imports/xml/${action}`, {
    method:"POST",
    headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/xml","X-Filename":encodeURIComponent(file.name)},
    body:await file.arrayBuffer(),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Импорт вернул ошибку ${response.status}`);
  return payload;
};

const renderXmlPreview = (payload) => {
  xmlPreview.innerHTML = `
    <div class="preview-grid">
      <article><span>Всего позиций</span><strong>${payload.total}</strong></article>
      <article><span>Новых товаров</span><strong>${payload.new_count}</strong></article>
      <article><span>Будут обновлены</span><strong>${payload.existing_count}</strong></article>
      <article><span>Предупреждений</span><strong>${payload.warning_count}</strong></article>
    </div>
    <div class="preview-sample">${payload.sample.map((item) => `<div><strong>${escapeHtml(item.name)}</strong><span>Kaspi ${escapeHtml(item.kaspi_product_id)}${item.brand ? ` · ${escapeHtml(item.brand)}` : ""}</span></div>`).join("")}</div>`;
  xmlWarnings.classList.toggle("hidden", !payload.warnings.length);
  xmlWarnings.innerHTML = payload.warnings.map((item) => `<p>${escapeHtml(item)}</p>`).join("");
};

const previewXml = async (file) => {
  selectedXmlFile = file;
  document.querySelector("#xml-file-name").textContent = `${file.name} · ${(file.size / 1024 / 1024).toLocaleString("ru-RU", {maximumFractionDigits:2})} МБ`;
  xmlPreview.innerHTML = '<div class="empty">Проверяю XML…</div>';
  xmlWarnings.classList.add("hidden");
  confirmImportButton.disabled = true;
  xmlDialog.showModal();
  try {
    renderXmlPreview(await xmlRequest("preview", file));
    confirmImportButton.disabled = false;
  } catch (error) {
    xmlPreview.innerHTML = `<div class="empty">${escapeHtml(error instanceof Error ? error.message : "Не удалось проверить XML")}</div>`;
  }
};

const commitXml = async () => {
  if (!selectedXmlFile) return;
  confirmImportButton.disabled = true;
  confirmImportButton.textContent = "Импортирую…";
  try {
    const result = await xmlRequest("commit", selectedXmlFile);
    xmlDialog.close();
    message.textContent = `XML импортирован: создано ${result.created_count}, обновлено ${result.updated_count}, без изменений ${result.unchanged_count}.`;
    await loadProducts();
  } catch (error) {
    xmlWarnings.classList.remove("hidden");
    xmlWarnings.innerHTML = `<p>${escapeHtml(error instanceof Error ? error.message : "Не удалось импортировать XML")}</p>`;
  } finally {
    confirmImportButton.disabled = false;
    confirmImportButton.textContent = "Импортировать";
  }
};

tokenForm.addEventListener("submit", (event) => {event.preventDefault();const token=tokenInput.value.trim();if(!token)return;localStorage.setItem(storageKey,token);tokenInput.value="";loadProducts();});
filters.addEventListener("submit", (event) => {event.preventDefault();loadProducts();});
resetButton.addEventListener("click", () => {filters.reset();loadProducts();});
refreshButton.addEventListener("click", loadProducts);
importButton.addEventListener("click", () => xmlFileInput.click());
xmlFileInput.addEventListener("change", () => {const file=xmlFileInput.files?.[0];if(file)previewXml(file);xmlFileInput.value="";});
confirmImportButton.addEventListener("click", commitXml);
loadProducts();
