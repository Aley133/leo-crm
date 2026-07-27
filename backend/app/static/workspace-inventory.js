const SESSION_KEY = "leo_workspace_session";
const token = localStorage.getItem(SESSION_KEY);
if (!token) window.location.replace("/login");

const headers = (json = false) => ({Authorization: `Bearer ${token || ""}`, ...(json ? {"Content-Type":"application/json"} : {})});
const message = document.querySelector("#message");
const list = document.querySelector("#inventory-list");
const empty = document.querySelector("#empty");
const dialog = document.querySelector("#batch-dialog");
const form = document.querySelector("#batch-form");
let products = [];

const money = (value) => `${Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:2})} ₸`;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const readError = async (response) => { try { const body = await response.json(); return typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`; } catch (_) { return `HTTP ${response.status}`; } };

const api = async (url, options = {}) => {
  const response = await fetch(url, {...options, headers: {...headers(Boolean(options.body)), ...(options.headers || {})}, cache:"no-store"});
  if (response.status === 401) { localStorage.removeItem(SESSION_KEY); window.location.replace("/login"); throw new Error("Сессия завершена"); }
  if (!response.ok) throw new Error(await readError(response));
  if (response.status === 204) return null;
  return response.json();
};

const renderBatch = (batch, productId) => `<div class="order-line"><div><strong>${new Date(batch.received_at).toLocaleDateString("ru-RU")}</strong><span class="muted">${escapeHtml(batch.source_name || "Источник не указан")}</span></div><div><span class="muted">Принято</span><strong>${Number(batch.quantity_received)}</strong></div><div><span class="muted">Остаток</span><strong>${Number(batch.quantity_remaining)}</strong></div><div><span class="muted">Распределено</span><strong>${Number(batch.quantity_allocated)}</strong></div><div><span class="muted">Себестоимость</span><strong>${money(batch.unit_cost)}</strong>${batch.reference ? `<span class="muted">${escapeHtml(batch.reference)}</span>` : ""}<button class="button secondary delete-batch" type="button" data-product-id="${productId}" data-batch-id="${batch.id}">Удалить</button></div></div>`;

const render = () => {
  const query = document.querySelector("#search").value.trim().toLowerCase();
  const filtered = products.filter((item) => !query || [item.name, item.kaspi_product_id, item.merchant_sku].some((value) => String(value || "").toLowerCase().includes(query)));
  list.innerHTML = filtered.map((item) => `<article class="order-card"><div class="order-header"><div><span class="order-number">${escapeHtml(item.name)}</span><span class="order-meta">Kaspi ID ${escapeHtml(item.kaspi_product_id)}${item.merchant_sku ? ` · SKU ${escapeHtml(item.merchant_sku)}` : ""}</span></div><div class="order-stat"><span>Остаток</span><strong>${Number(item.inventory.on_hand)}</strong></div><div class="order-stat"><span>Принято</span><strong>${Number(item.inventory.received_total)}</strong></div><div class="order-stat"><span>Распределено</span><strong>${Number(item.inventory.allocated_total)}</strong></div><div class="order-stat"><button class="button add-batch" type="button" data-product-id="${item.product_id}" data-product-name="${escapeHtml(item.name)}">Добавить партию</button></div></div><div class="order-lines">${item.inventory.batches.length ? item.inventory.batches.map((batch) => renderBatch(batch, item.product_id)).join("") : '<div class="empty">Партий пока нет.</div>'}</div></article>`).join("");
  empty.classList.toggle("hidden", filtered.length > 0);
  document.querySelector("#rows-label").textContent = `Показано товаров: ${filtered.length}`;
  document.querySelector("#summary-products").textContent = products.length;
  document.querySelector("#summary-on-hand").textContent = products.reduce((sum, item) => sum + Number(item.inventory.on_hand || 0), 0);
  document.querySelector("#summary-received").textContent = products.reduce((sum, item) => sum + Number(item.inventory.received_total || 0), 0);
  document.querySelector("#summary-allocated").textContent = products.reduce((sum, item) => sum + Number(item.inventory.allocated_total || 0), 0);
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit"})}`;
};

const load = async () => {
  message.textContent = "Загружаю склад…";
  try {
    const me = await api("/api/auth/me");
    document.querySelector("#workspace-label").textContent = me.workspace_slug || "Workspace";
    const connection = await api("/api/workspace/kaspi");
    document.querySelector("#shop-title").textContent = `Склад и FIFO — ${connection.shop_name || me.workspace_slug}`;
    const rows = await api("/api/workspace/products?limit=500");
    products = await Promise.all(rows.map(async (product) => ({...product, inventory: await api(`/api/workspace/inventory/${product.product_id}`)})));
    render();
    message.textContent = "";
  } catch (error) { message.textContent = error.message || "Не удалось загрузить склад."; }
};

list.addEventListener("click", async (event) => {
  const add = event.target.closest(".add-batch");
  if (add) {
    document.querySelector("#batch-product-id").value = add.dataset.productId;
    document.querySelector("#batch-title").textContent = `Новая партия — ${add.dataset.productName}`;
    form.reset();
    document.querySelector("#batch-product-id").value = add.dataset.productId;
    dialog.showModal();
    return;
  }
  const remove = event.target.closest(".delete-batch");
  if (remove && confirm("Удалить эту партию и пересчитать FIFO?")) {
    try { await api(`/api/workspace/inventory/${remove.dataset.productId}/batches/${remove.dataset.batchId}`, {method:"DELETE"}); await load(); }
    catch (error) { message.textContent = error.message || "Не удалось удалить партию."; }
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const productId = document.querySelector("#batch-product-id").value;
  try {
    await api(`/api/workspace/inventory/${productId}/batches`, {method:"POST", body:JSON.stringify({quantity:Number(document.querySelector("#batch-quantity").value), unit_cost:Number(document.querySelector("#batch-unit-cost").value), source_name:document.querySelector("#batch-source").value.trim() || null, reference:document.querySelector("#batch-reference").value.trim() || null, note:document.querySelector("#batch-note").value.trim() || null, reconcile_existing_orders:true})});
    dialog.close();
    await load();
  } catch (error) { message.textContent = error.message || "Не удалось добавить партию."; }
});

document.querySelector("#batch-close").addEventListener("click", () => dialog.close());
document.querySelector("#batch-cancel").addEventListener("click", () => dialog.close());
document.querySelector("#filters").addEventListener("submit", (event) => { event.preventDefault(); render(); });
document.querySelector("#reset").addEventListener("click", () => { document.querySelector("#search").value = ""; render(); });
document.querySelector("#refresh").addEventListener("click", load);
document.querySelector("#logout").addEventListener("click", async () => { try { await fetch("/api/auth/logout", {method:"POST", headers:headers()}); } catch (_) {} localStorage.removeItem(SESSION_KEY); window.location.replace("/login"); });

load();
