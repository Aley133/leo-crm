const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const ordersPage = document.querySelector("#orders-page");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const filters = document.querySelector("#filters");
const resetButton = document.querySelector("#reset");
const ordersList = document.querySelector("#orders-list");
const empty = document.querySelector("#empty");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency = "KZT") => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency}`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const stageLabel = (stage) => ({new:"Новый",accepted:"Принят — требуется решение по запасу",preorder:"Предзаказ — закупка создана",in_transit:"Закупка в пути",assembly:"В сборке — товар подтверждён",handover:"Передача",shipping:"На доставке",delivered:"Доставлен",cancelled:"Отменён",returned:"Возврат",unknown:"Статус не детализирован"}[stage] || stage || "—");
const stageClass = (stage) => stage === "delivered" ? "ok" : ["cancelled","returned"].includes(stage) ? "bad" : ["new","accepted","preorder","in_transit","assembly","handover","shipping"].includes(stage) ? "warn" : "";
const rawStatusLabel = (status) => ({new:"new",accepted:"accepted",assembly:"assembly",shipping:"shipping",delivered:"delivered",cancelled:"cancelled",returned:"returned",unknown:"unknown"}[status] || status || "—");
const procurementLabel = (state) => ({required:"Нужно решить закупку/остаток",in_progress:"Закупка оформлена",received:"Получено",not_required:"Не требуется",unresolved:"Товар не распознан",cancelled:"Закупка отменена"}[state] || state || "—");
const procurementClass = (state) => state === "required" ? "procurement-required" : state === "received" ? "procurement-ready" : "";

const headers = () => ({"Authorization": `Bearer ${localStorage.getItem(storageKey) || ""}`});
const setLoading = (loading) => {
  ordersPage.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  refreshButton.textContent = loading ? "Обновление…" : "Обновить";
};
const queryString = () => {
  const params = new URLSearchParams({limit:"200"});
  const query = document.querySelector("#search").value.trim();
  const status = document.querySelector("#status").value;
  if (query) params.set("query", query);
  if (status) params.set("status", status);
  return params.toString();
};

const renderSummary = (summary) => {
  document.querySelector("#summary-orders").textContent = Number(summary.orders_count || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-active").textContent = Number(summary.active_orders || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-revenue").textContent = money(summary.revenue || 0);
  document.querySelector("#summary-procurement").textContent = Number(summary.procurement_required_lines || 0).toLocaleString("ru-RU");
};

const renderLine = (line) => `
  <div class="order-line">
    <div>
      ${line.product_id ? `<a class="line-title" href="/crm/products/${line.product_id}">${escapeHtml(line.title)}</a>` : `<strong>${escapeHtml(line.title)}</strong>`}
      <span class="muted">${line.external_product_id ? `Kaspi ${escapeHtml(line.external_product_id)}` : "Без Kaspi ID"}${line.merchant_sku ? ` · SKU ${escapeHtml(line.merchant_sku)}` : ""}</span>
    </div>
    <div><span class="muted">Количество</span><strong>${Number(line.quantity || 0)}</strong></div>
    <div><span class="muted">Цена</span><strong>${money(line.unit_price)}</strong></div>
    <div><span class="muted">Сумма</span><strong>${money(line.line_total)}</strong></div>
    <div class="${procurementClass(line.procurement_state)}"><span class="muted">Закупка/остаток</span><strong>${escapeHtml(procurementLabel(line.procurement_state))}</strong>${line.purchase_status ? `<span class="muted">${escapeHtml(line.purchase_status)}</span>` : ""}</div>
  </div>`;

const renderOrder = (order) => {
  const canCreatePurchase = Number(order.procurement_required_lines || 0) > 0 && order.operational_stage === "accepted";
  const stage = order.operational_stage || "unknown";
  return `
    <article class="order-card" data-order-id="${order.order_id}">
      <div class="order-header">
        <div><span class="order-number">Заказ №${escapeHtml(order.external_code || order.order_id)}</span><span class="order-meta">${escapeHtml(order.marketplace)} · ${dateTime(order.ordered_at)}</span></div>
        <div class="order-stat"><span>Этап LEO</span><strong><span class="badge ${stageClass(stage)}">${escapeHtml(stageLabel(stage))}</span></strong><span class="muted">Kaspi status: ${escapeHtml(order.original_status || rawStatusLabel(order.status))}</span></div>
        <div class="order-stat"><span>Единиц</span><strong>${Number(order.units || 0)}</strong></div>
        <div class="order-stat"><span>Сумма</span><strong>${money(order.total_amount, order.currency)}</strong></div>
        <div class="order-stat"><span>Не распознано</span><strong>${Number(order.unresolved_lines || 0)}</strong></div>
        <div class="order-stat"><span>К решению</span><strong>${Number(order.procurement_required_lines || 0)}</strong></div>
      </div>
      <div class="order-lines">${order.lines.map(renderLine).join("")}</div>
      ${canCreatePurchase ? `<div class="order-actions"><button class="button create-purchase" type="button" data-order-id="${order.order_id}">Создать заявку на закупку</button></div>` : ""}
    </article>`;
};

const render = (payload) => {
  renderSummary(payload.summary || {});
  ordersList.innerHTML = (payload.items || []).map(renderOrder).join("");
  empty.classList.toggle("hidden", (payload.items || []).length > 0);
  document.querySelector("#rows-label").textContent = `Показано заказов: ${(payload.items || []).length} из ${payload.total || 0}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
  authPanel.classList.add("hidden");
  ordersPage.classList.remove("hidden");
};

const responseError = async (response) => {
  let detail = `HTTP ${response.status}`;
  try { const payload = await response.json(); detail = payload.detail || detail; } catch (_) {}
  return new Error(detail);
};

const loadOrders = async () => {
  const token = localStorage.getItem(storageKey);
  if (!token) { authPanel.classList.remove("hidden"); ordersPage.classList.add("hidden"); return; }
  setLoading(true); message.textContent = "";
  try {
    const response = await fetch(`/api/commerce/orders?${queryString()}`, {headers: headers()});
    if (response.status === 401) { localStorage.removeItem(storageKey); throw new Error("Токен не принят. Введите актуальный SERVICE_API_TOKEN."); }
    if (!response.ok) throw await responseError(response);
    render(await response.json());
  } catch (error) {
    message.textContent = error.message || "Не удалось загрузить заказы.";
    if (!localStorage.getItem(storageKey)) authPanel.classList.remove("hidden");
  } finally { setLoading(false); }
};

const createPurchase = async (orderId, button) => {
  button.disabled = true; button.textContent = "Создание…"; message.textContent = "";
  try {
    const response = await fetch("/api/purchases/from-marketplace-order", {
      method:"POST",
      headers:{...headers(), "Content-Type":"application/json"},
      body:JSON.stringify({marketplace_order_id:Number(orderId), idempotency_key:`orders-center:${orderId}`, note:"Создано из Orders Center"})
    });
    if (!response.ok && response.status !== 409) throw await responseError(response);
    message.textContent = "Заявка на закупку создана.";
    await loadOrders();
  } catch (error) {
    message.textContent = error.message || "Не удалось создать заявку на закупку.";
    button.disabled = false; button.textContent = "Создать заявку на закупку";
  }
};

tokenForm.addEventListener("submit", (event) => { event.preventDefault(); localStorage.setItem(storageKey, tokenInput.value.trim()); tokenInput.value = ""; loadOrders(); });
filters.addEventListener("submit", (event) => { event.preventDefault(); loadOrders(); });
resetButton.addEventListener("click", () => { filters.reset(); loadOrders(); });
refreshButton.addEventListener("click", loadOrders);
ordersList.addEventListener("click", (event) => { const button = event.target.closest(".create-purchase"); if (button) createPurchase(button.dataset.orderId, button); });
loadOrders();
