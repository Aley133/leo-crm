const SESSION_KEY = "leo_workspace_session";
const token = localStorage.getItem(SESSION_KEY);
if (!token) window.location.replace("/login");
const productId = Number(location.pathname.split("/").filter(Boolean).at(-1));
const authHeaders = () => ({Authorization: `Bearer ${token || ""}`});
const setText = (id, value) => { const node = document.querySelector(`#${id}`); if (node) node.textContent = String(value ?? "—"); };
const statusLabel = (status) => ({active:"Активен",draft:"Черновик",paused:"Приостановлен",archived:"Архив"}[status] || status || "—");
const money = (value) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} KZT`;
const message = document.querySelector("#message");

const loadContext = async () => {
  const [meResponse, kaspiResponse] = await Promise.all([
    fetch("/api/auth/me", {headers:authHeaders(), cache:"no-store"}),
    fetch("/api/workspace/kaspi", {headers:authHeaders(), cache:"no-store"}),
  ]);
  if (meResponse.status === 401 || kaspiResponse.status === 401) {
    localStorage.removeItem(SESSION_KEY);
    window.location.replace("/login");
    return false;
  }
  if (meResponse.ok) {
    const me = await meResponse.json();
    setText("workspace-label", me.username || "Workspace");
  }
  return true;
};

const loadProduct = async () => {
  message.textContent = "";
  try {
    if (!(await loadContext())) return;
    const response = await fetch(`/api/workspace/products/${productId}`, {headers:authHeaders(), cache:"no-store"});
    if (response.status === 404) { message.textContent = "Товар не найден в текущем рабочем пространстве."; return; }
    if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
    const row = await response.json();
    setText("product-name", row.name);
    setText("product-meta", `Kaspi ${row.kaspi_product_id}${row.brand ? ` · ${row.brand}` : ""}${row.merchant_sku ? ` · SKU ${row.merchant_sku}` : ""}`);
    setText("kaspi-product-id", row.kaspi_product_id);
    setText("merchant-sku", row.merchant_sku || "—");
    setText("product-brand", row.brand || "—");
    setText("product-status", statusLabel(row.status));
    setText("units-sold", Number(row.units_sold || 0).toLocaleString("ru-RU"));
    setText("orders-count", `строк заказов: ${Number(row.orders_count || 0).toLocaleString("ru-RU")}`);
    setText("revenue-kzt", money(row.revenue_kzt));
    setText("bindings-count", Number(row.supplier_count || 0));
    setText("available-count", Number(row.available_offer_count || 0));
    setText("failures-count", Number(row.failed_monitor_count || 0));
    setText("observations-count", 0);
    setText("product-updated-at", "Данные текущего рабочего пространства");
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : "Не удалось загрузить карточку товара.";
  }
};

document.querySelector("#refresh")?.addEventListener("click", loadProduct);
document.querySelector("#logout")?.addEventListener("click", async () => {
  try { await fetch("/api/auth/logout", {method:"POST", headers:authHeaders()}); } catch (_) {}
  localStorage.removeItem(SESSION_KEY);
  window.location.replace("/login");
});
document.querySelector("#add-supplier")?.addEventListener("click", () => {
  message.textContent = "Подключение источников закупки к workspace-карточке переносится без изменения существующей логики.";
});
loadProduct();
