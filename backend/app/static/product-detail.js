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
const supplierDialog = document.querySelector("#supplier-dialog");
const supplierForm = document.querySelector("#supplier-form");
const supplierResult = document.querySelector("#supplier-result");
const addSupplierButton = document.querySelector("#add-supplier");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency = "KZT") => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency || "KZT"}`.trim();
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "Никогда";
const statusLabel = (status) => ({active:"Активен",draft:"Черновик",paused:"Приостановлен",archived:"Архив"}[status] || status || "—");
const productId = Number(location.pathname.split("/").filter(Boolean).at(-1));

const setText = (id, value) => { const element = document.querySelector(`#${id}`); if (element) element.textContent = String(value ?? 0); };
const setLoading = (loading) => { detailPage.setAttribute("aria-busy", String(loading)); refreshButton.disabled = loading; refreshButton.textContent = loading ? "Обновление…" : "Обновить"; };
const availabilityBadge = (available) => available === true ? '<span class="badge ok">В наличии</span>' : available === false ? '<span class="badge bad">Нет в наличии</span>' : '<span class="badge">Неизвестно</span>';
const monitorBadge = (binding) => binding.consecutive_failures > 0 ? '<span class="badge bad">Ошибка</span>' : binding.monitor_status === "active" ? '<span class="badge ok">Активен</span>' : binding.monitor_status ? `<span class="badge warn">${escapeHtml(binding.monitor_status)}</span>` : '<span class="badge">Не настроен</span>';
const scoreByBinding = (scores) => new Map((scores || []).map((score) => [Number(score.binding_id), score]));

const renderBestOffer = (bestOffer, bindings) => {
  const empty = document.querySelector("#best-offer-empty");
  if (!bestOffer) { bestOfferContainer.innerHTML = ""; empty.classList.remove("hidden"); return; }
  const binding = bindings.find((item) => Number(item.binding_id) === Number(bestOffer.binding_id));
  if (!binding) { bestOfferContainer.innerHTML = ""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  const reasons = (bestOffer.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("");
  bestOfferContainer.innerHTML = `<div><span>Поставщик</span><strong>${escapeHtml(binding.supplier_name)}</strong><small>${escapeHtml(binding.supplier_code)}</small></div><div><span>Score</span><strong>${Number(bestOffer.total_score).toLocaleString("ru-RU", {maximumFractionDigits:2})} / 100</strong><small>цена ${bestOffer.price_score} · доставка ${bestOffer.delivery_score}</small></div><div><span>Цена</span><strong>${money(binding.price, binding.currency)}</strong>${binding.old_price != null ? `<small>было ${money(binding.old_price, binding.currency)}</small>` : ""}</div><div><span>Доставка</span><strong>${binding.delivery_days == null ? "—" : `${binding.delivery_days} дн.`}</strong><small>${escapeHtml(binding.seller || "продавец не указан")}</small></div><div class="decision-reasons"><span>Почему выбран</span><ul>${reasons}</ul></div><a class="button secondary" href="${escapeHtml(binding.supplier_product_url)}" target="_blank" rel="noreferrer">Открыть поставщика</a>`;
};

const renderBindings = (bindings, supplierScores) => {
  const scores = scoreByBinding(supplierScores);
  bindingsContainer.innerHTML = bindings.map((binding) => {
    const score = scores.get(Number(binding.binding_id));
    const scoreHtml = score ? `<span class="muted">Score ${Number(score.total_score).toLocaleString("ru-RU", {maximumFractionDigits:2})}${score.eligible ? "" : " · не участвует"}</span>` : "";
    return `<article class="binding-card"><div class="binding-head"><h3 class="binding-title">${escapeHtml(binding.supplier_name)}${binding.is_primary ? '<span class="primary-mark">Основной</span>' : ""}</h3><a href="${escapeHtml(binding.supplier_product_url)}" target="_blank" rel="noreferrer">Открыть карточку поставщика</a><span class="muted">${escapeHtml(binding.supplier_code)} · ${escapeHtml(binding.binding_status)} · приоритет ${binding.priority}</span>${scoreHtml}</div><div><span class="label">Цена</span><strong>${money(binding.price, binding.currency)}</strong>${binding.old_price != null ? `<span class="muted">было ${money(binding.old_price, binding.currency)}</span>` : ""}</div><div><span class="label">Доставка</span><strong>${binding.delivery_days == null ? "—" : `${binding.delivery_days} дн.`}</strong><span class="muted">${escapeHtml(binding.seller || "продавец не указан")}</span></div><div><span class="label">Наличие</span>${availabilityBadge(binding.available)}${binding.stock != null ? `<span class="muted">остаток ${binding.stock}</span>` : ""}</div><div><span class="label">Мониторинг</span>${monitorBadge(binding)}<span class="muted">проверено ${dateTime(binding.last_checked_at)}</span></div></article>`;
  }).join("");
  document.querySelector("#bindings-empty").classList.toggle("hidden", bindings.length > 0);
};

const renderObservations = (observations) => {
  observationsBody.innerHTML = observations.map((item) => `<tr><td>${dateTime(item.observed_at)}</td><td>${escapeHtml(item.supplier_code)}</td><td>${money(item.price, item.currency)}${item.old_price != null ? `<span class="muted">было ${money(item.old_price, item.currency)}</span>` : ""}</td><td>${item.delivery_days == null ? "—" : `${item.delivery_days} дн.`}</td><td>${availabilityBadge(item.available)}${item.stock != null ? `<span class="muted">остаток ${item.stock}</span>` : ""}</td><td>${escapeHtml(item.seller || "—")}</td></tr>`).join("");
  document.querySelector("#observations-empty").classList.toggle("hidden", observations.length > 0);
};

const render = (data) => {
  const { product, sales, bindings, observations, best_offer: bestOffer, supplier_scores: supplierScores } = data;
  setText("product-name", product.name); setText("product-meta", `Kaspi ${product.kaspi_product_id}${product.brand ? ` · ${product.brand}` : ""}${product.merchant_sku ? ` · SKU ${product.merchant_sku}` : ""}`);
  setText("kaspi-product-id", product.kaspi_product_id); setText("merchant-sku", product.merchant_sku || "—"); setText("product-brand", product.brand || "—"); setText("product-status", statusLabel(product.status)); setText("product-updated-at", `Обновлено в CRM ${dateTime(product.updated_at)}`);
  setText("units-sold", Number(sales.units_sold || 0).toLocaleString("ru-RU")); setText("orders-count", `строк заказов: ${Number(sales.orders_count || 0).toLocaleString("ru-RU")}`); setText("revenue-kzt", money(sales.revenue_kzt)); setText("last-ordered-at", `последняя продажа: ${dateTime(sales.last_ordered_at)}`);
  setText("bindings-count", bindings.length); setText("observations-count", observations.length); setText("available-count", bindings.filter((item) => item.available === true).length); setText("failures-count", bindings.filter((item) => item.consecutive_failures > 0).length); setText("updated-at", `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`);
  renderBestOffer(bestOffer, bindings); renderBindings(bindings, supplierScores); renderObservations(observations); authPanel.classList.add("hidden"); detailPage.classList.remove("hidden");
};

const responseError = async (response) => { if ([502,503,504].includes(response.status)) return new Error("Сервис Render временно недоступен или перезапускается. Подождите минуту и нажмите «Обновить»."); try { const body = await response.json(); if (body.detail) return new Error(String(body.detail)); } catch {} return new Error(`API вернул ошибку ${response.status}`); };

const loadDetail = async () => {
  const token = localStorage.getItem(storageKey); if (!token) { authPanel.classList.remove("hidden"); detailPage.classList.add("hidden"); return; }
  if (!Number.isInteger(productId) || productId <= 0) { message.textContent = "Некорректный идентификатор товара."; return; }
  setLoading(true); message.textContent = "";
  try { const response = await fetch(`/api/products/${productId}/detail?observation_limit=100`, {headers:{Authorization:`Bearer ${token}`},cache:"no-store"}); if (response.status === 401) { localStorage.removeItem(storageKey); authPanel.classList.remove("hidden"); detailPage.classList.add("hidden"); message.textContent = "Токен не принят. Проверьте SERVICE_API_TOKEN."; return; } if (response.status === 404) throw new Error("Товар не найден."); if (!response.ok) throw await responseError(response); render(await response.json()); } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось загрузить карточку товара."; } finally { setLoading(false); }
};

const closeSupplierDialog = () => supplierDialog.close();
addSupplierButton.addEventListener("click", () => { supplierResult.textContent = ""; supplierForm.reset(); document.querySelector("#supplier-run-check").checked = true; supplierDialog.showModal(); });
document.querySelector("#close-supplier-dialog").addEventListener("click", closeSupplierDialog);
document.querySelector("#cancel-supplier").addEventListener("click", closeSupplierDialog);
supplierForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = localStorage.getItem(storageKey); const saveButton = document.querySelector("#save-supplier"); saveButton.disabled = true; supplierResult.textContent = "Создаю привязку и ставлю проверку в очередь…";
  try {
    const response = await fetch(`/api/product-registry/products/${productId}/supplier-bindings/manual`, {method:"POST",headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/json"},body:JSON.stringify({url:document.querySelector("#supplier-url").value.trim(),title:document.querySelector("#supplier-title").value.trim() || null,is_primary:document.querySelector("#supplier-primary").checked,run_initial_check:document.querySelector("#supplier-run-check").checked})});
    if (!response.ok) throw await responseError(response);
    const result = await response.json(); supplierResult.textContent = result.job_id ? `Поставщик привязан. Job #${result.job_id} уже в очереди.` : "Поставщик привязан."; await loadDetail(); setTimeout(closeSupplierDialog, 1200);
  } catch (error) { supplierResult.textContent = error instanceof Error ? error.message : "Не удалось привязать поставщика."; } finally { saveButton.disabled = false; }
});

tokenForm.addEventListener("submit", (event) => { event.preventDefault(); const token = tokenInput.value.trim(); if (!token) return; localStorage.setItem(storageKey, token); tokenInput.value = ""; loadDetail(); });
refreshButton.addEventListener("click", loadDetail);
loadDetail();
