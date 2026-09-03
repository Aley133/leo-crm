"use strict";

const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#lab-page");
const message = document.querySelector("#message");
const pageMode = document.body.dataset.productTestPage || "product-test";
const isAddProductPage = pageMode === "add-product";
let refreshTimer = null;
let currentNewCards = new Map();
const localNewCardDrafts = new Map();
const newCardSaveTimers = new Map();
const newCardSaveVersions = new Map();
const newCardSaveChains = new Map();

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
  needs_supplier_link: item.last_error || (item.offers?.discovery?.mode === "popular" ? "Ходовой товар отобран — вставьте ссылку Ozon" : "Точное совпадение Ozon не найдено — вставьте ссылку"),
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

const marketProduct = ({market, title, brand, sku, image, url, rating, reviews, sellers}) => `<div class="market-product ${market.toLowerCase()}">
  ${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(`${market}: ${title}`)}" loading="lazy" referrerpolicy="no-referrer">` : `<div class="market-image-placeholder">Нет фото ${escapeHtml(market)}</div>`}
  <div class="market-copy"><strong>${escapeHtml(title || `Карточка ${market}`)}</strong><small>${escapeHtml([brand, sku].filter(Boolean).join(" · ") || "—")}</small>${ratingLine(rating, reviews)}${sellers != null && Number.isFinite(Number(sellers)) ? `<span class="seller-count">${Number(sellers).toLocaleString("ru-RU")} продавцов</span>` : ""}${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(market)} ↗</a>` : '<span class="muted">Ссылка не найдена</span>'}</div>
</div>`;

const itemRow = (item, index) => {
  const supplier = item.offers?.supplier || {};
  const kaspi = item.offers?.kaspi || {};
  const autoOzon = item.offers?.ozon?.best || {};
  const manual = Boolean(supplier.manual_override);
  const ozonImage = supplier.supplier_image_url || (manual ? "" : autoOzon.image_url) || "";
  const ozonTitle = supplier.supplier_product_title || (manual ? "Карточка по вашей ссылке Ozon" : autoOzon.title) || "Точное совпадение Ozon не найдено";
  const visual = supplier.image_match || {};
  const score = supplier.match_score == null ? null : Math.round(Number(supplier.match_score) * 100);
  const visualText = manual ? "Выбрано вами" : visual.status === "CONFIRM" ? "Фото совпало" : visual.status === "SUPPORT" ? "Фото похоже" : "Проверить фото";
  const sellerOffers = Number(supplier.total_supplier_offers_checked || supplier.supplier_offer_count || 0);
  const source = String(supplier.supplier_price_source || "");
  const exactPagePrice = source.startsWith("manual_product_page.") || source.startsWith("product_page.");
  const cardPrice = source.startsWith("search_card.") || exactPagePrice;
  const priceSource = source.startsWith("manual_product_page.") ? "точно по вашей ссылке" : exactPagePrice ? "цена страницы Ozon" : cardPrice ? "цена карточки Ozon" : sellerOffers ? `${sellerOffers} предложений` : "";
  const supplierLabel = supplier.supplier_seller_name || (cardPrice ? "Ozon" : "не подтверждён");
  const locked = ["validating_supplier", "adding_to_kaspi", "enrolled_fast_dumping"].includes(item.status);
  const canAdd = item.status === "ready_to_add" && supplier.validated;
  const deliveryText = supplier.supplier_delivery_text || (supplier.supplier_delivery_date ? `до ${supplier.supplier_delivery_date}` : supplier.supplier_delivery_days != null ? `${supplier.supplier_delivery_days} дн.` : "—");
  const matchClass = ["CONFIRMED", "OPERATOR_CONFIRMED"].includes(supplier.match_status) ? "found" : supplier.match_status === "REVIEW" || supplier.match_status === "MANUAL_REVIEW" ? "review" : "missing";
  return `<article class="lab-result-row ${matchClass}" data-id="${item.id}" data-supplier-url="${escapeHtml(item.supplier_url || "")}" data-can-add="${canAdd ? "1" : "0"}">
    <div class="row-number">${index + 1}</div>
    <div data-label="KASPI">${marketProduct({market:"Kaspi", title:item.name, brand:item.brand, sku:item.kaspi_product_id, image:item.image_url, url:item.kaspi_url, rating:kaspi.rating, reviews:kaspi.reviews, sellers:kaspi.seller_count})}</div>
    <div class="table-value" data-label="KASPI ЦЕНА"><strong>${money(item.observed_price_kzt)}</strong><small>конкурент</small></div>
    <div data-label="OZON">${marketProduct({market:"Ozon", title:ozonTitle, brand:autoOzon.brand, sku:supplier.supplier_offer_sku || autoOzon.sku, image:ozonImage, url:item.supplier_url, rating:supplier.supplier_rating ?? autoOzon.rating, reviews:supplier.supplier_reviews ?? autoOzon.reviews})}</div>
    <div class="table-value supplier-cost" data-label="SUPPLIER COST"><strong>${money(supplier.supplier_price_kzt)}</strong><small>${escapeHtml(supplierLabel)}</small><em>${escapeHtml(priceSource)}</em></div>
    <div class="table-value" data-label="ДОСТАВКА"><strong>${escapeHtml(deliveryText)}</strong>${supplier.supplier_delivery_days != null ? `<small>${supplier.supplier_delivery_days} дн.</small>` : ""}</div>
    <div class="table-value kaspi-plan" data-label="СТАРТ KASPI"><strong>${money(item.test_price_kzt)}</strong><small>preOrder ${Math.max(1, Number(item.preorder_days || 1))} дн.</small></div>
    <div class="match-cell" data-label="MATCH"><strong>${score == null ? "—" : `${score}%`}</strong><span>${escapeHtml(visualText)}</span><small>${escapeHtml(supplier.match_status || "NO_RESULT")}</small></div>
    <div class="result-actions" data-label="СТАТУС / ДЕЙСТВИЯ"><span class="result-status ${matchClass}">${escapeHtml(statusText(item))}</span><label><span>Правильная ссылка Ozon</span><input class="supplier" type="url" maxlength="4000" value="${escapeHtml(item.supplier_url || "")}" placeholder="https://www.ozon.kz/product/…" ${locked ? "disabled" : ""}></label><button class="button validate" type="button" ${locked ? "disabled" : ""}>Проверить / заменить</button><button class="button add" type="button" ${canAdd ? "" : "disabled"}>Выгрузить на Kaspi</button>${item.product_id ? `<div class="enrolled-links"><a href="/crm/products/${item.product_id}">Товар</a><a href="/crm/monitoring">Мониторинг</a><a href="/crm/fast-dumping">Демпинг</a></div>` : ""}</div>
  </article>`;
};

const submissionRow = (item) => {
  const submission = item.offers?.kaspi_submission || {};
  const displayImage = item.image_url || item.offers?.new_card?.images?.[0] || item.offers?.supplier?.supplier_image_url || "";
  const newCardRoute = submission.route === "new_card";
  const status = submission.status || "waiting";
  const waiting = status === "waiting";
  const succeeded = status === "succeeded";
  const autoDismiss = status === "failed" && Boolean(submission.terminal_rejection && submission.hide_after);
  const retryAllowed = !newCardRoute || submission.stage === "product_import";
  const title = waiting
    ? newCardRoute && submission.stage === "product_import"
      ? "Kaspi проверяет новую карточку"
      : newCardRoute
        ? "Карточка принята: ждём masterSku и создаём оффер"
        : "Ждём появления товара на Kaspi"
    : succeeded ? "Успешно выгружен и обнаружен на Kaspi" : "Kaspi не подтвердил выгрузку";
  const meta = succeeded
    ? `SKU ${submission.merchant_sku || item.merchant_sku || "—"} · цена ${money(submission.actual_price_kzt || item.test_price_kzt)}`
    : waiting
      ? newCardRoute && submission.stage === "moderation"
        ? `Product Import ${submission.import_code || "—"} · следующая проверка ${dateTime(submission.next_check_at)}`
        : `попытка ${Number(submission.attempt || 1)} · поставлен ${dateTime(submission.queued_at)}`
      : (submission.error || item.last_error || "Неизвестная ошибка");
  return `<article class="submission-row ${escapeHtml(status)}" data-id="${item.id}" data-route="${newCardRoute ? "new_card" : "existing_card"}">
    ${displayImage ? `<img src="${escapeHtml(displayImage)}" alt="${escapeHtml(item.name)}" loading="lazy" referrerpolicy="no-referrer">` : '<div class="submission-image-placeholder">Нет фото</div>'}
    <div class="submission-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml([item.brand, item.kaspi_product_id].filter(Boolean).join(" · "))}</small><span>${escapeHtml(title)}</span><em>${escapeHtml(meta)}</em></div>
    <div class="submission-actions"><span class="submission-badge ${escapeHtml(status)}">${waiting ? "ОЖИДАНИЕ" : succeeded ? "УСПЕШНО" : "ОШИБКА"}</span>${succeeded ? `<div class="enrolled-links"><a href="/crm/products/${item.product_id}">Товар</a><a href="/crm/monitoring">Мониторинг</a><a href="/crm/fast-dumping">Демпинг</a></div><small>исчезнет после ${dateTime(submission.hide_after)}</small>` : autoDismiss ? `<small>категория недоступна · исчезнет после ${dateTime(submission.hide_after)}</small>` : status === "failed" && retryAllowed ? `<button class="button retry" type="button">${newCardRoute ? "Исправить и повторить" : "Повторить выгрузку"}</button>` : status === "failed" ? `<small>Product Import уже принят · проверьте карточку в кабинете Kaspi</small>` : `<small>${newCardRoute ? "Product Test Agent сам продолжит после модерации" : "Product Test Agent проверяет Merchant Cabinet"}</small>`}</div>
  </article>`;
};

const renderSubmissions = (submissions) => {
  const section = document.querySelector("#kaspi-submissions-section");
  const list = document.querySelector("#kaspi-submissions");
  if (!section || !list) return;
  section.classList.toggle("hidden", submissions.length === 0);
  list.innerHTML = submissions.map(submissionRow).join("");
  if (submissions.some((item) => (item.offers?.kaspi_submission?.status || "waiting") === "waiting")) {
    const hasImmediate = submissions.some((item) => {
      const value = item.offers?.kaspi_submission || {};
      return value.status === "waiting" && !(value.route === "new_card" && value.stage === "moderation");
    });
    scheduleRefresh(hasImmediate ? 3000 : 30000);
  } else if (submissions.some((item) => item.offers?.kaspi_submission?.hide_after)) {
    const remaining = submissions
      .filter((item) => item.offers?.kaspi_submission?.hide_after)
      .map((item) => new Date(item.offers.kaspi_submission.hide_after).getTime() - Date.now())
      .filter((value) => Number.isFinite(value) && value > 0);
    scheduleRefresh(Math.min(30000, ...(remaining.length ? remaining : [30000])) + 100);
  }
};

const newCardStatus = (item, mappingBusy = false) => ({
  new_card_draft: "Нужно проверить и заполнить обязательные поля",
  new_card_ready: "Черновик готов к Product Import",
  new_card_mapping: mappingBusy ? "Product Test Agent загружает поля категории" : "Поля категории загружены — карточку можно создавать",
  new_card_importing: "Kaspi выполняет Product Import",
  new_card_moderation: "Принято Kaspi, ожидаем masterSku",
  new_card_error: item.last_error || "Kaspi отклонил карточку — исправьте черновик",
}[item.status] || item.status || "Черновик");

const newCardAttributeKey = (row, index = 0) => String(row?.code || `index:${index}`).trim().toLowerCase();

const withLocalNewCardDraft = (item) => {
  const local = localNewCardDrafts.get(String(item.id));
  if (!local) return item;
  const serverDraft = item.offers?.new_card || {};
  const localAttributes = new Map((local.attributes || []).map((row, index) => [newCardAttributeKey(row, index), row]));
  const attributes = (serverDraft.attributes || []).map((row, index) => {
    const edited = localAttributes.get(newCardAttributeKey(row, index));
    return edited ? {...row, value:edited.value, manual_override:edited.manual_override ?? row.manual_override} : row;
  });
  return {
    ...item,
    offers:{
      ...(item.offers || {}),
      new_card:{...serverDraft, ...local, attributes},
    },
  };
};

const newCardRow = (item, activeJobTypes = new Set()) => {
  const draft = item.offers?.new_card || {};
  const supplier = item.offers?.supplier || {};
  const locked = ["new_card_importing", "new_card_moderation", "enrolled_fast_dumping"].includes(item.status);
  const mappingBusy = activeJobTypes.has("map_new_card_category");
  const editingLocked = locked || mappingBusy;
  const errors = draft.validation_errors || [];
  const categories = draft.categories || [];
  const attributes = draft.attributes || [];
  const displayImage = draft.images?.[0] || supplier.supplier_image_url || item.image_url || "";
  const delivery = supplier.supplier_delivery_text || (supplier.supplier_delivery_days != null ? `${supplier.supplier_delivery_days} дн.` : "—");
  const categoryList = categories.map((row) => `<option value="${escapeHtml(row.code)}">${escapeHtml(row.title)}</option>`).join("");
  const images = (draft.images || []).map((url, index) => `<label class="new-card-image"><img src="${escapeHtml(url)}" alt="Фото Ozon ${index + 1}" loading="lazy" referrerpolicy="no-referrer"><span><input class="new-card-image-use" type="checkbox" data-url="${escapeHtml(url)}" ${index < 10 ? "checked" : ""} ${editingLocked ? "disabled" : ""}> использовать</span></label>`).join("");
  const attrRows = attributes.map((row, index) => {
    const allowed = row.allowed_values || [];
    const listId = `new-card-attr-${item.id}-${index}`;
    const options = allowed.map((value) => `<option value="${escapeHtml(value.code || value.name || "")}">${escapeHtml(value.name || value.code || "")}</option>`).join("");
    const sourceText = row.manual_override ? "Введено вручную — автоподстановка не перезапишет" : ([row.source_name, row.source_value].filter(Boolean).join(": ") || "нет источника Ozon");
    return `<tr data-index="${index}"><td><strong>${escapeHtml(row.title || row.code)}</strong>${row.required ? '<span class="required-pill">обязательно</span>' : ""}<small>${escapeHtml(row.code || "")}${row.multi_valued ? " · несколько через ;" : ""}</small></td><td><input class="new-card-attr" ${allowed.length ? `list="${listId}"` : ""} value="${escapeHtml(row.value || "")}" ${editingLocked ? "disabled" : ""}>${allowed.length ? `<datalist id="${listId}">${options}</datalist>` : ""}</td><td><small>${escapeHtml(sourceText)}</small></td></tr>`;
  }).join("");
  return `<article class="new-card-editor" data-id="${item.id}" data-category="${escapeHtml(draft.category || "")}" data-dirty="${localNewCardDrafts.has(String(item.id)) ? "1" : "0"}">
    <div class="new-card-summary">
      ${displayImage ? `<img src="${escapeHtml(displayImage)}" alt="${escapeHtml(item.name)}" loading="lazy" referrerpolicy="no-referrer">` : '<div class="market-image-placeholder">Нет фото</div>'}
      <div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml([item.brand, draft.sku].filter(Boolean).join(" · "))}</small><a href="${escapeHtml(item.supplier_url || "")}" target="_blank" rel="noopener">Открыть Ozon ↗</a></div>
      <div class="new-card-plan"><span>Ozon: <strong>${money(supplier.supplier_price_kzt)}</strong></span><span>доставка: <strong>${escapeHtml(delivery)}</strong></span><span>Kaspi: <strong>${money(item.test_price_kzt)}</strong></span><span>preOrder: <strong>${Math.max(1, Number(item.preorder_days || 1))} дн.</strong></span></div>
      <span class="new-card-status">${escapeHtml(newCardStatus(item, mappingBusy))}</span>
    </div>
    <div class="new-card-fields">
      <label><span>SKU</span><input name="sku" maxlength="64" value="${escapeHtml(draft.sku || "")}" ${editingLocked ? "disabled" : ""}></label>
      <label><span>Бренд</span><input name="brand" maxlength="255" value="${escapeHtml(draft.brand || "")}" ${editingLocked ? "disabled" : ""}></label>
      <label class="wide"><span>Название</span><input name="title" maxlength="1024" value="${escapeHtml(draft.title || "")}" ${editingLocked ? "disabled" : ""}></label>
      <label><span>Вес, кг</span><input name="weight" type="number" min="0.001" max="10000" step="0.001" value="${escapeHtml(draft.weight || "")}" ${editingLocked ? "disabled" : ""}></label>
      <label><span>Категория Kaspi</span><input name="category" list="new-card-categories-${item.id}" maxlength="255" value="${escapeHtml(draft.category || "")}" ${editingLocked ? "disabled" : ""}><datalist id="new-card-categories-${item.id}">${categoryList}</datalist><small>${escapeHtml(draft.category_title || draft.category_hint || "")}</small></label>
      <label class="wide"><span>Описание Kaspi (100–1024 символа)</span><textarea name="description" maxlength="1024" ${editingLocked ? "disabled" : ""}>${escapeHtml(draft.description || "")}</textarea></label>
    </div>
    <details class="new-card-images" open><summary><strong>Фото Ozon (${(draft.images || []).length})</strong></summary><div>${images || '<span class="muted">Фото не найдены</span>'}</div></details>
    <details class="new-card-attributes" open><summary><strong>Поля Kaspi (${attributes.length})</strong></summary><div class="new-card-attributes-scroll"><table><thead><tr><th>Поле</th><th>Значение</th><th>Источник Ozon</th></tr></thead><tbody>${attrRows}</tbody></table></div></details>
    <div class="new-card-errors ${errors.length ? "" : "ok"}">${errors.length ? errors.map((value) => `<span>${escapeHtml(value)}</span>`).join("") : "Все обязательные поля заполнены"}</div>
    <div class="new-card-action-message ${mappingBusy ? "pending" : ""}">${mappingBusy ? "Поля категории загружаются. После завершения кнопка создания включится автоматически." : ""}</div>
    <div class="new-card-actions"><button class="button new-card-save" type="button" ${editingLocked ? "disabled" : ""}>Сохранить черновик</button><button class="button new-card-remap" type="button" ${editingLocked ? "disabled" : ""}>Загрузить поля категории</button><button class="button new-card-create" type="button" ${editingLocked || errors.length ? "disabled" : ""}>${mappingBusy ? "Ожидаем поля категории…" : "Создать новую карточку Kaspi"}</button></div>
  </article>`;
};

const renderNewCards = (items, jobs) => {
  const list = document.querySelector("#new-cards");
  const empty = document.querySelector("#new-cards-empty");
  const jobList = document.querySelector("#new-card-jobs");
  if (!list || !empty || !jobList) return;
  // A background poll may fire while the operator is typing. Capture the DOM
  // before replacing it, then overlay those unsaved values on the fresh server
  // payload so the form never jumps back or clears fields.
  list.querySelectorAll('.new-card-editor[data-dirty="1"]').forEach((card) => {
    localNewCardDrafts.set(String(card.dataset.id), collectNewCard(card));
  });
  const renderedItems = items.map(withLocalNewCardDraft);
  currentNewCards = new Map(renderedItems.map((item) => [String(item.id), item]));
  const activeByItem = new Map();
  jobs.filter((job) => ["queued", "leased"].includes(job.status)).forEach((job) => {
    const key = String(job.item_id || "");
    if (!activeByItem.has(key)) activeByItem.set(key, new Set());
    activeByItem.get(key).add(job.job_type);
  });
  list.innerHTML = renderedItems.map((item) => newCardRow(item, activeByItem.get(String(item.id)) || new Set())).join("");
  empty.classList.toggle("hidden", items.length > 0);
  const types = new Set(["prepare_new_card", "map_new_card_category", "create_new_card", "confirm_new_card"]);
  const relevant = jobs.filter((job) => types.has(job.job_type) && ["queued", "leased", "failed"].includes(job.status)).slice(0, 6);
  const labels = {prepare_new_card:"Чтение новой карточки Ozon", map_new_card_category:"Поля категории Kaspi", create_new_card:"Product Import Kaspi", confirm_new_card:"Ожидание masterSku"};
  jobList.innerHTML = relevant.map((job) => `<div class="job ${job.status === "failed" ? "failed" : "pending"}"><strong>${escapeHtml(labels[job.job_type] || job.job_type)}</strong> · ${job.status === "leased" ? "Product Test Agent выполняет" : job.status === "queued" ? (job.job_type === "confirm_new_card" ? "ожидает следующей проверки Kaspi" : "ожидает Product Test Agent") : escapeHtml(job.error_message || "ошибка")}</div>`).join("");
  if (relevant.some((job) => ["queued", "leased"].includes(job.status) && job.job_type !== "confirm_new_card")) scheduleRefresh(3000);
};

const collectNewCard = (card) => {
  const item = currentNewCards.get(String(card.dataset.id));
  const draft = item?.offers?.new_card || {};
  const attributes = (draft.attributes || []).map((row, index) => ({...row, value:card.querySelector(`tr[data-index="${index}"] .new-card-attr`)?.value.trim() || ""}));
  return {
    sku: card.querySelector('[name="sku"]').value.trim(),
    title: card.querySelector('[name="title"]').value.trim(),
    brand: card.querySelector('[name="brand"]').value.trim(),
    description: card.querySelector('[name="description"]').value.trim(),
    weight: card.querySelector('[name="weight"]').value || null,
    category: card.querySelector('[name="category"]').value.trim(),
    attributes,
    images: [...card.querySelectorAll(".new-card-image-use:checked")].map((input) => input.dataset.url),
  };
};

const persistNewCardDraft = (itemId, payload) => {
  const previous = newCardSaveChains.get(itemId) || Promise.resolve();
  const next = previous.catch(() => undefined).then(() => request(`/api/product-test/new-cards/${itemId}`, {method:"PATCH", body:JSON.stringify(payload)}));
  newCardSaveChains.set(itemId, next);
  return next;
};

const scheduleNewCardAutosave = (card) => {
  const itemId = String(card.dataset.id);
  const payload = collectNewCard(card);
  localNewCardDrafts.set(itemId, payload);
  card.dataset.dirty = "1";
  const version = (newCardSaveVersions.get(itemId) || 0) + 1;
  newCardSaveVersions.set(itemId, version);
  const previousTimer = newCardSaveTimers.get(itemId);
  if (previousTimer) window.clearTimeout(previousTimer);
  const inlineMessage = card.querySelector(".new-card-action-message");
  if (inlineMessage) {
    inlineMessage.textContent = "Сохраняю ручные изменения…";
    inlineMessage.className = "new-card-action-message pending";
  }
  newCardSaveTimers.set(itemId, window.setTimeout(async () => {
    newCardSaveTimers.delete(itemId);
    try {
      await persistNewCardDraft(itemId, payload);
      if (newCardSaveVersions.get(itemId) === version) {
        localNewCardDrafts.delete(itemId);
        const current = document.querySelector(`.new-card-editor[data-id="${itemId}"]`);
        if (current) current.dataset.dirty = "0";
        const currentMessage = current?.querySelector(".new-card-action-message");
        if (currentMessage) {
          currentMessage.textContent = "Ручные изменения сохранены автоматически.";
          currentMessage.className = "new-card-action-message success";
        }
      }
    } catch (error) {
      const current = document.querySelector(`.new-card-editor[data-id="${itemId}"]`);
      const currentMessage = current?.querySelector(".new-card-action-message");
      if (currentMessage) {
        currentMessage.textContent = `Не удалось автосохранить: ${error.message}`;
        currentMessage.className = "new-card-action-message error";
      }
    }
  }, 900));
};

const popularCompletionLabel = (result) => ({
  target_reached: "нужное количество найдено",
  kaspi_results_exhausted: "результаты Kaspi закончились",
  scan_budget_exhausted: "исчерпан бюджет проверки",
  page_safety_limit: "достигнут безопасный предел страниц",
  unknown: "Kaspi завершил выдачу",
}[result?.completion_reason] || result?.completion_reason || "Kaspi завершил выдачу");

const renderJobs = (jobs) => {
  const list = document.querySelector("#jobs");
  if (!list) return;
  const relevant = jobs.filter((job) => ["discover", "discover_popular", "validate_supplier"].includes(job.job_type));
  const active = relevant.filter((job) => ["queued", "leased", "failed"].includes(job.status)).slice(0, 6);
  const pending = active.filter((job) => ["queued", "leased"].includes(job.status));
  const lastSearch = relevant.find((job) => ["discover", "discover_popular"].includes(job.job_type) && job.status === "succeeded" && job.result);
  const labels = {discover:"Поиск новых товаров", discover_popular:"Поиск ходовых товаров", validate_supplier:"Проверка Ozon"};
  const popularSummary = lastSearch?.job_type === "discover_popular" || lastSearch?.result?.mode === "popular";
  const searchIdentity = !lastSearch ? "" : `Задание #${Number(lastSearch.id)} · запрос «${escapeHtml(lastSearch.reference || "—")}» · завершено ${dateTime(lastSearch.completed_at || lastSearch.updated_at)}`;
  const summary = !lastSearch ? "" : popularSummary
    ? `<div class="job success"><strong>Отбор ходовых товаров завершён</strong> · ${searchIdentity}. Запрошено до ${Number(lastSearch.result.requested_results || 0)}, найдено ${Number(lastSearch.result.persisted_count || 0)}, проверено карточек Kaspi ${Number(lastSearch.result.scanned || 0)} на ${Number(lastSearch.result.search_pages_requested || 0)} стр., точно проверено карточек по продавцам ${Number(lastSearch.result.seller_counts_checked || 0)}. Отсеяно: уже есть у нас ${Number(lastSearch.result.excluded_existing_crm || 0)}, мало отзывов ${Number(lastSearch.result.excluded_below_min_reviews || 0)}, много продавцов ${Number(lastSearch.result.excluded_too_many_sellers || 0)}, продавцы не определены ${Number(lastSearch.result.excluded_unknown_sellers || 0)}. Результат: ${escapeHtml(popularCompletionLabel(lastSearch.result))}${Number(lastSearch.result.result_shortfall || 0) > 0 ? `, не хватило ${Number(lastSearch.result.result_shortfall)} товаров. Заданное количество — верхняя цель; фильтры отзывов и продавцов не ослабляются.` : ""}</div>`
    : `<div class="job success"><strong>Последний поиск завершён</strong> · проверено ${Number(lastSearch.result.matched_products_checked || 0)}, точных пар ${Number(lastSearch.result.confirmed_pairs || 0)}, на ручную проверку ${Number(lastSearch.result.manual_review_pairs || 0)}</div>`;
  list.innerHTML = summary + active.map((job) => `<div class="job ${job.status === "failed" ? "failed" : "pending"}"><strong>${escapeHtml(labels[job.job_type] || job.job_type)}</strong> · ${job.status === "leased" ? "Product Test Agent выполняет" : job.status === "queued" ? "ожидает Product Test Agent" : escapeHtml(job.error_message || "ошибка")}</div>`).join("");
  if (pending.length) scheduleRefresh(3000);
};

const renderAgent = (payload) => {
  const panel = document.querySelector("#product-test-agent");
  const title = document.querySelector("#product-test-agent-title");
  const meta = document.querySelector("#product-test-agent-meta");
  const badge = document.querySelector("#product-test-agent-status");
  if (!panel || !title || !meta || !badge) return;
  const agent = payload?.agents?.[0];
  const online = Boolean(payload?.online && agent?.online);
  panel.classList.toggle("ready", online);
  panel.classList.toggle("missing", !online);
  badge.className = `agent-pill ${online ? "online" : "offline"}`;
  badge.textContent = online ? "В сети" : "Не в сети";
  if (!agent) {
    title.textContent = "Product Test Agent ещё не подключался";
    meta.textContent = "Запустите единый Product Test Agent — он обслуживает вкладки «Тест товара» и «Добавить товар».";
    return;
  }
  title.textContent = online ? `Подключён: ${agent.hostname || agent.agent_id}` : `Нет связи: ${agent.hostname || agent.agent_id}`;
  meta.textContent = `версия ${agent.version || "—"} · workspace ${agent.workspace_id} · heartbeat ${dateTime(agent.last_seen_at)}`;
};

const fillSettings = (settings) => {
  const form = document.querySelector("#settings-form");
  if (!form) return;
  Object.entries(settings || {}).forEach(([key, value]) => {
    const field = form.elements.namedItem(key); if (!field) return;
    if (key === "image_verify") field.checked = true;
    else if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value ?? "";
  });
  const targetNew = document.querySelector("#target-new");
  // Settings are refreshed while a job is running. Initialise this separate
  // search field once, but never overwrite the number the operator just typed.
  if (settings?.target_new && targetNew && !targetNew.dataset.initialized) {
    targetNew.value = settings.target_new;
    targetNew.dataset.initialized = "1";
  }
};

const syncDiscoveryModeControls = () => {
  const mode = document.querySelector("#discover-mode")?.value || "full";
  document.querySelectorAll(".popular-filter").forEach((field) => field.classList.toggle("hidden", mode !== "popular"));
  const hint = document.querySelector("#discover-mode-hint");
  const button = document.querySelector("#discover-button");
  if (hint) hint.textContent = mode === "popular"
    ? "Без автоматического Ozon-сопоставления: только Kaspi-карточки с нужным спросом и числом продавцов. Ссылку Ozon вы вставите вручную."
    : "Agent автоматически найдёт и проверит точные пары Kaspi ↔ Ozon.";
  if (button && !button.disabled) button.textContent = mode === "popular" ? "Найти ходовые товары" : "Найти товары";
  if (button) button.dataset.label = mode === "popular" ? "Найти ходовые товары" : "Найти товары";
};

const renderResultMode = (items) => {
  const popular = items.some((item) => item.offers?.discovery?.mode === "popular");
  const kicker = document.querySelector("#result-kicker");
  const title = document.querySelector("#result-title");
  const description = document.querySelector("#result-description");
  if (!kicker || !title || !description) return;
  kicker.textContent = popular ? "ОТБОР ХОДОВЫХ ТОВАРОВ" : "ВИЗУАЛЬНОЕ СОПОСТАВЛЕНИЕ";
  title.textContent = popular ? "Популярные товары Kaspi" : "Kaspi ↔ Ozon";
  description.textContent = popular
    ? "В списке только карточки, прошедшие ваши фильтры отзывов и продавцов. Автопоиск Ozon отключён: найдите точную Ozon-ссылку, вставьте её в строку и нажмите «Проверить / заменить»."
    : "Сначала сравните две фотографии. Если товар совпал — нажмите «Выгрузить на Kaspi». Если нет — вставьте правильную Ozon-ссылку и повторно проверьте. После подтверждения создаётся обычный товар и существующая привязка Мониторинга; параллельных путей нет.";
};

const render = (payload) => {
  if (refreshTimer) { window.clearTimeout(refreshTimer); refreshTimer = null; }
  const items = payload.items || [];
  const newCards = payload.new_cards || [];
  const submissions = payload.submissions || [];
  const jobs = payload.jobs || [];
  const pageSubmissions = submissions.filter((item) => {
    const isNewCard = item.offers?.kaspi_submission?.route === "new_card";
    return isAddProductPage ? isNewCard : !isNewCard;
  });
  const itemsList = document.querySelector("#items");
  const empty = document.querySelector("#empty");
  if (itemsList) itemsList.innerHTML = items.length ? `<div class="lab-results-table"><div class="lab-table-head"><span>#</span><span>KASPI</span><span>KASPI ЦЕНА</span><span>OZON</span><span>SUPPLIER COST</span><span>ДОСТАВКА</span><span>СТАРТ KASPI</span><span>MATCH</span><span>СТАТУС / ДЕЙСТВИЯ</span></div>${items.map(itemRow).join("")}</div>` : "";
  if (empty) empty.classList.toggle("hidden", items.length > 0);
  const newCardJobTypes = new Set(["prepare_new_card", "map_new_card_category", "create_new_card", "confirm_new_card"]);
  const pageJobs = jobs.filter((job) => isAddProductPage ? newCardJobTypes.has(job.job_type) : !newCardJobTypes.has(job.job_type));
  document.querySelector("#total-count").textContent = isAddProductPage ? newCards.length : items.filter((item) => item.status !== "enrolled_fast_dumping").length;
  document.querySelector("#ready-count").textContent = isAddProductPage ? newCards.filter((item) => item.status === "new_card_ready").length : items.filter((item) => item.status === "ready_to_add").length;
  document.querySelector("#job-count").textContent = pageJobs.filter((job) => ["queued", "leased"].includes(job.status)).length;
  document.querySelector("#enrolled-count").textContent = pageSubmissions.filter((item) => item.offers?.kaspi_submission?.status === "succeeded").length;
  fillSettings(payload.settings || {});
  syncDiscoveryModeControls();
  renderResultMode(items);
  if (isAddProductPage) renderNewCards(newCards, jobs);
  else renderJobs(jobs);
  renderSubmissions(pageSubmissions);
  renderAgent(payload.agent || {});
};

async function load() {
  if (!localStorage.getItem(storageKey)) { authPanel.classList.remove("hidden"); page.classList.add("hidden"); return; }
  try { const state = await request("/api/product-test"); authPanel.classList.add("hidden"); page.classList.remove("hidden"); render(state); notify(""); }
  catch (error) { notify(error.message, "error"); }
}

document.querySelector("#token-form")?.addEventListener("submit", (event) => { event.preventDefault(); localStorage.setItem(storageKey, document.querySelector("#token").value.trim()); load(); });
document.querySelector("#refresh")?.addEventListener("click", load);
document.querySelector("#discover-mode")?.addEventListener("change", syncDiscoveryModeControls);
document.querySelector("#new-card-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.querySelector("#new-card-prepare");
  setBusy(button, true, "Передаю Agent…");
  try {
    await request("/api/product-test/new-cards/prepare", {method:"POST", body:JSON.stringify({supplier_url:document.querySelector("#new-card-url").value.trim()})});
    notify("Product Test Agent готовит новую карточку по точной ссылке Ozon.", "success");
    await load();
  } catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
document.querySelector("#discover-form")?.addEventListener("submit", async (event) => {
  event.preventDefault(); const button = document.querySelector("#discover-button"); setBusy(button, true, "Передаю Agent…");
  const mode = document.querySelector("#discover-mode").value;
  const body = {
    query:document.querySelector("#query").value.trim(),
    target_new:Number(document.querySelector("#target-new").value),
    mode,
    minimum_reviews:Number(document.querySelector("#minimum-reviews").value),
    maximum_sellers:Number(document.querySelector("#maximum-sellers").value),
  };
  try {
    const queued = await request("/api/product-test/discover", {method:"POST", body:JSON.stringify(body)});
    await load();
    const jobId = Number(queued?.job?.id || 0);
    notify(
      `${jobId ? `Задание #${jobId}` : "Новое задание"} принято: запрошено до ${body.target_new} товаров. ${mode === "popular" ? "Фильтры отзывов и продавцов применяются строго." : "Кандидаты появятся автоматически."}`,
      "success",
    );
  }
  catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
document.querySelector("#settings-form")?.addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const button = form.querySelector("button"); setBusy(button, true, "Сохраняю…");
  const body = {}; new FormData(form).forEach((value, key) => { body[key] = ["city_id", "zone_id"].includes(key) ? String(value) : Number(value); });
  body.image_verify = form.elements.image_verify.checked; body.allow_price_raise = form.elements.allow_price_raise.checked;
  try { await request("/api/product-test/settings", {method:"PATCH", body:JSON.stringify(body)}); notify("Значения по умолчанию сохранены.", "success"); await load(); }
  catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
document.querySelector("#items")?.addEventListener("click", async (event) => {
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
document.querySelector("#items")?.addEventListener("input", (event) => {
  if (!event.target.classList.contains("supplier")) return;
  const card = event.target.closest(".lab-result-row"); if (!card) return;
  const dirty = event.target.value.trim() !== (card.dataset.supplierUrl || "");
  card.classList.toggle("link-dirty", dirty);
  const add = card.querySelector(".add"); if (add) add.disabled = dirty || card.dataset.canAdd !== "1";
});
document.querySelector("#new-cards")?.addEventListener("input", (event) => {
  const card = event.target.closest(".new-card-editor");
  if (!card || event.target.disabled) return;
  scheduleNewCardAutosave(card);
});
document.querySelector("#new-cards")?.addEventListener("change", (event) => {
  if (!event.target.classList.contains("new-card-image-use")) return;
  const card = event.target.closest(".new-card-editor");
  if (!card || event.target.disabled) return;
  scheduleNewCardAutosave(card);
});
document.querySelector("#new-cards")?.addEventListener("click", async (event) => {
  const card = event.target.closest(".new-card-editor");
  const button = event.target.closest("button");
  if (!card || !button) return;
  const payload = collectNewCard(card);
  const itemId = String(card.dataset.id);
  const pendingTimer = newCardSaveTimers.get(itemId);
  if (pendingTimer) window.clearTimeout(pendingTimer);
  newCardSaveTimers.delete(itemId);
  localNewCardDrafts.set(itemId, payload);
  const saveVersion = newCardSaveVersions.get(itemId) || 0;
  const originalCategory = card.dataset.category || "";
  const action = button.classList.contains("new-card-create") ? "Создаю…" : button.classList.contains("new-card-remap") ? "Загружаю…" : "Сохраняю…";
  const inlineMessage = card.querySelector(".new-card-action-message");
  const notifyInline = (text, kind = "") => {
    if (!inlineMessage) return;
    inlineMessage.textContent = text;
    inlineMessage.className = `new-card-action-message ${kind}`.trim();
  };
  notifyInline("");
  setBusy(button, true, action);
  try {
    if (button.classList.contains("new-card-create") && payload.category !== originalCategory) {
      throw new Error("Категория изменена. Сначала нажмите «Загрузить поля категории».");
    }
    await persistNewCardDraft(itemId, payload);
    if (newCardSaveVersions.get(itemId) === saveVersion) localNewCardDrafts.delete(itemId);
    if (button.classList.contains("new-card-remap")) {
      await request(`/api/product-test/new-cards/${card.dataset.id}/map-category`, {method:"POST", body:JSON.stringify({category:payload.category})});
      notify("Product Test Agent загружает реальные поля и enum-значения выбранной категории Kaspi.", "success");
    } else if (button.classList.contains("new-card-create")) {
      await request(`/api/product-test/new-cards/${card.dataset.id}/create`, {method:"POST"});
      notify("Новая карточка передана Product Import. После detailed result агент сам дождётся masterSku, создаст оффер и подключит существующие Мониторинг и Fast Dumping.", "success");
      notifyInline("Создание запущено. Карточка перенесена в список ожидания Kaspi.", "success");
    } else {
      notify("Черновик новой карточки сохранён.", "success");
    }
    await load();
  } catch (error) { notify(error.message, "error"); notifyInline(error.message, "error"); } finally { setBusy(button, false, ""); }
});
document.querySelector("#kaspi-submissions")?.addEventListener("click", async (event) => {
  const row = event.target.closest(".submission-row"); const button = event.target.closest("button.retry");
  if (!row || !button) return;
  setBusy(button, true, "Повторяю…");
  try {
    const endpoint = row.dataset.route === "new_card"
      ? `/api/product-test/new-cards/${row.dataset.id}/create`
      : `/api/product-test/items/${row.dataset.id}/add`;
    await request(endpoint, {method:"POST"});
    notify("Повторная выгрузка передана Product Test Agent.", "success");
    await load();
  } catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
load();
