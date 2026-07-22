const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const detailPage = document.querySelector("#detail");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const bindingsContainer = document.querySelector("#bindings");
const observationsBody = document.querySelector("#observations-body");
const bestOfferContainer = document.querySelector("#best-offer");
const actionCenterContainer = document.querySelector("#action-center");
const decisionTimelineContainer = document.querySelector("#decision-timeline");
const supplierDialog = document.querySelector("#supplier-dialog");
const supplierForm = document.querySelector("#supplier-form");
const supplierResult = document.querySelector("#supplier-result");
const addSupplierButton = document.querySelector("#add-supplier");
const sourceTypeInput = document.querySelector("#supplier-source-type");
const onlineFields = document.querySelector("#online-source-fields");
const fixedFields = document.querySelector("#fixed-source-fields");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency = "KZT") => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency || "KZT"}`.trim();
const signedMoney = (value) => value == null ? null : `${Number(value) > 0 ? "+" : ""}${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ₸`;
const signedDays = (value) => value == null ? null : `${Number(value) > 0 ? "+" : ""}${Number(value)} дн.`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "Никогда";
const statusLabel = (status) => ({active:"Активен",draft:"Черновик",paused:"Приостановлен",archived:"Архив"}[status] || status || "—");
const confidenceLabel = (value) => ({high:"Высокая",medium:"Средняя",low:"Низкая",none:"Нет решения"}[value] || value || "—");
const confidenceClass = (value) => value === "high" ? "ok" : value === "medium" ? "warn" : "bad";
const eventLabel = (value) => ({initial_leader:"Первый лидер",leader_changed:"Смена лидера",leader_reaffirmed:"Лидер сохранился",no_decision:"Решения нет"}[value] || value || "Событие");
const eventClass = (value) => value === "leader_changed" ? "changed" : value === "no_decision" ? "lost" : value === "initial_leader" ? "initial" : "stable";
const actionLabel = (value) => ({no_action:"Ничего не менять",switch_supplier:"Смена поставщика",manual_review:"Ручная проверка",collect_more_data:"Нужно больше данных",no_available_offer:"Нет предложения"}[value] || value || "Рекомендация");
const actionClass = (value) => ({success:"success",info:"info",warning:"warning",critical:"critical"}[value] || "info");
const productId = Number(location.pathname.split("/").filter(Boolean).at(-1));
const isFixedSource = (binding) => String(binding.supplier_code || "").startsWith("offline-") || String(binding.supplier_code || "").startsWith("production-");
const fixedSourceLabel = (binding) => String(binding.supplier_code || "").startsWith("production-") ? "Собственное производство" : "Офлайн-поставщик";

const setText = (id, value) => { const element = document.querySelector(`#${id}`); if (element) element.textContent = String(value ?? 0); };
const setLoading = (loading) => { detailPage.setAttribute("aria-busy", String(loading)); refreshButton.disabled = loading; refreshButton.textContent = loading ? "Обновление…" : "Обновить"; };
const availabilityBadge = (available) => available === true ? '<span class="badge ok">В наличии</span>' : available === false ? '<span class="badge bad">Нет в наличии</span>' : '<span class="badge">Неизвестно</span>';
const monitorBadge = (binding) => isFixedSource(binding) ? '<span class="badge">Без мониторинга</span>' : binding.consecutive_failures > 0 ? '<span class="badge bad">Ошибка</span>' : binding.monitor_status === "active" ? '<span class="badge ok">Активен</span>' : binding.monitor_status ? `<span class="badge warn">${escapeHtml(binding.monitor_status)}</span>` : '<span class="badge">Не настроен</span>';
const scoreByBinding = (scores) => new Map((scores || []).map((score) => [Number(score.binding_id), score]));
const supplierLink = (binding, label = "Открыть поставщика") => isFixedSource(binding) ? `<span class="muted">${fixedSourceLabel(binding)}</span>` : `<a href="${escapeHtml(binding.supplier_product_url)}" target="_blank" rel="noreferrer">${label}</a>`;

const renderBestOffer = (bestOffer, decision, bindings) => {
  const empty = document.querySelector("#best-offer-empty");
  if (!bestOffer) { bestOfferContainer.innerHTML = ""; empty.classList.remove("hidden"); return; }
  const binding = bindings.find((item) => Number(item.binding_id) === Number(bestOffer.binding_id));
  if (!binding) { bestOfferContainer.innerHTML = ""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  const reasons = (bestOffer.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("");
  const warnings = (decision?.warnings || []).map((warning) => `<li>${escapeHtml(warning)}</li>`).join("");
  const challenger = decision?.runner_up ? `${escapeHtml(decision.runner_up.supplier_name)} · ${Number(decision.runner_up.total_score).toLocaleString("ru-RU", {maximumFractionDigits:2})}` : "Нет второго доступного предложения";
  bestOfferContainer.innerHTML = `<div><span>Источник закупки</span><strong>${escapeHtml(binding.supplier_name)}</strong><small>${isFixedSource(binding) ? fixedSourceLabel(binding) : escapeHtml(binding.supplier_code)}</small></div><div><span>Рейтинг предложения</span><strong>${Number(bestOffer.total_score).toLocaleString("ru-RU", {maximumFractionDigits:2})} / 100</strong><small>цена ${bestOffer.price_score} · доставка ${bestOffer.delivery_score}</small></div><div><span>Уверенность</span><strong><span class="badge ${confidenceClass(decision?.confidence)}">${confidenceLabel(decision?.confidence)}</span></strong><small>${decision?.score_gap == null ? "разрыв не рассчитан" : `разрыв ${Number(decision.score_gap).toLocaleString("ru-RU", {maximumFractionDigits:2})} балла`}</small></div><div><span>Ближайший конкурент</span><strong>${challenger}</strong><small>доступных предложений: ${Number(decision?.eligible_count || 0)}</small></div><div><span>Цена</span><strong>${money(binding.price, binding.currency)}</strong>${binding.old_price != null ? `<small>было ${money(binding.old_price, binding.currency)}</small>` : ""}</div><div><span>Получение</span><strong>${binding.delivery_days == null ? "—" : `${binding.delivery_days} дн.`}</strong><small>${escapeHtml(binding.seller || "источник не указан")}</small></div><div class="decision-reasons"><span>Почему выбран</span><ul>${reasons}</ul></div>${warnings ? `<div class="decision-warnings"><span>Ограничения решения</span><ul>${warnings}</ul></div>` : ""}${supplierLink(binding, "Открыть поставщика")}`;
};

const renderActionCenter = (action) => {
  const empty = document.querySelector("#action-center-empty");
  if (!action) { actionCenterContainer.innerHTML = ""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  const reasons = (action.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("");
  const target = action.target_supplier_name ? `<div><span>Целевой поставщик</span><strong>${escapeHtml(action.target_supplier_name)}</strong><small>${escapeHtml(action.target_supplier_code || "")}</small></div>` : "";
  const gap = action.score_gap == null ? "—" : Number(action.score_gap).toLocaleString("ru-RU", {maximumFractionDigits:2});
  actionCenterContainer.innerHTML = `<article class="action-card ${actionClass(action.severity)}"><div class="action-status"><span class="action-kind">${actionLabel(action.kind)}</span><h3>${escapeHtml(action.title)}</h3><p>${escapeHtml(action.summary)}</p></div>${target}<div><span>Разрыв рейтинга</span><strong>${gap}</strong><small>${action.auto_apply_allowed ? "Автоприменение разрешено" : "Только рекомендация"}</small></div><div class="action-reasons"><span>Основание</span><ul>${reasons}</ul></div><div class="action-safety"><strong>Автоматические действия отключены</strong><small>CRM пока ничего не меняет в XML и привязках без подтверждённой Pricing Policy.</small></div></article>`;
};

const renderDecisionTimeline = (entries) => {
  const empty = document.querySelector("#decision-timeline-empty");
  if (!entries?.length) { decisionTimelineContainer.innerHTML = ""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  decisionTimelineContainer.innerHTML = entries.map((entry) => {
    const leader = entry.leader_supplier_name || "Нет доступного лидера";
    const transition = entry.previous_supplier_name && entry.leader_supplier_name && entry.previous_binding_id !== entry.leader_binding_id ? `<span class="timeline-transition">${escapeHtml(entry.previous_supplier_name)} → ${escapeHtml(entry.leader_supplier_name)}</span>` : "";
    const deltas = [signedMoney(entry.price_delta), signedDays(entry.delivery_delta)].filter(Boolean).map((value) => `<span>${escapeHtml(value)}</span>`).join("");
    return `<article class="timeline-entry ${eventClass(entry.event_type)}"><div class="timeline-marker"></div><div class="timeline-content"><div class="timeline-head"><div><span class="timeline-type">${eventLabel(entry.event_type)}</span><strong>${escapeHtml(leader)}</strong></div><time>${dateTime(entry.occurred_at)}</time></div>${transition}<p>${escapeHtml(entry.reason)}</p><div class="timeline-meta"><span>Рейтинг ${entry.leader_score == null ? "—" : Number(entry.leader_score).toLocaleString("ru-RU", {maximumFractionDigits:2})}</span><span class="badge ${confidenceClass(entry.confidence)}">${confidenceLabel(entry.confidence)}</span>${entry.score_gap == null ? "" : `<span>разрыв ${Number(entry.score_gap).toLocaleString("ru-RU", {maximumFractionDigits:2})}</span>`}${deltas}</div></div></article>`;
  }).join("");
};

const renderBindings = (bindings, supplierScores) => {
  const scores = scoreByBinding(supplierScores);
  const rankedEligible = (supplierScores || []).filter((score) => score.eligible);
  const rankByBinding = new Map(rankedEligible.map((score, index) => [Number(score.binding_id), index + 1]));
  bindingsContainer.innerHTML = bindings.map((binding) => {
    const score = scores.get(Number(binding.binding_id));
    const rank = rankByBinding.get(Number(binding.binding_id));
    const scoreHtml = score ? `<span class="muted">${rank ? `Место ${rank} · ` : ""}Рейтинг ${Number(score.total_score).toLocaleString("ru-RU", {maximumFractionDigits:2})}${score.eligible ? "" : " · не участвует"}</span>` : "";
    return `<article class="binding-card"><div class="binding-head"><h3 class="binding-title">${escapeHtml(binding.supplier_name)}${binding.is_primary ? '<span class="primary-mark">Основной</span>' : ""}</h3>${supplierLink(binding, "Открыть карточку поставщика")}<span class="muted">${isFixedSource(binding) ? fixedSourceLabel(binding) : escapeHtml(binding.supplier_code)} · ${escapeHtml(binding.binding_status)} · приоритет ${binding.priority}</span>${scoreHtml}</div><div><span class="label">Цена</span><strong>${money(binding.price, binding.currency)}</strong>${binding.old_price != null ? `<span class="muted">было ${money(binding.old_price, binding.currency)}</span>` : ""}</div><div><span class="label">Получение</span><strong>${binding.delivery_days == null ? "—" : `${binding.delivery_days} дн.`}</strong><span class="muted">${escapeHtml(binding.seller || "источник не указан")}</span></div><div><span class="label">Наличие</span>${availabilityBadge(binding.available)}${binding.stock != null ? `<span class="muted">остаток ${binding.stock}</span>` : ""}</div><div><span class="label">Мониторинг</span>${monitorBadge(binding)}<span class="muted">${isFixedSource(binding) ? "цена изменяется вручную" : `проверено ${dateTime(binding.last_checked_at)}`}</span></div></article>`;
  }).join("");
  document.querySelector("#bindings-empty").classList.toggle("hidden", bindings.length > 0);
};

const renderObservations = (observations) => {
  observationsBody.innerHTML = observations.map((item) => `<tr><td>${dateTime(item.observed_at)}</td><td>${escapeHtml(item.supplier_code)}</td><td>${money(item.price, item.currency)}${item.old_price != null ? `<span class="muted">было ${money(item.old_price, item.currency)}</span>` : ""}</td><td>${item.delivery_days == null ? "—" : `${item.delivery_days} дн.`}</td><td>${availabilityBadge(item.available)}${item.stock != null ? `<span class="muted">остаток ${item.stock}</span>` : ""}</td><td>${escapeHtml(item.seller || "—")}</td></tr>`).join("");
  document.querySelector("#observations-empty").classList.toggle("hidden", observations.length > 0);
};

const render = (data, action) => {
  const { product, sales, bindings, observations, best_offer: bestOffer, supplier_scores: supplierScores, best_offer_decision: bestOfferDecision, decision_timeline: decisionTimeline } = data;
  setText("product-name", product.name); setText("product-meta", `Kaspi ${product.kaspi_product_id}${product.brand ? ` · ${product.brand}` : ""}${product.merchant_sku ? ` · SKU ${product.merchant_sku}` : ""}`);
  setText("kaspi-product-id", product.kaspi_product_id); setText("merchant-sku", product.merchant_sku || "—"); setText("product-brand", product.brand || "—"); setText("product-status", statusLabel(product.status)); setText("product-updated-at", `Обновлено в CRM ${dateTime(product.updated_at)}`);
  setText("units-sold", Number(sales.units_sold || 0).toLocaleString("ru-RU")); setText("orders-count", `строк заказов: ${Number(sales.orders_count || 0).toLocaleString("ru-RU")}`); setText("revenue-kzt", money(sales.revenue_kzt)); setText("last-ordered-at", `последняя продажа: ${dateTime(sales.last_ordered_at)}`);
  setText("bindings-count", bindings.length); setText("observations-count", observations.length); setText("available-count", bindings.filter((item) => item.available === true).length); setText("failures-count", bindings.filter((item) => item.consecutive_failures > 0).length); setText("updated-at", `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`);
  renderBestOffer(bestOffer, bestOfferDecision, bindings); renderActionCenter(action); renderDecisionTimeline(decisionTimeline); renderBindings(bindings, supplierScores); renderObservations(observations); authPanel.classList.add("hidden"); detailPage.classList.remove("hidden");
};

const responseError = async (response) => { if ([502,503,504].includes(response.status)) return new Error("Сервис Render временно недоступен или перезапускается. Подождите минуту и нажмите «Обновить»."); try { const body = await response.json(); if (body.detail) return new Error(String(body.detail)); } catch {} return new Error(`API вернул ошибку ${response.status}`); };
const isNotFound = (response) => response.status === 404;

const loadDetail = async () => {
  const token = localStorage.getItem(storageKey); if (!token) { authPanel.classList.remove("hidden"); detailPage.classList.add("hidden"); return; }
  if (!Number.isInteger(productId) || productId <= 0) { message.textContent = "Некорректный идентификатор товара."; return; }
  setLoading(true); message.textContent = "";
  try {
    const headers = {Authorization:`Bearer ${token}`};
    const [detailResponse, actionResponse] = await Promise.all([
      fetch(`/api/products/${productId}/detail?observation_limit=100`, {headers,cache:"no-store"}),
      fetch(`/api/actions/products/${productId}`, {headers,cache:"no-store"}),
    ]);
    if (detailResponse.status === 401 || actionResponse.status === 401) { localStorage.removeItem(storageKey); authPanel.classList.remove("hidden"); detailPage.classList.add("hidden"); message.textContent = "Токен не принят. Проверьте SERVICE_API_TOKEN."; return; }
    if (isNotFound(detailResponse) || isNotFound(actionResponse)) throw new Error("Товар не найден.");
    if (!detailResponse.ok) throw await responseError(detailResponse);
    if (!actionResponse.ok) throw await responseError(actionResponse);
    render(await detailResponse.json(), await actionResponse.json());
  } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось загрузить карточку товара."; } finally { setLoading(false); }
};

const toggleSourceFields = () => {
  const online = sourceTypeInput.value === "online";
  onlineFields.classList.toggle("hidden", !online);
  fixedFields.classList.toggle("hidden", online);
  document.querySelector("#supplier-url").required = online;
  document.querySelector("#fixed-source-name").required = !online;
  document.querySelector("#fixed-source-price").required = !online;
};

const closeSupplierDialog = () => supplierDialog.close();
addSupplierButton.addEventListener("click", () => { supplierResult.textContent = ""; supplierForm.reset(); sourceTypeInput.value = "online"; document.querySelector("#supplier-run-check").checked = true; document.querySelector("#supplier-primary").checked = true; toggleSourceFields(); supplierDialog.showModal(); });
sourceTypeInput.addEventListener("change", toggleSourceFields);
document.querySelector("#close-supplier-dialog").addEventListener("click", closeSupplierDialog);
document.querySelector("#cancel-supplier").addEventListener("click", closeSupplierDialog);
supplierForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = localStorage.getItem(storageKey); const saveButton = document.querySelector("#save-supplier"); saveButton.disabled = true;
  const sourceType = sourceTypeInput.value;
  supplierResult.textContent = sourceType === "online" ? "Создаю привязку и ставлю проверку в очередь…" : "Сохраняю фиксированный источник закупки…";
  try {
    let response;
    if (sourceType === "online") {
      response = await fetch(`/api/product-registry/products/${productId}/supplier-bindings/manual`, {method:"POST",headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/json"},body:JSON.stringify({url:document.querySelector("#supplier-url").value.trim(),title:document.querySelector("#supplier-title").value.trim() || null,is_primary:document.querySelector("#supplier-primary").checked,run_initial_check:document.querySelector("#supplier-run-check").checked})});
    } else {
      response = await fetch(`/api/products/${productId}/fixed-procurement-source`, {method:"POST",headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/json"},body:JSON.stringify({source_type:sourceType,source_name:document.querySelector("#fixed-source-name").value.trim(),price:Number(document.querySelector("#fixed-source-price").value),delivery_days:Number(document.querySelector("#fixed-source-delivery-days").value || 0),is_primary:document.querySelector("#supplier-primary").checked})});
    }
    if (!response.ok) throw await responseError(response);
    const result = await response.json(); supplierResult.textContent = sourceType === "online" && result.job_id ? `Поставщик привязан. Job #${result.job_id} уже в очереди.` : "Источник закупки сохранён."; await loadDetail(); setTimeout(closeSupplierDialog, 1200);
  } catch (error) { supplierResult.textContent = error instanceof Error ? error.message : "Не удалось сохранить источник закупки."; } finally { saveButton.disabled = false; }
});

tokenForm.addEventListener("submit", (event) => { event.preventDefault(); const token = tokenInput.value.trim(); if (!token) return; localStorage.setItem(storageKey, token); tokenInput.value = ""; loadDetail(); });
refreshButton.addEventListener("click", loadDetail);
toggleSourceFields();
loadDetail();
