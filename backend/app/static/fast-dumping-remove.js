"use strict";

(() => {
  const button = document.querySelector("#edit-remove");
  const dialog = document.querySelector("#edit-dialog");
  const productIdInput = document.querySelector("#edit-product-id");
  const title = document.querySelector("#edit-title");
  const message = document.querySelector("#message");
  if (!button || !dialog || !productIdInput) return;

  const storageKey = "leo_crm_service_token";

  button.addEventListener("click", async () => {
    const productId = Number(productIdInput.value);
    if (!productId) return;
    const productName = String(title?.textContent || "этот товар").trim();
    const confirmed = window.confirm(
      `Удалить «${productName}» из Fast Dumping?\n\n` +
      "CRM перестанет управлять этим SKU через Fast. Сам товар, FIFO, заказы, поставщики и текущая цена/остаток в Kaspi не удаляются и не изменяются."
    );
    if (!confirmed) return;

    const previous = button.textContent;
    button.disabled = true;
    button.textContent = "Удаляю…";
    try {
      const token = localStorage.getItem(storageKey) || "";
      const response = await fetch(`/api/fast-dumping/products/${productId}`, {
        method: "DELETE",
        cache: "no-store",
        headers: {Authorization: `Bearer ${token}`},
      });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401) {
        localStorage.removeItem(storageKey);
        throw new Error("SERVICE_API_TOKEN не принят");
      }
      if (!response.ok) {
        throw new Error(payload.detail || `API вернул HTTP ${response.status}`);
      }
      dialog.close();
      if (message) {
        message.textContent = "Товар удалён из Fast Dumping. Его карточка, FIFO, заказы и текущее состояние Kaspi не изменены.";
      }
      window.setTimeout(() => window.location.reload(), 250);
    } catch (error) {
      if (message) message.textContent = error.message || "Не удалось удалить товар из Fast Dumping";
      button.disabled = false;
      button.textContent = previous;
    }
  });
})();
