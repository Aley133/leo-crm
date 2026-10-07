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
  const render = data => {
    items = new Map(data.items.map(item => [Number(item.id), item]));
    const archiveReady = (data.agent?.agents || []).some(agent => agent.online && /^\d+\.\d+\.\d+$/.test(agent.version || "") && agent.version.split(".").map(Number).reduce((value, part) => value * 1000 + part, 0) >= 1001025);
    document.querySelector("#agent-status").textContent = data.required_agent_version && data.agent?.online && !archiveReady ? "Обновите «Тест товара» до 1.1.25: задания восстановления дождутся нового агента." : data.agent?.online ? "Агент «Тест товара» онлайн." : "Агент «Тест товара» офлайн. Задание дождётся его подключения.";
    const labels = {preorder_inspecting:"Чтение карточки",adding_to_kaspi:"Подключение к Kaspi",preorder_connected:"Подключён",enrolled_fast_dumping:"Подключён",error:"Ошибка"};
    list.innerHTML = data.items.map(item => `<article class="fast-card"><div class="fast-card-head"><div><h3>${escape(item.name)}</h3><span>${escape(labels[item.status] || item.status)}</span></div>${item.product_id && ["preorder_connected", "enrolled_fast_dumping"].includes(item.status) ? `<div class="fast-card-actions"><button class="button secondary" type="button" data-edit-preorder="${Number(item.id)}">Редактировать</button><a class="button secondary" href="/crm/products/${Number(item.product_id)}">Товар</a></div>` : item.status === "error" ? `<button class="button secondary" type="button" data-edit-preorder="${Number(item.id)}">Редактировать</button>` : ""}</div><div class="fast-card-reason">${Number(item.test_price_kzt).toLocaleString("ru-RU")} ₸ · предзаказ ${Number(item.preorder_days)} дн. · ${Number(item.stock_count)} шт. · SKU ${escape(item.merchant_sku)} · Kaspi ${escape(item.kaspi_product_id)}${item.last_error ? `<p>${escape(item.last_error)}</p>` : ""}</div></article>`).join("") || '<p>Пока нет подключений.</p>';
  };
  list.addEventListener("click", event => {
    const editButton = event.target.closest("[data-edit-preorder]");
    if (!editButton) return;
    const item = items.get(Number(editButton.dataset.editPreorder));
    if (!item) return;
    editingId = Number(item.id);
    document.querySelector("#edit-name").textContent = item.name;
    document.querySelector("#edit-price").value = Number(item.test_price_kzt);
    document.querySelector("#edit-days").value = Number(item.preorder_days);
    document.querySelector("#edit-stock").value = Number(item.stock_count);
    editMessage.replaceChildren();
    editMessage.classList.add("hidden");
    editDialog.showModal();
  });
  editDialog.addEventListener("close", () => { editingId = null; });
  document.querySelector("#edit-cancel").addEventListener("click", () => editDialog.close());
  document.querySelector("#edit-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (editingId === null || editSave.disabled) return;
    const submittedId = editingId;
    editSave.disabled = true;
    editSave.textContent = "Отправляю…";
    editMessage.classList.add("hidden");
    try {
      await request(`/api/preorder/${submittedId}`, {method:"PATCH", body:JSON.stringify({price_kzt:Number(document.querySelector("#edit-price").value), preorder_days:Number(document.querySelector("#edit-days").value), stock_count:Number(document.querySelector("#edit-stock").value)})});
      if (editingId === submittedId) editDialog.close();
      message.textContent = "Изменения цены, срока и количества переданы агенту. Дождитесь подтверждения Kaspi.";
      await load();
    } catch(error) { if (editingId === submittedId) showFeedback(editMessage, error.message, error); }
    finally { editSave.disabled = false; editSave.textContent = "Сохранить"; }
  });
  const load = async () => {
    const authorized = Boolean(localStorage.getItem(tokenKey));
    document.querySelector("#auth-panel").classList.toggle("hidden", authorized);
    document.querySelector("#preorder-page").classList.toggle("hidden", !authorized);
    if (!authorized || loading) return;
    loading = true;
    try { render(await request("/api/preorder")); }
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
      await request("/api/preorder", {method:"POST", body:JSON.stringify({reference:document.querySelector("#reference").value.trim(), price_kzt:Number(document.querySelector("#price").value), preorder_days:Number(document.querySelector("#days").value), stock_count:Number(document.querySelector("#stock").value), city_id:document.querySelector("#city").value.trim(), zone_id:document.querySelector("#zone").value.trim()})});
      message.textContent = "Задание передано агенту «Тест товара». Здесь появится результат подключения.";
      showFeedback(connectMessage, message.textContent);
      await load();
    } catch(error) { message.textContent = error.message; showFeedback(connectMessage, error.message, error); }
    finally { button.disabled = false; button.textContent = "Подключить к карточке"; }
  });
  document.querySelector("#token-form").addEventListener("submit", async event => { event.preventDefault();localStorage.setItem(tokenKey, document.querySelector("#token").value.trim());document.querySelector("#token").value="";await load(); });
  document.querySelector("#refresh").addEventListener("click", load);
  document.addEventListener("visibilitychange", ()=>{if(!document.hidden)load();});
  window.addEventListener("leo:workspace-changed", () => { editDialog.close(); items.clear(); load(); });
  setInterval(()=>{if(!document.hidden)load();},10000);
  load();
})();
