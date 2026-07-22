const storageKey = "leo_crm_service_token";
const merchantStorageKey = "leo_crm_kaspi_merchant_id";
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
const merchantInput = document.querySelector("#merchant-id");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency = "KZT") => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency}`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const stageLabel = (stage) => ({new:"Новый",preorder:"Предзаказ — товар в пути",assembly:"Упаковка",handover:"Передача",shipping:"Передан курьеру / на доставке",delivered:"Доставлен",cancelled:"Отменён",returned:"Возврат",unknown:"Статус не распознан",ACCEPTED_BY_MERCHANT:"Принят продавцом",ASSEMBLY:"Сборка",HANDOVER:"Собран и ждёт передачи",SHIPPING:"Передан курьеру",DELIVERED:"Доставлен",RETURNED:"Возврат",CANCELLED:"Отменён"}[stage] || stage || "—");
const stageClass = (stage) => ["delivered","DELIVERED"].includes(stage) ? "ok" : ["cancelled","returned","CANCELLED","RETURNED"].includes(stage) ? "bad" : ["new","preorder","assembly","handover","shipping","ACCEPTED_BY_MERCHANT","ASSEMBLY","HANDOVER","SHIPPING"].includes(stage) ? "warn" : "";
const rawStatusLabel = (status) => ({new:"new",accepted:"accepted",assembly:"assembly",handover:"handover",shipping:"shipping",delivered:"delivered",cancelled:"cancelled",returned:"returned",unknown:"unknown"}[status] || status || "—");
const procurementLabel = (state) => ({required:"Нужно закупить",in_progress:"Закупка оформлена",received:"Получено",not_required:"Не требуется",unresolved:"Товар не распознан",cancelled:"Закупка отменена"}[state] || state || "—");
const procurementClass = (state) => state === "required" ? "procurement-required" : state === "received" ? "procurement-ready" : "";
const purchaseStatusLabel = (status) => ({draft:"Черновик",requested:"Заявка отправлена",ordered:"Заказано",partially_received:"Получено частично",received:"Получено",closed:"Закрыто",cancelled:"Отменено"}[status] || status || "—");
const timelineLabel = (type) => ({ORDER_ACCEPTED:"Заказ принят продавцом",ORDER_ASSEMBLY_STARTED:"Начата сборка",ORDER_ASSEMBLED:"Заказ собран",ORDER_TRANSFERRED:"Передан курьеру",ORDER_DELIVERED:"Доставлен",ORDER_RETURNED:"Возврат",ORDER_CANCELLED:"Отменён",ORDER_STAGE_CHANGED:"Этап заказа изменён"}[type] || type || "Событие");
const nextPurchaseAction = (status) => ({
  draft: {target:"requested", label:"Отправить заявку"},
  requested: {target:"ordered", label:"Отметить заказанным"},
  ordered: {target:"received", label:"Отметить полученным"},
  partially_received: {target:"received", label:"Отметить полученным"},
  received: {target:"closed", label:"Закрыть закупку"}
}[status] || null);

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

const renderPurchaseAction = (line) => {
  const action = nextPurchaseAction(line.purchase_status);
  if (!action || !line.purchase_request_id || !line.purchase_version) return "";
  return `<button class="button purchase-transition" type="button" data-purchase-id="${escapeHtml(line.purchase_request_id)}" data-version="${Number(line.purchase_version)}" data-target-status="${action.target}">${escapeHtml(action.label)}</button>`;
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
    <div class="${procurementClass(line.procurement_state)}"><span class="muted">Закупка</span><strong>${escapeHtml(procurementLabel(line.procurement_state))}</strong>${line.purchase_status ? `<span class="muted">${escapeHtml(purchaseStatusLabel(line.purchase_status))}</span>` : ""}${renderPurchaseAction(line)}</div>
  </div>`;

const renderOrder = (order) => {
  const stage = order.operational_stage || "unknown";
  const canCreatePurchase = Number(order.procurement_required_lines || 0) > 0 && stage === "preorder";
  const externalCode = order.external_code || order.order_id;
  const kaspiDetailsButton = String(order.marketplace || "").toLowerCase().includes("kaspi")
    ? `<button class="button secondary load-kaspi-details" type="button" data-order-code="${escapeHtml(externalCode)}">Фактический статус Kaspi</button>`
    : "";
  return `
    <article class="order-card" data-order-id="${order.order_id}">
      <div class="order-header">
        <div><span class="order-number">Заказ №${escapeHtml(externalCode)}</span><span class="order-meta">${escapeHtml(order.marketplace)} · ${dateTime(order.ordered_at)}</span></div>
        <div class="order-stat"><span>Этап LEO</span><strong><span class="badge ${stageClass(stage)}">${escapeHtml(stageLabel(stage))}</span></strong><span class="muted">Источник: ${escapeHtml(order.original_status || rawStatusLabel(order.status))}</span></div>
        <div class="order-stat"><span>Единиц</span><strong>${Number(order.units || 0)}</strong></div>
        <div class="order-stat"><span>Сумма</span><strong>${money(order.total_amount, order.currency)}</strong></div>
        <div class="order-stat"><span>Не распознано</span><strong>${Number(order.unresolved_lines || 0)}</strong></div>
        <div class="order-stat"><span>К закупке</span><strong>${Number(order.procurement_required_lines || 0)}</strong></div>
      </div>
      <div class="order-lines">${order.lines.map(renderLine).join("")}</div>
      <div class="kaspi-detail hidden" data-kaspi-detail="${escapeHtml(externalCode)}"></div>
      ${(canCreatePurchase || kaspiDetailsButton) ? `<div class="order-actions">${kaspiDetailsButton}${canCreatePurchase ? `<button class="button create-purchase" type="button" data-order-id="${order.order_id}">Создать заявку на закупку</button>` : ""}</div>` : ""}
    </article>`;
};

const renderTimeline = (events) => {
  if (!events.length) return `<div class="timeline-empty">История изменений пока пуста.</div>`;
  return `<ol class="timeline">${events.map((event) => `<li><span class="timeline-dot"></span><div><strong>${escapeHtml(timelineLabel(event.event_type))}</strong><span>${dateTime(event.occurred_at)}</span><small>${escapeHtml(stageLabel(event.from_stage))} → ${escapeHtml(stageLabel(event.to_stage))}</small></div></li>`).join("")}</ol>`;
};

const renderKaspiDetail = (latest, timeline) => {
  const snapshot = latest.snapshot || {};
  const delivery = snapshot.delivery || {};
  const warehouse = snapshot.warehouse || {};
  const lines = snapshot.lines || [];
  return `
    <div class="kaspi-detail-grid">
      <section>
        <p class="eyebrow">Kaspi Seller Runtime</p>
        <h3>Фактический этап: <span class="badge ${stageClass(latest.stage)}">${escapeHtml(stageLabel(latest.stage))}</span></h3>
        <dl class="fact-grid">
          <div><dt>State</dt><dd>${escapeHtml(latest.state)}</dd></div>
          <div><dt>Status</dt><dd>${escapeHtml(latest.status)}</dd></div>
          <div><dt>Клиент</dt><dd>${escapeHtml(snapshot.customer_name || snapshot.recipient_name || "—")}</dd></div>
          <div><dt>Склад</dt><dd>${escapeHtml([warehouse.name, warehouse.city_name].filter(Boolean).join(", ") || "—")}</dd></div>
          <div><dt>Собран</dt><dd>${delivery.assembled ? "Да" : "Нет"}</dd></div>
          <div><dt>Передан курьеру</dt><dd>${delivery.transmitted_to_courier ? "Да" : "Нет"}</dd></div>
          <div><dt>План передачи</dt><dd>${dateTime(delivery.transmission_planned_at)}</dd></div>
          <div><dt>План доставки</dt><dd>${dateTime(delivery.planned_delivery_at)}</dd></div>
        </dl>
        <div class="snapshot-lines">${lines.map((line) => `<div><strong>${escapeHtml(line.title)}</strong><span>${Number(line.quantity || 0)} шт. · ${money(line.total_price)}</span><small>SKU ${escapeHtml(line.merchant_sku || "—")}</small></div>`).join("")}</div>
      </section>
      <section>
        <p class="eyebrow">Decision Timeline</p>
        ${renderTimeline(timeline.events || [])}
      </section>
    </div>`;
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
  try { const payload = await response.json(); detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || detail); } catch (_) {}
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

const loadKaspiDetails = async (button) => {
  const merchantId = merchantInput.value.trim();
  if (!merchantId) { message.textContent = "Укажите Kaspi Merchant ID в фильтрах."; merchantInput.focus(); return; }
  localStorage.setItem(merchantStorageKey, merchantId);
  const orderCode = button.dataset.orderCode;
  const panel = document.querySelector(`[data-kaspi-detail="${CSS.escape(orderCode)}"]`);
  button.disabled = true; const oldText = button.textContent; button.textContent = "Загрузка…"; message.textContent = "";
  try {
    const query = `merchant_id=${encodeURIComponent(merchantId)}`;
    const [latestResponse, timelineResponse] = await Promise.all([
      fetch(`/api/kaspi-seller/orders/${encodeURIComponent(orderCode)}/latest?${query}`, {headers:headers()}),
      fetch(`/api/kaspi-seller/orders/${encodeURIComponent(orderCode)}/timeline?${query}&limit=100`, {headers:headers()})
    ]);
    if (latestResponse.status === 404) throw new Error("Для заказа ещё нет Kaspi Seller Snapshot. Запустите browser job.");
    if (!latestResponse.ok) throw await responseError(latestResponse);
    if (!timelineResponse.ok) throw await responseError(timelineResponse);
    panel.innerHTML = renderKaspiDetail(await latestResponse.json(), await timelineResponse.json());
    panel.classList.remove("hidden");
    button.textContent = "Скрыть фактический статус";
    button.dataset.loaded = "true";
  } catch (error) {
    message.textContent = error.message || "Не удалось загрузить Kaspi Seller данные.";
    button.textContent = oldText;
  } finally { button.disabled = false; }
};

const toggleKaspiDetails = (button) => {
  const orderCode = button.dataset.orderCode;
  const panel = document.querySelector(`[data-kaspi-detail="${CSS.escape(orderCode)}"]`);
  if (button.dataset.loaded === "true" && !panel.classList.contains("hidden")) {
    panel.classList.add("hidden");
    button.textContent = "Фактический статус Kaspi";
    return;
  }
  if (button.dataset.loaded === "true") {
    panel.classList.remove("hidden");
    button.textContent = "Скрыть фактический статус";
    return;
  }
  loadKaspiDetails(button);
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

const transitionPurchase = async (button) => {
  const purchaseId = button.dataset.purchaseId;
  const targetStatus = button.dataset.targetStatus;
  const version = Number(button.dataset.version);
  button.disabled = true; const oldText = button.textContent; button.textContent = "Сохранение…"; message.textContent = "";
  try {
    const response = await fetch(`/api/purchases/${encodeURIComponent(purchaseId)}/transition`, {
      method:"POST",
      headers:{...headers(), "Content-Type":"application/json"},
      body:JSON.stringify({target_status:targetStatus, expected_version:version, idempotency_key:`orders-center:${purchaseId}:${version}:${targetStatus}`, metadata:{source:"orders-center"}})
    });
    if (!response.ok) throw await responseError(response);
    message.textContent = targetStatus === "received" ? "Товар отмечен полученным. Заказ переведён в упаковку." : "Статус закупки обновлён.";
    await loadOrders();
  } catch (error) {
    message.textContent = error.message || "Не удалось обновить статус закупки.";
    button.disabled = false; button.textContent = oldText;
  }
};

merchantInput.value = localStorage.getItem(merchantStorageKey) || "";
tokenForm.addEventListener("submit", (event) => { event.preventDefault(); localStorage.setItem(storageKey, tokenInput.value.trim()); tokenInput.value = ""; loadOrders(); });
filters.addEventListener("submit", (event) => { event.preventDefault(); localStorage.setItem(merchantStorageKey, merchantInput.value.trim()); loadOrders(); });
resetButton.addEventListener("click", () => { filters.reset(); merchantInput.value = localStorage.getItem(merchantStorageKey) || ""; loadOrders(); });
refreshButton.addEventListener("click", loadOrders);
ordersList.addEventListener("click", (event) => {
  const detailsButton = event.target.closest(".load-kaspi-details");
  if (detailsButton) { toggleKaspiDetails(detailsButton); return; }
  const createButton = event.target.closest(".create-purchase");
  if (createButton) { createPurchase(createButton.dataset.orderId, createButton); return; }
  const transitionButton = event.target.closest(".purchase-transition");
  if (transitionButton) transitionPurchase(transitionButton);
});
loadOrders();