(() => {
  const storageKey = "leo_crm_service_token";
  const productId = Number(location.pathname.split("/").filter(Boolean).at(-1));
  const dialog = document.querySelector("#inventory-dialog");
  const form = document.querySelector("#inventory-form");
  const result = document.querySelector("#inventory-result");
  const body = document.querySelector("#inventory-batches-body");
  const empty = document.querySelector("#inventory-empty");
  const dialogTitle = dialog?.querySelector("h2");
  const batchTypeInput = document.querySelector("#inventory-batch-type");
  const receivedCheck = document.querySelector("#inventory-is-received");
  const reconcileCheck = document.querySelector("#inventory-reconcile");
  const ownerDialog = document.querySelector("#inventory-owner-dialog");
  const ownerForm = document.querySelector("#inventory-owner-form");
  const ownerSearch = document.querySelector("#inventory-owner-search");
  const ownerSelect = document.querySelector("#inventory-owner-product");
  const ownerResult = document.querySelector("#inventory-owner-result");
  let editingBatchId = null;
  let batchesById = new Map();
  let ownerSearchTimer = null;

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
    if (summary) {
      const production = Number(inventory.production_remaining_total || 0);
      const expectedPurchase = Math.max(Number(inventory.expected_total || 0) - production, 0);
      summary.textContent = `на складе: ${Number(inventory.on_hand || 0)} · ожидается закупка: ${expectedPurchase} · в производстве: ${production} · списано/изготовлено: ${Number(inventory.allocated_total || 0)}`;
    }
    const ownerName = document.querySelector("#inventory-owner-name");
    const groupSummary = document.querySelector("#inventory-group-summary");
    const mergeButton = document.querySelector("#merge-inventory");
    const members = inventory.shared_products || [];
    if (ownerName) ownerName.textContent = members.length > 1 ? inventory.inventory_owner_name : "Отдельный остаток";
    if (groupSummary) {
      groupSummary.textContent = members.length > 1
        ? `${members.length} Kaspi-карточки используют одни партии: ${members.map((item) => item.merchant_sku || item.kaspi_product_id).join(", ")}`
        : "Только эта Kaspi-карточка";
    }
    if (mergeButton) mergeButton.textContent = members.length > 1 ? "Добавить ещё карточку" : "Объединить с другой карточкой";
  };

  const renderProductionOrders = (batch) => {
    const orders = batch.production_orders || [];
    const content = orders.length
      ? orders.map((order) => {
          const code = order.external_code || order.order_id;
          return `<div class="production-order">
            <div><strong>Заказ №${escapeHtml(code)}</strong><span>Создан ${dateTime(order.ordered_at)}</span></div>
            <div><strong>${Number(order.reserved_quantity || 0).toLocaleString("ru-RU")} шт.</strong><span>закреплено из ${Number(order.order_quantity || 0).toLocaleString("ru-RU")} шт. в заказе</span></div>
            <div><strong>Предзаказ</strong><span>ожидает изготовления</span></div>
            <button class="button manufacture-order" type="button" data-batch-id="${Number(batch.id)}" data-line-id="${Number(order.order_line_id)}" data-order-code="${escapeHtml(code)}">Изготовлено</button>
          </div>`;
        }).join("")
      : '<div class="production-order-empty">Активных заказов для изготовления сейчас нет.</div>';
    return `<tr class="production-orders-row"><td colspan="7"><div class="production-orders">
      <div class="production-orders-title"><strong>Активные заказы этой партии</strong><span>В «Упаковку» заказ перейдёт только после подтверждения изготовления</span></div>
      ${content}
    </div></td></tr>`;
  };

  const render = (inventory) => {
    setSummary(inventory);
    const batches = inventory.batches || [];
    batchesById = new Map(batches.map((batch) => [Number(batch.id), batch]));
    body.innerHTML = batches.map((batch) => {
      const received = batch.is_received === true;
      const production = batch.batch_type === "production";
      const status = production
        ? '<span class="badge warn">В производстве</span>'
        : received
          ? '<span class="badge ok">Прибыло</span>'
          : '<span class="badge warn">Ожидается</span>';
      const receiveButton = batch.can_receive ? `<button class="button receive-inventory-batch" type="button" data-batch-id="${Number(batch.id)}">Отметить прибытие</button>` : "";
      const editButton = batch.can_edit ? `<button class="button secondary edit-inventory-batch" type="button" data-batch-id="${Number(batch.id)}">Редактировать</button>` : "";
      const deleteButton = batch.can_delete ? `<button class="button secondary delete-inventory-batch" type="button" data-batch-id="${Number(batch.id)}">Удалить</button>` : "";
      const lockedLabel = !batch.can_edit && production ? '<span class="muted">Есть изготовленные заказы</span>' : "";
      const row = `
      <tr>
        <td>${status}${dateTime(batch.received_at)}${batch.reference ? `<span class="muted">${escapeHtml(batch.reference)}</span>` : ""}</td>
        <td>${escapeHtml(batch.source_name || "Не указан")}${batch.note ? `<span class="muted">${escapeHtml(batch.note)}</span>` : ""}</td>
        <td>${money(batch.unit_cost)}</td>
        <td>${production || received ? Number(batch.quantity_received).toLocaleString("ru-RU") : "—"}</td>
        <td>${production || received ? Number(batch.quantity_allocated).toLocaleString("ru-RU") : "—"}</td>
        <td><strong>${production || received ? Number(batch.quantity_remaining).toLocaleString("ru-RU") : "0"}</strong></td>
        <td>
          <div class="batch-actions">
            ${receiveButton}
            ${editButton}
            ${deleteButton}
            ${lockedLabel}
          </div>
        </td>
      </tr>`;
      return production ? row + renderProductionOrders(batch) : row;
    }).join("");
    empty.classList.toggle("hidden", batches.length > 0);
  };

  const syncBatchTypeControls = () => {
    const production = batchTypeInput?.value === "production";
    receivedCheck?.closest("label")?.classList.toggle("hidden", production || Boolean(editingBatchId));
    reconcileCheck?.closest("label")?.classList.toggle("hidden", production || Boolean(editingBatchId));
    const dateLabel = document.querySelector("#inventory-date-label");
    if (dateLabel) dateLabel.textContent = production ? "Плановая дата производства" : "Ожидаемая / фактическая дата поступления";
    if (production) {
      receivedCheck.checked = false;
      reconcileCheck.checked = false;
      const source = document.querySelector("#inventory-source-name");
      if (!source.value.trim()) source.value = "Производство";
    }
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
    result.textContent = "Выберите «Производство», если каждый заказ должен подтверждаться кнопкой «Изготовлено».";
    if (dialogTitle) dialogTitle.textContent = "Добавить партию товара";
    document.querySelector("#save-inventory").textContent = "Сохранить партию";
    document.querySelector("#inventory-received-at").value = localDateTimeValue();
    batchTypeInput.value = "purchase";
    receivedCheck.checked = false;
    reconcileCheck.checked = true;
    syncBatchTypeControls();
    dialog.showModal();
  };

  const openEditDialog = (batch) => {
    editingBatchId = Number(batch.id);
    form.reset();
    result.textContent = batch.batch_type === "production"
      ? "Производственная партия не является готовым складским остатком."
      : batch.is_received
        ? "После сохранения FIFO и прибыль связанных заказов будут пересчитаны."
        : "Ожидаемая партия останется недоступной до подтверждения прибытия.";
    if (dialogTitle) dialogTitle.textContent = "Редактировать партию товара";
    document.querySelector("#save-inventory").textContent = "Сохранить изменения";
    batchTypeInput.value = batch.batch_type || "purchase";
    document.querySelector("#inventory-quantity").value = Number(batch.quantity_received);
    document.querySelector("#inventory-unit-cost").value = Number(batch.unit_cost);
    document.querySelector("#inventory-received-at").value = localDateTimeValue(batch.received_at);
    document.querySelector("#inventory-source-name").value = batch.source_name || "";
    document.querySelector("#inventory-reference").value = batch.reference || "";
    document.querySelector("#inventory-note").value = batch.note || "";
    syncBatchTypeControls();
    dialog.showModal();
  };

  document.querySelector("#add-inventory-batch")?.addEventListener("click", openCreateDialog);
  document.querySelector("#close-inventory-dialog")?.addEventListener("click", () => dialog.close());
  document.querySelector("#cancel-inventory")?.addEventListener("click", () => dialog.close());
  batchTypeInput?.addEventListener("change", () => {
    syncBatchTypeControls();
    result.textContent = batchTypeInput.value === "production"
      ? "Количество — это план производства. Заказы останутся в «Предзаказе», пока вы не нажмёте «Изготовлено» у каждого."
      : "Ожидаемая закупка не попадёт в остаток и FIFO, пока вы не отметите её прибытие.";
  });

  const searchInventoryOwners = async () => {
    const query = ownerSearch?.value.trim() || "";
    if (query.length < 2) {
      ownerSelect.innerHTML = '<option value="">Введите минимум 2 символа</option>';
      return;
    }
    ownerResult.textContent = "Ищу карточки…";
    try {
      const token = localStorage.getItem(storageKey);
      const response = await fetch(`/api/product-registry/products?q=${encodeURIComponent(query)}&limit=50`, {headers:{Authorization:`Bearer ${token}`},cache:"no-store"});
      if (!response.ok) throw new Error(`API вернул ошибку ${response.status}`);
      const rows = (await response.json()).filter((row) => Number(row.product_id) !== productId);
      ownerSelect.innerHTML = rows.length
        ? '<option value="">Выберите карточку</option>' + rows.map((row) => `<option value="${Number(row.product_id)}">${escapeHtml(row.name)} · SKU ${escapeHtml(row.merchant_sku || row.kaspi_product_id)}</option>`).join("")
        : '<option value="">Совпадений не найдено</option>';
      ownerResult.textContent = rows.length ? `Найдено: ${rows.length}` : "Подходящих карточек нет.";
    } catch (error) {
      ownerResult.textContent = error instanceof Error ? error.message : "Не удалось найти карточки.";
    }
  };

  document.querySelector("#merge-inventory")?.addEventListener("click", () => {
    ownerForm.reset();
    ownerSelect.innerHTML = '<option value="">Сначала выполните поиск</option>';
    ownerResult.textContent = "";
    ownerDialog.showModal();
    ownerSearch.focus();
  });
  document.querySelector("#close-inventory-owner-dialog")?.addEventListener("click", () => ownerDialog.close());
  document.querySelector("#cancel-inventory-owner")?.addEventListener("click", () => ownerDialog.close());
  ownerSearch?.addEventListener("input", () => {
    clearTimeout(ownerSearchTimer);
    ownerSearchTimer = setTimeout(searchInventoryOwners, 350);
  });
  ownerForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ownerProductId = Number(ownerSelect.value);
    if (!Number.isInteger(ownerProductId) || ownerProductId <= 0) return;
    if (!confirm("Объединить партии и ожидаемые поставки этих карточек в один FIFO-остаток? Продажи с обоих SKU будут списываться из общего склада.")) return;
    const save = document.querySelector("#save-inventory-owner");
    save.disabled = true;
    ownerResult.textContent = "Объединяю партии и пересчитываю активные заказы…";
    try {
      const token = localStorage.getItem(storageKey);
      const response = await fetch(`/api/products/${productId}/inventory-owner`, {method:"PUT",headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/json"},body:JSON.stringify({owner_product_id:ownerProductId})});
      if (!response.ok) {
        let detail = `API вернул ошибку ${response.status}`;
        try { const payload = await response.json(); if (payload.detail) detail = String(payload.detail); } catch {}
        throw new Error(detail);
      }
      const inventory = await response.json();
      render(inventory);
      ownerResult.textContent = "Готово. Обе Kaspi-карточки теперь используют один FIFO-склад.";
      const pageMessage = document.querySelector("#message");
      if (pageMessage) pageMessage.textContent = "Общий склад создан: партии, активные заказы и XML пересчитаны.";
      document.querySelector("#refresh")?.click();
      setTimeout(() => ownerDialog.close(), 1600);
    } catch (error) {
      ownerResult.textContent = error instanceof Error ? error.message : "Не удалось объединить склад.";
    } finally {
      save.disabled = false;
    }
  });

  body?.addEventListener("click", async (event) => {
    const manufactureButton = event.target.closest(".manufacture-order");
    if (manufactureButton) {
      const batchId = Number(manufactureButton.dataset.batchId);
      const lineId = Number(manufactureButton.dataset.lineId);
      const orderCode = manufactureButton.dataset.orderCode || lineId;
      if (!confirm(`Подтвердить изготовление товара для заказа №${orderCode}? После этого заказ перейдёт в «Упаковку», если остальные его позиции тоже готовы.`)) return;
      manufactureButton.disabled = true;
      manufactureButton.textContent = "Сохраняю…";
      try {
        const token = localStorage.getItem(storageKey);
        const response = await fetch(`/api/products/${productId}/inventory/batches/${batchId}/orders/${lineId}/manufacture`, {
          method: "POST",
          headers: {Authorization: `Bearer ${token}`},
        });
        if (!response.ok) {
          let detail = `API вернул ошибку ${response.status}`;
          try { const payload = await response.json(); if (payload.detail) detail = String(payload.detail); } catch {}
          throw new Error(detail);
        }
        const data = await response.json();
        const pageMessage = document.querySelector("#message");
        if (pageMessage) {
          pageMessage.textContent = data.order_line_fully_allocated
            ? `Заказ №${orderCode}: изготовлено ${Number(data.completed_quantity || 0).toLocaleString("ru-RU")} шт. Товар готов к упаковке.`
            : `Заказ №${orderCode}: изготовлено ${Number(data.completed_quantity || 0).toLocaleString("ru-RU")} шт. Остальная часть заказа ещё ожидается.`;
        }
        await loadInventory();
        document.querySelector("#refresh")?.click();
      } catch (error) {
        alert(error instanceof Error ? error.message : "Не удалось подтвердить изготовление.");
        manufactureButton.disabled = false;
        manufactureButton.textContent = "Изготовлено";
      }
      return;
    }

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
        const data = await response.json();
        const pageMessage = document.querySelector("#message");
        if (pageMessage) {
          pageMessage.textContent = `Партия принята. FIFO списал ${Number(data.reallocated_quantity || 0).toLocaleString("ru-RU")} ед. на самые ранние активные заказы. Остаток на складе: ${Number(data.on_hand || 0).toLocaleString("ru-RU")} ед.`;
        }
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
    const batchType = batchTypeInput.value;
    const production = batchType === "production";
    const physicallyReceived = !production && receivedCheck.checked;
    result.textContent = editingBatchId
      ? "Сохраняю изменения…"
      : production
        ? "Создаю производственную очередь…"
        : physicallyReceived
          ? "Принимаю партию и выполняю FIFO-списание…"
          : "Сохраняю ожидаемую партию…";
    try {
      const receivedValue = document.querySelector("#inventory-received-at").value;
      const payload = {
        quantity: Number(document.querySelector("#inventory-quantity").value),
        unit_cost: Number(document.querySelector("#inventory-unit-cost").value),
        received_at: receivedValue ? new Date(receivedValue).toISOString() : null,
        source_name: document.querySelector("#inventory-source-name").value.trim() || null,
        reference: document.querySelector("#inventory-reference").value.trim() || null,
        note: document.querySelector("#inventory-note").value.trim() || null,
        batch_type: batchType,
      };
      if (!editingBatchId) {
        payload.is_received = physicallyReceived;
        payload.reconcile_existing_orders = !production && reconcileCheck.checked;
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
        : production
          ? "Производственная партия сохранена. Активные заказы закреплены по дате и ждут кнопки «Изготовлено»."
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
