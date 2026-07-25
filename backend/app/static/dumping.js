const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#dumping-page");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const policyForm = document.querySelector("#policy-form");
const productSelect = document.querySelector("#product-id");
const list = document.querySelector("#dumping-list");
const empty = document.querySelector("#empty");
let configuredRows = [];

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} KZT`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU") : "Ещё не запускался";

const request = async (url, options = {}) => {
  const token = localStorage.getItem(storageKey);
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
    headers: {Authorization:`Bearer ${token}`, ...(options.headers || {})},
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    localStorage.removeItem(storageKey);
    throw new Error("Токен не принят");
  }
  if (!response.ok) throw new Error(payload.detail || `API вернул ошибку ${response.status}`);
  return payload;
};

const setBusy = (button, busy, text) => {
  if (!button) return;
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? text : button.dataset.label;
};

const loadProducts = async () => {
  const rows = await request("/api/product-registry/products?limit=500");
  const configured = new Set(configuredRows.map((row) => Number(row.product_id)));
  productSelect.innerHTML = '<option value="">Выберите товар</option>' + rows
    .filter((row) => !configured.has(Number(row.product_id)))
    .map((row) => `<option value="${row.product_id}">${escapeHtml(row.name)} · ${escapeHtml(row.merchant_sku || row.kaspi_product_id)}</option>`)
    .join("");
};

const statusLabel = (row) => {
  if (!row.policy.enabled) return '<span class="badge-off">Отключён</span>';
  const status = row.latest_run?.status;
  if (status === "floor_limited") return '<span class="badge-limited">Ограничен порогом</span>';
  if (status) return '<span class="badge-ready">Активен</span>';
  return '<span class="badge-off">Ожидает запуска</span>';
};

const render = (rows) => {
  configuredRows = rows;
  document.querySelector("#summary-total").textContent = rows.length;
  document.querySelector("#summary-enabled").textContent = rows.filter((row) => row.policy.enabled).length;
  document.querySelector("#summary-limited").textContent = rows.filter((row) => row.latest_run?.status === "floor_limited").length;
  document.querySelector("#rows-label").textContent = `Подключено карточек: ${rows.length}`;
  document.querySelector("#updated-at").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit"})}`;
  list.innerHTML = rows.map((row) => `
    <article class="dumping-card" data-product-id="${row.product_id}">
      <div class="dumping-head">
        <div class="dumping-title"><h2>${escapeHtml(row.name)}</h2><span>Kaspi ${escapeHtml(row.kaspi_product_id)}${row.merchant_sku ? ` · SKU ${escapeHtml(row.merchant_sku)}` : ""}</span></div>
        <div class="dumping-actions"><button class="button secondary edit-policy" type="button">Настроить</button><button class="button run-now" type="button">Запустить сейчас</button></div>
      </div>
      <div class="dumping-grid">
        <div><span>Статус</span><strong>${statusLabel(row)}</strong></div>
        <div><span>Источник</span><strong>${escapeHtml(row.source?.name || "Нет источника")}</strong><small>${escapeHtml(row.source?.kind || "—")}</small></div>
        <div><span>Себестоимость</span><strong>${money(row.source?.unit_cost_kzt)}</strong></div>
        <div><span>Безопасный порог</span><strong>${money(row.latest_run?.safe_floor_kzt)}</strong></div>
        <div><span>Целевая цена</span><strong>${money(row.latest_run?.target_price_kzt)}</strong></div>
      </div>
      <div class="dumping-grid">
        <div><span>Минимальная прибыль</span><strong>${money(row.policy.minimum_profit_kzt)}</strong></div>
        <div><span>Шаг</span><strong>${money(row.policy.undercut_step_kzt)}</strong></div>
        <div><span>preOrder</span><strong>${row.latest_run?.preorder_days ?? "—"} дн.</strong></div>
        <div><span>Цена конкурента</span><strong>${money(row.latest_run?.competitor_price_kzt)}</strong></div>
        <div><span>Последний запуск</span><strong>${dateTime(row.latest_run?.created_at)}</strong></div>
      </div>
    </article>`).join("");
  empty.classList.toggle("hidden", rows.length > 0);
};

const fillForm = (row) => {
  productSelect.innerHTML = `<option value="${row.product_id}">${escapeHtml(row.name)}</option>`;
  productSelect.value = String(row.product_id);
  document.querySelector("#minimum-profit").value = row.policy.minimum_profit_kzt;
  document.querySelector("#undercut-step").value = row.policy.undercut_step_kzt;
  document.querySelector("#delivery-buffer").value = row.policy.supplier_delivery_buffer_days;
  document.querySelector("#city-id").value = row.policy.city_id;
  document.querySelector("#zone-id").value = row.policy.zone_id;
  document.querySelector("#inventory-first").checked = row.policy.inventory_first;
  document.querySelector("#auto-publish").checked = row.policy.auto_publish_xml;
  document.querySelector("#enabled").checked = row.policy.enabled;
  document.querySelector("#save-policy").textContent = "Сохранить настройки";
  policyForm.scrollIntoView({behavior:"smooth", block:"center"});
};

const loadPage = async () => {
  const token = localStorage.getItem(storageKey);
  if (!token) { authPanel.classList.remove("hidden"); page.classList.add("hidden"); return; }
  setBusy(refreshButton, true, "Обновляю…"); message.textContent = "";
  try {
    const rows = await request("/api/dumping");
    render(rows);
    await loadProducts();
    authPanel.classList.add("hidden");
    page.classList.remove("hidden");
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось загрузить демпинг";
    if (!localStorage.getItem(storageKey)) authPanel.classList.remove("hidden");
  } finally { setBusy(refreshButton, false, ""); }
};

policyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const productId = Number(productSelect.value);
  if (!productId) return;
  const button = document.querySelector("#save-policy");
  setBusy(button, true, "Сохраняю…"); message.textContent = "";
  try {
    await request(`/api/dumping/products/${productId}`, {
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        enabled:document.querySelector("#enabled").checked,
        minimum_profit_kzt:Number(document.querySelector("#minimum-profit").value),
        undercut_step_kzt:Number(document.querySelector("#undercut-step").value),
        supplier_delivery_buffer_days:Number(document.querySelector("#delivery-buffer").value),
        inventory_first:document.querySelector("#inventory-first").checked,
        auto_publish_xml:document.querySelector("#auto-publish").checked,
        city_id:document.querySelector("#city-id").value.trim(),
        zone_id:document.querySelector("#zone-id").value.trim(),
      }),
    });
    message.textContent = "Настройки демпинга сохранены.";
    policyForm.reset();
    document.querySelector("#minimum-profit").value = "1000";
    document.querySelector("#undercut-step").value = "1";
    document.querySelector("#delivery-buffer").value = "1";
    document.querySelector("#city-id").value = "750000000";
    document.querySelector("#zone-id").value = "Magnum_ZONE1";
    document.querySelector("#inventory-first").checked = true;
    document.querySelector("#auto-publish").checked = true;
    document.querySelector("#enabled").checked = true;
    button.dataset.label = "Подключить";
    await loadPage();
  } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось сохранить"; }
  finally { setBusy(button, false, ""); }
});

list.addEventListener("click", async (event) => {
  const card = event.target.closest(".dumping-card");
  if (!card) return;
  const productId = Number(card.dataset.productId);
  const row = configuredRows.find((item) => Number(item.product_id) === productId);
  if (event.target.closest(".edit-policy") && row) { fillForm(row); return; }
  const runButton = event.target.closest(".run-now");
  if (!runButton) return;
  setBusy(runButton, true, "Проверяю Kaspi…"); message.textContent = "";
  try {
    const result = await request(`/api/dumping/products/${productId}/run-now`, {method:"POST"});
    message.textContent = `Опубликовано: ${money(result.decision.target_price_kzt)}, preOrder ${result.decision.preorder_days} дн.`;
    await loadPage();
  } catch (error) { message.textContent = error instanceof Error ? error.message : "Не удалось выполнить демпинг"; }
  finally { setBusy(runButton, false, ""); }
});

tokenForm.addEventListener("submit", (event) => { event.preventDefault(); const token = tokenInput.value.trim(); if (!token) return; localStorage.setItem(storageKey, token); tokenInput.value = ""; loadPage(); });
refreshButton.addEventListener("click", loadPage);
loadPage();
