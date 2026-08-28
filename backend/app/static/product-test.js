"use strict";

const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#lab-page");
const message = document.querySelector("#message");
let refreshTimer = null;

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value) => value == null || value === "" ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ₸`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU") : "—";
const request = async (url, options = {}) => {
  const token = localStorage.getItem(storageKey) || "";
  const response = await fetch(url, {cache:"no-store", ...options, headers:{Authorization:`Bearer ${token}`, ...(options.body ? {"Content-Type":"application/json"} : {}), ...(options.headers || {})}});
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) { localStorage.removeItem(storageKey); throw new Error("SERVICE_API_TOKEN не принят"); }
  if (!response.ok) throw new Error(payload.detail || `API вернул HTTP ${response.status}`);
  return payload;
};
const notify = (text, kind = "") => { message.textContent = text; message.className = `message ${kind}`.trim(); };
const setBusy = (button, busy, label) => { if (!button.dataset.label) button.dataset.label = button.textContent; button.disabled = busy; button.textContent = busy ? label : button.dataset.label; };
const scheduleRefresh = (delay = 3000) => {
  if (refreshTimer) return;
  refreshTimer = window.setTimeout(() => { refreshTimer = null; load(); }, Math.max(500, delay));
};

const statusText = (item) => ({
  ready_to_add: "Готово к вашей визуальной проверке",
  needs_supplier_link: item.last_error || "Точное совпадение Ozon не найдено — вставьте ссылку",
  needs_supplier_validation: "Ссылка изменена — проверьте",
  validating_supplier: "Проверяем ссылку",
  adding_to_kaspi: "Выгружаем на Kaspi и ждём реальный SKU",
  enrolled_fast_dumping: "Создан в Товарах, подключён к Мониторингу и Быстрому демпингу",
  error: item.last_error || "Ошибка",
}[item.status] || item.status || "Кандидат");

const ratingLine = (rating, reviews) => {
  const score = Number(rating);
  const count = Number(reviews);
  if (!Number.isFinite(score) && !Number.isFinite(count)) return '<span class="muted">Рейтинг —</span>';
  return `<span class="rating">★ ${Number.isFinite(score) ? score.toFixed(1) : "—"}${Number.isFinite(count) ? ` · ${count.toLocaleString("ru-RU")} отзывов` : ""}</span>`;
};

const marketProduct = ({market, title, brand, sku, image, url, rating, reviews}) => `<div class="market-product ${market.toLowerCase()}">
  ${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(`${market}: ${title}`)}" loading="lazy" referrerpolicy="no-referrer">` : `<div class="market-image-placeholder">Нет фото ${escapeHtml(market)}</div>`}
  <div class="market-copy"><strong>${escapeHtml(title || `Карточка ${market}`)}</strong><small>${escapeHtml([brand, sku].filter(Boolean).join(" · ") || "—")}</small>${ratingLine(rating, reviews)}${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(market)} ↗</a>` : '<span class="muted">Ссылка не найдена</span>'}</div>
</div>`;

const itemRow = (item, index) => {
  const supplier = item.offers?.supplier || {};
  const kaspi = item.offers?.kaspi || {};
  const autoOzon = item.offers?.ozon?.best || {};
  const ozonImage = supplier.supplier_image_url || autoOzon.image_url || "";
  const ozonTitle = supplier.supplier_product_title || autoOzon.title || "Точное совпадение Ozon не найдено";
  const visual = supplier.image_match || {};
  const score = supplier.match_score == null ? null : Math.round(Number(supplier.match_score) * 100);
  const visualText = visual.status === "CONFIRM" ? "Фото совпало" : visual.status === "SUPPORT" ? "Фото похоже" : "Проверить фото";
  const sellerOffers = Number(supplier.total_supplier_offers_checked || supplier.supplier_offer_count || 0);
  const source = String(supplier.supplier_price_source || "");
  const cardPrice = source.startsWith("search_card.") || source.startsWith("product_page.");
  const priceSource = source.startsWith("product_page.") ? "цена страницы Ozon" : cardPrice ? "цена карточки Ozon" : sellerOffers ? `${sellerOffers} предложений` : "";
  const supplierLabel = supplier.supplier_seller_name || (cardPrice ? "Ozon" : "не подтверждён");
  const locked = ["validating_supplier", "adding_to_kaspi", "enrolled_fast_dumping"].includes(item.status);
  const canAdd = item.status === "ready_to_add" && supplier.validated;
  const deliveryText = supplier.supplier_delivery_text || (supplier.supplier_delivery_date ? `до ${supplier.supplier_delivery_date}` : supplier.supplier_delivery_days != null ? `${supplier.supplier_delivery_days} дн.` : "—");
  const matchClass = supplier.match_status === "CONFIRMED" ? "found" : supplier.match_status === "REVIEW" || supplier.match_status === "MANUAL_REVIEW" ? "review" : "missing";
  return `<article class="lab-result-row ${matchClass}" data-id="${item.id}" data-supplier-url="${escapeHtml(item.supplier_url || "")}" data-can-add="${canAdd ? "1" : "0"}">
    <div class="row-number">${index + 1}</div>
    <div data-label="KASPI">${marketProduct({market:"Kaspi", title:item.name, brand:item.brand, sku:item.kaspi_product_id, image:item.image_url, url:item.kaspi_url, rating:kaspi.rating, reviews:kaspi.reviews})}</div>
    <div class="table-value" data-label="KASPI ЦЕНА"><strong>${money(item.observed_price_kzt)}</strong><small>конкурент</small></div>
    <div data-label="OZON">${marketProduct({market:"Ozon", title:ozonTitle, brand:autoOzon.brand, sku:supplier.supplier_offer_sku || autoOzon.sku, image:ozonImage, url:item.supplier_url, rating:supplier.supplier_rating ?? autoOzon.rating, reviews:supplier.supplier_reviews ?? autoOzon.reviews})}</div>
    <div class="table-value supplier-cost" data-label="SUPPLIER COST"><strong>${money(supplier.supplier_price_kzt)}</strong><small>${escapeHtml(supplierLabel)}</small><em>${escapeHtml(priceSource)}</em></div>
    <div class="table-value" data-label="ДОСТАВКА"><strong>${escapeHtml(deliveryText)}</strong>${supplier.supplier_delivery_days != null ? `<small>${supplier.supplier_delivery_days} дн.</small>` : ""}</div>
    <div class="match-cell" data-label="MATCH"><strong>${score == null ? "—" : `${score}%`}</strong><span>${escapeHtml(visualText)}</span><small>${escapeHtml(supplier.match_status || "NO_RESULT")}</small></div>
    <div class="result-actions" data-label="СТАТУС / ДЕЙСТВИЯ"><span class="result-status ${matchClass}">${escapeHtml(statusText(item))}</span><label><span>Правильная ссылка Ozon</span><input class="supplier" type="url" maxlength="4000" value="${escapeHtml(item.supplier_url || "")}" placeholder="https://www.ozon.kz/product/…" ${locked ? "disabled" : ""}></label><button class="button validate" type="button" ${locked ? "disabled" : ""}>Проверить / заменить</button><button class="button add" type="button" ${canAdd ? "" : "disabled"}>Выгрузить на Kaspi</button>${item.product_id ? `<div class="enrolled-links"><a href="/crm/products/${item.product_id}">Товар</a><a href="/crm/monitoring">Мониторинг</a><a href="/crm/fast-dumping">Демпинг</a></div>` : ""}</div>
  </article>`;
};

const submissionRow = (item) => {
  const submission = item.offers?.kaspi_submission || {};
  const status = submission.status || "waiting";
  const waiting = status === "waiting";
  const succeeded = status === "succeeded";
  const title = waiting ? "Ждём появления товара на Kaspi" : succeeded ? "Успешно выгружен и обнаружен на Kaspi" : "Kaspi не подтвердил выгрузку";
  const meta = succeeded
    ? `SKU ${submission.merchant_sku || item.merchant_sku || "—"} · цена ${money(submission.actual_price_kzt || item.test_price_kzt)}`
    : waiting
      ? `попытка ${Number(submission.attempt || 1)} · поставлен ${dateTime(submission.queued_at)}`
      : (submission.error || item.last_error || "Неизвестная ошибка");
  return `<article class="submission-row ${escapeHtml(status)}" data-id="${item.id}">
    ${item.image_url ? `<img src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.name)}" loading="lazy" referrerpolicy="no-referrer">` : '<div class="submission-image-placeholder">Нет фото</div>'}
    <div class="submission-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml([item.brand, item.kaspi_product_id].filter(Boolean).join(" · "))}</small><span>${escapeHtml(title)}</span><em>${escapeHtml(meta)}</em></div>
    <div class="submission-actions"><span class="submission-badge ${escapeHtml(status)}">${waiting ? "ОЖИДАНИЕ" : succeeded ? "УСПЕШНО" : "ОШИБКА"}</span>${succeeded ? `<div class="enrolled-links"><a href="/crm/products/${item.product_id}">Товар</a><a href="/crm/monitoring">Мониторинг</a><a href="/crm/fast-dumping">Демпинг</a></div><small>исчезнет после ${dateTime(submission.hide_after)}</small>` : status === "failed" ? '<button class="button retry" type="button">Повторить выгрузку</button>' : '<small>Product Test Agent проверяет Merchant Cabinet</small>'}</div>
  </article>`;
};

const renderSubmissions = (submissions) => {
  const section = document.querySelector("#kaspi-submissions-section");
  const list = document.querySelector("#kaspi-submissions");
  section.classList.toggle("hidden", submissions.length === 0);
  list.innerHTML = submissions.map(submissionRow).join("");
  if (submissions.some((item) => (item.offers?.kaspi_submission?.status || "waiting") === "waiting")) {
    scheduleRefresh(3000);
  } else if (submissions.some((item) => item.offers?.kaspi_submission?.status === "succeeded")) {
    const remaining = submissions
      .filter((item) => item.offers?.kaspi_submission?.status === "succeeded")
      .map((item) => new Date(item.offers.kaspi_submission.hide_after).getTime() - Date.now())
      .filter((value) => Number.isFinite(value) && value > 0);
    scheduleRefresh(Math.min(30000, ...(remaining.length ? remaining : [30000])) + 100);
  }
};

const renderJobs = (jobs) => {
  const relevant = jobs.filter((job) => ["discover", "validate_supplier"].includes(job.job_type));
  const active = relevant.filter((job) => ["queued", "leased", "failed"].includes(job.status)).slice(0, 6);
  const pending = active.filter((job) => ["queued", "leased"].includes(job.status));
  const lastSearch = relevant.find((job) => job.job_type === "discover" && job.status === "succeeded" && job.result);
  const labels = {discover:"Поиск новых товаров", validate_supplier:"Проверка Ozon"};
  const summary = lastSearch ? `<div class="job success"><strong>Последний поиск завершён</strong> · проверено ${Number(lastSearch.result.matched_products_checked || 0)}, точных пар ${Number(lastSearch.result.confirmed_pairs || 0)}, на ручную проверку ${Number(lastSearch.result.manual_review_pairs || 0)}</div>` : "";
  document.querySelector("#jobs").innerHTML = summary + active.map((job) => `<div class="job ${job.status === "failed" ? "failed" : "pending"}"><strong>${escapeHtml(labels[job.job_type] || job.job_type)}</strong> · ${job.status === "leased" ? "Product Test Agent выполняет" : job.status === "queued" ? "ожидает Product Test Agent" : escapeHtml(job.error_message || "ошибка")}</div>`).join("");
  if (pending.length) scheduleRefresh(3000);
};

const renderAgent = (payload) => {
  const panel = document.querySelector("#product-test-agent");
  const title = document.querySelector("#product-test-agent-title");
  const meta = document.querySelector("#product-test-agent-meta");
  const badge = document.querySelector("#product-test-agent-status");
  const agent = payload?.agents?.[0];
  const online = Boolean(payload?.online && agent?.online);
  panel.classList.toggle("ready", online);
  panel.classList.toggle("missing", !online);
  badge.className = `agent-pill ${online ? "online" : "offline"}`;
  badge.textContent = online ? "В сети" : "Не в сети";
  if (!agent) {
    title.textContent = "Product Test Agent ещё не подключался";
    meta.textContent = "Скачайте и запустите отдельный агент — он не ждёт мониторинг или Быстрый демпинг.";
    return;
  }
  title.textContent = online ? `Подключён: ${agent.hostname || agent.agent_id}` : `Нет связи: ${agent.hostname || agent.agent_id}`;
  meta.textContent = `версия ${agent.version || "—"} · workspace ${agent.workspace_id} · heartbeat ${dateTime(agent.last_seen_at)}`;
};

const fillSettings = (settings) => {
  const form = document.querySelector("#settings-form");
  Object.entries(settings || {}).forEach(([key, value]) => {
    const field = form.elements.namedItem(key); if (!field) return;
    if (key === "image_verify") field.checked = true;
    else if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
  });
  if (settings?.target_new) document.querySelector("#target-new").value = settings.target_new;
};

const render = (payload) => {
  if (refreshTimer) { window.clearTimeout(refreshTimer); refreshTimer = null; }
  const items = payload.items || [];
  const submissions = payload.submissions || [];
  document.querySelector("#items").innerHTML = items.length ? `<div class="lab-results-table"><div class="lab-table-head"><span>#</span><span>KASPI</span><span>KASPI ЦЕНА</span><span>OZON</span><span>SUPPLIER COST</span><span>ДОСТАВКА</span><span>MATCH</span><span>СТАТУС / ДЕЙСТВИЯ</span></div>${items.map(itemRow).join("")}</div>` : "";
  document.querySelector("#empty").classList.toggle("hidden", items.length > 0);
  document.querySelector("#total-count").textContent = items.filter((item) => item.status !== "enrolled_fast_dumping").length;
  document.querySelector("#ready-count").textContent = items.filter((item) => item.status === "ready_to_add").length;
  document.querySelector("#job-count").textContent = (payload.jobs || []).filter((job) => ["queued", "leased"].includes(job.status)).length;
  document.querySelector("#enrolled-count").textContent = submissions.filter((item) => item.offers?.kaspi_submission?.status === "succeeded").length;
  fillSettings(payload.settings || {});
  renderJobs(payload.jobs || []);
  renderSubmissions(submissions);
  renderAgent(payload.agent || {});
};

async function load() {
  if (!localStorage.getItem(storageKey)) { authPanel.classList.remove("hidden"); page.classList.add("hidden"); return; }
  try { const state = await request("/api/product-test"); authPanel.classList.add("hidden"); page.classList.remove("hidden"); render(state); notify(""); }
  catch (error) { notify(error.message, "error"); }
}

document.querySelector("#token-form").addEventListener("submit", (event) => { event.preventDefault(); localStorage.setItem(storageKey, document.querySelector("#token").value.trim()); load(); });
document.querySelector("#refresh").addEventListener("click", load);
document.querySelector("#discover-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = document.querySelector("#discover-button"); setBusy(button, true, "Передаю Agent…");
  try { await request("/api/product-test/discover", {method:"POST", body:JSON.stringify({query:document.querySelector("#query").value.trim(), target_new:Number(document.querySelector("#target-new").value)})}); notify("Быстрый поиск запущен. Кандидаты появятся автоматически.", "success"); await load(); }
  catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
document.querySelector("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const button = form.querySelector("button"); setBusy(button, true, "Сохраняю…");
  const body = {}; new FormData(form).forEach((value, key) => { body[key] = ["city_id", "zone_id"].includes(key) ? String(value) : Number(value); });
  body.image_verify = form.elements.image_verify.checked; body.allow_price_raise = form.elements.allow_price_raise.checked;
  try { await request("/api/product-test/settings", {method:"PATCH", body:JSON.stringify(body)}); notify("Значения по умолчанию сохранены.", "success"); await load(); }
  catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
document.querySelector("#items").addEventListener("click", async (event) => {
  const card = event.target.closest(".lab-result-row"); const button = event.target.closest("button"); if (!card || !button) return;
  setBusy(button, true, button.classList.contains("add") ? "Выгружаю…" : "Проверяю…");
  try {
    if (button.classList.contains("validate")) {
      const supplierUrl = card.querySelector(".supplier").value.trim();
      await request(`/api/product-test/items/${card.dataset.id}/validate-supplier`, {method:"POST", body:JSON.stringify({supplier_url:supplierUrl})});
      notify("Ссылка передана отдельному Product Test Agent. После проверки старая привязка будет заменена.", "success");
    } else if (button.classList.contains("add")) {
      const currentUrl = card.querySelector(".supplier").value.trim();
      if (currentUrl !== (card.dataset.supplierUrl || "")) throw new Error("Ссылка Ozon изменена. Сначала нажмите «Проверить / заменить ссылку».");
      await request(`/api/product-test/items/${card.dataset.id}/add`, {method:"POST"});
      notify("Выгрузка запущена. После подтверждения Kaspi обычная карточка появится в «Товарах» и подключится к существующему Мониторингу и Быстрому демпингу.", "success");
    }
    await load();
  } catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
document.querySelector("#items").addEventListener("input", (event) => {
  if (!event.target.classList.contains("supplier")) return;
  const card = event.target.closest(".lab-result-row"); if (!card) return;
  const dirty = event.target.value.trim() !== (card.dataset.supplierUrl || "");
  card.classList.toggle("link-dirty", dirty);
  const add = card.querySelector(".add"); if (add) add.disabled = dirty || card.dataset.canAdd !== "1";
});
document.querySelector("#kaspi-submissions").addEventListener("click", async (event) => {
  const row = event.target.closest(".submission-row"); const button = event.target.closest("button.retry");
  if (!row || !button) return;
  setBusy(button, true, "Повторяю…");
  try {
    await request(`/api/product-test/items/${row.dataset.id}/add`, {method:"POST"});
    notify("Повторная выгрузка передана Product Test Agent.", "success");
    await load();
  } catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
load();
