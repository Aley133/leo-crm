(() => {
  const storageKey = "leo_crm_service_token";
  const advice = document.querySelector("#procurement-advice");
  const summaryValue = document.querySelector("#summary-procurement");
  const summaryCaption = document.querySelector("#summary-procurement-caption");
  let timer = null;

  const headers = () => ({Authorization: `Bearer ${localStorage.getItem(storageKey) || ""}`});
  const queryString = () => {
    const params = new URLSearchParams({limit: "200"});
    const query = document.querySelector("#search")?.value.trim();
    const status = document.querySelector("#status")?.value;
    if (query) params.set("query", query);
    if (status) params.set("status", status);
    return params.toString();
  };

  const render = (summary = {}) => {
    const shortage = Number(summary.procurement_required_units || 0);
    const incoming = Number(summary.incoming_reserved_units || 0);
    if (summaryValue) summaryValue.textContent = shortage.toLocaleString("ru-RU");
    if (summaryCaption) summaryCaption.textContent = `единиц · в пути: ${incoming.toLocaleString("ru-RU")}`;
    if (!advice) return;
    if (shortage > 0) {
      advice.textContent = `Требуется дополнительная закупка: ${shortage.toLocaleString("ru-RU")} ед. Уже покрыто товаром в пути: ${incoming.toLocaleString("ru-RU")} ед.`;
      advice.classList.remove("hidden");
    } else if (incoming > 0) {
      advice.textContent = `Все текущие предзаказы покрыты. В пути зарезервировано: ${incoming.toLocaleString("ru-RU")} ед.`;
      advice.classList.remove("hidden");
    } else {
      advice.textContent = "";
      advice.classList.add("hidden");
    }
  };

  const refresh = async () => {
    if (!localStorage.getItem(storageKey)) return;
    try {
      const response = await fetch(`/api/commerce/orders?${queryString()}`, {headers: headers(), cache: "no-store"});
      if (!response.ok) return;
      const payload = await response.json();
      render(payload.summary || {});
    } catch (_) {
      // Orders Center remains usable if advisory analytics are temporarily unavailable.
    }
  };

  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(refresh, 350);
  };

  document.querySelector("#filters")?.addEventListener("submit", schedule);
  document.querySelector("#reset")?.addEventListener("click", schedule);
  document.querySelector("#refresh")?.addEventListener("click", () => setTimeout(refresh, 1500));
  document.querySelector("#rebuild-orders")?.addEventListener("click", () => setTimeout(refresh, 2500));
  new MutationObserver(schedule).observe(document.querySelector("#orders-list"), {childList: true, subtree: false});
  document.addEventListener("DOMContentLoaded", schedule);
})();
