const SESSION_KEY = "leo_workspace_session";
const token = localStorage.getItem(SESSION_KEY);
if (!token) window.location.replace("/login");

const productId = Number(location.pathname.split("/").filter(Boolean).at(-1));
const headers = () => ({Authorization: `Bearer ${token || ""}`});
const message = document.querySelector("#message");
const refresh = document.querySelector("#refresh");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency = "KZT") => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency || "KZT"}`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "Никогда";
const statusLabel = (status) => ({active:"Активен",draft:"Черновик",paused:"Приостановлен",archived:"Архив"}[status] || status || "—");
const setText = (id, value) => { const node = document.querySelector(`#${id}`); if (node) node.textContent = String(value ?? "—"); };
const availabilityBadge = (available) => available === true ? '<span class="badge ok">В наличии</span>' : available === false ? '<span class="badge bad">Нет в наличии</span>' : '<span class="badge">Неизвестно</span>';

const readError = async (response) => {
  try { const payload = await response.json(); return payload.detail || `HTTP ${response.status}`; }
  catch (_) { return `HTTP ${response.status}`; }
};

const renderBindings = (items) => {
  const container = document.querySelector("#bindings");
  container.innerHTML = items.map((item) => `<article class="binding-card">
    <div class="binding-head"><h3 class="binding-title">${escapeHtml(item.supplier_name)}${item.is_primary ? '<span class="primary-mark">Основной</span>' : ""}</h3><a href="${escapeHtml(item.supplier_product_url)}" target="_blank" rel="noreferrer">Открыть поставщика</a><span class="muted">${escapeHtml(item.supplier_code)} · ${escapeHtml(item.binding_status)} · приоритет ${item.priority}</span></div>
    <div><span class="label">Цена</span><strong>${money(item.price, item.currency)}</strong></div>
    <div><span class="label">Получение</span><strong>${item.delivery_days == null ? "—" : `${item.delivery_days} дн.`}</strong><span class="muted">${escapeHtml(item.seller || "источник не указан")}</span></div>
    <div><span class="label">Наличие</span>${availabilityBadge(item.available)}${item.stock != null ? `<span class="muted">остаток ${item.stock}</span>` : ""}</div>
    <div><span class="label">Мониторинг</span><strong>${escapeHtml(item.monitor_status || "Не настроен")}</strong><span class="muted">проверено ${dateTime(item.last_checked_at)}</span></div>
  </article>`).join("");
  document.querySelector("#bindings-empty").classList.toggle("hidden", items.length > 0);
};

const renderObservations = (items) => {
  document.querySelector("#observations-body").innerHTML = items.map((item) => `<tr><td>${dateTime(item.observed_at)}</td><td>${escapeHtml(item.supplier_code)}</td><td>${money(item.price, item.currency)}</td><td>${item.delivery_days == null ? "—" : `${item.delivery_days} дн.`}</td><td>${availabilityBadge(item.available)}</td><td>${escapeHtml(item.seller || "—")}</td></tr>`).join("");
  document.querySelector("#observations-empty").classList.toggle("hidden", items.length > 0);
};

const renderBestOffer = (detail) => {
  const best = detail.best_offer;
  const empty = document.querySelector("#best-offer-empty");
  if (!best) { document.querySelector("#best-offer").innerHTML = ""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  const binding = detail.bindings.find((item) => Number(item.binding_id) === Number(best.binding_id));
  document.querySelector("#best-offer").innerHTML = `<div><span>Источник закупки</span><strong>${escapeHtml(best.supplier_name)}</strong><small>${escapeHtml(best.supplier_code)}</small></div><div><span>Рейтинг</span><strong>${Number(best.total_score).toLocaleString("ru-RU", {maximumFractionDigits:2})} / 100</strong></div><div><span>Цена</span><strong>${money(binding?.price, binding?.currency)}</strong></div><div><span>Получение</span><strong>${binding?.delivery_days == null ? "—" : `${binding.delivery_days} дн.`}</strong></div>`;
};

const renderTimeline = (items) => {
  const container = document.querySelector("#decision-timeline");
  container.innerHTML = items.map((item) => `<article class="timeline-entry"><div class="timeline-marker"></div><div class="timeline-content"><div class="timeline-head"><div><span class="timeline-type">${escapeHtml(item.event_type)}</span><strong>${escapeHtml(item.leader_supplier_name || "Нет лидера")}</strong></div><time>${dateTime(item.occurred_at)}</time></div><p>${escapeHtml(item.reason)}</p></div></article>`).join("");
  document.querySelector("#decision-timeline-empty").classList.toggle("hidden", items.length > 0);
};

const renderAction = (action) => {
  const empty = document.querySelector("#action-center-empty");
  if (!action) { empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  document.querySelector("#action-center").innerHTML = `<article class="action-card ${escapeHtml(action.severity)}"><div class="action-status"><span class="action-kind">${escapeHtml(action.kind)}</span><h3>${escapeHtml(action.title)}</h3><p>${escapeHtml(action.summary)}</p></div><div class="action-reasons"><span>Основание</span><ul>${(action.reasons || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div></article>`;
};

const renderEconomics = (data) => {
  setText("economics-sale-price", money(data.sale_unit_price));
  setText("economics-procurement", money(data.procurement_unit_cost));
  setText("economics-source", data.procurement_source_name || "Источник не выбран");
  setText("economics-commission", money(data.kaspi_commission));
  setText("economics-tax", money(data.tax));
  setText("economics-logistics", money(data.logistics));
  setText("economics-profit", money(data.net_profit));
  setText("economics-margin", data.net_margin_pct == null ? "—" : `${Number(data.net_margin_pct).toLocaleString("ru-RU")} %`);
  setText("total-net-profit", money(data.total_net_profit));
  setText("total-net-margin", data.total_net_margin_pct == null ? "После выбора источника" : `${Number(data.total_net_margin_pct).toLocaleString("ru-RU")} % · ${data.profit_units_count} ед.`);
};

const renderDetail = (detail) => {
  const {product, sales, bindings, observations} = detail;
  setText("product-name", product.name);
  setText("product-meta", `Kaspi ${product.kaspi_product_id}${product.brand ? ` · ${product.brand}` : ""}${product.merchant_sku ? ` · SKU ${product.merchant_sku}` : ""}`);
  setText("kaspi-product-id", product.kaspi_product_id); setText("merchant-sku", product.merchant_sku || "—"); setText("product-brand", product.brand || "—"); setText("product-status", statusLabel(product.status)); setText("product-updated-at", `Обновлено в CRM ${dateTime(product.updated_at)}`);
  setText("units-sold", Number(sales.units_sold || 0).toLocaleString("ru-RU")); setText("orders-count", `строк заказов: ${Number(sales.orders_count || 0).toLocaleString("ru-RU")}`); setText("revenue-kzt", money(sales.revenue_kzt)); setText("last-ordered-at", `последняя продажа: ${dateTime(sales.last_ordered_at)}`);
  setText("bindings-count", bindings.length); setText("observations-count", observations.length); setText("available-count", bindings.filter((item) => item.available === true).length); setText("failures-count", bindings.filter((item) => item.consecutive_failures > 0).length); setText("updated-at", `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`);
  renderBindings(bindings); renderObservations(observations); renderBestOffer(detail); renderTimeline(detail.decision_timeline || []);
};

const load = async () => {
  refresh.disabled = true; message.textContent = "";
  try {
    const [detailResponse, economicsResponse, actionResponse] = await Promise.all([
      fetch(`/api/workspace/products/${productId}/detail?observation_limit=100`, {headers:headers(), cache:"no-store"}),
      fetch(`/api/workspace/products/${productId}/economics`, {headers:headers(), cache:"no-store"}),
      fetch(`/api/workspace/products/${productId}/action`, {headers:headers(), cache:"no-store"}),
    ]);
    if ([detailResponse, economicsResponse, actionResponse].some((response) => response.status === 401)) { localStorage.removeItem(SESSION_KEY); window.location.replace("/login"); return; }
    if (!detailResponse.ok) throw new Error(await readError(detailResponse));
    renderDetail(await detailResponse.json());
    if (economicsResponse.ok) renderEconomics(await economicsResponse.json());
    if (actionResponse.ok) renderAction(await actionResponse.json());
  } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось загрузить карточку товара."; }
  finally { refresh.disabled = false; }
};

refresh.addEventListener("click", load);
document.querySelector("#logout")?.addEventListener("click", async () => { try { await fetch("/api/auth/logout", {method:"POST", headers:headers()}); } catch (_) {} localStorage.removeItem(SESSION_KEY); window.location.replace("/login"); });
document.querySelector("#add-supplier")?.addEventListener("click", () => document.querySelector("#supplier-dialog")?.showModal());
document.querySelector("#close-supplier-dialog")?.addEventListener("click", () => document.querySelector("#supplier-dialog")?.close());
document.querySelector("#cancel-supplier")?.addEventListener("click", () => document.querySelector("#supplier-dialog")?.close());
load();
