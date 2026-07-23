(() => {
  const storageKey = "leo_crm_service_token";
  const productId = Number(location.pathname.split("/").filter(Boolean).at(-1));
  const dialog = document.querySelector("#inventory-dialog");
  const form = document.querySelector("#inventory-form");
  const result = document.querySelector("#inventory-result");
  const body = document.querySelector("#inventory-batches-body");
  const empty = document.querySelector("#inventory-empty");
  const dialogTitle = dialog?.querySelector("h2");
  let editingBatchId = null;
  let batchesById = new Map();

  const money = (value) => value == null ? "—" : `${Number(value).toLocaleString("ru-RU", {maximumFractionDigits: 2})} KZT`;
  const dateTime = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
  const localDateTimeValue = (value = new Date()) => {
    const date = value instanceof Date ? value : new Date(value);
    const offset = date.getTimezoneOffset();
    return new Date(date.getTime() - offset * 60000).toISOString().slice(0, 16);
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
    batchesById = new Map(batches.map((batch) => [Number(batch.id), batch]));
    body.innerHTML = batches.map((batch) => `
      <tr>
        <td>${dateTime(batch.received_at)}${batch.reference ? `<span class="muted">${escapeHtml(batch.reference)}</span>` : ""}</td>
        <td>${escapeHtml(batch.source_name || "Не указан")}${batch.note ? `<span class="muted">${escapeHtml(batch.note)}</span>` : ""}</td>
        <td>${money(batch.unit_cost)}</td>
        <td>${Number(batch.quantity_received).toLocaleString("ru-RU")}</td>
        <td>${Number(batch.quantity_allocated).toLocaleString("ru-RU")}</td>
        <td><strong>${Number(batch.quantity_remaining).toLocaleString("ru-RU")}</strong></td>
        <td>
          <div class="batch-actions">
            <button class="button secondary edit-inventory-batch" type="button" data-batch-id="${Number(batch.id)}">Редактировать</button>
            <button class="button secondary delete-inventory-batch" type="button" data-batch-id="${Number(batch.id)}">Удалить</button>
          </div>
        </td>
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

  const openCreateDialog = () => {
    editingBatchId = null;
    form.reset();
    result.textContent = "";
    if (dialogTitle) dialogTitle.textContent = "Добавить партию товара";
    document.querySelector("#save-inventory").textContent = "Сохранить партию";
    document.querySelector("#inventory-received-at").value = localDateTimeValue();
    document.querySelector("#inventory-reconcile").checked = true;
    document.querySelector("#inventory-reconcile").closest("label")?.classList.remove("hidden");
    dialog.showModal();
  };

  const openEditDialog = (batch) => {
    editingBatchId = Number(batch.id);
    form.reset();
    result.textContent = "После сохранения FIFO и прибыль связанных заказов будут пересчитаны.";
    if (dialogTitle) dialogTitle.textContent = "Редактировать партию товара";
    document.querySelector("#save-inventory").textContent = "Сохранить изменения";
    document.querySelector("#inventory-quantity").value = Number(batch.quantity_received);
    document.querySelector("#inventory-unit-cost").value = Number(batch.unit_cost);
    document.querySelector("#inventory-received-at").value = localDateTimeValue(batch.received_at);
    document.querySelector("#inventory-source-name").value = batch.source_name || "";
    document.querySelector("#inventory-reference").value = batch.reference || "";
    document.querySelector("#inventory-note").value = batch.note || "";
    document.querySelector("#inventory-reconcile").closest("label")?.classList.add("hidden");
    dialog.showModal();
  };

  document.querySelector("#add-inventory-batch")?.addEventListener("click", openCreateDialog);
  document.querySelector("#close-inventory-dialog")?.addEventListener("click", () => dialog.close());
  document.querySelector("#cancel-inventory")?.addEventListener("click", () => dialog.close());

  body?.addEventListener("click", async (event) => {
    const editButton = event.target.closest(".edit-inventory-batch");
    if (editButton) {
      const batch = batchesById.get(Number(editButton.dataset.batchId));
      if (batch) openEditDialog(batch);
      return;
    }

    const deleteButton = event.target.closest(".delete-inventory-batch");
    if (!deleteButton) return;
    const batchId = Number(deleteButton.dataset.batchId);
    if (!Number.isInteger(batchId)) return;
    const confirmed = confirm("Удалить эту партию? Все FIFO-списания товара будут пересобраны по оставшимся партиям, а прибыль заказов пересчитана.");
    if (!confirmed) return;
    deleteButton.disabled = true;
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
      deleteButton.disabled = false;
    }
  });

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = localStorage.getItem(storageKey);
    const save = document.querySelector("#save-inventory");
    save.disabled = true;
    result.textContent = editingBatchId ? "Сохраняю изменения и полностью пересобираю FIFO…" : "Сохраняю партию и выполняю FIFO-списание…";
    try {
      const receivedValue = document.querySelector("#inventory-received-at").value;
      const payload = {
        quantity: Number(document.querySelector("#inventory-quantity").value),
        unit_cost: Number(document.querySelector("#inventory-unit-cost").value),
        received_at: receivedValue ? new Date(receivedValue).toISOString() : null,
        source_name: document.querySelector("#inventory-source-name").value.trim() || null,
        reference: document.querySelector("#inventory-reference").value.trim() || null,
        note: document.querySelector("#inventory-note").value.trim() || null,
      };
      if (!editingBatchId) payload.reconcile_existing_orders = document.querySelector("#inventory-reconcile").checked;

      const url = editingBatchId
        ? `/api/products/${productId}/inventory/batches/${editingBatchId}`
        : `/api/products/${productId}/inventory/batches`;
      const response = await fetch(url, {
        method: editingBatchId ? "PATCH" : "POST",
        headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        let detail = `API вернул ошибку ${response.status}`;
        try { const responsePayload = await response.json(); if (responsePayload.detail) detail = String(responsePayload.detail); } catch {}
        throw new Error(detail);
      }
      const data = await response.json();
      result.textContent = editingBatchId
        ? `Партия обновлена. FIFO-единиц распределено заново: ${Number(data.reallocated_quantity || 0)}. Остаток: ${Number(data.on_hand || 0)}.`
        : `Партия сохранена. На активные заказы списано: ${Number(data.allocated_to_existing_orders || 0)}. Остаток: ${Number(data.on_hand || 0)}.`;
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
