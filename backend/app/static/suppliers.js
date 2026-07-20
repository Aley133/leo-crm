const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#suppliers");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const filters = document.querySelector("#filters");
const resetButton = document.querySelector("#reset");
const body = document.querySelector("#suppliers-body");
const empty = document.querySelector("#empty");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU")} ${currency || ""}`.trim();
const checkedAt = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "Никогда";

const queryString = () => {
  const params = new URLSearchParams({limit:"200"});
  const q = document.querySelector("#search").value.trim();
  const supplier = document.querySelector("#supplier-code").value.trim();
  const availability = document.querySelector("#availability").value;
  if (q) params.set("q", q);
  if (supplier) params.set("supplier_code", supplier.toLowerCase());
  if (availability) params.set("availability", availability);
  if (document.querySelector("#only-unbound").checked) params.set("only_unbound", "true");
  if (document.querySelector("#only-failures").checked) params.set("only_failures", "true");
  return params.toString();
};

const render = (rows) => {
  body.innerHTML = rows.map((row) => `
    <tr>
      <td><a class="product-title" href="${escapeHtml(row.supplier_product_url)}" target="_blank" rel="noreferrer">${escapeHtml(row.supplier_product_title)}</a><span class="muted">ID ${escapeHtml(row.external_id)}</span></td>
      <td><strong>${escapeHtml(row.supplier_name)}</strong><span class="muted">${escapeHtml(row.supplier_code)}</span></td>
      <td>${money(row.price, row.currency)}</td>
      <td>${row.delivery_days == null ? "—" : `${row.delivery_days} дн.`}</td>
      <td>${row.available === true ? '<span class="badge ok">В наличии</span>' : row.available === false ? '<span class="badge bad">Нет</span>' : '<span class="badge">Неизвестно</span>'}</td>
      <td>${row.product_id ? `<a href="/crm/products/${row.product_id}">${escapeHtml(row.kaspi_product_name)}</a><span class="muted">Kaspi ${escapeHtml(row.kaspi_product_id)}</span>` : '<span class="badge bad">Не привязано</span>'}</td>
      <td><span class="badge">${escapeHtml(row.binding_status || "нет")}</span>${row.is_primary ? '<span class="muted">основной</span>' : ''}${row.confidence_score != null ? `<span class="muted">совпадение ${row.confidence_score}%</span>` : ''}${row.consecutive_failures ? `<span class="muted">ошибок: ${row.consecutive_failures}</span>` : ''}</td>
      <td>${checkedAt(row.last_checked_at)}</td>
    </tr>`).join("");
  empty.classList.toggle("hidden", rows.length > 0);
  document.querySelector("#rows-label").textContent = `Показано предложений: ${rows.length}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
  authPanel.classList.add("hidden"); page.classList.remove("hidden");
};

const load = async () => {
  const token = localStorage.getItem(storageKey);
  if (!token) { authPanel.classList.remove("hidden"); page.classList.add("hidden"); return; }
  refreshButton.disabled = true; message.textContent = "";
  try {
    const response = await fetch(`/api/catalog/supplier-offers?${queryString()}`, {headers:{Authorization:`Bearer ${token}`},cache:"no-store"});
    if (response.status === 401) { localStorage.removeItem(storageKey); authPanel.classList.remove("hidden"); page.classList.add("hidden"); message.textContent = "Токен не принят."; return; }
    if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
    render(await response.json());
  } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось загрузить поставщиков."; }
  finally { refreshButton.disabled = false; }
};

tokenForm.addEventListener("submit", (event) => {event.preventDefault();const token=tokenInput.value.trim();if(!token)return;localStorage.setItem(storageKey,token);tokenInput.value="";load();});
filters.addEventListener("submit", (event) => {event.preventDefault();load();});
resetButton.addEventListener("click", () => {filters.reset();load();});
refreshButton.addEventListener("click", load);
load();
