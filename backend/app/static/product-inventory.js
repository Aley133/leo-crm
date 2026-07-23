(() => {
  const storageKey = "leo_crm_service_token";
  const productId = Number(location.pathname.split("/").filter(Boolean).at(-1));
  const dialog = document.querySelector("#inventory-dialog");
  const form = document.querySelector("#inventory-form");
  const result = document.querySelector("#inventory-result");
  const body = document.querySelector("#inventory-batches-body");
  const empty = document.querySelector("#inventory-empty");

  const money = (value) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits: 2})} KZT`;
  const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
  const localDateTimeValue = () => {
    const now = new Date();
    const offset = now.getTimezoneOffset();
    return new Date(now.getTime() - offset * 60000).toISOString().slice(0, 16);
  };

  const setSummary = (inventory) => {
    const onHand = document.querySelector("#inventory-on-hand");
    const summary = document.querySelector("#inventory-summary");
    if (onHand) onHand.textContent = Number(inventory.on_hand || 0).toLocaleString("ru-RU");
    if (summary) summary.textContent = `партий: ${Number(inventory.batches?.length || 0)} · списано: ${Number(inventory.allocated_total || 0)}`;
  };

  const render = (inventory) => {
    setSummary(inventory);
    const batches = inventory.batches || [];
    body.innerHTML = batches.map((batch) => `
      <tr>
        <td>${dateTime(batch.received_at)}${batch.reference ? `<span class="muted">${escapeHtml(batch.reference)}</span>` : ""}</td>
        <td>${escapeHtml(batch.source_name || "Не указан")}${batch.note ? `<span class="muted">${escapeHtml(batch.note)}</span>` : ""}</td>
        <td>${money(batch.unit_cost)}</td>
        <td>${Number(batch.quantity_received).toLocaleString("ru-RU")}</td>
        <td>${Number(batch.quantity_allocated).toLocaleString("ru-RU")}</td>
        <td><strong>${Number(batch.quantity_remaining).toLocaleString("ru-RU")}</strong></td>
        <td>${batch.can_delete ? `<button class="button secondary delete-inventory-batch" type="button" data-batch-id="${Number(batch.id)}">Удалить</button>` : `<span class="muted">Есть списания</span>`}</td>
      </tr>
    `).join("");
    empty.classList.toggle("hidden", batches.length > 0);
  };

  const loadInventory = async () => {
    const token = localStorage.getItem(storageKey);
    if (!token || !Number.isInteger(productId) || productId <= 0) return;
    try {
      const response = await fetch(`/api/products/${productId}/inventory`, {
        headers: {Authorization: `Bearer ${token}`},
        cache: "no-store",
      });
      if (!response.ok) return;
      render(await response.json());
    } catch (_) {
      // The rest of the product card remains available during a transient inventory error.
    }
  };

  document.querySelector("#add-inventory-batch")?.addEventListener("click", () => {
    form.reset();
    result.textContent = "";
    document.querySelector("#inventory-received-at").value = localDateTimeValue();
    document.querySelector("#inventory-reconcile").checked = true;
    dialog.showModal();
  });
  document.querySelector("#close-inventory-dialog")?.addEventListener("click", () => dialog.close());
  document.querySelector("#cancel-inventory")?.addEventListener("click", () => dialog.close());

  body?.addEventListener("click", async (event) => {
    const button = event.target.closest(".delete-inventory-batch");
    if (!button) return;
    const batchId = Number(button.dataset.batchId);
    if (!Number.isInteger(batchId) || !confirm("Удалить эту неиспользованную партию?")) return;
    button.disabled = true;
    try {
      const token = localStorage.getItem(storageKey);
      const response = await fetch(`/api/products/${productId}/inventory/batches/${batchId}`, {
        method: "DELETE",
        headers: {Authorization: `Bearer ${token}`},
      });
      if (!response.ok) {
        let detail = `API вернул ошибку ${response.status}`;
        try { const payload = await response.json(); if (payload.detail) detail = String(payload.detail); } catch {}
        throw new Error(detail);
      }
      await loadInventory();
      document.querySelector("#refresh")?.click();
    } catch (error) {
      alert(error instanceof Error ? error.message : "Не удалось удалить партию.");
      button.disabled = false;
    }
  });

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = localStorage.getItem(storageKey);
    const save = document.querySelector("#save-inventory");
    save.disabled = true;
    result.textContent = "Сохраняю партию и выполняю FIFO-списание…";
    try {
      const receivedValue = document.querySelector("#inventory-received-at").value;
      const response = await fetch(`/api/products/${productId}/inventory/batches`, {
        method: "POST",
        headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"},
        body: JSON.stringify({
          quantity: Number(document.querySelector("#inventory-quantity").value),
          unit_cost: Number(document.querySelector("#inventory-unit-cost").value),
          received_at: receivedValue ? new Date(receivedValue).toISOString() : null,
          source_name: document.querySelector("#inventory-source-name").value.trim() || null,
          reference: document.querySelector("#inventory-reference").value.trim() || null,
          note: document.querySelector("#inventory-note").value.trim() || null,
          reconcile_existing_orders: document.querySelector("#inventory-reconcile").checked,
        }),
      });
      if (!response.ok) {
        let detail = `API вернул ошибку ${response.status}`;
        try { const payload = await response.json(); if (payload.detail) detail = String(payload.detail); } catch {}
        throw new Error(detail);
      }
      const data = await response.json();
      result.textContent = `Партия сохранена. На активные заказы списано: ${Number(data.allocated_to_existing_orders || 0)}. Остаток: ${Number(data.on_hand || 0)}.`;
      await loadInventory();
      document.querySelector("#refresh")?.click();
      setTimeout(() => dialog.close(), 1600);
    } catch (error) {
      result.textContent = error instanceof Error ? error.message : "Не удалось сохранить партию.";
    } finally {
      save.disabled = false;
    }
  });

  document.addEventListener("DOMContentLoaded", loadInventory);
  document.querySelector("#refresh")?.addEventListener("click", () => setTimeout(loadInventory, 250));
})();