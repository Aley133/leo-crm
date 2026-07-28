const storageKey = "leo_workspace_session";
const ordersPage = document.querySelector("#orders-page");
const message = document.querySelector("#message");
const refreshButton = document.querySelector("#refresh");
const rebuildButton = document.querySelector("#rebuild-orders");
const captureRevenueButton = document.querySelector("#capture-revenue");
const rebuildDays = document.querySelector("#rebuild-days");
const filters = document.querySelector("#filters");
const resetButton = document.querySelector("#reset");
const ordersList = document.querySelector("#orders-list");
const empty = document.querySelector("#empty");

if (!localStorage.getItem(storageKey)) window.location.replace("/login");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency = "KZT") => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency}`;
const percent = (value) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})}%`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const stageLabel = (stage) => ({new:"Новый",accepted:"Принят",preorder:"Предзаказ",assembly:"Упаковка",handover:"Передача",shipping:"Передан в доставку",cancelling:"Отмена в процессе",delivered:"Завершён",cancelled:"Отменён",returned:"Возврат",unknown:"Прочее"}[stage] || stage || "—");
const stageClass = (stage) => stage === "delivered" ? "ok" : ["cancelling","cancelled","returned"].includes(stage) ? "bad" : "warn";
const procurementLabel = (state) => ({required:"Нужно закупить",in_progress:"Закупка оформлена",received:"Получено",not_required:"Закупка не требуется",cancelled:"Закупка отменена"}[state] || state || "—");
const purchaseStatusLabel = (status) => ({draft:"Черновик",requested:"Заявка отправлена",ordered:"Заказано",partially_received:"Получено частично",received:"Получено",closed:"Закрыто",cancelled:"Отменено"}[status] || status || "—");
const nextPurchaseAction = (status) => ({draft:{target:"requested",label:"Отправить заявку",loading:"Отправляю…"},requested:{target:"ordered",label:"Отметить заказанным",loading:"Сохраняю…"},ordered:{target:"received",label:"Отметить полученным",loading:"Принимаю…"},partially_received:{target:"received",label:"Отметить полученным",loading:"Принимаю…"},received:{target:"closed",label:"Закрыть закупку",loading:"Закрываю…"}}[status] || null);
const headers = (json = false) => ({Authorization:`Bearer ${localStorage.getItem(storageKey) || ""}`,...(json?{"Content-Type":"application/json"}:{})});

const responseError = async (response) => {
  let detail = `HTTP ${response.status}`;
  try { const payload = await response.json(); detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || detail); } catch (_) {}
  return new Error(detail);
};

const setLoading = (loading) => {
  ordersPage.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  rebuildButton.disabled = loading;
  captureRevenueButton.disabled = loading;
  rebuildDays.disabled = loading;
  refreshButton.textContent = loading ? "Обновление…" : "Обновить экран";
};

const setButtonBusy = (button, loadingText) => {
  button.dataset.originalLabel ||= button.textContent.trim();
  button.disabled = true;
  button.classList.add("is-loading");
  button.setAttribute("aria-busy", "true");
  button.innerHTML = `<span class="button-spinner" aria-hidden="true"></span><span>${escapeHtml(loadingText)}</span>`;
};
const restoreButton = (button) => {
  button.disabled = false;
  button.classList.remove("is-loading");
  button.removeAttribute("aria-busy");
  button.textContent = button.dataset.originalLabel || "Повторить";
};

const queryString = () => {
  const params = new URLSearchParams({limit:"200"});
  const query = document.querySelector("#search").value.trim();
  const status = document.querySelector("#status").value;
  if (query) params.set("query", query);
  if (status) params.set("status", status);
  return params.toString();
};

const renderPurchaseAction = (line) => {
  const action = nextPurchaseAction(line.purchase_status);
  if (!action || !line.purchase_request_id || !line.purchase_version) return "";
  return `<button class="button purchase-transition" type="button" data-purchase-id="${escapeHtml(line.purchase_request_id)}" data-version="${Number(line.purchase_version)}" data-target-status="${action.target}" data-loading-label="${escapeHtml(action.loading)}">${escapeHtml(action.label)}</button>`;
};

const renderLine = (line) => {
  const identity = [line.merchant_sku ? `Артикул ${escapeHtml(line.merchant_sku)}` : null, line.external_product_id ? `Kaspi ID ${escapeHtml(line.external_product_id)}` : null].filter(Boolean).join(" · ") || "Идентификатор не получен";
  const title = line.product_id ? `<a class="line-title" href="/crm/workspace/products/${line.product_id}">${escapeHtml(line.title)}</a>` : `<strong>${escapeHtml(line.title)}</strong>`;
  const cost = line.procurement_unit_cost == null ? "—" : money(line.procurement_unit_cost);
  const source = line.procurement_source_name ? escapeHtml(line.procurement_source_name) : "Источник не выбран";
  const netProfit = line.net_profit == null ? "—" : `${money(line.net_profit)} · ${percent(line.net_margin_pct)}`;
  const fees = `Комиссия ${money(line.kaspi_commission)} · налог ${money(line.tax)} · логистика ${money(line.logistics)}`;
  return `<div class="order-line"><div>${title}<span class="muted">${identity}</span></div><div><span class="muted">Количество</span><strong>${Number(line.quantity || 0)}</strong></div><div><span class="muted">Цена продажи</span><strong>${money(line.unit_price)}</strong></div><div><span class="muted">Закупочная цена</span><strong>${cost}</strong><span class="muted">${source}</span></div><div><span class="muted">Чистая прибыль</span><strong>${netProfit}</strong><span class="muted">${fees}</span><span class="muted">${escapeHtml(procurementLabel(line.procurement_state))}</span>${line.purchase_status ? `<span class="muted">${escapeHtml(purchaseStatusLabel(line.purchase_status))}</span>` : ""}${renderPurchaseAction(line)}</div></div>`;
};

const renderOrder = (order) => {
  const stage = order.operational_stage || "unknown";
  const externalCode = order.external_code || order.order_id;
  const canCreatePurchase = Number(order.procurement_required_lines || 0) > 0 && stage === "preorder";
  const bindingText = Number(order.unresolved_lines || 0) === 0 ? "Товары привязаны" : `Не привязано: ${Number(order.unresolved_lines || 0)}`;
  return `<article class="order-card" data-order-id="${order.order_id}"><div class="order-header"><div><span class="order-number">Заказ №${escapeHtml(externalCode)}</span><span class="order-meta">${escapeHtml(order.marketplace)} · кабинет ${escapeHtml(order.marketplace_external_account_id)} · ${dateTime(order.ordered_at)}</span></div><div class="order-stat"><span>Этап LEO</span><strong><span class="badge ${stageClass(stage)}">${escapeHtml(stageLabel(stage))}</span></strong><span class="muted">Kaspi Orders API</span></div><div class="order-stat"><span>Единиц</span><strong>${Number(order.units || 0)}</strong></div><div class="order-stat"><span>Сумма заказа</span><strong>${money(order.total_amount, order.currency)}</strong></div><div class="order-stat"><span>Связь с каталогом</span><strong>${escapeHtml(bindingText)}</strong></div></div><div class="order-lines">${(order.lines || []).map(renderLine).join("")}</div>${canCreatePurchase ? `<div class="order-actions"><button class="button create-purchase" type="button" data-order-id="${order.order_id}">Создать заявку на закупку</button></div>` : ""}</article>`;
};

const updateSummary = (summary = {}) => {
  document.querySelector("#summary-orders").textContent = Number(summary.orders_count || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-active").textContent = Number(summary.active_orders || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-revenue").textContent = money(summary.revenue || 0);
  document.querySelector("#summary-profit").textContent = money(summary.confirmed_net_profit || 0);
  document.querySelector("#summary-profit-units").textContent = `по ${Number(summary.confirmed_profit_units || 0).toLocaleString("ru-RU")} ед. с подтверждённой себестоимостью`;
  document.querySelector("#summary-procurement").textContent = Number(summary.procurement_required_lines || 0).toLocaleString("ru-RU");
};

const fetchOrdersPayload = async () => {
  const response = await fetch(`/api/workspace/orders?${queryString()}`, {headers:headers(), cache:"no-store"});
  if (response.status === 401) { localStorage.removeItem(storageKey); window.location.replace("/login"); throw new Error("Сессия истекла"); }
  if (!response.ok) throw await responseError(response);
  return response.json();
};

const render = (payload) => {
  updateSummary(payload.summary || {});
  ordersList.innerHTML = (payload.items || []).map(renderOrder).join("");
  empty.classList.toggle("hidden", (payload.items || []).length > 0);
  document.querySelector("#rows-label").textContent = `Показано заказов: ${(payload.items || []).length} из ${payload.total || 0}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
};

const loadContext = async () => {
  const [meResponse, kaspiResponse] = await Promise.all([
    fetch("/api/auth/me", {headers:headers(), cache:"no-store"}),
    fetch("/api/workspace/kaspi", {headers:headers(), cache:"no-store"}),
  ]);
  if (meResponse.status === 401 || kaspiResponse.status === 401) { localStorage.removeItem(storageKey); window.location.replace("/login"); return; }
  if (meResponse.ok) document.querySelector("#workspace-label").textContent = (await meResponse.json()).username || "Workspace";
  if (kaspiResponse.ok) {
    const kaspi = await kaspiResponse.json();
    if (kaspi.configured) document.querySelector("h1").textContent = `Заказы — ${kaspi.shop_name}`;
  }
};

const loadOrders = async () => {
  setLoading(true); message.textContent = "";
  try { render(await fetchOrdersPayload()); }
  catch (error) { message.textContent = error.message || "Не удалось загрузить заказы."; }
  finally { setLoading(false); }
};

const refreshSingleOrder = async (orderId, currentCard) => {
  const scrollTop = window.scrollY;
  const payload = await fetchOrdersPayload();
  updateSummary(payload.summary || {});
  const order = (payload.items || []).find((item) => Number(item.order_id) === Number(orderId));
  if (!order) currentCard?.remove();
  else { const wrapper = document.createElement("div"); wrapper.innerHTML = renderOrder(order); currentCard?.replaceWith(wrapper.firstElementChild); }
  requestAnimationFrame(() => window.scrollTo({top:scrollTop,left:0,behavior:"instant"}));
};

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const pollImport = async (jobId) => {
  while (true) {
    const response = await fetch(`/api/workspace/kaspi/import/${encodeURIComponent(jobId)}`, {headers:headers(), cache:"no-store"});
    if (!response.ok) throw await responseError(response);
    const job = await response.json();
    const progress = job.progress || {};
    message.textContent = `${job.message || job.status}. Прогресс: ${Number(progress.percent || 0)}%. Заказов: ${Number(job.orders_count || 0)}. Запросов: ${Number(job.request_count || 0)}.`;
    if (["completed","completed_with_errors","failed","succeeded","partial"].includes(job.status)) return job;
    await sleep(1200);
  }
};

const rebuildOrders = async (daysOverride = null, preserveFilters = false) => {
  const days = Number(daysOverride || rebuildDays.value || 7);
  setLoading(true); rebuildButton.textContent = "Загрузка…"; message.textContent = `Загружаю свежие заказы Kaspi за ${days} дн.`;
  try {
    const response = await fetch(`/api/workspace/kaspi/import?days=${days}`, {method:"POST",headers:headers()});
    if (!response.ok) throw await responseError(response);
    const started = await response.json();
    const result = await pollImport(started.job_id);
    if (result.status === "failed") throw new Error(result.message || "Импорт завершился с ошибкой");
    message.textContent = result.message || "Заказы загружены";
    if (!preserveFilters) filters.reset();
    await loadOrders();
  } catch (error) { message.textContent = error.message || "Не удалось загрузить заказы Kaspi."; }
  finally { setLoading(false); rebuildButton.textContent = "Загрузить заказы Kaspi"; }
};

const createPurchase = async (orderId, button) => {
  setButtonBusy(button, "Создаю…");
  try {
    const response = await fetch("/api/workspace/purchases/from-marketplace-order", {method:"POST",headers:headers(true),body:JSON.stringify({marketplace_order_id:Number(orderId),idempotency_key:`orders-center:${orderId}`,note:"Создано из Orders Center"})});
    if (!response.ok && response.status !== 409) throw await responseError(response);
    const purchase = await response.json();
    if (purchase.first_product_id) { window.location.assign(`/crm/workspace/products/${encodeURIComponent(purchase.first_product_id)}`); return; }
    message.textContent = "Заявка создана, но товар ещё не удалось связать с карточкой.";
    await refreshSingleOrder(orderId, button.closest(".order-card"));
  } catch (error) { message.textContent = error.message || "Не удалось создать заявку на закупку."; restoreButton(button); }
};

const transitionPurchase = async (button) => {
  const purchaseId = button.dataset.purchaseId;
  const targetStatus = button.dataset.targetStatus;
  const version = Number(button.dataset.version);
  const card = button.closest(".order-card");
  const orderId = card?.dataset.orderId;
  setButtonBusy(button, button.dataset.loadingLabel || "Сохраняю…");
  try {
    const response = await fetch(`/api/workspace/purchases/${encodeURIComponent(purchaseId)}/transition`, {method:"POST",headers:headers(true),body:JSON.stringify({target_status:targetStatus,expected_version:version,idempotency_key:`orders-center:${purchaseId}:${version}:${targetStatus}`,metadata:{source:"orders-center"}})});
    if (!response.ok) throw await responseError(response);
    message.textContent = "Статус закупки обновлён.";
    await refreshSingleOrder(orderId, card);
  } catch (error) { message.textContent = error.message || "Не удалось обновить статус закупки."; restoreButton(button); }
};

const captureRevenue = () => { message.textContent = "Раздел выручки восстанавливается в workspace-контуре без изменения старой механики."; };

filters.addEventListener("submit", (event) => { event.preventDefault(); loadOrders(); });
resetButton.addEventListener("click", () => { filters.reset(); loadOrders(); });
refreshButton.addEventListener("click", () => rebuildOrders(1, true));
rebuildButton.addEventListener("click", () => rebuildOrders());
captureRevenueButton.addEventListener("click", captureRevenue);
ordersList.addEventListener("click", (event) => {
  const createButton = event.target.closest(".create-purchase");
  if (createButton) { createPurchase(createButton.dataset.orderId, createButton); return; }
  const transitionButton = event.target.closest(".purchase-transition");
  if (transitionButton) transitionPurchase(transitionButton);
});

Promise.all([loadContext(), loadOrders()]).catch((error) => { message.textContent = error.message || "Не удалось открыть Orders Center."; });
