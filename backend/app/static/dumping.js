const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#dumping-page");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const policyForm = document.querySelector("#policy-form");
const productIdInput = document.querySelector("#product-id");
const productSearch = document.querySelector("#product-search");
const productResults = document.querySelector("#product-results");
const selectedProduct = document.querySelector("#selected-product");
const list = document.querySelector("#dumping-list");
const empty = document.querySelector("#empty");
const xmlSource = document.querySelector("#xml-source");
const xmlSourceTitle = document.querySelector("#xml-source-title");
const xmlSourceMeta = document.querySelector("#xml-source-meta");
const xmlSourceStatus = document.querySelector("#xml-source-status");
let configuredRows = [];
let searchTimer = null;
let searchController = null;
let databaseReadInFlight = false;
let dumpingWorkActive = false;

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} KZT`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU") : "Ещё не запускался";
const shortDateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "—";
const formatDuration = (milliseconds) => {
  const value = Number(milliseconds);
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 60000) return `${Math.max(1, Math.round(value / 1000))} с`;
  const minutes = Math.floor(value / 60000);
  const seconds = Math.round((value % 60000) / 1000);
  if (minutes < 60) return `${minutes} мин ${seconds} с`;
  return `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
};
const elapsed = (value) => value ? formatDuration(Math.max(0, Date.now() - new Date(value).getTime())) : "—";
const runtimeStatusLabel = (status) => ({
  queued:"В очереди",
  processing:"Проверяется",
  lease_expired:"Проверка прервана",
  succeeded:"Проверка завершена",
  failed:"Не удалось проверить",
}[status] || status || "Нет данных");
const runtimeKind = (status) => status === "succeeded" ? "success" : ["failed", "lease_expired"].includes(status) ? "error" : "working";
const runtimeTechnicalDetails = (row) => `
  <details class="dumping-runtime-technical">
    <summary>Технические детали</summary>
    <span>Job #${row.job_id} · Kaspi Agent ${escapeHtml(row.agent_id || "не указан")}${row.lease_until ? ` · lease до ${shortDateTime(row.lease_until)}` : ""}</span>
  </details>`;
const runtimeItem = (row, {result=false}={}) => {
  const kind = runtimeKind(row.status);
  const timeLabel = result ? shortDateTime(row.updated_at) : elapsed(row.started_at);
  const timeAttributes = result ? "" : ` data-started-at="${escapeHtml(row.started_at)}"`;
  return `
    <article class="dumping-runtime-item ${kind}">
      <span class="dumping-runtime-item-indicator" aria-hidden="true"></span>
      <div class="dumping-runtime-item-main">
        <div class="dumping-runtime-item-title">
          <div>
            <span class="dumping-runtime-badge ${kind}">${escapeHtml(runtimeStatusLabel(row.status))}</span>
            <a href="/crm/products/${row.product_id}">${escapeHtml(row.product_name)}</a>
          </div>
          <strong class="dumping-runtime-elapsed"${timeAttributes}>${timeLabel}</strong>
        </div>
        <p>${escapeHtml(row.detail)}</p>
        <span class="dumping-runtime-meta">${escapeHtml(row.merchant_sku ? `SKU ${row.merchant_sku}` : `Kaspi ID ${row.kaspi_product_id}`)}</span>
        ${runtimeTechnicalDetails(row)}
      </div>
    </article>`;
};

const request = async (url, options = {}) => {
  const token = localStorage.getItem(storageKey);
  const response = await fetch(url, {cache:"no-store", ...options, headers:{Authorization:`Bearer ${token}`, ...(options.headers || {})}});
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) { localStorage.removeItem(storageKey); throw new Error("Токен не принят"); }
  if (!response.ok) throw new Error(payload.detail || `API вернул ошибку ${response.status}`);
  return payload;
};

const setBusy = (button, busy, text) => {
  if (!button) return;
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? text : button.dataset.label;
};

const productCaption = (row) => `${row.name} · ${row.merchant_sku || row.kaspi_product_id || "без артикула"}`;
const closeProductResults = () => { productResults.classList.add("hidden"); productSearch.setAttribute("aria-expanded", "false"); };
const clearProductSelection = ({keepQuery=false}={}) => {
  productIdInput.value = "";
  selectedProduct.textContent = "Товар не выбран";
  selectedProduct.classList.remove("selected");
  if (!keepQuery) productSearch.value = "";
};
const selectProduct = (row) => {
  productIdInput.value = String(row.product_id);
  productSearch.value = row.name;
  selectedProduct.textContent = `Выбрано: ${productCaption(row)} · Kaspi ID ${row.kaspi_product_id}`;
  selectedProduct.classList.add("selected");
  closeProductResults();
};

const renderProductResults = (rows, query) => {
  const configured = new Set(configuredRows.map((row) => Number(row.product_id)));
  const available = rows.filter((row) => !configured.has(Number(row.product_id)));
  productResults.innerHTML = available.length ? available.map((row) => `
    <button class="product-result" type="button" role="option" data-product-id="${row.product_id}">
      <strong>${escapeHtml(row.name)}</strong>
      <span>Артикул: ${escapeHtml(row.merchant_sku || "—")} · Kaspi ID: ${escapeHtml(row.kaspi_product_id)}</span>
    </button>`).join("") : `<div class="product-result-empty">По запросу «${escapeHtml(query)}» свободные карточки не найдены.</div>`;
  productResults.classList.remove("hidden");
  productSearch.setAttribute("aria-expanded", "true");
  productResults.querySelectorAll(".product-result").forEach((button) => button.addEventListener("click", () => {
    const row = available.find((item) => Number(item.product_id) === Number(button.dataset.productId));
    if (row) selectProduct(row);
  }));
};

const searchProducts = async () => {
  const query = productSearch.value.trim();
  if (query.length < 2) { closeProductResults(); return; }
  if (searchController) searchController.abort();
  searchController = new AbortController();
  productResults.innerHTML = '<div class="product-result-empty">Ищу товар…</div>';
  productResults.classList.remove("hidden");
  try {
    const rows = await request(`/api/product-registry/products?q=${encodeURIComponent(query)}&limit=20`, {signal:searchController.signal});
    renderProductResults(rows, query);
  } catch (error) {
    if (error?.name === "AbortError") return;
    productResults.innerHTML = `<div class="product-result-empty">${escapeHtml(error instanceof Error ? error.message : "Поиск временно недоступен")}</div>`;
  }
};

const scanLabel = (row) => {
  const state = row.scan_state;
  if (!row.policy.enabled) return '<span class="badge-off">Отключён</span>';
  if (state?.status === "queued") return '<span class="badge-off">В очереди</span>';
  if (state?.status === "scanning") return '<span class="badge-ready">Проверяется</span>';
  if (state?.status === "retry_wait") return '<span class="badge-limited">Автоповтор</span>';
  if (state?.status === "completed") return '<span class="badge-ready">Успешно</span>';
  if (state?.status === "blocked" || state?.status === "failed") return '<span class="badge-limited">Нужна проверка</span>';
  if (row.latest_run?.status === "floor_limited") return '<span class="badge-limited">Ограничен порогом</span>';
  if (row.latest_run?.status) return '<span class="badge-ready">Активен</span>';
  return '<span class="badge-off">Ожидает запуска</span>';
};

const scanMeta = (row) => {
  const state = row.scan_state;
  if (!state) return "Отдельная очередь конкурентов готова";
  if (state.status === "retry_wait") return `${state.last_error || "Kaspi временно ограничил запросы"}. Попытка №${state.attempts || 1}; следующий запуск: ${dateTime(state.next_retry_at)}`;
  if (state.status === "queued") return `Ожидает выполнения. В очереди сейчас: ${state.queue_size ?? 0}`;
  if (state.status === "scanning") return state.stage === "opening_product_card" ? "Открываем карточку Kaspi и получаем параметры офферов" : "Получаем продавцов и рассчитываем новую цену";
  if (state.status === "completed") return `XML обновлён: ${dateTime(state.last_success_at || state.finished_at)}`;
  if (state.last_error) return state.last_error;
  return `Обновлено: ${dateTime(state.updated_at)}`;
};

const renderFeedStatus = (feed) => {
  xmlSource.classList.toggle("ready", Boolean(feed.ready));
  xmlSource.classList.toggle("missing", !feed.configured);
  if (!feed.configured) {
    xmlSourceTitle.textContent = "XML ещё не импортирован";
    xmlSourceMeta.innerHTML = 'Демпинг использует XML из раздела «Товары». <a href="/crm/products">Перейти к товарам</a>';
    xmlSourceStatus.textContent = "Нет источника";
    return;
  }
  xmlSourceTitle.textContent = feed.source_filename || "Последний XML каталога";
  const merchant = feed.merchant_id ? `merchantId ${feed.merchant_id}` : "merchantId не найден";
  const imported = feed.imported_at ? `импортирован ${dateTime(feed.imported_at)}` : "дата импорта неизвестна";
  xmlSourceMeta.textContent = `${merchant} · ${imported} · используется Pricing Engine`;
  xmlSourceStatus.textContent = feed.ready ? "Готов к публикации" : "Нужен merchantId";
};

const renderDumpingRuntime = (snapshot) => {
  const rows = snapshot.active_runs || [];
  const results = snapshot.recent_results || [];
  const latest = snapshot.latest_run;
  dumpingWorkActive = rows.length > 0 || Number(snapshot.queued_count || 0) > 0;
  const panel = document.querySelector("#dumping-runtime-panel");
  const body = document.querySelector("#dumping-runtime-body");
  const resultsBody = document.querySelector("#dumping-runtime-results-body");
  const activeGroup = document.querySelector("#dumping-runtime-active");
  const resultsGroup = document.querySelector("#dumping-runtime-results");
  const idle = document.querySelector("#dumping-runtime-empty");
  const successCount = results.filter((row) => row.status === "succeeded").length;
  const errorCount = results.length - successCount;
  const queuedCount = Number(snapshot.queued_count || 0);

  body.innerHTML = rows.map((row) => runtimeItem(row)).join("");
  resultsBody.innerHTML = results.map((row) => runtimeItem(row, {result:true})).join("");
  activeGroup.classList.toggle("hidden", rows.length === 0);
  resultsGroup.classList.toggle("hidden", results.length === 0);
  idle.classList.toggle("hidden", rows.length > 0 || results.length > 0);
  document.querySelector("#dumping-runtime-active-count").textContent = rows.length;
  document.querySelector("#dumping-runtime-result-count").textContent = results.length;

  const label = document.querySelector("#dumping-runtime-label");
  label.className = "dumping-runtime-summary-badge";
  if (errorCount) {
    label.textContent = `${errorCount} требует внимания`;
    label.classList.add("error");
  } else if (rows.length || queuedCount) {
    const parts = [];
    if (rows.length) parts.push(`${rows.length} проверяется`);
    if (queuedCount) parts.push(`${queuedCount} в очереди`);
    label.textContent = parts.join(" · ");
    label.classList.add("working");
  } else if (successCount) {
    label.textContent = `${successCount} успешно`;
    label.classList.add("success");
  } else {
    label.textContent = "Заданий нет";
    label.classList.add("idle");
  }

  const summary = document.querySelector("#dumping-runtime-summary");
  summary.textContent = errorCount
    ? "Есть свежая ошибка. Раскройте панель, чтобы увидеть товар и причину."
    : rows.length || queuedCount
      ? "Kaspi Competitor Agent работает — список обновляется автоматически."
      : successCount
        ? "Последние проверки завершены успешно."
        : "Kaspi Competitor Agent ожидает следующую проверку.";
  panel.classList.toggle("has-error", errorCount > 0);
  panel.classList.toggle("has-work", !errorCount && (rows.length > 0 || queuedCount > 0));
  panel.classList.toggle("has-success", !errorCount && !rows.length && !queuedCount && successCount > 0);

  document.querySelector("#dumping-runtime-queue").textContent = snapshot.queued_count || 0;
  document.querySelector("#dumping-runtime-last-time").textContent = latest ? shortDateTime(latest.updated_at) : "Нет данных";
  document.querySelector("#dumping-runtime-last-result").textContent = latest ? runtimeStatusLabel(latest.status) : "Нет данных";

  const idleTitle = document.querySelector("#dumping-runtime-idle-title");
  const idleDetail = document.querySelector("#dumping-runtime-idle-detail");
  if (snapshot.queued_count) {
    idleTitle.textContent = "Очередь ждёт Kaspi Competitor Agent";
    idleDetail.textContent = `Ожидают проверки: ${snapshot.queued_count}. Агент заберёт следующую карточку при свободном потоке.`;
  } else {
    idleTitle.textContent = "Сейчас активных проверок нет";
    idleDetail.textContent = "Свежих результатов и ошибок нет. Kaspi Competitor Agent ожидает следующую карточку.";
  }

};

const tickRuntimeDurations = () => document.querySelectorAll(".dumping-runtime-elapsed[data-started-at]").forEach((element) => {
  element.textContent = elapsed(element.dataset.startedAt);
});

const render = (rows) => {
  configuredRows = rows;
  document.querySelector("#summary-total").textContent = rows.length;
  document.querySelector("#summary-enabled").textContent = rows.filter((row) => row.policy.enabled).length;
  document.querySelector("#summary-limited").textContent = rows.filter((row) => row.latest_run?.status === "floor_limited").length;
  document.querySelector("#rows-label").textContent = `Подключено карточек: ${rows.length}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit"})}`;
  list.innerHTML = rows.map((row) => {
    const run = row.latest_run || {};
    const preview = row.pricing_preview || {};
    const state = row.scan_state || {};
    const explanation = run.explanation_json || {};
    const ownPrice = run.own_price_kzt ?? state.own_price_kzt;
    const competitorPrice = run.competitor_price_kzt ?? state.competitor_price_kzt;
    const competitorName = explanation.competitor_name || state.competitor_name || "—";
    const ownPosition = explanation.own_position ?? state.own_position;
    const sellerCount = explanation.seller_count ?? state.seller_count;
    const safeFloor = preview.safe_floor_kzt ?? run.safe_floor_kzt;
    const targetPrice = run.target_price_kzt ?? state.target_price_kzt;
    const preorderDays = run.preorder_days ?? preview.preorder_days;
    return `
    <article class="dumping-card" data-product-id="${row.product_id}">
      <div class="dumping-head">
        <div class="dumping-title"><h2>${escapeHtml(row.name)}</h2><span>Kaspi ${escapeHtml(row.kaspi_product_id)}${row.merchant_sku ? ` · SKU ${escapeHtml(row.merchant_sku)}` : ""}</span></div>
        <div class="dumping-actions"><button class="button secondary edit-policy" type="button">Настроить</button><button class="button run-now" type="button">Проверить сейчас</button></div>
      </div>
      <div class="dumping-grid">
        <div><span>Статус проверки</span><strong>${scanLabel(row)}</strong><small>${escapeHtml(scanMeta(row))}</small></div>
        <div><span>Источник себестоимости</span><strong>${escapeHtml(row.source?.name || "Нет источника")}</strong><small>${escapeHtml(row.source?.kind || "—")}</small></div>
        <div><span>Себестоимость</span><strong>${money(row.source?.unit_cost_kzt)}</strong></div>
        <div><span>Безопасный порог</span><strong>${money(safeFloor)}</strong></div>
        <div><span>Целевая цена XML</span><strong>${money(targetPrice)}</strong></div>
      </div>
      <div class="dumping-grid">
        <div><span>Наша цена</span><strong>${money(ownPrice)}</strong></div>
        <div><span>Первое место</span><strong>${money(competitorPrice)}</strong><small>${escapeHtml(competitorName)}</small></div>
        <div><span>Наша позиция</span><strong>${ownPosition == null ? "—" : `№ ${ownPosition}`}</strong><small>${sellerCount == null ? "Продавцы ещё не загружены" : `Всего продавцов: ${sellerCount}`}</small></div>
        <div><span>preOrder</span><strong>${preorderDays ?? "—"} дн.</strong></div>
        <div><span>Последний успешный запуск</span><strong>${dateTime(run.created_at || state.last_success_at)}</strong></div>
      </div>
      <div class="dumping-grid">
        <div><span>Минимальная прибыль</span><strong>${money(row.policy.minimum_profit_kzt)}</strong></div>
        <div><span>Шаг ниже конкурента</span><strong>${money(row.policy.undercut_step_kzt)}</strong></div>
      </div>
    </article>`;
  }).join("");
  empty.classList.toggle("hidden", rows.length > 0);
};

const fillForm = (row) => {
  selectProduct({product_id:row.product_id,name:row.name,merchant_sku:row.merchant_sku,kaspi_product_id:row.kaspi_product_id});
  document.querySelector("#minimum-profit").value = row.policy.minimum_profit_kzt;
  document.querySelector("#undercut-step").value = row.policy.undercut_step_kzt;
  document.querySelector("#delivery-buffer").value = row.policy.supplier_delivery_buffer_days;
  document.querySelector("#city-id").value = row.policy.city_id;
  document.querySelector("#zone-id").value = row.policy.zone_id;
  document.querySelector("#inventory-first").checked = row.policy.inventory_first;
  document.querySelector("#auto-publish").checked = row.policy.auto_publish_xml;
  document.querySelector("#enabled").checked = row.policy.enabled;
  document.querySelector("#save-policy").textContent = "Сохранить настройки";
  policyForm.scrollIntoView({behavior:"smooth", block:"center"});
};

const resetPolicyForm = () => {
  policyForm.reset(); clearProductSelection();
  document.querySelector("#minimum-profit").value = "1000";
  document.querySelector("#undercut-step").value = "1";
  document.querySelector("#delivery-buffer").value = "1";
  document.querySelector("#city-id").value = "750000000";
  document.querySelector("#zone-id").value = "Magnum_ZONE1";
  document.querySelector("#inventory-first").checked = true;
  document.querySelector("#auto-publish").checked = true;
  document.querySelector("#enabled").checked = true;
  const button = document.querySelector("#save-policy"); button.dataset.label = "Подключить"; button.textContent = "Подключить";
};

const loadPage = async ({silent=false}={}) => {
  const token = localStorage.getItem(storageKey);
  if (!token) { authPanel.classList.remove("hidden"); page.classList.add("hidden"); return; }
  if (databaseReadInFlight) return;
  databaseReadInFlight = true;
  if (!silent) setBusy(refreshButton, true, "Обновляю…");
  if (!silent) message.textContent = "";
  try {
    const rows = await request("/api/dumping");
    const feed = await request("/api/dumping/feed-status");
    const runtime = await request("/api/dumping/runtime");
    render(rows); renderFeedStatus(feed); renderDumpingRuntime(runtime); authPanel.classList.add("hidden"); page.classList.remove("hidden");
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось загрузить демпинг";
    if (!localStorage.getItem(storageKey)) authPanel.classList.remove("hidden");
  } finally {
    databaseReadInFlight = false;
    if (!silent) setBusy(refreshButton, false, "");
  }
};

const pollDumpingRuntime = async () => {
  if (!localStorage.getItem(storageKey) || document.hidden || databaseReadInFlight) return;
  if (dumpingWorkActive) {
    await loadPage({silent:true});
    return;
  }
  databaseReadInFlight = true;
  try {
    renderDumpingRuntime(await request("/api/dumping/runtime"));
  } catch {
    document.querySelector("#dumping-runtime-label").textContent = "Нет свежих данных — повторяем запрос";
  } finally {
    databaseReadInFlight = false;
  }
};

productSearch.addEventListener("input", () => { clearProductSelection({keepQuery:true}); clearTimeout(searchTimer); searchTimer = setTimeout(searchProducts, 250); });
productSearch.addEventListener("focus", () => { if (productSearch.value.trim().length >= 2 && !productIdInput.value) searchProducts(); });
document.addEventListener("click", (event) => { if (!event.target.closest("#product-picker")) closeProductResults(); });

policyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const productId = Number(productIdInput.value);
  if (!productId) { message.textContent = "Сначала найди и выбери товар из результатов поиска."; productSearch.focus(); return; }
  const button = document.querySelector("#save-policy"); setBusy(button, true, "Сохраняю…"); message.textContent = "";
  try {
    await request(`/api/dumping/products/${productId}`, {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({
      enabled:document.querySelector("#enabled").checked,
      minimum_profit_kzt:Number(document.querySelector("#minimum-profit").value),
      undercut_step_kzt:Number(document.querySelector("#undercut-step").value),
      supplier_delivery_buffer_days:Number(document.querySelector("#delivery-buffer").value),
      inventory_first:document.querySelector("#inventory-first").checked,
      auto_publish_xml:document.querySelector("#auto-publish").checked,
      city_id:document.querySelector("#city-id").value.trim(),
      zone_id:document.querySelector("#zone-id").value.trim(),
    })});
    message.textContent = "Настройки сохранены. Первая проверка поставлена в отдельную очередь.";
    resetPolicyForm(); await loadPage();
  } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось сохранить"; }
  finally { setBusy(button, false, ""); }
});

list.addEventListener("click", async (event) => {
  const card = event.target.closest(".dumping-card"); if (!card) return;
  const productId = Number(card.dataset.productId);
  const row = configuredRows.find((item) => Number(item.product_id) === productId);
  if (event.target.closest(".edit-policy") && row) { fillForm(row); return; }
  const runButton = event.target.closest(".run-now"); if (!runButton) return;
  setBusy(runButton, true, "Ставлю в очередь…"); message.textContent = "";
  try {
    const result = await request(`/api/dumping/products/${productId}/run-now`, {method:"POST"});
    message.textContent = result.status === "already_queued" ? "Карточка уже ожидает проверку." : "Проверка запущена. Статус и цены будут обновляться автоматически каждые 5 секунд.";
    await loadPage();
  } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось поставить проверку в очередь"; }
  finally { setBusy(runButton, false, ""); }
});

tokenForm.addEventListener("submit", (event) => { event.preventDefault(); const token=tokenInput.value.trim(); if(!token)return; localStorage.setItem(storageKey,token); tokenInput.value=""; loadPage(); });
refreshButton.addEventListener("click", () => loadPage());
const runtimePanel = document.querySelector("#dumping-runtime-panel");
const savedRuntimePanelState = localStorage.getItem("leo_dumping_runtime_open");
if (savedRuntimePanelState !== null) runtimePanel.open = savedRuntimePanelState === "true";
runtimePanel.addEventListener("toggle", () => localStorage.setItem("leo_dumping_runtime_open", String(runtimePanel.open)));
loadPage();
window.setInterval(pollDumpingRuntime, 5000);
window.setInterval(tickRuntimeDurations, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollDumpingRuntime(); });
