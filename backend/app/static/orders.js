const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const ordersPage = document.querySelector("#orders-page");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const rebuildButton = document.querySelector("#rebuild-orders");
const filters = document.querySelector("#filters");
const resetButton = document.querySelector("#reset");
const ordersList = document.querySelector("#orders-list");
const empty = document.querySelector("#empty");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency = "KZT") => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency}`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const stageLabel = (stage) => ({new:"Новый",accepted:"Принят",preorder:"Предзаказ",assembly:"Упаковка",handover:"Передача",shipping:"Переданы на доставку",delivered:"Завершён",cancelled:"Отменён при доставке",returned:"Возврат",unknown:"Прочее"}[stage] || stage || "—");
const stageClass = (stage) => stage === "delivered" ? "ok" : ["cancelled","returned"].includes(stage) ? "bad" : "warn";
const procurementLabel = (state) => ({required:"Нужно закупить",in_progress:"Закупка оформлена",received:"Получено",not_required:"Закупка не требуется",cancelled:"Закупка отменена"}[state] || state || "—");
const purchaseStatusLabel = (status) => ({draft:"Черновик",requested:"Заявка отправлена",ordered:"Заказано",partially_received:"Получено частично",received:"Получено",closed:"Закрыто",cancelled:"Отменено"}[status] || status || "—");
const nextPurchaseAction = (status) => ({draft:{target:"requested",label:"Отправить заявку"},requested:{target:"ordered",label:"Отметить заказанным"},ordered:{target:"received",label:"Отметить полученным"},partially_received:{target:"received",label:"Отметить полученным"},received:{target:"closed",label:"Закрыть закупку"}}[status] || null);
const headers = () => ({"Authorization": `Bearer ${localStorage.getItem(storageKey) || ""}`});

const setLoading = (loading) => {
  ordersPage.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  rebuildButton.disabled = loading;
  refreshButton.textContent = loading ? "Обновление…" : "Обновить экран";
};

const queryString = () => {
  const params = new URLSearchParams({limit:"200"});
  const query = document.querySelector("#search").value.trim();
  const status = document.querySelector("#status").value;
  if (query) params.set("query", query);
  if (status) params.set("status", status);
  return params.toString();
};

const responseError = async (response) => {
  let detail = `HTTP ${response.status}`;
  try { const payload = await response.json(); detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || detail); } catch (_) {}
  return new Error(detail);
};

const renderPurchaseAction = (line) => {
  const action = nextPurchaseAction(line.purchase_status);
  if (!action || !line.purchase_request_id || !line.purchase_version) return "";
  return `<button class="button purchase-transition" type="button" data-purchase-id="${escapeHtml(line.purchase_request_id)}" data-version="${Number(line.purchase_version)}" data-target-status="${action.target}">${escapeHtml(action.label)}</button>`;
};

const renderLine = (line) => `<div class="order-line"><div>${line.product_id ? `<a class="line-title" href="/crm/products/${line.product_id}">${escapeHtml(line.title)}</a>` : `<strong>${escapeHtml(line.title)}</strong>`}<span class="muted">${line.external_product_id ? `Kaspi ${escapeHtml(line.external_product_id)}` : "Без Kaspi ID"}${line.merchant_sku ? ` · SKU ${escapeHtml(line.merchant_sku)}` : ""}</span></div><div><span class="muted">Количество</span><strong>${Number(line.quantity || 0)}</strong></div><div><span class="muted">Цена</span><strong>${money(line.unit_price)}</strong></div><div><span class="muted">Сумма</span><strong>${money(line.line_total)}</strong></div><div><span class="muted">Закупка</span><strong>${escapeHtml(procurementLabel(line.procurement_state))}</strong>${line.purchase_status ? `<span class="muted">${escapeHtml(purchaseStatusLabel(line.purchase_status))}</span>` : ""}${renderPurchaseAction(line)}</div></div>`;

const renderOrder = (order) => {
  const stage = order.operational_stage || "unknown";
  const externalCode = order.external_code || order.order_id;
  const canCreatePurchase = Number(order.procurement_required_lines || 0) > 0 && stage === "preorder";
  return `<article class="order-card" data-order-id="${order.order_id}"><div class="order-header"><div><span class="order-number">Заказ №${escapeHtml(externalCode)}</span><span class="order-meta">${escapeHtml(order.marketplace)} · кабинет ${escapeHtml(order.marketplace_external_account_id)} · ${dateTime(order.ordered_at)}</span></div><div class="order-stat"><span>Этап LEO</span><strong><span class="badge ${stageClass(stage)}">${escapeHtml(stageLabel(stage))}</span></strong><span class="muted">Источник: Kaspi Orders API</span></div><div class="order-stat"><span>Единиц</span><strong>${Number(order.units || 0)}</strong></div><div class="order-stat"><span>Сумма</span><strong>${money(order.total_amount, order.currency)}</strong></div><div class="order-stat"><span>Не привязано к товару</span><strong>${Number(order.unresolved_lines || 0)}</strong></div><div class="order-stat"><span>К закупке</span><strong>${Number(order.procurement_required_lines || 0)}</strong></div></div><div class="order-lines">${order.lines.map(renderLine).join("")}</div>${canCreatePurchase ? `<div class="order-actions"><button class="button create-purchase" type="button" data-order-id="${order.order_id}">Создать заявку на закупку</button></div>` : ""}</article>`;
};

const render = (payload) => {
  const summary = payload.summary || {};
  document.querySelector("#summary-orders").textContent = Number(summary.orders_count || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-active").textContent = Number(summary.active_orders || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-revenue").textContent = money(summary.revenue || 0);
  document.querySelector("#summary-procurement").textContent = Number(summary.procurement_required_lines || 0).toLocaleString("ru-RU");
  ordersList.innerHTML = (payload.items || []).map(renderOrder).join("");
  empty.classList.toggle("hidden", (payload.items || []).length > 0);
  document.querySelector("#rows-label").textContent = `Показано заказов: ${(payload.items || []).length} из ${payload.total || 0}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
  authPanel.classList.add("hidden");
  ordersPage.classList.remove("hidden");
};

const loadOrders = async () => {
  if (!localStorage.getItem(storageKey)) { authPanel.classList.remove("hidden"); ordersPage.classList.add("hidden"); return; }
  setLoading(true);
  try {
    const response = await fetch(`/api/commerce/orders?${queryString()}`, {headers:headers()});
    if (response.status === 401) { localStorage.removeItem(storageKey); throw new Error("Токен не принят. Введите актуальный SERVICE_API_TOKEN."); }
    if (!response.ok) throw await responseError(response);
    render(await response.json());
  } catch (error) { message.textContent = error.message || "Не удалось загрузить заказы."; }
  finally { setLoading(false); }
};

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

const pollRebuildJob = async (jobId) => {
  while (true) {
    const response = await fetch(`/api/commerce/orders/rebuild/${encodeURIComponent(jobId)}`, {headers:headers()});
    if (!response.ok) throw await responseError(response);
    const job = await response.json();
    const progress = job.progress || {};
    message.textContent = `${job.message || job.status}. Прогресс: ${Number(progress.percent || 0)}%. Заказов: ${Number(job.orders_count || 0)}. Запросов: ${Number(job.request_count || 0)}. Ошибок диапазонов: ${(job.errors || []).length}.`;
    if (["completed", "completed_with_errors", "failed"].includes(job.status)) return job;
    await sleep(1200);
  }
};

const rebuildOrders = async () => {
  rebuildButton.disabled = true;
  refreshButton.disabled = true;
  rebuildButton.textContent = "Загрузка…";
  message.textContent = "Создаю фоновую выгрузку Kaspi за 7 дней.";
  try {
    const response = await fetch("/api/commerce/orders/rebuild?days=7", {method:"POST", headers:headers()});
    if (!response.ok) throw await responseError(response);
    const started = await response.json();
    const result = await pollRebuildJob(started.job_id);
    if (result.status === "failed") throw new Error(result.message || "Kaspi raw receiver завершился с ошибкой.");
    message.textContent = `Готово. Уникальных заказов: ${Number(result.orders_count || 0)}, добавлено: ${Number(result.imported_count || 0)}, обновлено: ${Number(result.updated_count || 0)}, частичных ошибок: ${(result.errors || []).length}.`;
    filters.reset();
    await loadOrders();
  } catch (error) { message.textContent = error.message || "Не удалось загрузить заказы Kaspi."; }
  finally {
    rebuildButton.disabled = false;
    refreshButton.disabled = false;
    rebuildButton.textContent = "Загрузить заказы Kaspi";
  }
};

const createPurchase = async (orderId, button) => {
  button.disabled = true;
  try {
    const response = await fetch("/api/purchases/from-marketplace-order", {method:"POST",headers:{...headers(),"Content-Type":"application/json"},body:JSON.stringify({marketplace_order_id:Number(orderId),idempotency_key:`orders-center:${orderId}`,note:"Создано из Orders Center"})});
    if (!response.ok && response.status !== 409) throw await responseError(response);
    await loadOrders();
  } catch (error) { message.textContent = error.message || "Не удалось создать заявку на закупку."; button.disabled = false; }
};

const transitionPurchase = async (button) => {
  const purchaseId = button.dataset.purchaseId;
  const targetStatus = button.dataset.targetStatus;
  const version = Number(button.dataset.version);
  button.disabled = true;
  try {
    const response = await fetch(`/api/purchases/${encodeURIComponent(purchaseId)}/transition`, {method:"POST",headers:{...headers(),"Content-Type":"application/json"},body:JSON.stringify({target_status:targetStatus,expected_version:version,idempotency_key:`orders-center:${purchaseId}:${version}:${targetStatus}`,metadata:{source:"orders-center"}})});
    if (!response.ok) throw await responseError(response);
    await loadOrders();
  } catch (error) { message.textContent = error.message || "Не удалось обновить статус закупки."; button.disabled = false; }
};

tokenForm.addEventListener("submit", (event) => { event.preventDefault(); localStorage.setItem(storageKey, tokenInput.value.trim()); tokenInput.value = ""; loadOrders(); });
filters.addEventListener("submit", (event) => { event.preventDefault(); loadOrders(); });
resetButton.addEventListener("click", () => { filters.reset(); loadOrders(); });
refreshButton.addEventListener("click", loadOrders);
rebuildButton.addEventListener("click", rebuildOrders);
ordersList.addEventListener("click", (event) => {
  const createButton = event.target.closest(".create-purchase");
  if (createButton) { createPurchase(createButton.dataset.orderId, createButton); return; }
  const transitionButton = event.target.closest(".purchase-transition");
  if (transitionButton) transitionPurchase(transitionButton);
});
loadOrders();
