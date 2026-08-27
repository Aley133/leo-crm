"use strict";

const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#lab-page");
const message = document.querySelector("#message");
let refreshTimer = null;

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value) => value == null || value === "" ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} ₸`;
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

const statusText = (item) => ({
  ready_to_add: "Поставщик подтверждён",
  needs_supplier_link: "Нужна ссылка Ozon",
  needs_supplier_validation: "Ссылка изменена — проверьте",
  validating_supplier: "Проверяем ссылку",
  adding_to_kaspi: "Добавляем на Kaspi",
  enrolled_fast_dumping: "В Быстром демпинге",
  error: item.last_error || "Ошибка",
}[item.status] || item.status || "Кандидат");

const itemCard = (item) => {
  const supplier = item.offers?.supplier || {};
  const pricing = item.offers?.initial_pricing || {};
  const locked = ["validating_supplier", "adding_to_kaspi", "enrolled_fast_dumping"].includes(item.status);
  const canAdd = item.status === "ready_to_add" && supplier.validated;
  return `<article class="lab-card" data-id="${item.id}">
    ${item.image_url ? `<img class="lab-photo" src="${escapeHtml(item.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '<div class="lab-photo placeholder">Нет фото</div>'}
    <div class="lab-product"><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.brand || "Без бренда")} · Kaspi ${escapeHtml(item.kaspi_product_id)}</p><a href="${escapeHtml(item.kaspi_url)}" target="_blank" rel="noopener">Kaspi</a><span> · </span>${item.supplier_url ? `<a href="${escapeHtml(item.supplier_url)}" target="_blank" rel="noopener">Ozon</a>` : "Ozon не выбран"}<p><strong>${escapeHtml(statusText(item))}</strong></p></div>
    <div class="lab-value"><span>Конкурент Kaspi</span><strong>${money(item.observed_price_kzt)}</strong><small>${pricing.status ? escapeHtml(pricing.status) : "для стартовой цены"}</small></div>
    <div class="lab-value"><span>Поставщик Ozon</span><strong>${money(supplier.supplier_price_kzt)}</strong><small>${escapeHtml(supplier.supplier_seller_name || "не подтверждён")}</small></div>
    <label class="lab-value"><span>Своя ссылка Ozon</span><input class="supplier" type="url" maxlength="4000" value="${escapeHtml(item.supplier_url || "")}" placeholder="https://www.ozon.kz/product/…" ${locked ? "disabled" : ""}></label>
    <div class="lab-actions">
      <button class="button validate" type="button" ${locked ? "disabled" : ""}>Проверить ссылку</button>
      <button class="button add" type="button" ${canAdd ? "" : "disabled"}>Добавить на Kaspi</button>
      ${item.product_id ? `<a class="button secondary" href="/crm/products/${item.product_id}">Открыть товар</a><a class="button secondary" href="/crm/fast-dumping">Демпинг</a>` : ""}
    </div>
  </article>`;
};

const renderJobs = (jobs) => {
  const active = jobs.filter((job) => ["queued", "leased", "failed"].includes(job.status)).slice(0, 6);
  const pending = active.filter((job) => ["queued", "leased"].includes(job.status));
  const labels = {discover:"Поиск новых товаров", validate_supplier:"Проверка Ozon", create_offer:"Добавление на Kaspi", inspect:"Чтение карточки"};
  document.querySelector("#jobs").innerHTML = active.map((job) => `<div class="job ${job.status === "failed" ? "failed" : "pending"}"><strong>${escapeHtml(labels[job.job_type] || job.job_type)}</strong> · ${job.status === "leased" ? "Agent выполняет" : job.status === "queued" ? "ожидает Agent" : escapeHtml(job.error_message || "ошибка")}</div>`).join("");
  document.querySelector("#job-count").textContent = pending.length;
  if (pending.length && !refreshTimer) refreshTimer = window.setTimeout(() => { refreshTimer = null; load(); }, 3000);
};

const fillSettings = (settings) => {
  const form = document.querySelector("#settings-form");
  Object.entries(settings || {}).forEach(([key, value]) => {
    const field = form.elements.namedItem(key); if (!field) return;
    if (field.type === "checkbox") field.checked = Boolean(value); else field.value = value ?? "";
  });
  if (settings?.target_new) document.querySelector("#target-new").value = settings.target_new;
};

const render = (payload) => {
  const items = payload.items || [];
  document.querySelector("#items").innerHTML = items.map(itemCard).join("");
  document.querySelector("#empty").classList.toggle("hidden", items.length > 0);
  document.querySelector("#total-count").textContent = items.filter((item) => item.status !== "enrolled_fast_dumping").length;
  document.querySelector("#ready-count").textContent = items.filter((item) => item.status === "ready_to_add").length;
  document.querySelector("#enrolled-count").textContent = items.filter((item) => item.status === "enrolled_fast_dumping").length;
  fillSettings(payload.settings || {});
  renderJobs(payload.jobs || []);
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
  const card = event.target.closest(".lab-card"); const button = event.target.closest("button"); if (!card || !button) return;
  setBusy(button, true, button.classList.contains("add") ? "Добавляю…" : "Проверяю…");
  try {
    if (button.classList.contains("validate")) {
      const supplierUrl = card.querySelector(".supplier").value.trim();
      await request(`/api/product-test/items/${card.dataset.id}/validate-supplier`, {method:"POST", body:JSON.stringify({supplier_url:supplierUrl})});
      notify("Ссылка передана локальному HTTP Agent.", "success");
    } else if (button.classList.contains("add")) {
      await request(`/api/product-test/items/${card.dataset.id}/add`, {method:"POST"});
      notify("Создание оффера запущено. В демпинг он попадёт только после подтверждения Kaspi.", "success");
    }
    await load();
  } catch (error) { notify(error.message, "error"); } finally { setBusy(button, false, ""); }
});
load();
