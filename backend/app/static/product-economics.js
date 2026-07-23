(() => {
  const storageKey = "leo_crm_service_token";
  const productId = Number(location.pathname.split("/").filter(Boolean).at(-1));
  const money = (value) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})} KZT`;
  const percent = (value) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits:2})}%`;
  const setText = (id, value) => {
    const element = document.querySelector(`#${id}`);
    if (element) element.textContent = String(value ?? "—");
  };

  const loadEconomics = async () => {
    const token = localStorage.getItem(storageKey);
    if (!token || !Number.isInteger(productId) || productId <= 0) return;
    try {
      const response = await fetch(`/api/products/${productId}/economics`, {
        headers: {Authorization: `Bearer ${token}`},
        cache: "no-store",
      });
      if (!response.ok) return;
      const data = await response.json();
      setText("economics-sale-price", money(data.sale_unit_price));
      setText("economics-procurement", money(data.procurement_unit_cost));
      setText("economics-source", data.procurement_source_name || "Источник не выбран");
      setText("economics-commission", money(data.kaspi_commission));
      setText("economics-tax", money(data.tax));
      setText("economics-logistics", money(data.logistics));
      setText("economics-profit", money(data.net_profit));
      setText("economics-margin", data.net_margin_pct == null ? "После выбора источника закупки" : `Маржа ${percent(data.net_margin_pct)}`);
      setText("total-net-profit", money(data.total_net_profit));
      setText(
        "total-net-margin",
        data.total_net_profit == null
          ? "После выбора источника закупки"
          : `по ${Number(data.profit_units_count || 0).toLocaleString("ru-RU")} ед. · маржа ${percent(data.total_net_margin_pct)}`,
      );
    } catch (_) {
      // The main product card remains usable even when economics is temporarily unavailable.
    }
  };

  document.addEventListener("DOMContentLoaded", loadEconomics);
  document.querySelector("#refresh")?.addEventListener("click", () => setTimeout(loadEconomics, 300));
  document.querySelector("#supplier-form")?.addEventListener("submit", () => setTimeout(loadEconomics, 1500));
})();