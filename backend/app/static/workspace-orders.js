const SESSION_KEY = "leo_workspace_session";
const token = localStorage.getItem(SESSION_KEY);
if (!token) window.location.replace("/login");

const headers = (json = false) => ({Authorization:`Bearer ${token || ""}`,...(json?{"Content-Type":"application/json"}:{})});
const money = (value, currency="KZT") => `${Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:2})} ${currency}`;
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const stageLabel = (value) => ({preorder:"Предзаказ",assembly:"Упаковка",handover:"Передача",shipping:"Передан в доставку",delivered:"Завершён",cancelled:"Отменён",returned:"Возврат",new:"Новый",accepted:"Принят",unknown:"Прочее"}[value] || value || "—");
const message = document.querySelector("#message");
const list = document.querySelector("#orders-list");
const empty = document.querySelector("#empty");

const readError = async (response) => { try { const p=await response.json(); return p.detail || `HTTP ${response.status}`; } catch (_) { return `HTTP ${response.status}`; } };
const queryString = () => { const p=new URLSearchParams({limit:"200"}); const q=document.querySelector("#search").value.trim(); const s=document.querySelector("#status").value; if(q)p.set("query",q); if(s)p.set("status",s); return p.toString(); };

const renderLine = (line) => `<div class="order-line"><div><strong>${esc(line.title)}</strong><span class="muted">${line.merchant_sku ? `Артикул ${esc(line.merchant_sku)}` : ""}${line.external_product_id ? ` · Kaspi ID ${esc(line.external_product_id)}` : ""}</span></div><div><span class="muted">Количество</span><strong>${Number(line.quantity || 0)}</strong></div><div><span class="muted">Цена</span><strong>${money(line.unit_price)}</strong></div><div><span class="muted">Себестоимость</span><strong>${line.procurement_unit_cost == null ? "—" : money(line.procurement_unit_cost)}</strong></div></div>`;
const renderOrder = (order) => `<article class="order-card"><div class="order-header"><div><span class="order-number">Заказ №${esc(order.external_code || order.order_id)}</span><span class="order-meta">Kaspi · ${new Date(order.ordered_at).toLocaleString("ru-RU")}</span></div><div class="order-stat"><span>Этап</span><strong>${esc(stageLabel(order.operational_stage))}</strong></div><div class="order-stat"><span>Единиц</span><strong>${Number(order.units || 0)}</strong></div><div class="order-stat"><span>Сумма</span><strong>${money(order.total_amount, order.currency)}</strong></div></div><div class="order-lines">${(order.lines || []).map(renderLine).join("")}</div></article>`;

const loadShop = async () => {
  const response = await fetch("/api/workspace/kaspi", {headers:headers(), cache:"no-store"});
  if (response.status === 401) return logout();
  if (!response.ok) throw new Error(await readError(response));
  const p = await response.json();
  if (!p.configured) { window.location.replace("/crm/account"); return; }
  document.querySelector("#shop-title").textContent = `Заказы — ${p.shop_name || "магазин"}`;
};

const loadOrders = async () => {
  const response = await fetch(`/api/workspace/orders?${queryString()}`, {headers:headers(), cache:"no-store"});
  if (response.status === 401) return logout();
  if (!response.ok) throw new Error(await readError(response));
  const p = await response.json();
  document.querySelector("#summary-orders").textContent = Number(p.summary?.orders_count || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-active").textContent = Number(p.summary?.active_orders || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-revenue").textContent = money(p.summary?.revenue || 0);
  document.querySelector("#summary-procurement").textContent = Number(p.summary?.procurement_required_lines || 0).toLocaleString("ru-RU");
  list.innerHTML = (p.items || []).map(renderOrder).join("");
  empty.classList.toggle("hidden", (p.items || []).length > 0);
  document.querySelector("#rows-label").textContent = `Показано заказов: ${(p.items || []).length} из ${p.total || 0}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
};

const pollImport = async (jobId) => {
  for (;;) {
    const response = await fetch(`/api/workspace/kaspi/import/${encodeURIComponent(jobId)}`, {headers:headers(), cache:"no-store"});
    if (!response.ok) throw new Error(await readError(response));
    const p = await response.json();
    message.textContent = p.message || `Статус импорта: ${p.status}`;
    if (["completed","failed","succeeded","partial"].includes(p.status)) return p;
    await new Promise(r => setTimeout(r, 1200));
  }
};

const startImport = async () => {
  const button=document.querySelector("#import"); button.disabled=true; message.textContent="Запускаю импорт заказов…";
  try {
    const days=document.querySelector("#import-days").value;
    const response=await fetch(`/api/workspace/kaspi/import?days=${days}`, {method:"POST",headers:headers()});
    if(!response.ok) throw new Error(await readError(response));
    const started=await response.json();
    await pollImport(started.job_id);
    await loadOrders();
  } catch(error) { message.textContent=error.message || "Импорт не выполнен."; }
  finally { button.disabled=false; }
};

const logout = async () => { try { await fetch("/api/auth/logout", {method:"POST",headers:headers()}); } catch (_) {} localStorage.removeItem(SESSION_KEY); window.location.replace("/login"); };

document.querySelector("#refresh").addEventListener("click", () => loadOrders().catch(e => message.textContent=e.message));
document.querySelector("#import").addEventListener("click", startImport);
document.querySelector("#filters").addEventListener("submit", e => {e.preventDefault(); loadOrders().catch(err => message.textContent=err.message);});
document.querySelector("#logout").addEventListener("click", logout);

Promise.all([loadShop(), loadOrders()]).catch(error => message.textContent=error.message || "Не удалось открыть кабинет.");
