const SESSION_KEY = "leo_workspace_session";
const token = localStorage.getItem(SESSION_KEY);
if (!token) window.location.replace("/login");

const headers = (json = false) => ({
  Authorization: `Bearer ${token || ""}`,
  ...(json ? {"Content-Type": "application/json"} : {}),
});

const form = document.querySelector("#kaspi-form");
const configured = document.querySelector("#configured");
const save = document.querySelector("#save");
const message = document.querySelector("#message");

const readError = async (response) => {
  try {
    const payload = await response.json();
    return typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`;
  } catch (_) {
    return `HTTP ${response.status}`;
  }
};

const applyConnection = (payload) => {
  configured.classList.toggle("hidden", !payload.configured);
  if (!payload.configured) return;
  document.querySelector("#current-shop").textContent = payload.shop_name || "—";
  document.querySelector("#current-partner").textContent = payload.partner_id || "—";
  document.querySelector("#shop-name").value = payload.shop_name || "";
  document.querySelector("#partner-id").value = payload.partner_id || "";
  document.querySelector("#timezone").value = payload.timezone || "Asia/Almaty";
  save.textContent = "Обновить подключение";
};

const load = async () => {
  try {
    const response = await fetch("/api/workspace/kaspi", {headers: headers(), cache: "no-store"});
    if (response.status === 401) {
      localStorage.removeItem(SESSION_KEY);
      window.location.replace("/login");
      return;
    }
    if (!response.ok) throw new Error(await readError(response));
    applyConnection(await response.json());
  } catch (error) {
    message.textContent = error.message || "Не удалось проверить подключение.";
  }
};

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const wasConfigured = !configured.classList.contains("hidden");
  save.disabled = true;
  save.textContent = "Сохраняю…";
  message.textContent = "";
  try {
    const response = await fetch("/api/workspace/kaspi", {
      method: "PUT",
      headers: headers(true),
      body: JSON.stringify({
        shop_name: document.querySelector("#shop-name").value.trim(),
        partner_id: document.querySelector("#partner-id").value.trim(),
        api_token: document.querySelector("#api-token").value,
        timezone: document.querySelector("#timezone").value.trim(),
      }),
    });
    if (!response.ok) throw new Error(await readError(response));
    const payload = await response.json();
    applyConnection(payload);
    document.querySelector("#api-token").value = "";
    message.textContent = wasConfigured
      ? "Подключение обновлено."
      : "Магазин подключён. Открываю Orders Center…";
    if (!wasConfigured) {
      window.setTimeout(() => window.location.assign("/crm/orders"), 500);
    }
  } catch (error) {
    message.textContent = error.message || "Не удалось подключить магазин.";
  } finally {
    save.disabled = false;
    if (!configured.classList.contains("hidden")) save.textContent = "Обновить подключение";
    else save.textContent = "Подключить магазин";
  }
});

document.querySelector("#logout").addEventListener("click", async () => {
  try { await fetch("/api/auth/logout", {method: "POST", headers: headers()}); } catch (_) {}
  localStorage.removeItem(SESSION_KEY);
  window.location.replace("/login");
});

load();