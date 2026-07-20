const storageKey = "leo_crm_service_token";

const authPanel = document.querySelector("#auth-panel");
const dashboard = document.querySelector("#dashboard");
const message = document.querySelector("#message");
const tokenInput = document.querySelector("#token");
const tokenForm = document.querySelector("#token-form");
const refreshButton = document.querySelector("#refresh");

const setText = (id, value) => {
  const element = document.querySelector(`#${id}`);
  if (element) element.textContent = String(value ?? 0);
};

const setLoading = (loading) => {
  dashboard.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  refreshButton.textContent = loading ? "Обновление…" : "Обновить";
};

const render = (data) => {
  const { products, monitoring, suppliers } = data;

  setText("products-total", products.total);
  setText("products-unbound", products.without_supplier);
  setText("monitoring-active", monitoring.active);
  setText("monitoring-errors", monitoring.failures);

  setText("attention-failures", monitoring.failures);
  setText("attention-stale", monitoring.stale);
  setText("attention-unbound", products.without_supplier);
  setText("attention-unavailable", suppliers.unavailable);

  setText("queue-count", monitoring.browser_queue);
  setText("leased-count", monitoring.browser_leased);
  setText("degraded-count", monitoring.degraded);
  setText("suppliers-active", suppliers.active_sources);

  setText("offers-total", suppliers.offers);
  setText("offers-state", suppliers.offers_with_state);
  setText("offers-available", suppliers.available);
  setText("offers-unavailable", suppliers.unavailable);

  const hasRuntimeProblems = monitoring.failures > 0 || monitoring.degraded > 0;
  setText("runtime-label", hasRuntimeProblems ? "Требует внимания" : "Работает штатно");
  setText("updated-at", `Обновлено ${new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`);

  authPanel.classList.add("hidden");
  dashboard.classList.remove("hidden");
};

const loadDashboard = async () => {
  const token = localStorage.getItem(storageKey);
  if (!token) {
    authPanel.classList.remove("hidden");
    dashboard.classList.add("hidden");
    message.textContent = "";
    return;
  }

  setLoading(true);
  message.textContent = "";

  try {
    const response = await fetch("/api/dashboard", {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });

    if (response.status === 401) {
      localStorage.removeItem(storageKey);
      authPanel.classList.remove("hidden");
      dashboard.classList.add("hidden");
      message.textContent = "Токен не принят. Проверьте SERVICE_API_TOKEN.";
      return;
    }
    if (response.status === 503) {
      throw new Error("SERVICE_API_TOKEN не настроен на сервере Render.");
    }
    if (!response.ok) {
      throw new Error(`API вернул ошибку ${response.status}`);
    }

    render(await response.json());
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось загрузить Dashboard.";
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
  loadDashboard();
});

refreshButton.addEventListener("click", loadDashboard);
loadDashboard();
