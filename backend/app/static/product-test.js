"use strict";

const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#lab-page");
const message = document.querySelector("#message");
let pollTimer = null;

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
const setBusy = (button, busy, busyLabel) => { if (!button.dataset.label) button.dataset.label = button.textContent; button.disabled = busy; button.textContent = busy ? busyLabel : button.dataset.label; };

const image = (item) => item.image_url
  ? `<img class="lab-photo" src="${escapeHtml(item.image_url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">`
  : '<div class="lab-photo placeholder">Фото появится<br>после проверки</div>';

const itemCard = (item) => `<article class="lab-card" data-id="${item.id}">
  ${image(item)}
  <div class="lab-product"><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.brand || "Без бренда")} · Kaspi ${escapeHtml(item.kaspi_product_id)}<br>SKU ${escapeHtml(item.merchant_sku)}</p><a href="${escapeHtml(item.kaspi_url)}" target="_blank" rel="noopener">Открыть карточку Kaspi</a><input class="supplier" type="url" maxlength="4000" value="${escapeHtml(item.supplier_url || "")}" placeholder="Ссылка поставщика (необязательно)"></div>
  <div class="lab-value"><span>Цена карточки</span><strong>${money(item.observed_price_kzt)}</strong><small>последнее чтение</small></div>
  <label class="lab-value"><span>Цена для XML, ₸</span><input class="price" type="number" min="1" step="1" value="${item.test_price_kzt == null ? "" : Number(item.test_price_kzt)}"></label>
  <label class="lab-value"><span>Предзаказ, дней</span><input class="preorder" type="number" min="0" max="365" value="${Number(item.preorder_days || 0)}"></label>
  <label class="lab-value"><span>Остаток, шт.</span><input class="stock" type="number" min="0" max="1000000" value="${Number(item.stock_count || 0)}"></label>
  <div class="lab-actions"><button class="button save" type="button">Сохранить</button><button class="button secondary toggle" type="button">${item.active ? "Убрать из XML" : "Вернуть в XML"}</button></div>
</article>`;

const renderJobs = (jobs) => {
  const active = jobs.filter((job) => ["queued","leased","failed"].includes(job.status)).slice(0, 5);
  document.querySelector("#jobs").innerHTML = active.map((job) => `<div class="job ${job.status === "failed" ? "failed" : "pending"}"><strong>${job.status === "failed" ? "Ошибка" : job.status === "leased" ? "Agent читает карточку" : "В очереди Agent"}</strong> · ${escapeHtml(job.reference)}${job.error_message ? ` · ${escapeHtml(job.error_message)}` : ""}</div>`).join("");
  document.querySelector("#job-count").textContent = jobs.filter((job) => ["queued","leased"].includes(job.status)).length;
  if (jobs.some((job) => ["queued","leased"].includes(job.status))) {
    clearTimeout(pollTimer); pollTimer = setTimeout(load, 4000);
  }
};

const render = (payload) => {
  const items = payload.items || [];
  document.querySelector("#items").innerHTML = items.map(itemCard).join("");
  document.querySelector("#empty").classList.toggle("hidden", items.length > 0);
  document.querySelector("#total-count").textContent = items.length;
  document.querySelector("#xml-count").textContent = items.filter((item) => item.active && item.test_price_kzt != null).length;
  document.querySelector("#feed-state").textContent = payload.feed ? "Готов" : "Нет";
  document.querySelector("#feed-meta").textContent = payload.feed?.source_filename || "загрузите XML в Товарах";
  document.querySelector("#download-xml").classList.toggle("disabled", !payload.feed);
  renderJobs(payload.jobs || []);
};

const renderAgent = (payload) => {
  const card = document.querySelector("#agent-card");
  card.classList.toggle("ready", Boolean(payload.online)); card.classList.toggle("missing", !payload.online);
  document.querySelector("#agent-title").textContent = payload.online ? "Agent подключён" : "Agent не найден";
  const agent = (payload.agents || [])[0];
  document.querySelector("#agent-meta").textContent = payload.online ? `Версия ${agent?.version || "—"} · ${agent?.hostname || "локальный компьютер"} · новые карточки обрабатываются после задач Fast Dumping.` : "Запустите Agent 1.0.8 или новее на компьютере с доступом к Kaspi.";
};

async function load() {
  if (!localStorage.getItem(storageKey)) { authPanel.classList.remove("hidden"); page.classList.add("hidden"); return; }
  try {
    const [state, agent] = await Promise.all([request("/api/product-test"), request("/api/fast-dumping-agent/agents/status")]);
    authPanel.classList.add("hidden"); page.classList.remove("hidden"); render(state); renderAgent(agent); notify("");
  } catch (error) { notify(error.message, "error"); if (!localStorage.getItem(storageKey)) { authPanel.classList.remove("hidden"); page.classList.add("hidden"); } }
}

document.querySelector("#token-form").addEventListener("submit", (event) => { event.preventDefault(); localStorage.setItem(storageKey, document.querySelector("#token").value.trim()); load(); });
document.querySelector("#refresh").addEventListener("click", load);
document.querySelector("#inspect-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = document.querySelector("#inspect-button"); setBusy(button, true, "Ставлю в очередь…");
  try {
    await request("/api/product-test/inspect", {method:"POST", body:JSON.stringify({reference:document.querySelector("#reference").value.trim(), city_id:document.querySelector("#city-id").value.trim(), zone_id:document.querySelector("#zone-id").value.trim()})});
    document.querySelector("#reference").value = ""; notify("Карточка передана локальному Agent. Результат появится автоматически.", "success"); await load();
  } catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
document.querySelector("#items").addEventListener("click", async (event) => {
  const card = event.target.closest(".lab-card"); if (!card) return;
  const current = event.target.closest("button"); if (!current) return;
  setBusy(current, true, "Сохраняю…");
  try {
    const priceValue = card.querySelector(".price").value.trim();
    const body = current.classList.contains("toggle") ? {active:current.textContent.includes("Вернуть")} : {test_price_kzt:priceValue ? Number(priceValue) : null, preorder_days:Number(card.querySelector(".preorder").value), stock_count:Number(card.querySelector(".stock").value), supplier_url:card.querySelector(".supplier").value.trim() || null};
    await request(`/api/product-test/items/${card.dataset.id}`, {method:"PATCH", body:JSON.stringify(body)}); notify("Настройки тестового XML сохранены.", "success"); await load();
  } catch (error) { notify(error.message, "error"); } finally { setBusy(current, false, ""); }
});
document.querySelector("#download-xml").addEventListener("click", (event) => {
  const token = localStorage.getItem(storageKey) || "";
  if (!token) { event.preventDefault(); notify("Сначала подключитесь к CRM.", "error"); return; }
  event.preventDefault(); fetch("/api/product-test/xml", {headers:{Authorization:`Bearer ${token}`}}).then(async (response) => {
    if (!response.ok) throw new Error((await response.json()).detail || `HTTP ${response.status}`);
    const blob = await response.blob(); const href = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = href; link.download = "leo-product-test.xml"; link.click(); URL.revokeObjectURL(href);
  }).catch((error) => notify(error.message, "error"));
});
load();
