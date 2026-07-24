(() => {
  const ordersList = document.querySelector("#orders-list");
  const message = document.querySelector("#message");
  if (!ordersList) return;

  const authHeaders = () => ({
    "Authorization": `Bearer ${localStorage.getItem("leo_crm_service_token") || ""}`,
    "Content-Type": "application/json",
  });

  const parseError = async (response) => {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string"
        ? payload.detail
        : JSON.stringify(payload.detail || detail);
    } catch (_) {}
    return detail;
  };

  const loadingLabel = (targetStatus) => ({
    requested: "Отправляю…",
    ordered: "Сохраняю…",
    received: "Принимаю…",
    closed: "Закрываю…",
  }[targetStatus] || "Сохраняю…");

  ordersList.addEventListener("click", async (event) => {
    const button = event.target.closest(".purchase-transition");
    if (!button) return;

    event.preventDefault();
    event.stopImmediatePropagation();

    if (button.disabled || button.dataset.loading === "true") return;

    const originalHtml = button.innerHTML;
    const purchaseId = button.dataset.purchaseId;
    const targetStatus = button.dataset.targetStatus;
    const version = Number(button.dataset.version);

    button.dataset.loading = "true";
    button.disabled = true;
    button.classList.add("is-loading");
    button.textContent = loadingLabel(targetStatus);
    button.setAttribute("aria-busy", "true");
    if (message) message.textContent = "Сохраняю изменение закупки…";

    try {
      const response = await fetch(
        `/api/purchases/${encodeURIComponent(purchaseId)}/transition`,
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({
            target_status: targetStatus,
            expected_version: version,
            idempotency_key: `orders-center:${purchaseId}:${version}:${targetStatus}`,
            metadata: {source: "orders-center"},
          }),
        },
      );

      if (!response.ok) throw new Error(await parseError(response));

      button.textContent = "Готово";
      if (message) message.textContent = "Статус закупки обновлён.";
      window.location.reload();
    } catch (error) {
      button.innerHTML = originalHtml;
      button.disabled = false;
      button.classList.remove("is-loading");
      button.removeAttribute("aria-busy");
      button.dataset.loading = "false";
      if (message) message.textContent = error.message || "Не удалось обновить статус закупки.";
    }
  }, true);
})();
