const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const revenuePage = document.querySelector("#revenue-page");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const message = document.querySelector("#message");
const list = document.querySelector("#revenue-list");
const empty = document.querySelector("#empty");
const canvas = document.querySelector("#business-chart");
const tooltip = document.querySelector("#chart-tooltip");
const periodSwitch = document.querySelector("#period-switch");
const metricSwitch = document.querySelector("#metric-switch");

let payloadCache = null;
let selectedDays = 30;
let selectedMetric = "revenue";
let chartPoints = [];

const headers = () => ({"Authorization": `Bearer ${localStorage.getItem(storageKey) || ""}`});
const money = (value) => `${Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:2})} KZT`;
const axisMoney = (value) => {
  const numberValue = Number(value || 0);
  const abs = Math.abs(numberValue);
  if (abs >= 1_000_000_000) return `${number(numberValue / 1_000_000_000, 1)} млрд KZT`;
  if (abs >= 1_000_000) return `${number(numberValue / 1_000_000, 1)} млн KZT`;
  if (abs >= 1_000) return `${number(numberValue / 1_000, 1)} тыс. KZT`;
  return `${number(numberValue)} KZT`;
};
const percent = (value) => `${Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:2})}%`;
const number = (value, digits = 0) => Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:digits});
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const businessDate = (value) => value ? new Date(`${value}T00:00:00`).toLocaleDateString("ru-RU") : "—";
const signedPercent = (value) => {
  if (!Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toLocaleString("ru-RU", {maximumFractionDigits:1})}%`;
};
const signedMoney = (value) => {
  if (!Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${Math.abs(value).toLocaleString("ru-RU", {maximumFractionDigits:0})} KZT`;
};
const changeClass = (value) => value > 0 ? "positive" : value < 0 ? "negative" : "";

const responseError = async (response) => {
  let detail = `HTTP ${response.status}`;
  try {
    const payload = await response.json();
    detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || detail);
  } catch (_) {}
  return new Error(detail);
};

const aggregateByDate = (items) => {
  const map = new Map();
  for (const item of items || []) {
    const key = item.business_date;
    if (!map.has(key)) {
      map.set(key, {business_date:key, orders_count:0, units_count:0, revenue:0, net_profit:0, captured_at:item.captured_at});
    }
    const row = map.get(key);
    row.orders_count += Number(item.orders_count || 0);
    row.units_count += Number(item.units_count || 0);
    row.revenue += Number(item.revenue || 0);
    row.net_profit += Number(item.net_profit || 0);
    if (item.captured_at && (!row.captured_at || new Date(item.captured_at) > new Date(row.captured_at))) row.captured_at = item.captured_at;
  }
  return [...map.values()].sort((a,b) => a.business_date.localeCompare(b.business_date)).map((row) => ({
    ...row,
    margin_pct: row.revenue > 0 ? row.net_profit * 100 / row.revenue : 0,
  }));
};

const slicePeriod = (rows, days) => {
  if (!rows.length || days === 0) return rows;
  return rows.slice(Math.max(rows.length - days, 0));
};

const previousPeriod = (rows, days, currentLength) => {
  if (!rows.length || days === 0) return [];
  const end = Math.max(rows.length - currentLength, 0);
  const start = Math.max(end - currentLength, 0);
  return rows.slice(start, end);
};

const comparisonPeriods = (allRows, currentRows) => {
  const previousRows = previousPeriod(allRows, selectedDays, currentRows.length);
  if (previousRows.length && previousRows.length === currentRows.length) {
    return {current:currentRows, previous:previousRows, mode:"previous_period"};
  }
  if (currentRows.length >= 8) {
    const half = Math.floor(currentRows.length / 2);
    const previous = currentRows.slice(0, half);
    const current = currentRows.slice(currentRows.length - half);
    return {current, previous, mode:"within_period"};
  }
  return {current:currentRows, previous:previousRows, mode:"none"};
};

const summarize = (rows) => {
  const orders = rows.reduce((sum,row) => sum + row.orders_count, 0);
  const units = rows.reduce((sum,row) => sum + row.units_count, 0);
  const revenue = rows.reduce((sum,row) => sum + row.revenue, 0);
  const profit = rows.reduce((sum,row) => sum + row.net_profit, 0);
  return {
    days: rows.length,
    orders,
    units,
    revenue,
    profit,
    margin: revenue > 0 ? profit * 100 / revenue : 0,
    basket: orders > 0 ? units / orders : 0,
    unitPrice: units > 0 ? revenue / units : 0,
    averageOrder: orders > 0 ? revenue / orders : 0,
  };
};

const pctChange = (current, previous) => {
  if (!previous) return current ? 100 : 0;
  return (current - previous) * 100 / Math.abs(previous);
};

const setChange = (selector, current, previous) => {
  const node = document.querySelector(selector);
  const value = previous > 0 ? pctChange(current, previous) : NaN;
  node.textContent = Number.isFinite(value) ? `${signedPercent(value)} к прошлому периоду` : "нет полного прошлого периода";
  node.className = changeClass(value);
};

const decomposeRevenue = (current, previous) => {
  if (!previous.orders || !previous.units || !current.orders || !current.units) {
    return {orders:NaN,basket:NaN,price:NaN};
  }
  const prevBasket = previous.units / previous.orders;
  const currBasket = current.units / current.orders;
  const prevPrice = previous.revenue / previous.units;
  const currPrice = current.revenue / current.units;
  return {
    orders: (current.orders - previous.orders) * prevBasket * prevPrice,
    basket: current.orders * (currBasket - prevBasket) * prevPrice,
    price: current.orders * currBasket * (currPrice - prevPrice),
  };
};

const renderDrivers = (current, previous, mode) => {
  const totalNode = document.querySelector("#growth-total");
  const note = document.querySelector("#growth-note");
  const diff = current.revenue - previous.revenue;
  const totalPct = previous.revenue > 0 ? pctChange(current.revenue, previous.revenue) : NaN;
  totalNode.textContent = Number.isFinite(totalPct) ? signedPercent(totalPct) : "—";
  totalNode.className = changeClass(totalPct);
  if (note) {
    note.textContent = mode === "previous_period"
      ? "Разница с предыдущим таким же периодом раскладывается на три фактора."
      : mode === "within_period"
        ? "Полного прошлого периода ещё нет — сравниваем вторую половину выбранного периода с первой."
        : "Для расчёта драйверов нужно хотя бы несколько дней истории продаж.";
  }

  const parts = decomposeRevenue(current, previous);
  for (const [key, value] of Object.entries(parts)) {
    const node = document.querySelector(`#driver-${key}`);
    node.textContent = Number.isFinite(value) ? signedMoney(value) : "недостаточно данных";
    node.className = changeClass(value);
  }
  if (!Number.isFinite(parts.orders) && diff === 0) totalNode.textContent = "0%";
};

const metricConfig = {
  revenue:{title:"Выручка", value:(row)=>row.revenue, total:(summary)=>summary.revenue, format:money, axis:axisMoney},
  profit:{title:"Чистая прибыль", value:(row)=>row.net_profit, total:(summary)=>summary.profit, format:money, axis:axisMoney},
  orders:{title:"Заказы", value:(row)=>row.orders_count, total:(summary)=>summary.orders, format:(v)=>number(v), axis:(v)=>number(v)},
  units:{title:"Продано единиц", value:(row)=>row.units_count, total:(summary)=>summary.units, format:(v)=>number(v), axis:(v)=>number(v)},
};

const movingAverage = (values, size=7) => values.map((_, index) => {
  const start = Math.max(0, index - size + 1);
  const part = values.slice(start, index + 1);
  return part.reduce((sum,value)=>sum+value,0) / part.length;
});

const drawChart = (rows, summary, previousSummary) => {
  if (!canvas) return;
  const config = metricConfig[selectedMetric];
  document.querySelector("#chart-title").textContent = config.title;
  document.querySelector("#chart-total").textContent = config.format(config.total(summary));
  const currentTotal = config.total(summary);
  const previousTotal = config.total(previousSummary);
  const chartChange = previousTotal > 0 ? pctChange(currentTotal, previousTotal) : NaN;
  const changeNode = document.querySelector("#chart-change");
  changeNode.textContent = Number.isFinite(chartChange) ? `${signedPercent(chartChange)} к прошлому периоду` : "нет полного прошлого периода";
  changeNode.className = changeClass(chartChange);

  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = Math.max(window.devicePixelRatio || 1, 1);
  canvas.width = Math.max(Math.floor(rect.width * dpr), 1);
  canvas.height = Math.max(Math.floor(rect.height * dpr), 1);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr,0,0,dpr,0,0);
  const width = rect.width;
  const height = rect.height;
  ctx.clearRect(0,0,width,height);

  if (!rows.length) {
    ctx.fillStyle = "#7799bd";
    ctx.font = "13px sans-serif";
    ctx.fillText("Нет данных за выбранный период", 24, 38);
    chartPoints = [];
    return;
  }

  const values = rows.map(config.value);
  const trend = movingAverage(values);
  const maxValue = Math.max(...values, ...trend, 1);
  const minValue = Math.min(...values, ...trend, 0);
  const range = Math.max(maxValue - minValue, 1);

  ctx.font = "10px sans-serif";
  const axisLabels = Array.from({length:5}, (_, i) => config.axis(maxValue - range * i / 4));
  const widestAxisLabel = Math.max(...axisLabels.map((label)=>ctx.measureText(label).width), 48);
  const pad = {left:Math.ceil(widestAxisLabel + 22),right:18,top:20,bottom:32};
  const innerW = Math.max(width - pad.left - pad.right, 20);
  const innerH = Math.max(height - pad.top - pad.bottom, 20);
  const x = (index) => pad.left + (rows.length === 1 ? innerW/2 : index * innerW / (rows.length - 1));
  const y = (value) => pad.top + (maxValue - value) * innerH / range;

  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(125,162,203,.16)";
  ctx.fillStyle = "#6f91b8";
  ctx.font = "10px sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i=0;i<=4;i++) {
    const value = maxValue - range * i / 4;
    const py = pad.top + innerH * i / 4;
    ctx.beginPath();ctx.moveTo(pad.left,py);ctx.lineTo(width-pad.right,py);ctx.stroke();
    ctx.fillText(config.axis(value), pad.left-8, py);
  }

  const gradient = ctx.createLinearGradient(0,pad.top,0,height-pad.bottom);
  gradient.addColorStop(0,"rgba(56,158,255,.14)");
  gradient.addColorStop(1,"rgba(56,158,255,0)");
  ctx.beginPath();
  values.forEach((value,index) => index === 0 ? ctx.moveTo(x(index),y(value)) : ctx.lineTo(x(index),y(value)));
  ctx.lineTo(x(values.length-1),height-pad.bottom);
  ctx.lineTo(x(0),height-pad.bottom);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  // Daily change candles: green means the selected metric grew vs yesterday,
  // red means it fell. They deliberately represent close-to-close movement,
  // not exchange OHLC candles because CRM has one aggregate value per day.
  if (values.length > 1) {
    const candleWidth = Math.max(3, Math.min(9, innerW / Math.max(values.length, 1) * 0.36));
    for (let index=1; index<values.length; index++) {
      const previousValue = values[index-1];
      const currentValue = values[index];
      const rising = currentValue >= previousValue;
      const candleColor = rising ? "rgba(55,203,134,.88)" : "rgba(255,92,92,.88)";
      const px = x(index);
      const pyPrev = y(previousValue);
      const pyCurrent = y(currentValue);
      ctx.strokeStyle = candleColor;
      ctx.fillStyle = candleColor;
      ctx.lineWidth = 1;
      ctx.beginPath();ctx.moveTo(px,pyPrev);ctx.lineTo(px,pyCurrent);ctx.stroke();
      const top = Math.min(pyPrev, pyCurrent);
      const bodyHeight = Math.max(Math.abs(pyPrev-pyCurrent), 2);
      ctx.fillRect(px-candleWidth/2, top, candleWidth, bodyHeight);
    }
  }

  ctx.beginPath();
  values.forEach((value,index) => index === 0 ? ctx.moveTo(x(index),y(value)) : ctx.lineTo(x(index),y(value)));
  ctx.strokeStyle = "#42a5ff";
  ctx.lineWidth = 1.35;
  ctx.shadowColor = "rgba(66,165,255,.25)";
  ctx.shadowBlur = 5;
  ctx.stroke();
  ctx.shadowBlur = 0;

  ctx.beginPath();
  trend.forEach((value,index) => index === 0 ? ctx.moveTo(x(index),y(value)) : ctx.lineTo(x(index),y(value)));
  ctx.setLineDash([5,5]);
  ctx.strokeStyle = "#8aa6c8";
  ctx.lineWidth = 1.2;
  ctx.stroke();
  ctx.setLineDash([]);

  const labels = Math.min(rows.length, 6);
  ctx.fillStyle = "#6f91b8";
  ctx.font = "10px sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i=0;i<labels;i++) {
    const index = labels === 1 ? 0 : Math.round(i * (rows.length - 1) / (labels - 1));
    const label = new Date(`${rows[index].business_date}T00:00:00`).toLocaleDateString("ru-RU", {day:"2-digit",month:"2-digit"});
    ctx.fillText(label,x(index),height-pad.bottom+9);
  }

  chartPoints = rows.map((row,index)=>({
    x:x(index),
    y:y(values[index]),
    row,
    value:values[index],
    previousValue:index > 0 ? values[index-1] : null,
  }));
};

const renderCapitalProducts = (inventory) => {
  const container = document.querySelector("#capital-products");
  const rows = inventory.top_capital || [];
  container.innerHTML = rows.map((item) => {
    const age = item.oldest_received_at ? Math.max(0,Math.floor((Date.now()-new Date(item.oldest_received_at).getTime())/86400000)) : null;
    return `<div class="capital-product"><div class="capital-product-main"><strong>${escapeHtml(item.name || item.merchant_sku || "Товар")}</strong><small>${escapeHtml(item.merchant_sku || "без SKU")}${age !== null ? ` · старейшая партия ${age} дн.` : ""}</small></div><div class="capital-product-value"><strong>${money(item.inventory_cost)}</strong><small>${number(item.units)} ед.</small></div></div>`;
  }).join("");
  document.querySelector("#capital-empty").classList.toggle("hidden", rows.length > 0);
};

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g,(ch)=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));

const render = (payload) => {
  payloadCache = payload;
  const allRows = aggregateByDate(payload.items || []);
  const currentRows = slicePeriod(allRows, selectedDays);
  const previousRows = previousPeriod(allRows, selectedDays, currentRows.length);
  const summary = summarize(currentRows);
  const previous = summarize(previousRows);
  const driverComparison = comparisonPeriods(allRows, currentRows);
  const driverCurrent = summarize(driverComparison.current);
  const driverPrevious = summarize(driverComparison.previous);
  const inventory = payload.inventory || {};

  document.querySelector("#summary-days").textContent = number(summary.days);
  document.querySelector("#summary-revenue").textContent = money(summary.revenue);
  document.querySelector("#summary-profit").textContent = money(summary.profit);
  document.querySelector("#summary-margin-value").textContent = percent(summary.margin);
  document.querySelector("#summary-margin").textContent = `за ${summary.days} дн.`;
  document.querySelector("#summary-orders").textContent = number(summary.orders);
  document.querySelector("#summary-units").textContent = number(summary.units);
  document.querySelector("#avg-basket").textContent = `${number(summary.basket,2)} ед. на заказ`;
  document.querySelector("#inventory-cost").textContent = money(inventory.on_hand_cost);
  document.querySelector("#inventory-units").textContent = `${number(inventory.on_hand_units)} ед. · ${number(inventory.sku_count)} SKU`;

  setChange("#revenue-change", summary.revenue, previous.revenue);
  setChange("#profit-change", summary.profit, previous.profit);
  setChange("#orders-change", summary.orders, previous.orders);
  renderDrivers(driverCurrent, driverPrevious, driverComparison.mode);

  document.querySelector("#capital-stock").textContent = money(inventory.on_hand_cost);
  document.querySelector("#capital-incoming").textContent = money(inventory.incoming_cost);
  const incomingMeta = document.querySelector("#capital-incoming-meta");
  if (incomingMeta) {
    const meta = [`${number(inventory.incoming_units)} ед. в пути`];
    if (Number(inventory.incoming_estimated_units || 0) > 0) meta.push(`${number(inventory.incoming_estimated_units)} ед. оценены по текущей цене поставщика`);
    if (Number(inventory.incoming_unpriced_units || 0) > 0) meta.push(`${number(inventory.incoming_unpriced_units)} ед. пока без цены`);
    incomingMeta.textContent = meta.join(" · ");
  }
  const ratio = Number(inventory.on_hand_cost || 0) > 0 ? summary.revenue / Number(inventory.on_hand_cost) : NaN;
  document.querySelector("#capital-ratio").textContent = Number.isFinite(ratio) ? `${number(ratio,2)}×` : "—";
  document.querySelector("#average-order").textContent = money(summary.averageOrder);
  document.querySelector("#average-day-revenue").textContent = money(summary.days ? summary.revenue/summary.days : 0);
  document.querySelector("#average-day-profit").textContent = money(summary.days ? summary.profit/summary.days : 0);
  document.querySelector("#profit-per-revenue").textContent = `${number(summary.revenue ? summary.profit/summary.revenue : 0,2)} KZT`;
  renderCapitalProducts(inventory);

  const periodLabel = selectedDays === 0 ? "Вся сохранённая история" : `Последние ${selectedDays} дней`;
  document.querySelector("#period-caption").textContent = periodLabel;

  const tableRows = [...currentRows].reverse();
  list.innerHTML = tableRows.map((item) => `<div class="revenue-row"><strong>${businessDate(item.business_date)}</strong><span>${number(item.orders_count)}</span><span>${number(item.units_count)}</span><span>${money(item.revenue)}</span><span>${money(item.net_profit)}</span><span>${percent(item.margin_pct)}</span><span>${dateTime(item.captured_at)}</span></div>`).join("");
  empty.classList.toggle("hidden", tableRows.length > 0);
  drawChart(currentRows, summary, previous);
  authPanel.classList.add("hidden");
  revenuePage.classList.remove("hidden");
};

const loadRevenue = async () => {
  if (!localStorage.getItem(storageKey)) {
    authPanel.classList.remove("hidden");
    revenuePage.classList.add("hidden");
    return;
  }
  refreshButton.disabled = true;
  try {
    const response = await fetch("/api/revenue/daily?limit=366", {headers:headers(), cache:"no-store"});
    if (response.status === 401) {
      localStorage.removeItem(storageKey);
      throw new Error("Токен не принят. Введите актуальный SERVICE_API_TOKEN.");
    }
    if (!response.ok) throw await responseError(response);
    render(await response.json());
    message.textContent = "";
  } catch (error) {
    message.textContent = error.message || "Не удалось загрузить аналитику.";
  } finally {
    refreshButton.disabled = false;
  }
};

periodSwitch.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-days]");
  if (!button) return;
  selectedDays = Number(button.dataset.days);
  periodSwitch.querySelectorAll("button").forEach((node)=>node.classList.toggle("active",node===button));
  if (payloadCache) render(payloadCache);
});

metricSwitch.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-metric]");
  if (!button) return;
  selectedMetric = button.dataset.metric;
  metricSwitch.querySelectorAll("button").forEach((node)=>node.classList.toggle("active",node===button));
  if (payloadCache) render(payloadCache);
});

canvas.addEventListener("mousemove", (event) => {
  if (!chartPoints.length) return;
  const rect = canvas.getBoundingClientRect();
  const mouseX = event.clientX - rect.left;
  let nearest = chartPoints[0];
  for (const point of chartPoints) if (Math.abs(point.x-mouseX) < Math.abs(nearest.x-mouseX)) nearest = point;
  const config = metricConfig[selectedMetric];
  let movement = "";
  if (nearest.previousValue != null) {
    const delta = nearest.value - nearest.previousValue;
    const deltaPct = nearest.previousValue !== 0 ? delta * 100 / Math.abs(nearest.previousValue) : NaN;
    const formattedDelta = selectedMetric === "revenue" || selectedMetric === "profit" ? signedMoney(delta) : `${delta > 0 ? "+" : ""}${number(delta)}`;
    movement = `<small class="${changeClass(delta)}">${formattedDelta}${Number.isFinite(deltaPct) ? ` · ${signedPercent(deltaPct)}` : ""} к предыдущему дню</small>`;
  }
  tooltip.innerHTML = `${businessDate(nearest.row.business_date)}<strong>${config.format(nearest.value)}</strong>${movement}`;
  tooltip.style.left = `${Math.min(Math.max(nearest.x,90),rect.width-90)}px`;
  tooltip.style.top = `${Math.max(nearest.y,70)}px`;
  tooltip.classList.remove("hidden");
});
canvas.addEventListener("mouseleave",()=>tooltip.classList.add("hidden"));
window.addEventListener("resize",()=>{if(payloadCache)render(payloadCache);});

tokenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  localStorage.setItem(storageKey, tokenInput.value.trim());
  tokenInput.value = "";
  loadRevenue();
});
refreshButton.addEventListener("click", loadRevenue);
loadRevenue();