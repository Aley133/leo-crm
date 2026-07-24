const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const revenuePage = document.querySelector("#revenue-page");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const message = document.querySelector("#message");
const list = document.querySelector("#revenue-list");
const empty = document.querySelector("#empty");

const headers = () => ({"Authorization": `Bearer ${localStorage.getItem(storageKey) || ""}`});
const money = (value) => `${Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:2})} KZT`;
const percent = (value) => `${Number(value || 0).toLocaleString("ru-RU", {maximumFractionDigits:2})}%`;
const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const businessDate = (value) => value ? new Date(`${value}T00:00:00`).toLocaleDateString("ru-RU") : "—";

const responseError = async (response) => {
  let detail = `HTTP ${response.status}`;
  try {
    const payload = await response.json();
    detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || detail);
  } catch (_) {}
  return new Error(detail);
};

const render = (payload) => {
  const summary = payload.summary || {};
  const items = payload.items || [];
  document.querySelector("#summary-days").textContent = Number(payload.total || 0).toLocaleString("ru-RU");
  document.querySelector("#summary-revenue").textContent = money(summary.revenue);
  document.querySelector("#summary-profit").textContent = money(summary.net_profit);
  document.querySelector("#summary-margin").textContent = `маржа ${percent(summary.margin_pct)}`;
  list.innerHTML = items.map((item) => `<div class="revenue-row"><strong>${businessDate(item.business_date)}</strong><span>${Number(item.orders_count || 0)}</span><span>${Number(item.units_count || 0)}</span><span>${money(item.revenue)}</span><span>${money(item.net_profit)}</span><span>${percent(item.margin_pct)}</span><span>${dateTime(item.captured_at)}</span></div>`).join("");
  empty.classList.toggle("hidden", items.length > 0);
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
    message.textContent = error.message || "Не удалось загрузить дневную выручку.";
  } finally {
    refreshButton.disabled = false;
  }
};

tokenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  localStorage.setItem(storageKey, tokenInput.value.trim());
  tokenInput.value = "";
  loadRevenue();
});
refreshButton.addEventListener("click", loadRevenue);
loadRevenue();
