const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const detailPage = document.querySelector("#detail");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const bindingsContainer = document.querySelector("#bindings");
const observationsBody = document.querySelector("#observations-body");

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const money = (value, currency) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU")} ${currency || ""}`.trim();
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "Никогда";
const productId = Number(location.pathname.split("/").filter(Boolean).at(-1));

const setText = (id, value) => {
  const element = document.querySelector(`#${id}`);
  if (element) element.textContent = String(value ?? 0);
};

const setLoading = (loading) => {
  detailPage.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  refreshButton.textContent = loading ? "Обновление…" : "Обновить";
};

const availabilityBadge = (available) => {
  if (available === true) return '<span class="badge ok">В наличии</span>';
  if (available === false) return '<span class="badge bad">Нет в наличии</span>';
  return '<span class="badge">Неизвестно</span>';
};

const monitorBadge = (binding) => {
  if (binding.consecutive_failures > 0) return '<span class="badge bad">Ошибка</span>';
  if (binding.monitor_status === "active") return '<span class="badge ok">Активен</span>';
  if (binding.monitor_status) return `<span class="badge warn">${escapeHtml(binding.monitor_status)}</span>`;
  return '<span class="badge">Не настроен</span>';
};

const renderBindings = (bindings) => {
  bindingsContainer.innerHTML = bindings.map((binding) => `
    <article class="binding-card">
      <div class="binding-head">
        <h3 class="binding-title">${escapeHtml(binding.supplier_name)}${binding.is_primary ? '<span class="primary-mark">Основной</span>' : ""}</h3>
        <a href="${escapeHtml(binding.supplier_product_url)}" target="_blank" rel="noreferrer">Открыть карточку поставщика</a>
        <span class="muted">${escapeHtml(binding.supplier_code)} · ${escapeHtml(binding.binding_status)}</span>
      </div>
      <div><span class="label">Цена</span><strong>${money(binding.price, binding.currency)}</strong>${binding.old_price != null ? `<span class="muted">было ${money(binding.old_price, binding.currency)}</span>` : ""}</div>
      <div><span class="label">Доставка</span><strong>${binding.delivery_days == null ? "—" : `${binding.delivery_days} дн.`}</strong><span class="muted">${escapeHtml(binding.seller || "продавец не указан")}</span></div>
      <div><span class="label">Наличие</span>${availabilityBadge(binding.available)}${binding.stock != null ? `<span class="muted">остаток ${binding.stock}</span>` : ""}</div>
      <div><span class="label">Мониторинг</span>${monitorBadge(binding)}<span class="muted">проверено ${dateTime(binding.last_checked_at)}</span></div>
    </article>`).join("");
  document.querySelector("#bindings-empty").classList.toggle("hidden", bindings.length > 0);
};

const renderObservations = (observations) => {
  observationsBody.innerHTML = observations.map((item) => `
    <tr>
      <td>${dateTime(item.observed_at)}</td>
      <td>${escapeHtml(item.supplier_code)}</td>
      <td>${money(item.price, item.currency)}${item.old_price != null ? `<span class="muted">было ${money(item.old_price, item.currency)}</span>` : ""}</td>
      <td>${item.delivery_days == null ? "—" : `${item.delivery_days} дн.`}</td>
      <td>${availabilityBadge(item.available)}${item.stock != null ? `<span class="muted">остаток ${item.stock}</span>` : ""}</td>
      <td>${escapeHtml(item.seller || "—")}</td>
    </tr>`).join("");
  document.querySelector("#observations-empty").classList.toggle("hidden", observations.length > 0);
};

const render = (data) => {
  const { product, bindings, observations } = data;
  setText("product-name", product.name);
  setText("product-meta", `Kaspi ${product.kaspi_product_id}${product.brand ? ` · ${product.brand}` : ""}${product.merchant_sku ? ` · SKU ${product.merchant_sku}` : ""}`);
  setText("bindings-count", bindings.length);
  setText("observations-count", observations.length);
  setText("available-count", bindings.filter((item) => item.available === true).length);
  setText("failures-count", bindings.filter((item) => item.consecutive_failures > 0).length);
  setText("updated-at", `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`);
  renderBindings(bindings);
  renderObservations(observations);
  authPanel.classList.add("hidden");
  detailPage.classList.remove("hidden");
};

const loadDetail = async () => {
  const token = localStorage.getItem(storageKey);
  if (!token) {
    authPanel.classList.remove("hidden");
    detailPage.classList.add("hidden");
    return;
  }
  if (!Number.isInteger(productId) || productId <= 0) {
    message.textContent = "Некорректный идентификатор товара.";
    return;
  }
  setLoading(true);
  message.textContent = "";
  try {
    const response = await fetch(`/api/products/${productId}/detail?observation_limit=100`, {headers:{Authorization:`Bearer ${token}`},cache:"no-store"});
    if (response.status === 401) {
      localStorage.removeItem(storageKey);
      authPanel.classList.remove("hidden");
      detailPage.classList.add("hidden");
      message.textContent = "Токен не принят. Проверьте SERVICE_API_TOKEN.";
      return;
    }
    if (response.status === 404) throw new Error("Товар не найден.");
    if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
    render(await response.json());
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось загрузить карточку товара.";
  } finally {
    setLoading(false);
  }
};

tokenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const token = tokenInput.value.trim();
  if (!token) return;
  localStorage.setItem(storageKey, token);
  tokenInput.value = "";
  loadDetail();
});
refreshButton.addEventListener("click", loadDetail);
loadDetail();
