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
  const supplier = document.querySelector("#supplier-code").value.trim();
  const availability = document.querySelector("#availability").value;
  if (q) params.set("q", q);
  if (supplier) params.set("supplier_code", supplier.toLowerCase());
  if (availability) params.set("availability", availability);
  if (document.querySelector("#only-stale").checked) params.set("only_stale", "true");
  if (document.querySelector("#only-failures").checked) params.set("only_failures", "true");
  return params.toString();
};

const statusBadge = (row) => {
  if (row.consecutive_failures > 0) return '<span class="badge bad">Ошибка</span>';
  if (row.is_stale) return '<span class="badge warn">Устарело</span>';
  if (row.monitor_status === "active") return '<span class="badge ok">Активен</span>';
  return `<span class="badge">${escapeHtml(row.monitor_status || "Не настроен")}</span>`;
};

const render = (rows) => {
  body.innerHTML = rows.map((row) => `
    <tr data-product-id="${row.product_id}">
      <td><a class="product-title" href="/crm/products/${row.product_id}">${escapeHtml(row.product_name)}</a><span class="muted">Kaspi ${escapeHtml(row.kaspi_product_id)}${row.brand ? ` · ${escapeHtml(row.brand)}` : ""}</span></td>
      <td><a href="${escapeHtml(row.supplier_product_url)}" target="_blank" rel="noreferrer">${escapeHtml(row.supplier_name)}</a><span class="muted">${escapeHtml(row.supplier_code)}${row.is_primary ? " · основной" : ""}</span></td>
      <td>${money(row.price, row.currency)}${row.old_price != null ? `<span class="muted">было ${money(row.old_price, row.currency)}</span>` : ""}</td>
      <td>${row.delivery_days == null ? "—" : `${row.delivery_days} дн.`}</td>
      <td>${row.available === true ? '<span class="badge ok">В наличии</span>' : row.available === false ? '<span class="badge bad">Нет</span>' : '<span class="badge">Неизвестно</span>'}${row.stock != null ? `<span class="muted">остаток ${row.stock}</span>` : ""}</td>
      <td>${statusBadge(row)}${row.consecutive_failures ? `<span class="muted">ошибок подряд: ${row.consecutive_failures}</span>` : ""}</td>
      <td>${checkedAt(row.last_checked_at)}</td>
    </tr>`).join("");
  empty.classList.toggle("hidden", rows.length > 0);
  document.querySelector("#rows-label").textContent = `Показано строк: ${rows.length}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
  authPanel.classList.add("hidden");
  productsPage.classList.remove("hidden");
};

const loadProducts = async () => {
  const token = localStorage.getItem(storageKey);
  if (!token) {
    authPanel.classList.remove("hidden");
    productsPage.classList.add("hidden");
    return;
  }
  setLoading(true);
  message.textContent = "";
  try {
    const response = await fetch(`/api/supplier-state/offers?${queryString()}`, {headers:{Authorization:`Bearer ${token}`},cache:"no-store"});
    if (response.status === 401) {
      localStorage.removeItem(storageKey);
      authPanel.classList.remove("hidden");
      productsPage.classList.add("hidden");
      message.textContent = "Токен не принят. Проверьте SERVICE_API_TOKEN.";
      return;
    }
    if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
    render(await response.json());
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось загрузить товары.";
  } finally { setLoading(false); }
};

tokenForm.addEventListener("submit", (event) => {event.preventDefault();const token=tokenInput.value.trim();if(!token)return;localStorage.setItem(storageKey,token);tokenInput.value="";loadProducts();});
filters.addEventListener("submit", (event) => {event.preventDefault();loadProducts();});
resetButton.addEventListener("click", () => {filters.reset();loadProducts();});
refreshButton.addEventListener("click", loadProducts);
loadProducts();
