"use strict";
(() => {
  const tokenKey = "leo_crm_service_token";
  const message = document.querySelector("#message");
  const form = document.querySelector("#preorder-form");
  const button = document.querySelector("#connect");
  const connectMessage = document.querySelector("#connect-message");
  const list = document.querySelector("#preorder-list");
  const editDialog = document.querySelector("#edit-dialog");
  const editMessage = document.querySelector("#edit-message");
  const editSave = document.querySelector("#edit-save");
  let items = new Map();
  let editingId = null;
  let loading = false;
  let inventoryData = null;
  let editingReference = null;
  let editingRow = null;
  let lastRefresh = 0;
  let loadVersion = 0;
  let catalogOffset = 0;
  let catalogVersion = 0;
  let catalogItems = new Map();
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const request = async (url, options={}) => {
    const response = await fetch(url, {cache:"no-store", ...options, headers:{Authorization:`Bearer ${localStorage.getItem(tokenKey) || ""}`, "Content-Type":"application/json"}});
    const data = await response.json().catch(()=>({}));
    if (response.status === 401) { localStorage.removeItem(tokenKey); throw Error("SERVICE_API_TOKEN не принят"); }
    if (!response.ok) {
      const error = Error(typeof data.detail === "string" ? data.detail : `Ошибка HTTP ${response.status}`);
      throw error;
    }
    return data;
  };
  const showFeedback = (target, text, error=null) => {
    target.replaceChildren(document.createTextNode(text));
    target.classList.remove("hidden");
    target.scrollIntoView({block:"nearest", behavior:"smooth"});
    target.focus({preventScroll:true});
  };
  const pricingPayload = (prefix="") => ({dump_enabled:document.querySelector(`#${prefix}dump`).checked,
    rocket_enabled:document.querySelector(`#${prefix}rocket`).checked,
    ozon_url:document.querySelector(`#${prefix}ozon`).value.trim() || null,
    minimum_profit_kzt:Number(document.querySelector(`#${prefix}profit`).value)});
  for (const prefix of ["", "edit-"]) {
    const sync = () => {
      const enabled = document.querySelector(`#${prefix}dump`).checked;
      document.querySelector(`#${prefix}ozon`).required = enabled;
      document.querySelector(`#${prefix}rocket`).disabled = !enabled;
      if (!enabled) document.querySelector(`#${prefix}rocket`).checked = false;
    };
    document.querySelector(`#${prefix}dump`).addEventListener("change", sync);
    sync();
  }
  const render = data => {
    inventoryData = data;
    lastRefresh = Date.now();
    items = new Map(data.items.map(item => [item.row_key, item]));
    const required = data.required_agent_version || "1.1.28";
    const versionNumber = value => /^\d+\.\d+\.\d+$/.test(value || "") ? value.split(".").map(Number).reduce((n, part) => n * 1000 + part, 0) : 0;
    const archiveReady = (data.agent?.agents || []).some(agent => agent.online && versionNumber(agent.version) >= versionNumber(required));
    document.querySelector("#agent-status").textContent = data.agent?.online && !archiveReady ? `Обновите «Тест товара» до ${required} для снятия с продажи. Задания сохраняются в очереди.` : data.agent?.online ? "Агент «Тест товара» онлайн." : "Агент «Тест товара» офлайн. Задание дождётся его подключения.";
    const groups = {all:"Все",active:"Активные",working:"В работе",attention:"Требуют внимания",stock:"Физический остаток",disabled:"Отключены / закончились"};
    const selected = document.querySelector("#preorder-filter").value;
    const query = document.querySelector("#connection-search").value.trim().toLocaleLowerCase("ru-RU");
    const rows = data.items.filter(item => (selected === "all" || item.group === selected) && (!query || [item.name,item.merchant_sku,item.kaspi_product_id].join(" ").toLocaleLowerCase("ru-RU").includes(query)));
    const labels = {preorder_inspecting:"Чтение карточки",adding_to_kaspi:"Подключение к Kaspi",stock_trading:"Обычная торговля",ready_to_resume:"Можно выставить снова",preorder_connected:"Предзаказ",enrolled_fast_dumping:"Предзаказ",error:"Ошибка",preorder_disabled:"Управление отключено",preorder_withdrawing:"Снимается с продажи",sale_removed:"Снят с продажи"};
    const when = value => { const date = value ? new Date(value) : null; return date && !Number.isNaN(date.valueOf()) ? date.toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : ""; };
    const operationText = item => {
      const operation = item.operation;
      if (!item.pending || !operation) return "";
      const minutes = Math.max(0, Math.floor((Date.now() - new Date(operation.created_at).valueOf()) / 60000));
      if (operation.status === "leased") return `${operation.job_type === "inspect" ? "Агент проверяет карточку" : "Агент выполняет задание Kaspi"} · ${minutes} мин.`;
      const online = (data.agent?.agents || []).filter(agent => agent.online);
      const needed = operation.job_type === "withdraw_preorder" ? "1.1.28" : "1.1.25";
      if (!online.length) return `Агент «Тест товара» офлайн · ожидание ${minutes} мин.`;
      if (!online.some(agent => versionNumber(agent.version) >= versionNumber(needed))) return `Нужен «Тест товара» ${needed} · ожидание ${minutes} мин.`;
      if (online.some(agent => agent.job_id)) return `Агент занят другим заданием · ожидание ${minutes} мин.`;
      return `В приоритетной очереди · ожидание ${minutes} мин.`;
    };
    const money = value => value == null ? "—" : `${Number(value).toLocaleString("ru-RU")} ₸`;
    document.querySelector("#preorder-summary").innerHTML = Object.entries(groups).map(([key,label]) => `<button type="button" data-filter="${key}" aria-pressed="${selected === key}"><span>${label}</span><strong>${Number(data.summary[key === "all" ? "total" : key] || 0)}</strong></button>`).join("");
    list.innerHTML = rows.length ? `<table class="preorder-table"><thead><tr><th scope="col">Товар</th><th scope="col">Цена</th><th scope="col">Предзаказ</th><th scope="col">Количество</th><th scope="col">Физ. остаток</th><th scope="col">Режим</th><th scope="col">Статус</th><th scope="col">Действия</th></tr></thead><tbody>${rows.map(item => `<tr class="preorder-row group-${escape(item.group)}"><td><div class="preorder-product">${item.image_url && /^https:\/\//.test(item.image_url) ? `<img src="${escape(item.image_url)}" alt="" loading="lazy" width="44" height="44">` : ""}<div><strong>${escape(item.name)}</strong><small>SKU ${escape(item.merchant_sku || "—")} · Kaspi ${escape(item.kaspi_product_id || "—")}</small><small>${escape(item.source_label)}${item.confirmed ? " · подтверждено" : " · требуется сверка"}${item.observed_at ? ` · ${escape(when(item.observed_at))}` : ""}</small></div></div></td><td class="preorder-number">${money(item.test_price_kzt)}</td><td class="preorder-number">${Number(item.preorder_days) > 0 ? `${Number(item.preorder_days)} дн.` : "—"}</td><td class="preorder-number">${item.stock_count == null ? "—" : `${Number(item.stock_count)} шт.`}</td><td class="preorder-number">${Number(item.physical_stock || 0)} шт.</td><td>${item.automation_mode === "rocket" ? "🚀 Автоматизация" : item.automation_enabled ? "Демпинг" : "Фиксированная цена"}</td><td><span class="preorder-badge">${escape(item.group === "working" ? "Ожидается подтверждение" : item.group === "attention" ? "Требует внимания" : labels[item.status] || groups[item.group])}</span>${item.pending ? `<small>${escape(operationText(item))}</small>` : ""}${item.last_error ? `<small class="preorder-error">${escape(item.last_error)}</small>` : ""}</td><td><div class="preorder-actions">${item.can_connect ? `<button class="button secondary" type="button" data-edit-preorder="${escape(item.row_key)}">${!item.id ? "Настроить" : ["ready_to_resume","sale_removed","preorder_disabled","error"].includes(item.status) ? "Выставить снова" : "Редактировать"}</button>` : ""}${item.id && !item.pending && item.status !== "preorder_disabled" ? `<button class="button secondary" type="button" data-action="disable" data-item="${Number(item.id)}">Отключить управление</button>` : ""}${item.id && item.product_id && !item.pending && item.status !== "sale_removed" ? `<button class="button secondary" type="button" data-action="withdraw" data-item="${Number(item.id)}">Снять с продажи</button>` : ""}${item.product_id ? `<a href="/crm/products/${Number(item.product_id)}">Открыть товар</a>` : ""}</div></td></tr>`).join("")}</tbody></table>` : '<p class="preorder-empty">Товары не найдены. Измените поиск или подключите карточку.</p>';
    document.querySelector("#connection-count").textContent = `Всего товаров: ${data.summary.total} · показано: ${rows.length}`;
    const warnings = data.warnings || [];
    document.querySelector("#inventory-warning").textContent = warnings.join(" ");
    document.querySelector("#inventory-warning").classList.toggle("hidden", !warnings.length);
  };
  list.addEventListener("click", async event => {
    const actionButton = event.target.closest("[data-action]");
    if (actionButton) {
      actionButton.disabled = true;
      try {
        const data = await request(`/api/preorder/${Number(actionButton.dataset.item)}/actions`, {method:"POST", body:JSON.stringify({action:actionButton.dataset.action})});
        message.textContent = data.job ? "Снятие отправлено агенту. Ожидается подтверждение Kaspi." : "Управление отключено. Товар остаётся в Kaspi до отдельного снятия с продажи.";
        await load(true);
      } catch (error) { message.textContent = error.message; }
      finally { actionButton.disabled = false; }
      return;
    }
    const editButton = event.target.closest("[data-edit-preorder]");
    if (!editButton) return;
    const item = items.get(editButton.dataset.editPreorder);
    if (!item) return;
    editingId = item.id ? Number(item.id) : null;
    editingReference = item.merchant_sku || item.kaspi_url || item.kaspi_product_id;
    editingRow = item;
    document.querySelector("#edit-name").textContent = item.name;
    document.querySelector("#edit-price").value = item.test_price_kzt == null ? "" : Number(item.test_price_kzt);
    document.querySelector("#edit-days").value = Number(item.preorder_days) > 0 ? Number(item.preorder_days) : "";
    document.querySelector("#edit-stock").value = item.stock_count == null ? "" : Number(item.stock_count);
    const pricing = item.offers || {};
    document.querySelector("#edit-dump").checked = Boolean(pricing.dump_enabled);
    document.querySelector("#edit-rocket").checked = Boolean(pricing.rocket_enabled);
    document.querySelector("#edit-ozon").value = pricing.ozon_url || "";
    document.querySelector("#edit-profit").value = Number(pricing.minimum_profit_kzt ?? 1000);
    document.querySelector("#edit-dump").dispatchEvent(new Event("change"));
    editMessage.replaceChildren();
    editMessage.classList.add("hidden");
    editDialog.showModal();
  });
  editDialog.addEventListener("close", () => { editingId = null; editingReference = null; editingRow = null; });
  document.querySelector("#edit-cancel").addEventListener("click", () => editDialog.close());
  document.querySelector("#edit-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!editingReference || editSave.disabled) return;
    const submittedId = editingId;
    editSave.disabled = true;
    editSave.textContent = "Отправляю…";
    editMessage.classList.add("hidden");
    try {
      const payload = {...pricingPayload("edit-"), price_kzt:Number(document.querySelector("#edit-price").value), preorder_days:Number(document.querySelector("#edit-days").value), stock_count:Number(document.querySelector("#edit-stock").value)};
      if (submittedId === null) {
        await request("/api/preorder", {method:"POST", body:JSON.stringify({...payload, reference:editingReference, city_id:editingRow.city_id || "196220100", zone_id:editingRow.zone_id || "Magnum_ZONE1"})});
      } else {
        await request(`/api/preorder/${submittedId}`, {method:"PATCH", body:JSON.stringify(payload)});
      }
      editDialog.close();
      message.textContent = "Изменения цены, срока и количества переданы агенту. Дождитесь подтверждения Kaspi.";
      await load(true);
    } catch(error) { if (editingId === submittedId) showFeedback(editMessage, error.message, error); }
    finally { editSave.disabled = false; editSave.textContent = "Сохранить"; }
  });
  const load = async (fresh=false) => {
    const authorized = Boolean(localStorage.getItem(tokenKey));
    document.querySelector("#auth-panel").classList.toggle("hidden", authorized);
    document.querySelector("#preorder-page").classList.toggle("hidden", !authorized);
    if (!authorized) return;
    const version = ++loadVersion;
    loading = true;
    try {
      const data = await request(`/api/preorder/offers${fresh === true ? "?fresh=true" : ""}`);
      if (version === loadVersion) render(data);
    }
    catch(error) { message.textContent = error.message; if (!localStorage.getItem(tokenKey)) { document.querySelector("#auth-panel").classList.remove("hidden"); document.querySelector("#preorder-page").classList.add("hidden"); } }
    finally { loading = false; }
  };
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = "Отправляю…";
    connectMessage.classList.add("hidden");
    try {
      await request("/api/preorder", {method:"POST", body:JSON.stringify({...pricingPayload(), reference:document.querySelector("#reference").value.trim(), price_kzt:Number(document.querySelector("#price").value), preorder_days:Number(document.querySelector("#days").value), stock_count:Number(document.querySelector("#stock").value), city_id:document.querySelector("#city").value.trim(), zone_id:document.querySelector("#zone").value.trim()})});
      message.textContent = "Задание передано агенту «Тест товара». Здесь появится результат подключения.";
      showFeedback(connectMessage, message.textContent);
      await load(true);
    } catch(error) { message.textContent = error.message; showFeedback(connectMessage, error.message, error); }
    finally { button.disabled = false; button.textContent = "Подключить к карточке"; }
  });
  document.querySelector("#token-form").addEventListener("submit", async event => { event.preventDefault();localStorage.setItem(tokenKey, document.querySelector("#token").value.trim());document.querySelector("#token").value="";await load(true);await searchCatalog(); });
  document.querySelector("#refresh").addEventListener("click", load);
  document.addEventListener("visibilitychange", ()=>{if(!document.hidden)load();});
  window.addEventListener("leo:workspace-changed", () => {
    editDialog.close(); items.clear(); inventoryData = null; catalogItems.clear(); catalogOffset = 0;
    document.querySelector("#catalog-results").replaceChildren(); ++catalogVersion;
    load(); searchCatalog();
  });
  const debounce = callback => { let timer; return () => { clearTimeout(timer); timer = setTimeout(callback, 250); }; };
  const renderCached = () => { if (inventoryData) render(inventoryData); };
  document.querySelector("#connection-search").addEventListener("input", debounce(renderCached));
  document.querySelector("#preorder-filter").addEventListener("change", renderCached);
  document.querySelector("#preorder-summary").addEventListener("click", event => {
    const selected = event.target.closest("[data-filter]");
    if (selected) { document.querySelector("#preorder-filter").value = selected.dataset.filter; renderCached(); }
  });
  document.querySelector("#open-connect").addEventListener("click", () => {
    const panel = document.querySelector("#connect-panel"); panel.open = true;
    panel.scrollIntoView({block:"start", behavior:"smooth"});
  });
  document.querySelector("#connect-panel").addEventListener("toggle", () => {
    if (document.querySelector("#connect-panel").open && !catalogItems.size) searchCatalog();
  });
  const searchCatalog = async (append=false) => {
    if (!localStorage.getItem(tokenKey)) return;
    const version = ++catalogVersion;
    if (!append) { catalogOffset = 0; catalogItems.clear(); }
    try {
      const data = await request(`/api/preorder/catalog?q=${encodeURIComponent(document.querySelector("#catalog-search").value.trim())}&offset=${catalogOffset}&limit=30`);
      if (version !== catalogVersion) return;
      data.items.forEach(item => catalogItems.set(Number(item.product_id), item));
      document.querySelector("#catalog-results").innerHTML = [...catalogItems.values()].map(item => `<article class="fast-card"><h3>${escape(item.name)}</h3><p>SKU ${escape(item.merchant_sku)} · Kaspi ${escape(item.kaspi_product_id)} · остаток ${Number(item.physical_stock)} · ${item.sale_enabled ? "В продаже" : "Снят с продажи"}</p><button class="button secondary" type="button" data-catalog-product="${Number(item.product_id)}" ${item.physical_stock > 0 ? "disabled" : ""}>${item.physical_stock > 0 ? "Есть физический остаток" : "Выбрать для предзаказа"}</button></article>`).join("") || "<p>В каталоге не найдено. Подключите новую карточку по ссылке или ID ниже.</p>";
      catalogOffset = catalogItems.size;
      document.querySelector("#catalog-more").classList.toggle("hidden", catalogOffset >= data.total);
    } catch(error) { if (version === catalogVersion) message.textContent = error.message; }
  };
  document.querySelector("#catalog-search").addEventListener("input", debounce(() => searchCatalog()));
  document.querySelector("#catalog-more").addEventListener("click", () => searchCatalog(true));
  document.querySelector("#catalog-results").addEventListener("click", event => {
    const selected = event.target.closest("[data-catalog-product]");
    const item = selected && catalogItems.get(Number(selected.dataset.catalogProduct));
    if (!item) return;
    document.querySelector("#reference").value = item.merchant_sku || item.kaspi_product_id;
    form.scrollIntoView({block:"start", behavior:"smooth"});
    document.querySelector("#price").focus({preventScroll:true});
  });
  setInterval(()=>{
    const interval = inventoryData?.items.some(item => item.pending) ? 5000 : 15000;
    if (!document.hidden && !loading && Date.now() - lastRefresh >= interval) load();
  },1000);
  load();
})();
