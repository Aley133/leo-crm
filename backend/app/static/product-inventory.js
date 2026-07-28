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
    if (summary) summary.textContent = `на складе: ${Number(inventory.on_hand || 0)} · ожидается: ${Number(inventory.expected_total || 0)} · списано: ${Number(inventory.allocated_total || 0)}`;
  };

  const render = (inventory) => {
    setSummary(inventory);
    const batches = inventory.batches || [];
    batchesById = new Map(batches.map((batch) => [Number(batch.id), batch]));
    body.innerHTML = batches.map((batch) => {
      const received = batch.is_received === true;
      const status = received ? '<span class="badge ok">Прибыло</span>' : '<span class="badge warn">Ожидается</span>';
      const receiveButton = received ? "" : `<button class="button receive-inventory-batch" type="button" data-batch-id="${Number(batch.id)}">Отметить прибытие</button>`;
      return `
      <tr>
        <td>${status}${dateTime(batch.received_at)}${batch.reference ? `<span class="muted">${escapeHtml(batch.reference)}</span>` : ""}</td>
        <td>${escapeHtml(batch.source_name || "Не указан")}${batch.note ? `<span class="muted">${escapeHtml(batch.note)}</span>` : ""}</td>
        <td>${money(batch.unit_cost)}</td>
        <td>${received ? Number(batch.quantity_received).toLocaleString("ru-RU") : "—"}</td>
        <td>${received ? Number(batch.quantity_allocated).toLocaleString("ru-RU") : "—"}</td>
        <td><strong>${received ? Number(batch.quantity_remaining).toLocaleString("ru-RU") : "0"}</strong></td>
        <td>
          <div class="batch-actions">
            ${receiveButton}
            <button class="button secondary edit-inventory-batch" type="button" data-batch-id="${Number(batch.id)}">Редактировать</button>
            <button class="button secondary delete-inventory-batch" type="button" data-batch-id="${Number(batch.id)}">Удалить</button>
          </div>
        </td>
      </tr>`;
    }).join("");
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
    result.textContent = "Ожидаемая партия не попадёт в остаток и FIFO, пока вы не отметите её прибытие.";
    if (dialogTitle) dialogTitle.textContent = "Добавить партию товара";
    document.querySelector("#save-inventory").textContent = "Сохранить партию";
    document.querySelector("#inventory-received-at").value = localDateTimeValue();
    document.querySelector("#inventory-is-received").checked = false;
    document.querySelector("#inventory-is-received").closest("label")?.classList.remove("hidden");
    document.querySelector("#inventory-reconcile").checked = true;
    document.querySelector("#inventory-reconcile").closest("label")?.classList.remove("hidden");
    dialog.showModal();
  };

  const openEditDialog = (batch) => {
    editingBatchId = Number(batch.id);
    form.reset();
    result.textContent = batch.is_received ? "После сохранения FIFO и прибыль связанных заказов будут пересчитаны." : "Ожидаемая партия останется недоступной до подтверждения прибытия.";
    if (dialogTitle) dialogTitle.textContent = "Редактировать партию товара";
    document.querySelector("#save-inventory").textContent = "Сохранить изменения";
    document.querySelector("#inventory-quantity").value = Number(batch.quantity_received);
    document.querySelector("#inventory-unit-cost").value = Number(batch.unit_cost);
    document.querySelector("#inventory-received-at").value = localDateTimeValue(batch.received_at);
    document.querySelector("#inventory-source-name").value = batch.source_name || "";
    document.querySelector("#inventory-reference").value = batch.reference || "";
    document.querySelector("#inventory-note").value = batch.note || "";
    document.querySelector("#inventory-is-received").closest("label")?.classList.add("hidden");
    document.querySelector("#inventory-reconcile").closest("label")?.classList.add("hidden");
    dialog.showModal();
  };

  document.querySelector("#add-inventory-batch")?.addEventListener("click", openCreateDialog);
  document.querySelector("#close-inventory-dialog")?.addEventListener("click", () => dialog.close());
  document.querySelector("#cancel-inventory")?.addEventListener("click", () => dialog.close());

  body?.addEventListener("click", async (event) => {
    const receiveButton = event.target.closest(".receive-inventory-batch");
    if (receiveButton) {
      const batchId = Number(receiveButton.dataset.batchId);
      if (!confirm("Подтвердить, что товар физически прибыл? После этого FIFO распределит его на активные заказы.")) return;
      receiveButton.disabled = true;
      try {
        const token = localStorage.getItem(storageKey);
        const response = await fetch(`/api/products/${productId}/inventory/batches/${batchId}/receive`, {
          method: "POST",
          headers: {Authorization: `Bearer ${token}`},
        });
        if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
        await loadInventory();
        document.querySelector("#refresh")?.click();
      } catch (error) {
        alert(error instanceof Error ? error.message : "Не удалось подтвердить прибытие.");
        receiveButton.disabled = false;
      }
      return;
    }

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
      if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
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
    const physicallyReceived = document.querySelector("#inventory-is-received").checked;
    result.textContent = editingBatchId ? "Сохраняю изменения…" : physicallyReceived ? "Принимаю партию и выполняю FIFO-списание…" : "Сохраняю ожидаемую партию…";
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
      if (!editingBatchId) {
        payload.is_received = physicallyReceived;
        payload.reconcile_existing_orders = document.querySelector("#inventory-reconcile").checked;
      }

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
        ? `Партия обновлена. FIFO пересчитан: ${Number(data.reallocated_quantity || 0)} ед.`
        : physicallyReceived
          ? `Партия принята. На активные заказы списано: ${Number(data.allocated_to_existing_orders || 0)}.`
          : "Ожидаемая партия сохранена. Заказы останутся в предзаказах до подтверждения прибытия.";
      await loadInventory();
      document.querySelector("#refresh")?.click();
      setTimeout(() => dialog.close(), 1800);
    } catch (error) {
      result.textContent = error instanceof Error ? error.message : "Не удалось сохранить партию.";
    } finally {
      save.disabled = false;
    }
  });

  document.addEventListener("DOMContentLoaded", loadInventory);
  document.querySelector("#refresh")?.addEventListener("click", () => setTimeout(loadInventory, 250));
})();
