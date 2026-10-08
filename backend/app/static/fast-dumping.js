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
const attentionList = document.querySelector("#attention-list");
const attentionSection = document.querySelector("#attention-section");
const disabledList = document.querySelector("#disabled-list");
const disabledSection = document.querySelector("#disabled-section");
const empty = document.querySelector("#empty");
const editDialog = document.querySelector("#edit-dialog");
const editForm = document.querySelector("#edit-form");
const editRemove = document.querySelector("#edit-remove");
let rows = [];
let searchTimer = null;
let searchController = null;
let loading = false;
const offersCache = new Map();

const attentionStatuses = new Set(["price_anomaly","market_context_mismatch","own_offer_missing","out_of_stock","apply_timeout","apply_unconfirmed","verification_retry","error","apply_failed","merchant_write_failed","automation_market_incomplete","automation_market_stale"]);
const workingStatuses = new Set(["queued","scanning","queued_apply","preparing_apply","applying","verifying"]);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value) => value == null || value === "" ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ₸`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU") : "—";
const statusOf = (row) => row.policy.enabled ? row.state?.status || "idle" : "paused";
const isFloor = (row) => row.policy.enabled && !attentionStatuses.has(statusOf(row)) && (statusOf(row) === "floor_limited" || row.state?.decision_status === "floor_limited");
const productPhoto = (row, css = "fast-product-photo") => row.image_url
  ? `<img class="${css}" src="${escapeHtml(row.image_url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">`
  : `<span class="${css} placeholder" data-resolve-product-image data-product-id="${Number(row.product_id)}" data-image-class="${css}">Фото…</span>`;

const request = async (url, options = {}) => {
  const token = localStorage.getItem(storageKey) || "";
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  let response;
  let payload;
  try {
    response = await fetch(url, {cache:"no-store", signal:controller.signal, ...options, headers:{Authorization:`Bearer ${token}`, ...(options.headers || {})}});
    payload = await response.json().catch(() => ({}));
  } catch (error) {
    if (controller.signal.aborted) throw new Error("CRM не ответила за 30 секунд. Обновите страницу, чтобы проверить, сохранились ли настройки.");
    throw error;
  } finally { clearTimeout(timer); }
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
    applied:"Применено", watching:"Цена актуальна", delivery_advantage:"Быстрая доставка",
    preorder_position:"Место предзаказа", preorder_position_best_effort:"Место best-effort",
    floor_limited:"На пороге",
    owned_group_reset:"Возврат цикла", owned_group_band:"Цикл магазинов",
    owned_peer_guard:"Маржа сохранена", owned_cycle_sync:"Синхронизация магазинов",
    cooldown:"Интервал цены",
    price_anomaly:"Аномалия цены", market_context_mismatch:"Контекст не совпал",
    own_offer_missing:"Наша строка не найдена", out_of_stock:"Нет FIFO-остатка",
    apply_timeout:"Не подтверждено", apply_unconfirmed:"Защитная пауза", verification_retry:"Перепроверка", error:"Ошибка",
    paused:"Отключена", stale:"Решение устарело", apply_failed:"Ошибка записи",
  };
  const successStatuses = ["applied","watching","cooldown","delivery_advantage","preorder_position","preorder_position_best_effort","owned_group_reset","owned_group_band","owned_peer_guard","owned_cycle_sync"];
  const kind = isFloor(row) ? "floor" : workingStatuses.has(status) ? "working" : successStatuses.includes(status) ? "success" : attentionStatuses.has(status) || status === "apply_failed" ? "error" : "off";
  return {status, label:labels[status] || status, kind};
};

const offersTable = (offers) => {
  if (!Array.isArray(offers) || !offers.length) return '<p class="fast-card-reason">Диагностика офферов появится после первой проверки.</p>';
  return `<div class="offers-wrap"><table class="offers-table"><thead><tr><th>Продавец</th><th>Цена API</th><th>Роль</th><th>Доставка</th><th>В расчёте</th><th>Решение</th></tr></thead><tbody>${offers.map((offer) => {
    const rowClass = offer.is_owned_group ? "offer-own" : offer.used_for_dumping ? "" : "offer-ignore";
    const delivery = offer.delivery_days == null ? (offer.delivery || "не распознано") : `${Number(offer.delivery_days)} дн. · ${offer.delivery || "срок Kaspi"}`;
    const deliveryGap = offer.delivery_gap_days == null ? null : Number(offer.delivery_gap_days) > 0 ? `наша быстрее на ${Number(offer.delivery_gap_days)} дн.` : Number(offer.delivery_gap_days) < 0 ? `конкурент быстрее на ${Math.abs(Number(offer.delivery_gap_days))} дн.` : "одинаковый срок";
    const comparison = offer.is_owned_group ? "" : [offer.price_gap_kzt == null ? null : `разница ${money(offer.price_gap_kzt)}`, deliveryGap].filter(Boolean).join(" · ");
    const reason = offer.decision_reason || offer.ignored_reason || (offer.used_for_dumping ? "Выбран как ценовой ориентир" : "Не выбран");
    const decision = offer.is_own ? "Наша доставка" : offer.is_owned_peer ? reason : comparison ? `${comparison}. ${reason}` : reason;
    const role = offer.is_own ? "Наша строка" : offer.is_owned_peer ? "Свой магазин" : "Конкурент";
    return `<tr class="${rowClass}"><td>${escapeHtml(offer.merchant_name || offer.merchant_id || "—")}</td><td>${money(offer.price_kzt)}</td><td>${role}</td><td>${escapeHtml(delivery)}</td><td>${offer.is_owned_group ? "—" : offer.used_for_dumping ? "Да" : "Нет"}</td><td>${escapeHtml(decision)}</td></tr>`;
  }).join("")}</tbody></table></div>`;
};

const renderFloor = () => {
  const limited = rows.filter(isFloor);
  if (!floorSection.classList.contains("is-open")) return;
  document.querySelector("#floor-count").textContent = limited.length;
  floorList.innerHTML = limited.map((row) => `<article class="floor-item" data-product-id="${row.product_id}">
    <div><h3>${escapeHtml(row.name)}</h3><small>SKU ${escapeHtml(row.merchant_sku || "—")} · ${escapeHtml(row.state?.status_reason || "Конкурент ниже безопасного floor")}</small></div>
    <div class="floor-value"><span>Наша цена</span><strong>${money(row.state?.own_price_kzt)}</strong></div>
    <div class="floor-value"><span>Конкурент</span><strong>${money(row.state?.competitor_price_kzt)}</strong></div>
    <div class="floor-value"><span>Текущий floor</span><strong>${money(row.current_safe_floor_kzt ?? row.state?.safe_floor_kzt)}</strong></div>
    <div class="floor-value"><span>Мин. прибыль</span><strong>${money(row.policy.minimum_profit_kzt)}</strong></div>
    <div class="floor-action"><button class="button edit-policy" type="button">Изменить порог</button></div>
  </article>`).join("") || '<p class="fast-card-reason">Товаров на пороге нет.</p>';
};

const renderAttention = () => {
  if (!attentionSection.classList.contains("is-open")) return;
  const needingAttention = rows.filter((row) => attentionStatuses.has(statusOf(row)));
  document.querySelector("#attention-count").textContent = needingAttention.length;
  attentionList.innerHTML = needingAttention.map((row) => `<article class="attention-item" data-product-id="${row.product_id}">
    <div><span class="fast-status ${statusView(row).kind}">${escapeHtml(statusView(row).label)}</span><h3>${escapeHtml(row.name)}</h3><small>SKU ${escapeHtml(row.merchant_sku || "—")}</small><p>${escapeHtml(row.state?.last_error_message || row.state?.status_reason || "Проверьте настройки товара")}</p></div>
    <div class="floor-action"><button class="button secondary edit-policy" type="button">Настроить</button>${row.state?.automatic_writes_paused ? '<button class="button resume-product" type="button">Возобновить</button>' : ""}</div>
  </article>`).join("") || '<p class="fast-card-reason">Товаров, требующих внимания, нет.</p>';
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

const renderDisabled = () => {
  if (!disabledSection.classList.contains("is-open")) return;
  const disabled = rows.filter((row) => !row.policy.enabled);
  document.querySelector("#disabled-count").textContent = disabled.length;
  disabledList.innerHTML = disabled.map((row) => `<article class="attention-item" data-product-id="${row.product_id}"><div><span class="fast-status off">Отключена</span><h3>${escapeHtml(row.name)}</h3><small>SKU ${escapeHtml(row.merchant_sku || "—")}</small><p>${escapeHtml(row.state?.status_reason || "Демпинг выключен")}</p></div><div class="floor-action"><button class="button secondary edit-policy" type="button">Настроить</button></div></article>`).join("") || '<p class="fast-card-reason">Отключённых карточек нет.</p>';
};

const card = (row) => {
  const state = row.state || {};
  const view = statusView(row);
  const source = row.current_source || {};
  const scanDue = !state.next_scan_at || new Date(state.next_scan_at).getTime() <= Date.now();
  const canRun = row.policy.enabled && !state.automatic_writes_paused && !workingStatuses.has(view.status) && scanDue;
  const positionMode = (source.kind || state.source_kind) === "supplier";
  return `<article class="fast-card ${view.kind}" data-product-id="${row.product_id}">
    <div class="fast-card-head">
      <div class="fast-card-title"><span class="fast-status ${view.kind}">${escapeHtml(view.label)}</span>${productPhoto(row)}<div><h3>${escapeHtml(row.name)}</h3><p>Kaspi ${escapeHtml(row.kaspi_product_id)} · SKU ${escapeHtml(row.merchant_sku || "—")}${row.brand ? ` · ${escapeHtml(row.brand)}` : ""}</p></div></div>
      <div class="fast-card-actions"><button class="button secondary edit-policy" type="button">Настроить</button>${state.automatic_writes_paused ? '<button class="button resume-product" type="button">Возобновить</button>' : `<button class="button run-now" type="button" ${canRun ? "" : "disabled"}>Проверить сейчас</button>`}</div>
    </div>
    <div class="fast-card-grid">
      <div><span>FIFO-остаток</span><strong>${Number(row.current_inventory_on_hand || 0).toLocaleString("ru-RU")} шт.</strong><small>повторно читается перед write</small></div>
      <div><span>Себестоимость</span><strong>${money(source.unit_cost_kzt ?? state.source_cost_kzt)}</strong><small>${escapeHtml(source.name || state.source_name || "Нет источника")}</small></div>
      <div><span>Безопасный floor</span><strong>${money(row.current_safe_floor_kzt ?? state.safe_floor_kzt)}</strong><small>мин. прибыль ${money(row.policy.minimum_profit_kzt)}</small></div>
      <div><span>Наша цена</span><strong>${money(state.own_price_kzt)}</strong><small>${state.own_position ? `позиция №${state.own_position} из ${state.seller_count || "—"}` : "позиция —"}</small></div>
      <div><span>Лучший конкурент</span><strong>${money(state.competitor_price_kzt)}</strong><small>${escapeHtml(state.competitor_name || "—")}</small></div>
      <div><span>Целевая цена</span><strong>${money(state.target_price_kzt)}</strong><small>${row.policy.pricing_mode === "automation" ? "🚀 Полная автоматизация" : `шаг ${money(row.policy.undercut_step_kzt)}`}</small></div>
      <div><span>Место в предзаказе</span><strong>№${Number(row.policy.preorder_target_position || 4)}</strong><small>${positionMode ? "стратегия активна" : "включится при FIFO=0 + supplier"}</small></div>
      <div><span>Цена карточки</span><strong>${money(state.page_visible_price_kzt)}</strong><small>${state.market_context_ok ? "контекст подтверждён" : "ожидает подтверждения"}</small></div>
      <div><span>Последний scan</span><strong>${dateTime(state.last_scanned_at)}</strong><small>следующий ${dateTime(state.next_scan_at)}</small></div>
      <div><span>Последний apply</span><strong>${dateTime(state.last_applied_at)}</strong><small>${state.last_operation_id ? `operation ${escapeHtml(state.last_operation_id)}` : "операций ещё нет"}</small></div>
      <div><span>Интервал</span><strong>${Number(row.policy.pricing_mode === "automation" ? row.policy.automation_config?.monitor_seconds || 120 : row.policy.scan_interval_seconds) / 60} мин.</strong><small>проверка и максимум один write · аномалия ${Number(row.policy.max_undercut_gap_percent)}%</small></div>
      <div><span>Преимущество доставки</span><strong>до ${money(row.policy.delivery_price_premium_kzt)}</strong><small>для физического FIFO</small></div>
      <div><span>Цикл BARWORK ↔ LeoXpress</span><strong>${money(row.policy.owned_price_band_kzt)}</strong><small>якорь ${money(state.owned_cycle_anchor_price_kzt)}</small></div>
      <div><span>Agent / версия решения</span><strong>${escapeHtml(state.last_agent_id || "—")}</strong><small>state v${Number(state.state_version || 0)}</small></div>
      <div><span>Канал</span><strong>Realtime API</strong><small>XML — страховочное зеркало</small></div>
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
  document.querySelector("#summary-disabled").textContent = rows.filter((row) => !row.policy.enabled).length;
  document.querySelector("#summary-floor").textContent = summary.floor_limited || 0;
  document.querySelector("#summary-working").textContent = summary.working || 0;
  document.querySelector("#summary-attention").textContent = summary.attention || 0;
  renderFloor();
  renderAttention();
  renderDisabled();
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
      document.querySelector("#zone-id").value = ordinary.policy.zone_id;
      selectedProduct.textContent += " · порог и зона взяты из обычного демпинга; город Fast Dumping сохранён";
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
    productResults.innerHTML = found.length ? found.map((row) => `<button class="product-result" type="button" data-product-id="${row.product_id}">${productPhoto(row, "product-result-photo")}<span><strong>${escapeHtml(row.name)}</strong><span>SKU ${escapeHtml(row.merchant_sku || "нет")} · Kaspi ${escapeHtml(row.kaspi_product_id)} · ${configured.has(Number(row.product_id)) ? "уже подключён" : "можно подключить"}</span></span></button>`).join("") : `<div class="product-result-empty">По запросу «${escapeHtml(query)}» ничего не найдено.</div>`;
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
  delivery_price_premium_kzt:Number(document.querySelector(`#${prefix}delivery-premium`).value),
  delivery_advantage_days:Number(document.querySelector(`#${prefix}delivery-days`).value),
  owned_price_band_kzt:Number(document.querySelector(`#${prefix}owned-price-band`).value),
  preorder_target_position:Number(document.querySelector(`#${prefix}preorder-position`).value),
  city_id:document.querySelector(`#${prefix}city-id`).value.trim(),
  zone_id:document.querySelector(`#${prefix}zone-id`).value.trim(),
});

const savePolicy = async (productId, payload) => request(`/api/fast-dumping/products/${productId}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});

let editingRow = null;
const manualControls = ["undercut-step", "scan-interval", "preorder-position", "delivery-premium", "delivery-days", "owned-price-band", "max-gap", "allow-raise", "enabled"];
const setAutomation = (prefix, active) => {
  document.querySelector(`#${prefix}automation-toggle`).setAttribute("aria-pressed", String(active));
  document.querySelector(`#${prefix}automation-note`).textContent = active
    ? "Включена: приоритет прибыли и TOP-3 с учётом доставки. Применится после сохранения."
    : "Выключена. Применятся обычные настройки демпинга.";
  for (const name of manualControls) {
    const input = document.querySelector(`#${prefix}${name}`);
    input.disabled = active;
    input.closest("label").classList.toggle("hidden", active);
  }
};
for (const prefix of ["", "edit-"]) {
  document.querySelector(`#${prefix}automation-toggle`).addEventListener("click", () => {
    const active = document.querySelector(`#${prefix}automation-toggle`).getAttribute("aria-pressed") !== "true";
    if (prefix && editingRow?.policy.pricing_mode === "automation") {
      const previous = editingRow.policy.automation_config?.previous || {};
      document.querySelector("#edit-minimum-profit").value = active
        ? editingRow.policy.minimum_profit_kzt : previous.minimum_profit_kzt ?? editingRow.policy.minimum_profit_kzt;
      document.querySelector("#edit-enabled").checked = active || Boolean(previous.enabled);
    }
    setAutomation(prefix, active);
  });
}
const saveSettings = async (productId, prefix = "") => {
  const payload = policyPayload(prefix);
  const active = document.querySelector(`#${prefix}automation-toggle`).getAttribute("aria-pressed") === "true";
  const original = prefix ? editingRow?.policy : null;
  if (!active && original?.pricing_mode !== "automation") return savePolicy(productId, payload);
  const config = original?.automation_config || {};
  const settings = {enabled:active, minimum_profit_kzt:payload.minimum_profit_kzt, manual_policy:payload};
  for (const key of ["monitor_seconds", "premium_per_day_kzt", "premium_cap_kzt", "delivery_advantage_days", "maximum_price_kzt", "observation_hours"]) {
    if (config[key] !== undefined) settings[key] = config[key];
  }
  return request(`/api/full-automation/products/${productId}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(settings)});
};

const openEdit = (row) => {
  editingRow = row;
  document.querySelector("#edit-message").hidden = true;
  document.querySelector("#edit-message").textContent = "";
  const policy = row.policy;
  document.querySelector("#edit-product-id").value = row.product_id;
  document.querySelector("#edit-title").textContent = row.name;
  document.querySelector("#edit-economics").textContent = `Себестоимость ${money(row.current_source?.unit_cost_kzt)} · текущий floor ${money(row.current_safe_floor_kzt)} · наша цена ${money(row.state?.own_price_kzt)}. При supplier preorder Fast будет целиться в место №${Number(policy.preorder_target_position || 4)}. После сохранения товар сканируется заново.`;
  document.querySelector("#edit-minimum-profit").value = policy.minimum_profit_kzt;
  document.querySelector("#edit-undercut-step").value = policy.undercut_step_kzt;
  document.querySelector("#edit-scan-interval").value = policy.scan_interval_seconds;
  document.querySelector("#edit-max-gap").value = policy.max_undercut_gap_percent;
  document.querySelector("#edit-delivery-premium").value = policy.delivery_price_premium_kzt;
  document.querySelector("#edit-delivery-days").value = policy.delivery_advantage_days;
  document.querySelector("#edit-owned-price-band").value = policy.owned_price_band_kzt ?? 200;
  document.querySelector("#edit-preorder-position").value = policy.preorder_target_position || 4;
  document.querySelector("#edit-city-id").value = policy.city_id;
  document.querySelector("#edit-zone-id").value = policy.zone_id;
  document.querySelector("#edit-allow-raise").checked = policy.allow_price_raise;
  document.querySelector("#edit-enabled").checked = policy.enabled;
  setAutomation("edit-", policy.pricing_mode === "automation");
  editRemove.classList.toggle("hidden", policy.pricing_mode === "automation");
  editDialog.showModal();
};

policyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const productId = Number(productIdInput.value);
  if (!productId) { message.textContent = "Сначала выберите товар из результатов поиска."; return; }
  const button = document.querySelector("#save-policy"); setBusy(button, true, "Сохраняю…");
  try {
    await saveSettings(productId);
    setAutomation("", false);
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
  const editMessage = document.querySelector("#edit-message");
  editMessage.hidden = true;
  try {
    await saveSettings(productId, "edit-");
    editDialog.close();
    message.textContent = "Настройки сохранены. Fast пересчитает цену и место по свежему рынку.";
    await loadPage();
  } catch (error) {
    editMessage.textContent = error.message || "Не удалось изменить настройки";
    editMessage.hidden = false;
    message.textContent = editMessage.textContent;
  }
  finally { setBusy(button, false, ""); }
});

editRemove.addEventListener("click", async () => {
  const productId = Number(document.querySelector("#edit-product-id").value);
  const row = rows.find((item) => Number(item.product_id) === productId);
  if (!productId || !row) return;
  if (!window.confirm(`Удалить «${row.name}» из Fast Dumping? Товар, остатки и карточка Kaspi останутся без изменений.`)) return;
  setBusy(editRemove, true, "Удаляю…");
  try {
    await request(`/api/fast-dumping/products/${productId}`, {method:"DELETE"});
    editDialog.close();
    offersCache.delete(productId);
    message.textContent = "Товар удалён из Fast Dumping. Карточка, остатки и текущий offer Kaspi не изменялись.";
    await loadPage();
  } catch (error) {
    message.textContent = error.message || "Не удалось удалить товар из Fast Dumping";
  } finally {
    setBusy(editRemove, false, "");
  }
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
    const result = await request(`/api/fast-dumping/products/${productId}/${resume ? "resume" : "run"}`, {method:"POST"});
    message.textContent = resume ? "Защитная пауза снята. Запущена новая проверка без повторения старой операции." : result.queued ? "Товар поставлен в очередь Fast Agent." : "Следующая проверка будет выполнена по выбранному интервалу.";
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
attentionList.addEventListener("click", actionClick);
disabledList.addEventListener("click", actionClick);
for (const [name, section, renderContent] of [["floor", floorSection, renderFloor], ["attention", attentionSection, renderAttention], ["disabled", disabledSection, renderDisabled]]) {
  const toggle = document.querySelector(`#${name}-toggle`);
  toggle.addEventListener("click", () => {
    const open = section.classList.toggle("is-open");
    toggle.setAttribute("aria-expanded", String(open));
    section.setAttribute("aria-hidden", String(!open));
    section.inert = !open;
    if (open) renderContent();
  });
}
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
