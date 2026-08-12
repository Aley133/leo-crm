"use strict";

const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#fast-page");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const policyForm = document.querySelector("#policy-form");
const productSearch = document.querySelector("#product-search");
const productIdInput = document.querySelector("#product-id");
const productResults = document.querySelector("#product-results");
const selectedProduct = document.querySelector("#selected-product");
const statusFilter = document.querySelector("#status-filter");
const list = document.querySelector("#fast-list");
const floorList = document.querySelector("#floor-list");
const floorSection = document.querySelector("#floor-section");
const empty = document.querySelector("#empty");
const editDialog = document.querySelector("#edit-dialog");
const editForm = document.querySelector("#edit-form");
let rows = [];
let searchTimer = null;
let searchController = null;
let loading = false;
const offersCache = new Map();

const attentionStatuses = new Set(["floor_limited","price_anomaly","market_context_mismatch","own_offer_missing","out_of_stock","apply_timeout","apply_unconfirmed","error"]);
const workingStatuses = new Set(["queued","scanning","queued_apply","preparing_apply","applying","verifying"]);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value) => value == null || value === "" ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ₸`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU") : "—";
const statusOf = (row) => row.state?.status || (row.policy.enabled ? "idle" : "paused");
const isFloor = (row) => statusOf(row) === "floor_limited" || row.state?.decision_status === "floor_limited";

const request = async (url, options = {}) => {
  const token = localStorage.getItem(storageKey) || "";
  const response = await fetch(url, {cache:"no-store", ...options, headers:{Authorization:`Bearer ${token}`, ...(options.headers || {})}});
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    localStorage.removeItem(storageKey);
    throw new Error("SERVICE_API_TOKEN не принят");
  }
  if (!response.ok) throw new Error(payload.detail || `API вернул HTTP ${response.status}`);
  return payload;
};

const setBusy = (button, busy, label) => {
  if (!button) return;
  if (!button.dataset.defaultLabel) button.dataset.defaultLabel = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? label : button.dataset.defaultLabel;
};

const statusView = (row) => {
  const status = statusOf(row);
  const labels = {
    idle:"Ожидает", queued:"В очереди", scanning:"Сканирование", queued_apply:"Цена готова",
    preparing_apply:"Сверка остатка", applying:"Запись PENDING", verifying:"Проверка цены",
    applied:"Применено", watching:"Цена актуальна", floor_limited:"На пороге",
    price_anomaly:"Аномалия цены", market_context_mismatch:"Контекст не совпал",
    own_offer_missing:"Наша строка не найдена", out_of_stock:"Нет FIFO-остатка",
    apply_timeout:"Не подтверждено", apply_unconfirmed:"Защитная пауза", error:"Ошибка",
    paused:"Отключён", stale:"Решение устарело", apply_failed:"Ошибка записи",
  };
  const kind = isFloor(row) ? "floor" : workingStatuses.has(status) ? "working" : ["applied","watching"].includes(status) ? "success" : attentionStatuses.has(status) || status === "apply_failed" ? "error" : "off";
  return {status, label:labels[status] || status, kind};
};

const offersTable = (offers) => {
  if (!Array.isArray(offers) || !offers.length) return '<p class="fast-card-reason">Диагностика офферов появится после первой проверки.</p>';
  return `<div class="offers-wrap"><table class="offers-table"><thead><tr><th>Продавец</th><th>Цена API</th><th>Роль</th><th>В расчёте</th><th>Доставка / причина</th></tr></thead><tbody>${offers.map((offer) => {
    const rowClass = offer.is_own ? "offer-own" : offer.used_for_dumping ? "" : "offer-ignore";
    const detail = offer.ignored_reason || offer.delivery || "—";
    return `<tr class="${rowClass}"><td>${escapeHtml(offer.merchant_name || offer.merchant_id || "—")}</td><td>${money(offer.price_kzt)}</td><td>${offer.is_own ? "Наша строка" : "Конкурент"}</td><td>${offer.is_own ? "—" : offer.used_for_dumping ? "Да" : "Нет"}</td><td>${escapeHtml(detail)}</td></tr>`;
  }).join("")}</tbody></table></div>`;
};

const renderFloor = () => {
  const limited = rows.filter(isFloor);
  floorSection.classList.toggle("hidden", limited.length === 0);
  document.querySelector("#floor-count").textContent = limited.length;
  floorList.innerHTML = limited.map((row) => `<article class="floor-item" data-product-id="${row.product_id}">
    <div><h3>${escapeHtml(row.name)}</h3><small>SKU ${escapeHtml(row.merchant_sku || "—")} · ${escapeHtml(row.state?.status_reason || "Конкурент ниже безопасного floor")}</small></div>
    <div class="floor-value"><span>Наша цена</span><strong>${money(row.state?.own_price_kzt)}</strong></div>
    <div class="floor-value"><span>Конкурент</span><strong>${money(row.state?.competitor_price_kzt)}</strong></div>
    <div class="floor-value"><span>Текущий floor</span><strong>${money(row.current_safe_floor_kzt ?? row.state?.safe_floor_kzt)}</strong></div>
    <div class="floor-value"><span>Мин. прибыль</span><strong>${money(row.policy.minimum_profit_kzt)}</strong></div>
    <div class="floor-action"><button class="button edit-policy" type="button">Изменить порог</button></div>
  </article>`).join("");
};

const rowMatches = (row) => {
  const filter = statusFilter.value;
  const status = statusOf(row);
  if (filter === "all") return true;
  if (filter === "enabled") return row.policy.enabled;
  if (filter === "floor") return isFloor(row);
  if (filter === "attention") return attentionStatuses.has(status);
  if (filter === "working") return workingStatuses.has(status);
  if (filter === "paused") return !row.policy.enabled || status === "paused";
  return true;
};

const card = (row) => {
  const state = row.state || {};
  const view = statusView(row);
  const source = row.current_source || {};
  const canRun = row.policy.enabled && !state.automatic_writes_paused && !workingStatuses.has(view.status);
  return `<article class="fast-card ${view.kind}" data-product-id="${row.product_id}">
    <div class="fast-card-head">
      <div class="fast-card-title"><span class="fast-status ${view.kind}">${escapeHtml(view.label)}</span><div><h3>${escapeHtml(row.name)}</h3><p>Kaspi ${escapeHtml(row.kaspi_product_id)} · SKU ${escapeHtml(row.merchant_sku || "—")}${row.brand ? ` · ${escapeHtml(row.brand)}` : ""}</p></div></div>
      <div class="fast-card-actions"><button class="button secondary edit-policy" type="button">Настроить</button>${state.automatic_writes_paused ? '<button class="button resume-product" type="button">Возобновить</button>' : `<button class="button run-now" type="button" ${canRun ? "" : "disabled"}>Проверить сейчас</button>`}</div>
    </div>
    <div class="fast-card-grid">
      <div><span>FIFO-остаток</span><strong>${Number(row.current_inventory_on_hand || 0).toLocaleString("ru-RU")} шт.</strong><small>повторно читается перед write</small></div>
      <div><span>Себестоимость</span><strong>${money(source.unit_cost_kzt ?? state.source_cost_kzt)}</strong><small>${escapeHtml(source.name || state.source_name || "Нет источника")}</small></div>
      <div><span>Безопасный floor</span><strong>${money(row.current_safe_floor_kzt ?? state.safe_floor_kzt)}</strong><small>мин. прибыль ${money(row.policy.minimum_profit_kzt)}</small></div>
      <div><span>Наша цена</span><strong>${money(state.own_price_kzt)}</strong><small>${state.own_position ? `позиция №${state.own_position} из ${state.seller_count || "—"}` : "позиция —"}</small></div>
      <div><span>Лучший конкурент</span><strong>${money(state.competitor_price_kzt)}</strong><small>${escapeHtml(state.competitor_name || "—")}</small></div>
      <div><span>Целевая цена</span><strong>${money(state.target_price_kzt)}</strong><small>шаг ${money(row.policy.undercut_step_kzt)}</small></div>
      <div><span>Цена карточки</span><strong>${money(state.page_visible_price_kzt)}</strong><small>${state.market_context_ok ? "контекст подтверждён" : "ожидает подтверждения"}</small></div>
      <div><span>Последний scan</span><strong>${dateTime(state.last_scanned_at)}</strong><small>следующий ${dateTime(state.next_scan_at)}</small></div>
      <div><span>Последний apply</span><strong>${dateTime(state.last_applied_at)}</strong><small>${state.last_operation_id ? `operation ${escapeHtml(state.last_operation_id)}` : "операций ещё нет"}</small></div>
      <div><span>Интервал</span><strong>${row.policy.scan_interval_seconds} сек.</strong><small>аномалия ${Number(row.policy.max_undercut_gap_percent)}%</small></div>
      <div><span>Agent / версия решения</span><strong>${escapeHtml(state.last_agent_id || "—")}</strong><small>state v${Number(state.state_version || 0)}</small></div>
      <div><span>Канал</span><strong>Realtime API</strong><small>XML не изменяется</small></div>
    </div>
    <div class="fast-card-reason"><strong>${escapeHtml(view.label)}.</strong> ${escapeHtml(state.pause_reason || state.status_reason || "Первая проверка ещё не выполнялась.")}${state.last_error_message ? ` · ${escapeHtml(state.last_error_message)}` : ""}</div>
    <details class="fast-details" data-product-id="${row.product_id}" data-state-version="${Number(state.state_version || 0)}"><summary>Офферы и проверка buyer-context · ${Number(state.offers_count || 0)}</summary><div class="offers-container"><p class="fast-card-reason">Раскройте блок — CRM загрузит диагностику только этого товара.</p></div></details>
  </article>`;
};

const render = (payload) => {
  rows = payload.items || [];
  const summary = payload.summary || {};
  document.querySelector("#summary-total").textContent = summary.total || 0;
  document.querySelector("#summary-enabled").textContent = summary.enabled || 0;
  document.querySelector("#summary-floor").textContent = summary.floor_limited || 0;
  document.querySelector("#summary-working").textContent = summary.working || 0;
  document.querySelector("#summary-attention").textContent = summary.attention || 0;
  renderFloor();
  const visible = rows.filter(rowMatches);
  list.innerHTML = visible.map(card).join("");
  empty.classList.toggle("hidden", rows.length > 0);
  document.querySelector("#rows-label").textContent = `${visible.length} из ${rows.length} · обновлено ${dateTime(payload.checked_at)}`;
};

const renderAgent = (payload) => {
  const cardEl = document.querySelector("#fast-agent");
  const title = document.querySelector("#fast-agent-title");
  const meta = document.querySelector("#fast-agent-meta");
  const badge = document.querySelector("#fast-agent-status");
  const agent = payload.agents?.[0];
  const online = Boolean(payload.online && agent?.online);
  cardEl.classList.toggle("ready", online);
  cardEl.classList.toggle("missing", !online);
  badge.className = `fast-pill ${online ? "success" : "warning"}`;
  badge.textContent = online ? "Онлайн" : "Офлайн";
  if (!agent) {
    title.textContent = "Fast Agent ещё не подключался";
    meta.textContent = "Скачайте отдельный агент; при первом запуске укажите workspace, Merchant UID, Store ID и данные Merchant Cabinet.";
    return;
  }
  title.textContent = online ? `Подключён: ${agent.hostname || agent.agent_id}` : `Нет связи: ${agent.hostname || agent.agent_id}`;
  meta.textContent = `версия ${agent.version || "—"} · потоков ${agent.concurrency || 1} · workspace ${agent.workspace_id} · Merchant ${agent.merchant_uid || "—"} · heartbeat ${dateTime(agent.last_seen_at)}`;
};

const loadPage = async ({silent=false}={}) => {
  if (!localStorage.getItem(storageKey)) {
    authPanel.classList.remove("hidden");
    page.classList.add("hidden");
    return;
  }
  if (loading) return;
  loading = true;
  if (!silent) setBusy(refreshButton, true, "Обновляю…");
  try {
    const [payload, agent] = await Promise.all([request("/api/fast-dumping"), request("/api/fast-dumping-agent/agents/status")]);
    render(payload);
    renderAgent(agent);
    authPanel.classList.add("hidden");
    page.classList.remove("hidden");
    if (!silent) message.textContent = "";
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось загрузить быстрый демпинг";
    if (!localStorage.getItem(storageKey)) authPanel.classList.remove("hidden");
  } finally {
    loading = false;
    if (!silent) setBusy(refreshButton, false, "");
  }
};

const closeProductResults = () => { productResults.classList.add("hidden"); productSearch.setAttribute("aria-expanded", "false"); };
const selectProduct = async (row) => {
  productIdInput.value = String(row.product_id);
  productSearch.value = row.name;
  selectedProduct.textContent = `Выбрано: ${row.name} · SKU ${row.merchant_sku || "нет"} · Kaspi ${row.kaspi_product_id}`;
  selectedProduct.classList.add("selected");
  closeProductResults();
  try {
    const ordinary = await request(`/api/dumping/products/${row.product_id}`);
    if (ordinary.policy) {
      document.querySelector("#minimum-profit").value = ordinary.policy.minimum_profit_kzt;
      document.querySelector("#undercut-step").value = ordinary.policy.undercut_step_kzt;
      document.querySelector("#city-id").value = ordinary.policy.city_id;
      document.querySelector("#zone-id").value = ordinary.policy.zone_id;
      selectedProduct.textContent += " · порог и зона взяты из обычного демпинга";
    }
  } catch (_) {
    // A product does not need an ordinary dumping policy to use Fast Dumping.
  }
};
const clearSelection = () => { productIdInput.value = ""; productSearch.value = ""; selectedProduct.textContent = "Товар не выбран"; selectedProduct.classList.remove("selected"); };

const searchProducts = async () => {
  const query = productSearch.value.trim();
  productIdInput.value = "";
  if (query.length < 2) { closeProductResults(); return; }
  if (searchController) searchController.abort();
  searchController = new AbortController();
  productResults.innerHTML = '<div class="product-result-empty">Ищу товар…</div>';
  productResults.classList.remove("hidden");
  try {
    const found = await request(`/api/product-registry/products?q=${encodeURIComponent(query)}&limit=20`, {signal:searchController.signal});
    const configured = new Map(rows.map((row) => [Number(row.product_id), row]));
    productResults.innerHTML = found.length ? found.map((row) => `<button class="product-result" type="button" data-product-id="${row.product_id}"><strong>${escapeHtml(row.name)}</strong><span>SKU ${escapeHtml(row.merchant_sku || "нет")} · Kaspi ${escapeHtml(row.kaspi_product_id)} · ${configured.has(Number(row.product_id)) ? "уже подключён" : "можно подключить"}</span></button>`).join("") : `<div class="product-result-empty">По запросу «${escapeHtml(query)}» ничего не найдено.</div>`;
    productResults.querySelectorAll(".product-result").forEach((button) => button.addEventListener("click", async () => {
      const id = Number(button.dataset.productId);
      const existing = configured.get(id);
      if (existing) { openEdit(existing); closeProductResults(); return; }
      const row = found.find((item) => Number(item.product_id) === id);
      if (row) await selectProduct(row);
    }));
  } catch (error) {
    if (error?.name !== "AbortError") productResults.innerHTML = `<div class="product-result-empty">${escapeHtml(error.message || "Ошибка поиска")}</div>`;
  }
};

const policyPayload = (prefix="") => ({
  enabled:document.querySelector(`#${prefix}enabled`).checked,
  minimum_profit_kzt:Number(document.querySelector(`#${prefix}minimum-profit`).value),
  undercut_step_kzt:Number(document.querySelector(`#${prefix}undercut-step`).value),
  allow_price_raise:document.querySelector(`#${prefix}allow-raise`).checked,
  max_undercut_gap_percent:Number(document.querySelector(`#${prefix}max-gap`).value),
  scan_interval_seconds:Number(document.querySelector(`#${prefix}scan-interval`).value),
  city_id:document.querySelector(`#${prefix}city-id`).value.trim(),
  zone_id:document.querySelector(`#${prefix}zone-id`).value.trim(),
});

const savePolicy = async (productId, payload) => request(`/api/fast-dumping/products/${productId}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});

const openEdit = (row) => {
  const policy = row.policy;
  document.querySelector("#edit-product-id").value = row.product_id;
  document.querySelector("#edit-title").textContent = row.name;
  document.querySelector("#edit-economics").textContent = `Себестоимость ${money(row.current_source?.unit_cost_kzt)} · текущий floor ${money(row.current_safe_floor_kzt)} · наша цена ${money(row.state?.own_price_kzt)}. После сохранения старое решение отменяется и товар сканируется заново.`;
  document.querySelector("#edit-minimum-profit").value = policy.minimum_profit_kzt;
  document.querySelector("#edit-undercut-step").value = policy.undercut_step_kzt;
  document.querySelector("#edit-scan-interval").value = policy.scan_interval_seconds;
  document.querySelector("#edit-max-gap").value = policy.max_undercut_gap_percent;
  document.querySelector("#edit-city-id").value = policy.city_id;
  document.querySelector("#edit-zone-id").value = policy.zone_id;
  document.querySelector("#edit-allow-raise").checked = policy.allow_price_raise;
  document.querySelector("#edit-enabled").checked = policy.enabled;
  editDialog.showModal();
};

policyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const productId = Number(productIdInput.value);
  if (!productId) { message.textContent = "Сначала выберите товар из результатов поиска."; return; }
  const button = document.querySelector("#save-policy"); setBusy(button, true, "Сохраняю…");
  try {
    await savePolicy(productId, policyPayload());
    message.textContent = "Товар подключён. Первая проверка поставлена в отдельную realtime-очередь.";
    clearSelection();
    await loadPage();
  } catch (error) { message.textContent = error.message || "Не удалось сохранить"; }
  finally { setBusy(button, false, ""); }
});

editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const productId = Number(document.querySelector("#edit-product-id").value);
  const button = document.querySelector("#edit-save"); setBusy(button, true, "Сохраняю…");
  try {
    await savePolicy(productId, policyPayload("edit-"));
    editDialog.close();
    message.textContent = "Настройки сохранены. Floor и цена будут рассчитаны заново по свежему рынку.";
    await loadPage();
  } catch (error) { message.textContent = error.message || "Не удалось изменить порог"; }
  finally { setBusy(button, false, ""); }
});

const actionClick = async (event) => {
  const container = event.target.closest("[data-product-id]");
  if (!container) return;
  const productId = Number(container.dataset.productId);
  const row = rows.find((item) => Number(item.product_id) === productId);
  if (!row) return;
  if (event.target.closest(".edit-policy")) { openEdit(row); return; }
  const resume = event.target.closest(".resume-product");
  const run = event.target.closest(".run-now");
  if (!resume && !run) return;
  const button = resume || run; setBusy(button, true, resume ? "Возобновляю…" : "Ставлю…");
  try {
    await request(`/api/fast-dumping/products/${productId}/${resume ? "resume" : "run"}`, {method:"POST"});
    message.textContent = resume ? "Защитная пауза снята. Запущена новая проверка без повторения старой операции." : "Товар поставлен в очередь Fast Agent.";
    await loadPage();
  } catch (error) { message.textContent = error.message || "Операция не выполнена"; }
  finally { setBusy(button, false, ""); }
};

list.addEventListener("click", actionClick);
list.addEventListener("toggle", async (event) => {
  const details = event.target.closest(".fast-details");
  if (!details || !details.open || details.dataset.loading === "true") return;
  const productId = Number(details.dataset.productId);
  const version = Number(details.dataset.stateVersion || 0);
  const container = details.querySelector(".offers-container");
  const cached = offersCache.get(productId);
  if (cached?.version === version) {
    container.innerHTML = offersTable(cached.offers);
    return;
  }
  details.dataset.loading = "true";
  container.innerHTML = '<p class="fast-card-reason">Загружаю офферы…</p>';
  try {
    const payload = await request(`/api/fast-dumping/products/${productId}/offers`);
    offersCache.set(productId, {version:Number(payload.state_version || 0), offers:payload.offers || []});
    container.innerHTML = offersTable(payload.offers || []);
  } catch (error) {
    container.innerHTML = `<p class="fast-card-reason">${escapeHtml(error.message || "Не удалось загрузить офферы")}</p>`;
  } finally {
    details.dataset.loading = "false";
  }
}, true);
floorList.addEventListener("click", actionClick);
statusFilter.addEventListener("change", () => render({items:rows, summary:{total:rows.length,enabled:rows.filter((r)=>r.policy.enabled).length,floor_limited:rows.filter(isFloor).length,attention:rows.filter((r)=>attentionStatuses.has(statusOf(r))).length,working:rows.filter((r)=>workingStatuses.has(statusOf(r))).length}, checked_at:new Date().toISOString()}));
productSearch.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchProducts, 250); });
document.addEventListener("click", (event) => { if (!event.target.closest("#product-picker")) closeProductResults(); });
document.querySelector("#edit-close").addEventListener("click", () => editDialog.close());
document.querySelector("#edit-cancel").addEventListener("click", () => editDialog.close());
tokenForm.addEventListener("submit", (event) => { event.preventDefault(); const token = tokenInput.value.trim(); if (!token) return; localStorage.setItem(storageKey, token); tokenInput.value = ""; loadPage(); });
refreshButton.addEventListener("click", () => loadPage());
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadPage({silent:true}); });
loadPage();
window.setInterval(() => { if (!document.hidden) loadPage({silent:true}); }, 10000);
