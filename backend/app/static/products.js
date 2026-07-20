const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const productsPage = document.querySelector("#products");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const filters = document.querySelector("#filters");
const resetButton = document.querySelector("#reset");
const body = document.querySelector("#products-body");
const empty = document.querySelector("#empty");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU")} ${currency || ""}`.trim();
const checkedAt = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "Никогда";

const setLoading = (loading) => {
  productsPage.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  refreshButton.textContent = loading ? "Обновление…" : "Обновить";
};

const queryString = () => {
  const params = new URLSearchParams({limit:"200"});
  const q = document.querySelector("#search").value.trim();
  if (q) params.set("q", q);
  if (document.querySelector("#only-unbound").checked) params.set("only_without_supplier", "true");
  if (document.querySelector("#only-failures").checked) params.set("only_failures", "true");
  return params.toString();
};

const monitoringBadge = (row) => {
  if (row.failed_monitor_count > 0) return '<span class="badge bad">Есть ошибки</span>';
  if (row.monitored_count > 0) return '<span class="badge ok">Активен</span>';
  return '<span class="badge">Не настроен</span>';
};

const render = (rows) => {
  body.innerHTML = rows.map((row) => `
    <tr>
      <td><a class="product-title" href="/crm/products/${row.product_id}">${escapeHtml(row.product_name)}</a><span class="muted">Kaspi ${escapeHtml(row.kaspi_product_id)}${row.merchant_sku ? ` · SKU ${escapeHtml(row.merchant_sku)}` : ""}${row.brand ? ` · ${escapeHtml(row.brand)}` : ""}</span></td>
      <td><span class="badge">${escapeHtml(row.product_status)}</span></td>
      <td><strong>${row.supplier_count}</strong><span class="muted">привязок</span></td>
      <td>${row.best_supplier_name ? `<strong>${escapeHtml(row.best_supplier_name)}</strong><span class="muted">${escapeHtml(row.best_supplier_code || "")}</span>` : '<span class="badge bad">Нет поставщика</span>'}</td>
      <td>${money(row.best_supplier_price, row.best_supplier_currency)}</td>
      <td>${row.available_offer_count > 0 ? `<span class="badge ok">${row.available_offer_count} в наличии</span>` : '<span class="badge warn">Нет доступных</span>'}</td>
      <td>${monitoringBadge(row)}<span class="muted">${row.monitored_count} целей</span></td>
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
    const response = await fetch(`/api/catalog/products?${queryString()}`, {headers:{Authorization:`Bearer ${token}`},cache:"no-store"});
    if (response.status === 401) { localStorage.removeItem(storageKey); authPanel.classList.remove("hidden"); productsPage.classList.add("hidden"); message.textContent = "Токен не принят."; return; }
    if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
    render(await response.json());
  } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось загрузить товары."; }
  finally { setLoading(false); }
};

tokenForm.addEventListener("submit", (event) => {event.preventDefault();const token=tokenInput.value.trim();if(!token)return;localStorage.setItem(storageKey,token);tokenInput.value="";loadProducts();});
filters.addEventListener("submit", (event) => {event.preventDefault();loadProducts();});
resetButton.addEventListener("click", () => {filters.reset();loadProducts();});
refreshButton.addEventListener("click", loadProducts);
loadProducts();
