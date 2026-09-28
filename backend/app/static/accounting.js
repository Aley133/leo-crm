const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#accounting-page");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const message = document.querySelector("#message");
const periodSwitch = document.querySelector("#period-switch");
const productsBody = document.querySelector("#products-body");
const productsEmpty = document.querySelector("#products-empty");
const productSearch = document.querySelector("#product-search");
const abcFilter = document.querySelector("#abc-filter");
const signalFilter = document.querySelector("#signal-filter");
const capitalEditButton = document.querySelector("#capital-edit");
const capitalForm = document.querySelector("#capital-form");
const capitalCancelButton = document.querySelector("#capital-cancel");

let selectedDays = 30;
let productCache = [];
let capitalCache = null;

const headers = () => ({Authorization: `Bearer ${localStorage.getItem(storageKey) || ""}`});
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]));
const money = (value) => `${Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:2})} ₸`;
const number = (value, digits = 0) => Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:digits});
const percent = (value) => `${number(value, 2)}%`;
const signedPercent = (value) => value == null ? "—" : `${Number(value) > 0 ? "+" : ""}${number(value, 1)}%`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric"}) : "—";
const signedMoney = (value) => `${Number(value || 0) < 0 ? "−" : Number(value || 0) > 0 ? "+" : ""}${money(Math.abs(Number(value || 0)))}`;
const resultClass = (value) => Number(value || 0) > 0 ? "money-positive" : Number(value || 0) < 0 ? "money-negative" : "";
const trendClass = (value) => Number(value || 0) > 0 ? "trend-positive" : Number(value || 0) < 0 ? "trend-negative" : "";
const moneyOrDash = (value) => value == null ? "—" : money(value);

const responseError = async (response) => {
  let detail = `HTTP ${response.status}`;
  try {
    const payload = await response.json();
    detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || detail);
  } catch (_) {}
  return new Error(detail);
};

const setText = (selector, value) => {
  const node = document.querySelector(selector);
  if (node) node.textContent = value;
};

const renderCapital = (capital) => {
  capitalCache = capital;
  setText("#capital-cash", moneyOrDash(capital.cash_balance));
  setText("#capital-warehouse", money(capital.warehouse_at_cost));
  setText("#capital-transit", money(capital.goods_in_transit));
  setText("#capital-free", moneyOrDash(capital.free_capital));
  setText("#capital-total-label", `Общий капитал ${capital.workspace_name || ""}`.trim());

  const totalNode = document.querySelector("#capital-total");
  const totalCard = totalNode.closest(".capital-total");
  const unpricedUnits = Number(capital.unpriced_warehouse_units || 0) + Number(capital.unpriced_incoming_units || 0);
  if (!capital.cash_is_configured) {
    totalNode.textContent = "—";
    setText("#capital-total-note", "Укажите текущие деньги для полного расчёта");
  } else if (!capital.valuation_is_complete) {
    totalNode.textContent = `≥ ${money(capital.known_total_capital)}`;
    setText("#capital-total-note", `Минимально подтверждённая сумма · ${number(unpricedUnits)} ед. без закупочной цены`);
  } else {
    totalNode.textContent = money(capital.total_capital);
    const updated = capital.snapshot_created_at ? new Date(capital.snapshot_created_at).toLocaleString("ru-RU") : "";
    setText("#capital-total-note", updated ? `Деньги обновлены ${updated}` : "Деньги + склад + товар в пути");
  }
  totalCard.classList.toggle("incomplete", !capital.cash_is_configured || !capital.valuation_is_complete);
  capitalEditButton.textContent = capital.cash_is_configured ? "Изменить деньги" : "Указать деньги";

  capitalForm.elements.cash_balance_kzt.value = capital.cash_balance ?? "";
  capitalForm.elements.free_capital_kzt.value = capital.free_capital ?? "";
  capitalForm.elements.note.value = capital.snapshot_note ?? "";
};

const showCapitalForm = () => {
  if (capitalCache) {
    capitalForm.elements.cash_balance_kzt.value = capitalCache.cash_balance ?? "";
    capitalForm.elements.free_capital_kzt.value = capitalCache.free_capital ?? "";
    capitalForm.elements.note.value = capitalCache.snapshot_note ?? "";
  }
  capitalForm.classList.remove("hidden");
  capitalForm.elements.cash_balance_kzt.focus();
};

const renderSummary = (payload) => {
  const summary = payload.summary;
  const inventory = payload.inventory;
  const displayedResult = summary.net_profit == null ? summary.known_net_profit : summary.net_profit;
  const resultCard = document.querySelector("#result-card");
  resultCard.classList.toggle("negative", Number(displayedResult || 0) < 0);
  setText("#summary-result", signedMoney(displayedResult));
  setText("#summary-result-note", summary.result_is_complete
    ? `${percent(summary.net_margin_pct)} от выданного оборота`
    : `учтено ${percent(summary.cost_coverage_pct)} проданных единиц`);
  setText("#summary-revenue", money(summary.delivered_revenue));
  setText("#summary-revenue-note", `${number(summary.delivered_orders)} завершённых заказов · ${number(summary.delivered_units)} ед.`);
  setText("#summary-procurement", money(summary.procurement_cost));
  setText("#summary-cost-coverage", `${percent(summary.cost_coverage_pct)} покрытия цен · FIFO ${number(summary.fifo_cost_units)} ед.`);
  setText("#summary-inventory", money(inventory.inventory_value));
  setText("#summary-inventory-note", `${number(inventory.on_hand_units)} ед. · ${number(inventory.sku_count)} SKU · в пути ${number(inventory.incoming_units)}`);
  setText("#summary-cancelled", money(summary.cancelled_demand));
  setText("#summary-cancelled-note", `${number(summary.cancelled_orders)} отмен · ${percent(summary.cancellation_rate_pct)}`);
  setText("#summary-returned", money(summary.returned_value));
  setText("#summary-returned-note", `${number(summary.returned_orders)} возвратов · ${percent(summary.return_rate_pct)}`);

  setText("#pnl-revenue", money(summary.delivered_revenue));
  setText("#pnl-procurement", `−${money(summary.procurement_cost)}`);
  setText("#pnl-commission", `−${money(summary.kaspi_commission)}`);
  setText("#pnl-tax", `−${money(summary.tax)}`);
  setText("#pnl-logistics", `−${money(summary.logistics)}`);
  const pnlResult = document.querySelector("#pnl-result");
  pnlResult.textContent = signedMoney(displayedResult);
  pnlResult.className = resultClass(displayedResult).replace("money-", "");
  setText("#commission-rate", `${percent(payload.calculation.commission_rate_pct)}`);
  setText("#tax-rate", `${percent(payload.calculation.tax_rate_pct)}`);
  const status = document.querySelector("#pnl-status");
  status.textContent = summary.result_is_complete ? "Себестоимость покрыта" : `${number(summary.unpriced_units)} ед. без цены`;
  status.className = `quality-badge ${summary.result_is_complete ? "ok" : "warn"}`;
  setText("#pnl-note", summary.result_is_complete
    ? `FIFO использован для ${number(summary.fifo_cost_units)} ед.; для ${number(summary.estimated_cost_units)} ед. применена средняя или последняя закупочная цена. Расчёт только читает данные.`
    : `Показан подтверждённый результат по оценённым единицам. Полный плюс/минус появится после внесения закупочной цены для ${number(summary.unpriced_units)} ед.; система ничего не подставляет молча.`);

  setText("#orders-total", number(summary.orders_total));
  setText("#orders-delivered", number(summary.delivered_orders));
  setText("#average-order", money(summary.average_delivered_order));
  setText("#median-order", money(summary.median_delivered_order));
  setText("#units-per-order", number(summary.units_per_delivered_order, 2));
  setText("#multi-unit-share", percent(summary.multi_unit_orders_share_pct));
  setText("#multi-line-share", percent(summary.multi_line_orders_share_pct));
  setText("#loss-rates", `${percent(summary.cancellation_rate_pct)} / ${percent(summary.return_rate_pct)}`);

  const comparison = payload.comparison;
  const periodLabel = payload.period.days ? `Последние ${payload.period.days} дней` : "Вся история";
  const revenueChange = comparison?.delivered_revenue_change_pct;
  setText("#period-caption", `${periodLabel}${revenueChange == null ? "" : ` · оборот ${signedPercent(revenueChange)} к прошлому периоду`}`);
};

const renderAbc = (payload) => {
  document.querySelector("#abc-groups").innerHTML = (payload.abc?.groups || []).map((group) => `
    <div class="abc-group" data-class="${escapeHtml(group.class)}"><b>${escapeHtml(group.class)}</b><strong>${money(group.revenue)}</strong><small>${number(group.products_count)} товаров · ${percent(group.share_pct)} оборота</small></div>`).join("");
  document.querySelector("#brand-list").innerHTML = (payload.brands || []).map((brand) => `
    <div class="brand-row"><span>${escapeHtml(brand.brand)}<small>${number(brand.products_count)} позиций</small></span><strong>${percent(brand.share_pct)}<small>${money(brand.revenue)}</small></strong></div>`).join("") || '<div class="empty">Нет продаж для расчёта.</div>';
};

const signalDefinitions = [
  ["lost_sales", "Пропали из продаж", "danger"],
  ["out_of_stock", "Продажи без остатка", "danger"],
  ["low_stock", "Запас до 7 дней", "warning"],
  ["frozen_capital", "Капитал без продаж", "warning"],
  ["accelerating", "Разгоняются", "good"],
  ["new_demand", "Новый спрос", "good"],
];

const renderSignals = (payload) => {
  const blocks = signalDefinitions.flatMap(([key, title, kind]) => {
    const rows = payload.signals?.[key] || [];
    if (!rows.length) return [];
    return [`<article class="signal-block ${kind}"><h3>${escapeHtml(title)} · ${rows.length}</h3><ul>${rows.slice(0, 5).map((row) => `<li>${row.product_id ? `<a href="/crm/products/${row.product_id}">${escapeHtml(row.name)}</a>` : `<span>${escapeHtml(row.name)}</span>`}<small>${money(row.delivered_revenue)} · остаток ${number(row.on_hand_units)} ед.${row.days_of_stock == null ? "" : ` · ${number(row.days_of_stock, 1)} дн.`}</small></li>`).join("")}</ul></article>`];
  });
  document.querySelector("#signals").innerHTML = blocks.join("");
  document.querySelector("#signals-empty").classList.toggle("hidden", blocks.length > 0);
};

const renderProducts = () => {
  const query = productSearch.value.trim().toLocaleLowerCase("ru-RU");
  const abc = abcFilter.value;
  const signal = signalFilter.value;
  const rows = productCache.filter((row) => {
    const haystack = [row.name, row.merchant_sku, row.kaspi_product_id, ...(row.shared_merchant_skus || [])].join(" ").toLocaleLowerCase("ru-RU");
    return (!query || haystack.includes(query)) && (!abc || row.abc_class === abc) && (!signal || row.signal === signal);
  });
  setText("#product-count", `${number(rows.length)} из ${number(productCache.length)} позиций`);
  productsBody.innerHTML = rows.map((row) => {
    const purchasePrice = row.weighted_purchase_price ?? row.last_purchase_price;
    const trend = row.revenue_change_pct;
    const profitNote = row.result_is_complete ? "" : `<small class="cell-detail">без ${number(row.unpriced_units)} ед. без цены</small>`;
    const productTitle = row.product_id ? `<a href="/crm/products/${row.product_id}">${escapeHtml(row.name)}</a>` : `<span>${escapeHtml(row.name)}</span>`;
    return `<tr>
      <td class="product-cell">${productTitle}<small>${escapeHtml(row.merchant_sku || "SKU не задан")} · Kaspi ${escapeHtml(row.kaspi_product_id || "—")}</small></td>
      <td><span class="abc-badge ${row.abc_class.toLowerCase()}">${escapeHtml(row.abc_class)}</span><small class="cell-detail">${percent(row.revenue_share_pct)}</small></td>
      <td><strong>${number(row.delivered_units)} ед.</strong><small class="cell-detail">${number(row.orders_count)} заказов</small></td>
      <td><strong>${money(row.delivered_revenue)}</strong><small class="cell-detail">отменено ${money(row.cancelled_demand)}</small></td>
      <td><strong class="${resultClass(row.known_net_profit)}">${signedMoney(row.known_net_profit)}</strong>${profitNote}</td>
      <td><strong class="${trendClass(trend)}">${signedPercent(trend)}</strong><small class="cell-detail">было ${money(row.previous_revenue)}</small></td>
      <td><strong>${number(row.on_hand_units)} ед.</strong><small class="cell-detail">в пути ${number(row.incoming_units)}</small></td>
      <td><strong>${purchasePrice == null ? "—" : money(purchasePrice)}</strong><small class="cell-detail">${row.weighted_purchase_price == null ? "последняя" : "средняя FIFO"}</small></td>
      <td><strong>${money(row.inventory_value)}</strong></td>
      <td><strong>${row.days_of_stock == null ? "—" : `${number(row.days_of_stock, 1)} дн.`}</strong></td>
      <td>${dateTime(row.last_sale_at)}</td>
      <td><span class="signal-badge ${escapeHtml(row.signal)}">${escapeHtml(row.signal_label)}</span></td>
    </tr>`;
  }).join("");
  productsEmpty.classList.toggle("hidden", rows.length > 0);
};

const loadReport = async () => {
  page.setAttribute("aria-busy", "true");
  refreshButton.disabled = true;
  message.textContent = "Формирую отчёт без изменения данных…";
  try {
    const response = await fetch(`/api/accounting/report?days=${selectedDays}`, {headers:headers(), cache:"no-store"});
    if (!response.ok) throw await responseError(response);
    const payload = await response.json();
    renderSummary(payload);
    renderCapital(payload.capital);
    renderAbc(payload);
    renderSignals(payload);
    productCache = payload.products || [];
    renderProducts();
    authPanel.classList.add("hidden");
    page.classList.remove("hidden");
    message.textContent = `Отчёт обновлён ${new Date(payload.generated_at).toLocaleString("ru-RU")}. Все расчёты выполнены только для чтения.`;
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось сформировать отчёт.";
    if (/401|Bearer|token/i.test(message.textContent)) authPanel.classList.remove("hidden");
  } finally {
    page.setAttribute("aria-busy", "false");
    refreshButton.disabled = false;
  }
};

const downloadExport = async (format, button) => {
  button.disabled = true;
  message.textContent = `Готовлю ${format === "xlsx" ? "Excel" : "XML"}…`;
  try {
    const includeZero = document.querySelector("#include-zero").checked;
    const response = await fetch(`/api/accounting/inventory/export?format=${format}&include_zero=${includeZero}`, {headers:headers(), cache:"no-store"});
    if (!response.ok) throw await responseError(response);
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="([^"]+)"/)?.[1] || `leo-inventory.${format}`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    message.textContent = `${filename} скачан. Данные CRM не изменялись.`;
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось сформировать выгрузку.";
  } finally {
    button.disabled = false;
  }
};

tokenForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  localStorage.setItem(storageKey, tokenInput.value.trim());
  await loadReport();
});
refreshButton.addEventListener("click", loadReport);
periodSwitch.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-days]");
  if (!button) return;
  selectedDays = Number(button.dataset.days);
  periodSwitch.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
  await loadReport();
});
for (const control of [productSearch, abcFilter, signalFilter]) control.addEventListener("input", renderProducts);
document.querySelectorAll(".export-button").forEach((button) => button.addEventListener("click", () => downloadExport(button.dataset.format, button)));
capitalEditButton.addEventListener("click", showCapitalForm);
capitalCancelButton.addEventListener("click", () => capitalForm.classList.add("hidden"));
capitalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitButton = capitalForm.querySelector('button[type="submit"]');
  const cashBalance = Number(capitalForm.elements.cash_balance_kzt.value);
  const freeCapital = Number(capitalForm.elements.free_capital_kzt.value);
  if (freeCapital > cashBalance) {
    message.textContent = "Свободный капитал не может быть больше общей суммы денег.";
    capitalForm.elements.free_capital_kzt.focus();
    return;
  }
  submitButton.disabled = true;
  message.textContent = "Сохраняю новый снимок капитала…";
  try {
    const response = await fetch("/api/accounting/capital-snapshot", {
      method: "POST",
      headers: {...headers(), "Content-Type":"application/json"},
      body: JSON.stringify({
        cash_balance_kzt: capitalForm.elements.cash_balance_kzt.value,
        free_capital_kzt: capitalForm.elements.free_capital_kzt.value,
        note: capitalForm.elements.note.value.trim() || null,
      }),
    });
    if (!response.ok) throw await responseError(response);
    renderCapital(await response.json());
    capitalForm.classList.add("hidden");
    message.textContent = "Снимок капитала сохранён. Заказы, партии, FIFO и выручка не изменялись.";
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось сохранить снимок капитала.";
  } finally {
    submitButton.disabled = false;
  }
});

if (localStorage.getItem(storageKey)) loadReport();
