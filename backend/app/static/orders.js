const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const ordersPage = document.querySelector("#orders-page");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const rebuildButton = document.querySelector("#rebuild-orders");
const captureRevenueButton = document.querySelector("#capture-revenue");
const rebuildDays = document.querySelector("#rebuild-days");
const filters = document.querySelector("#filters");
const resetButton = document.querySelector("#reset");
const ordersList = document.querySelector("#orders-list");
const empty = document.querySelector("#empty");
let pendingScrollTop = 0;

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency = "KZT") => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency}`;
const percent = (value) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})}%`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const stageLabel = (stage) => ({new:"Новый",accepted:"Принят",preorder:"Предзаказ",assembly:"Упаковка",handover:"Передача",shipping:"Передан в доставку",cancelling:"Отмена в процессе",delivered:"Завершён",cancelled:"Отменён",returned:"Возврат",unknown:"Прочее"}[stage] || stage || "—");
const stageClass = (stage) => stage === "delivered" ? "ok" : ["cancelling","cancelled","returned"].includes(stage) ? "bad" : "warn";
const procurementLabel = (state) => ({required:"Нужно закупить",in_progress:"Закупка оформлена",received:"Получено",not_required:"Закупка не требуется",cancelled:"Закупка отменена"}[state] || state || "—");
const purchaseStatusLabel = (status) => ({draft:"Черновик",requested:"Заявка отправлена",ordered:"Заказано",partially_received:"Получено частично",received:"Получено",closed:"Закрыто",cancelled:"Отменено"}[status] || status || "—");
const nextPurchaseAction = (status) => ({draft:{target:"requested",label:"Отправить заявку",loading:"Отправляю…"},requested:{target:"ordered",label:"Отметить заказанным",loading:"Сохраняю…"},ordered:{target:"received",label:"Отметить полученным",loading:"Принимаю…"},partially_received:{target:"received",label:"Отметить полученным",loading:"Принимаю…"},received:{target:"closed",label:"Закрыть закупку",loading:"Закрываю…"}}[status] || null);
const headers = () => ({"Authorization": `Bearer ${localStorage.getItem(storageKey) || ""}`});

const setLoading = (loading) => {
  ordersPage.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  rebuildButton.disabled = loading;
  if (captureRevenueButton) captureRevenueButton.disabled = loading;
  if (rebuildDays) rebuildDays.disabled = loading;
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

const restoreListContextFromUrl = () => {
  const params = new URLSearchParams(window.location.search);
  const query = params.get("query");
  const status = params.get("status");
  const scrollTop = Number(params.get("scroll") || 0);
  if (query != null) document.querySelector("#search").value = query;
  if (status != null) document.querySelector("#status").value = status;
  pendingScrollTop = Number.isFinite(scrollTop) && scrollTop > 0 ? scrollTop : 0;
};

const currentOrdersReturnUrl = () => {
  const params = new URLSearchParams();
  const query = document.querySelector("#search").value.trim();
  const status = document.querySelector("#status").value;
  if (query) params.set("query", query);
  if (status) params.set("status", status);
  params.set("scroll", String(Math.max(0, Math.round(window.scrollY))));
  return `/crm/orders?${params.toString()}`;
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
  return `<button class="button purchase-transition" type="button" data-purchase-id="${escapeHtml(line.purchase_request_id)}" data-version="${Number(line.purchase_version)}" data-target-status="${action.target}" data-loading-label="${escapeHtml(action.loading)}">${escapeHtml(action.label)}</button>`;
};

const renderLine = (line, multiLineOrder = false) => {
  const identity = [line.merchant_sku ? `Артикул ${escapeHtml(line.merchant_sku)}` : null, line.external_product_id ? `Kaspi ID ${escapeHtml(line.external_product_id)}` : null].filter(Boolean).join(" · ") || "Идентификатор не получен";
  const title = line.product_id ? `<a class="line-title order-product-link" data-product-id="${Number(line.product_id)}" href="/crm/products/${Number(line.product_id)}">${escapeHtml(line.title)}</a>` : `<strong>${escapeHtml(line.title)}</strong>`;
  const cost = line.procurement_unit_cost == null ? "—" : money(line.procurement_unit_cost);
  const source = line.procurement_source_name ? escapeHtml(line.procurement_source_name) : "Источник не выбран";
  const netProfit = line.net_profit == null ? "—" : `${money(line.net_profit)} · ${percent(line.net_margin_pct)}`;
  const logisticsLabel = multiLineOrder ? "доля логистики" : "логистика";
  const fees = `Комиссия ${money(line.kaspi_commission)} · налог ${money(line.tax)} · ${logisticsLabel} ${money(line.logistics)}`;
  const photo = line.image_url
    ? `<img class="order-product-photo" src="${escapeHtml(line.image_url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">`
    : line.product_id
      ? `<span class="order-product-photo placeholder" data-resolve-product-image data-product-id="${Number(line.product_id)}" data-image-class="order-product-photo">Фото…</span>`
      : '<span class="order-product-photo placeholder">Нет фото</span>';
  return `<div class="order-line"><div class="order-product">${photo}<div>${title}<span class="muted">${identity}</span></div></div><div><span class="muted">Количество</span><strong>${Number(line.quantity || 0)}</strong></div><div><span class="muted">Цена продажи</span><strong>${money(line.unit_price)}</strong></div><div><span class="muted">Закупочная цена</span><strong>${cost}</strong><span class="muted">${source}</span></div><div><span class="muted">Чистая прибыль</span><strong>${netProfit}</strong><span class="muted">${fees}</span><span class="muted">${escapeHtml(procurementLabel(line.procurement_state))}</span>${line.purchase_status ? `<span class="muted">${escapeHtml(purchaseStatusLabel(line.purchase_status))}</span>` : ""}${renderPurchaseAction(line)}</div></div>`;
};

const renderOrder = (order) => {
  const stage = order.operational_stage || "unknown";
  const externalCode = order.external_code || order.order_id;
  const canCreatePurchase = Number(order.procurement_required_lines || 0) > 0 && stage === "preorder";
  const bindingText = Number(order.unresolved_lines || 0) === 0 ? "Товары привязаны" : `Не привязано: ${Number(order.unresolved_lines || 0)}`;
  const manualNote = order.manual_stage_reason ? `<span class="muted">${escapeHtml(order.manual_stage_reason)}</span>` : "";
  const stageOptions = ["preorder","assembly","handover","shipping","delivered","cancelled"].map((value) => `<option value="${value}" ${order.manual_stage === value ? "selected" : ""}>${escapeHtml(stageLabel(value))}</option>`).join("");
  const editableStage = !["handover","shipping","cancelling","delivered","cancelled","returned"].includes(order.status);
  const stageControls = editableStage ? `<div class="stage-override-controls"><select class="stage-override-select" aria-label="Новый этап заказа"><option value="">Выберите этап</option>${stageOptions}</select><button class="button secondary apply-stage-override" type="button">Изменить этап</button>${order.manual_stage ? '<button class="button secondary clear-stage-override" type="button">Вернуть автостатус</button>' : ""}</div>` : "";
  const purchaseAction = canCreatePurchase ? `<button class="button create-purchase" type="button" data-order-id="${order.order_id}">Создать заявку на закупку</button>` : "";
  const orderActions = stageControls || purchaseAction ? `<div class="order-actions">${stageControls}${purchaseAction}</div>` : "";
  return `<article class="order-card" data-order-id="${order.order_id}"><div class="order-header"><div><span class="order-number">Заказ №${escapeHtml(externalCode)}</span><span class="order-meta">${escapeHtml(order.marketplace)} · кабинет ${escapeHtml(order.marketplace_external_account_id)} · ${dateTime(order.ordered_at)}</span></div><div class="order-stat"><span>Статус Kaspi</span><strong><span class="badge ${stageClass(stage)}">${escapeHtml(stageLabel(stage))}</span></strong>${manualNote}</div><div class="order-stat"><span>Единиц</span><strong>${Number(order.units || 0)}</strong></div><div class="order-stat"><span>Сумма заказа</span><strong>${money(order.total_amount, order.currency)}</strong><span class="muted">Логистика заказа ${money(order.logistics, order.currency)}</span></div><div class="order-stat"><span>Связь с каталогом</span><strong>${escapeHtml(bindingText)}</strong></div></div><div class="order-lines">${order.lines.map((line) => renderLine(line, order.lines.length > 1)).join("")}</div>${orderActions}</article>`;
};

const procurementProductKey = (line) => {
  if (line.product_id != null) return `product:${line.product_id}`;
  if (line.merchant_sku) return `sku:${line.merchant_sku}`;
  if (line.external_product_id) return `kaspi:${line.external_product_id}`;
  return `title:${line.title || line.line_id}`;
};

const procurementBreakdown = (orders = []) => {
  const products = new Map();
  for (const order of orders) {
    if (!["new", "preorder"].includes(order.operational_stage)) continue;
    for (const line of order.lines || []) {
      const incoming = Number(line.incoming_reserved_quantity || 0);
      const shortage = Number(line.uncovered_quantity || 0);
      if (incoming <= 0 && shortage <= 0) continue;
      const key = procurementProductKey(line);
      const current = products.get(key) || {
        productId: line.product_id,
        title: line.title || "Товар без названия",
        merchantSku: line.merchant_sku,
        externalProductId: line.external_product_id,
        demand: 0,
        inventory: 0,
        incoming: 0,
        shortage: 0,
      };
      current.demand += Number(line.quantity || 0);
      current.inventory += Number(line.inventory_allocated_quantity || 0);
      current.incoming += incoming;
      current.shortage += shortage;
      products.set(key, current);
    }
  }
  return [...products.values()].sort((left, right) =>
    right.shortage - left.shortage
    || right.incoming - left.incoming
    || left.title.localeCompare(right.title, "ru")
  );
};

const procurementProductIdentity = (product) => [
  product.merchantSku ? `Артикул ${product.merchantSku}` : null,
  product.externalProductId ? `Kaspi ID ${product.externalProductId}` : null,
].filter(Boolean).join(" · ");

const renderProcurementProduct = (product) => {
  const demand = product.demand.toLocaleString("ru-RU");
  const incoming = product.incoming.toLocaleString("ru-RU");
  const shortage = product.shortage.toLocaleString("ru-RU");
  const inventory = product.inventory.toLocaleString("ru-RU");
  const identity = procurementProductIdentity(product);
  const title = product.productId
    ? `<a href="/crm/products/${product.productId}">${escapeHtml(product.title)}</a>`
    : `<strong>${escapeHtml(product.title)}</strong>`;
  let action;
  if (product.shortage > 0 && product.incoming > 0) {
    action = `В пути ${incoming} шт. — они уже распределены по предзаказам. <b>Закажите ещё ${shortage} шт.</b>`;
  } else if (product.shortage > 0) {
    action = `Товаром в пути не покрыто. <b>Закажите ${shortage} шт.</b>`;
  } else {
    action = `В пути ${incoming} шт. — текущие предзаказы покрыты. Дозаказ не нужен.`;
  }
  const physical = product.inventory > 0 ? `<span>Со склада покрыто: ${inventory} шт.</span>` : "";
  return `<article class="procurement-product"><div>${title}${identity ? `<span>${escapeHtml(identity)}</span>` : ""}</div><div><span>Предзаказано: ${demand} шт.</span>${physical}<p>${action}</p></div></article>`;
};

const updateSummary = (summary = {}, orders = []) => {
  const shortage = Number(summary.procurement_required_units || 0);
  const incoming = Number(summary.incoming_reserved_units || 0);
  const products = procurementBreakdown(orders);
  document.querySelector("#summary-orders").textContent = Number(summary.orders_count || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-active").textContent = Number(summary.active_orders || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-revenue").textContent = money(summary.revenue || 0);
  document.querySelector("#summary-profit").textContent = money(summary.confirmed_net_profit || 0);
  document.querySelector("#summary-profit-units").textContent = `по ${Number(summary.confirmed_profit_units || 0).toLocaleString("ru-RU")} ед. с подтверждённой себестоимостью`;
  document.querySelector("#summary-procurement").textContent = shortage.toLocaleString("ru-RU");
  document.querySelector("#summary-procurement-caption").textContent = `единиц · в пути: ${incoming.toLocaleString("ru-RU")}`;
  const advice = document.querySelector("#procurement-advice");
  if (shortage > 0) {
    const headline = incoming > 0
      ? `В пути ${incoming.toLocaleString("ru-RU")} шт. — они уже распределены по предзаказам. Закажите ещё ${shortage.toLocaleString("ru-RU")} шт.`
      : `Товаров в пути нет. Закажите ${shortage.toLocaleString("ru-RU")} шт.`;
    advice.innerHTML = `<details class="procurement-disclosure"><summary class="procurement-advice-header"><strong>${headline}</strong><span class="procurement-toggle"><span class="procurement-toggle-open">Показать список товаров</span><span class="procurement-toggle-close">Скрыть список товаров</span><b aria-hidden="true">⌄</b></span></summary><div class="procurement-products">${products.map(renderProcurementProduct).join("")}</div></details>`;
    advice.classList.remove("hidden");
  } else if (incoming > 0) {
    advice.innerHTML = `<details class="procurement-disclosure"><summary class="procurement-advice-header"><strong>В пути ${incoming.toLocaleString("ru-RU")} шт. — они уже распределены по предзаказам. Текущие предзаказы покрыты.</strong><span class="procurement-toggle"><span class="procurement-toggle-open">Показать список товаров</span><span class="procurement-toggle-close">Скрыть список товаров</span><b aria-hidden="true">⌄</b></span></summary><div class="procurement-products">${products.map(renderProcurementProduct).join("")}</div></details>`;
    advice.classList.remove("hidden");
  } else {
    advice.replaceChildren();
    advice.classList.add("hidden");
  }
};

const render = (payload) => {
  const orders = payload.items || [];
  updateSummary(payload.summary || {}, orders);
  ordersList.innerHTML = orders.map(renderOrder).join("");
  empty.classList.toggle("hidden", orders.length > 0);
  document.querySelector("#rows-label").textContent = `Показано заказов: ${orders.length} из ${payload.total || 0}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
  authPanel.classList.add("hidden");
  ordersPage.classList.remove("hidden");
  window.LEOProductImageResolver?.observe(ordersList);
  if (pendingScrollTop > 0) {
    const scrollTop = pendingScrollTop;
    pendingScrollTop = 0;
    requestAnimationFrame(() => window.scrollTo({top: scrollTop, left: 0, behavior: "instant"}));
  }
};

const fetchOrdersPayload = async () => {
  const response = await fetch(`/api/commerce/orders?${queryString()}`, {headers:headers(), cache:"no-store"});
  if (response.status === 401) { localStorage.removeItem(storageKey); throw new Error("Токен не принят. Введите актуальный SERVICE_API_TOKEN."); }
  if (!response.ok) throw await responseError(response);
  return response.json();
};

const refreshSingleOrder = async (orderId, currentCard) => {
  const payload = await fetchOrdersPayload();
  updateSummary(payload.summary || {}, payload.items || []);
  const order = (payload.items || []).find((item) => Number(item.order_id) === Number(orderId));
  if (!order) {
    currentCard?.remove();
  } else {
    const wrapper = document.createElement("div");
    wrapper.innerHTML = renderOrder(order);
    const nextCard = wrapper.firstElementChild;
    currentCard?.replaceWith(nextCard);
    window.LEOProductImageResolver?.observe(nextCard);
  }
  const visibleCount = ordersList.querySelectorAll(".order-card").length;
  empty.classList.toggle("hidden", visibleCount > 0);
  document.querySelector("#rows-label").textContent = `Показано заказов: ${visibleCount} из ${payload.total || 0}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
};

const loadOrders = async () => {
  if (!localStorage.getItem(storageKey)) { authPanel.classList.remove("hidden"); ordersPage.classList.add("hidden"); return; }
  setLoading(true);
  try { render(await fetchOrdersPayload()); }
  catch (error) { message.textContent = error.message || "Не удалось загрузить заказы."; }
  finally { setLoading(false); }
};

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const pollRebuildJob = async (jobId) => {
  while (true) {
    const response = await fetch(`/api/commerce/orders/rebuild/${encodeURIComponent(jobId)}`, {headers:headers(), cache:"no-store"});
    if (response.status === 404) return {status:"lost", message:"Render перезапустил процесс и потерял временный job_id"};
    if (!response.ok) throw await responseError(response);
    const job = await response.json();
    const progress = job.progress || {};
    message.textContent = `${job.message || job.status}. Прогресс: ${Number(progress.percent || 0)}%. Заказов: ${Number(job.orders_count || 0)}. Запросов: ${Number(job.request_count || 0)}. Ошибок диапазонов: ${(job.errors || []).length}.`;
    if (job.orders_ready === true || ["completed", "completed_with_errors", "failed"].includes(job.status)) return job;
    await sleep(1200);
  }
};

const startRebuild = async (days, retry = true) => {
  const response = await fetch(`/api/commerce/orders/rebuild?days=${days}`, {method:"POST", headers:headers()});
  if (!response.ok) throw await responseError(response);
  const started = await response.json();
  const result = await pollRebuildJob(started.job_id);
  if (result.status === "lost" && retry) {
    message.textContent = "Render перезапустился во время загрузки. Автоматически запускаю импорт ещё раз…";
    return startRebuild(days, false);
  }
  return result;
};

const rebuildOrders = async (daysOverride = null, preserveFilters = false) => {
  const days = Number(daysOverride || rebuildDays?.value || 7);
  rebuildButton.disabled = true;
  refreshButton.disabled = true;
  if (captureRevenueButton) captureRevenueButton.disabled = true;
  if (rebuildDays) rebuildDays.disabled = true;
  rebuildButton.textContent = "Загрузка…";
  message.textContent = `Загружаю свежие заказы Kaspi за ${days} дн.`;
  try {
    const result = await startRebuild(days);
    if (result.status === "lost") throw new Error("Render дважды перезапустил процесс во время загрузки. Повторите после завершения деплоя.");
    if (result.status === "failed") throw new Error(result.message || "Kaspi raw receiver завершился с ошибкой.");
    const enrichment = result.product_enrichment || {};
    message.textContent = result.status === "enriching_products"
      ? `Заказы сохранены: ${Number(result.orders_count || 0)}. Уточнение названий товаров продолжается в фоне.`
      : `Готово. Заказов: ${Number(result.orders_count || 0)}, новых: ${Number(result.imported_count || 0)}, обновлено: ${Number(result.updated_count || 0)}, товарных строк: ${Number(enrichment.updated || 0)}.`;
    if (!preserveFilters) filters.reset();
    await loadOrders();
  } catch (error) {
    message.textContent = error.message || "Не удалось загрузить заказы Kaspi.";
    await loadOrders();
  } finally {
    rebuildButton.disabled = false;
    refreshButton.disabled = false;
    if (captureRevenueButton) captureRevenueButton.disabled = false;
    if (rebuildDays) rebuildDays.disabled = false;
    rebuildButton.textContent = "Загрузить заказы Kaspi";
  }
};

const captureRevenue = async () => {
  captureRevenueButton.disabled = true;
  captureRevenueButton.textContent = "Сохраняю…";
  message.textContent = "Фиксирую выручку и прибыль по заказам в упаковке.";
  try {
    const response = await fetch("/api/revenue/daily/capture?timezone_name=Asia%2FAlmaty", {method:"POST", headers:headers()});
    if (!response.ok) throw await responseError(response);
    const result = await response.json();
    if (Number(result.captured_count || 0) === 0) throw new Error("Нет заказов в упаковке, которые можно сохранить.");
    window.location.assign("/crm/revenue");
  } catch (error) {
    message.textContent = error.message || "Не удалось сохранить выручку и маржу.";
    captureRevenueButton.disabled = false;
    captureRevenueButton.textContent = "Сохранить выручку и маржу";
  }
};

const createPurchase = async (orderId, button) => {
  setButtonBusy(button, "Создаю…");
  try {
    const response = await fetch("/api/purchases/from-marketplace-order", {method:"POST",headers:{...headers(),"Content-Type":"application/json"},body:JSON.stringify({marketplace_order_id:Number(orderId),idempotency_key:`orders-center:${orderId}`,note:"Создано из Orders Center"})});
    if (!response.ok && response.status !== 409) throw await responseError(response);
    const purchase = await response.json();
    if (purchase.first_product_id) {
      const returnTo = currentOrdersReturnUrl();
      window.location.assign(`/crm/products/${encodeURIComponent(purchase.first_product_id)}?return_to=${encodeURIComponent(returnTo)}`);
      return;
    }
    message.textContent = "Заявка создана, но товар ещё не удалось связать с карточкой.";
    await refreshSingleOrder(orderId, button.closest(".order-card"));
  } catch (error) {
    message.textContent = error.message || "Не удалось создать заявку на закупку.";
    restoreButton(button);
  }
};

const transitionPurchase = async (button) => {
  const purchaseId = button.dataset.purchaseId;
  const targetStatus = button.dataset.targetStatus;
  const version = Number(button.dataset.version);
  const card = button.closest(".order-card");
  const orderId = card?.dataset.orderId;
  setButtonBusy(button, button.dataset.loadingLabel || "Сохраняю…");
  try {
    const response = await fetch(`/api/purchases/${encodeURIComponent(purchaseId)}/transition`, {method:"POST",headers:{...headers(),"Content-Type":"application/json"},body:JSON.stringify({target_status:targetStatus,expected_version:version,idempotency_key:`orders-center:${purchaseId}:${version}:${targetStatus}`,metadata:{source:"orders-center"}})});
    if (!response.ok) throw await responseError(response);
    message.textContent = "Статус закупки обновлён.";
    await refreshSingleOrder(orderId, card);
  } catch (error) {
    message.textContent = error.message || "Не удалось обновить статус закупки.";
    restoreButton(button);
  }
};

const changeOrderStage = async (button, clear = false) => {
  const card = button.closest(".order-card");
  const orderId = Number(card?.dataset.orderId);
  const stage = clear ? null : card?.querySelector(".stage-override-select")?.value || null;
  if (!clear && !stage) { message.textContent = "Выберите новый этап заказа."; return; }
  const reason = clear ? null : prompt("Почему вы меняете этап заказа? Причина сохранится в истории.", "Ручная коррекция владельцем");
  if (!clear && !reason?.trim()) return;
  setButtonBusy(button, "Пересчитываю…");
  try {
    const response = await fetch(`/api/commerce/orders/${orderId}/stage-override`, {method:"POST",headers:{...headers(),"Content-Type":"application/json"},body:JSON.stringify({stage,reason})});
    if (!response.ok) throw await responseError(response);
    message.textContent = clear ? "Ручная коррекция снята. Статус снова определяется по Kaspi." : "Этап изменён, FIFO и XML пересчитаны.";
    await refreshSingleOrder(orderId, card);
  } catch (error) {
    message.textContent = error.message || "Не удалось изменить этап заказа.";
    restoreButton(button);
  }
};

tokenForm.addEventListener("submit", (event) => { event.preventDefault(); localStorage.setItem(storageKey, tokenInput.value.trim()); tokenInput.value = ""; loadOrders(); });
filters.addEventListener("submit", (event) => { event.preventDefault(); loadOrders(); });
resetButton.addEventListener("click", () => { filters.reset(); loadOrders(); });
refreshButton.addEventListener("click", () => rebuildOrders(1, true));
rebuildButton.addEventListener("click", () => rebuildOrders());
captureRevenueButton.addEventListener("click", captureRevenue);
ordersList.addEventListener("click", (event) => { const productLink = event.target.closest(".order-product-link"); if (productLink) { event.preventDefault(); const returnTo = currentOrdersReturnUrl(); window.location.assign(`/crm/products/${encodeURIComponent(productLink.dataset.productId)}?return_to=${encodeURIComponent(returnTo)}`); return; } const createButton = event.target.closest(".create-purchase"); if (createButton) { createPurchase(createButton.dataset.orderId, createButton); return; } const transitionButton = event.target.closest(".purchase-transition"); if (transitionButton) { transitionPurchase(transitionButton); return; } const stageButton = event.target.closest(".apply-stage-override"); if (stageButton) { changeOrderStage(stageButton); return; } const clearButton = event.target.closest(".clear-stage-override"); if (clearButton) changeOrderStage(clearButton, true); });
restoreListContextFromUrl();
loadOrders();
