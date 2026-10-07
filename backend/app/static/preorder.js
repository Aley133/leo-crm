"use strict";
(() => {
  const tokenKey = "leo_crm_service_token";
  const message = document.querySelector("#message");
  const form = document.querySelector("#preorder-form");
  const button = document.querySelector("#connect");
  const list = document.querySelector("#preorder-list");
  let loading = false;
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const request = async (url, options={}) => {
    const response = await fetch(url, {cache:"no-store", ...options, headers:{Authorization:`Bearer ${localStorage.getItem(tokenKey) || ""}`, "Content-Type":"application/json"}});
    const data = await response.json().catch(()=>({}));
    if (response.status === 401) { localStorage.removeItem(tokenKey); throw Error("SERVICE_API_TOKEN не принят"); }
    if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : `Ошибка HTTP ${response.status}`);
    return data;
  };
  const render = data => {
    document.querySelector("#agent-status").textContent = data.agent?.online ? "Агент «Тест товара» онлайн." : "Агент «Тест товара» офлайн. Задание дождётся его подключения.";
    const labels = {preorder_inspecting:"Чтение карточки",adding_to_kaspi:"Подключение к Kaspi",enrolled_fast_dumping:"Подключён",error:"Ошибка"};
    list.innerHTML = data.items.map(item => `<article class="fast-card"><div class="fast-card-head"><div><h3>${escape(item.name)}</h3><span>${escape(labels[item.status] || item.status)}</span></div>${item.product_id && item.status === "enrolled_fast_dumping" ? `<div class="fast-card-actions"><a class="button secondary" href="/crm/products/${Number(item.product_id)}">Товар</a><a class="button secondary" href="/crm/fast-dumping">Fast Dumping</a></div>` : ""}</div><div class="fast-card-reason">${Number(item.test_price_kzt).toLocaleString("ru-RU")} ₸ · предзаказ ${Number(item.preorder_days)} дн. · Kaspi ${escape(item.kaspi_product_id)}${item.last_error ? `<p>${escape(item.last_error)}</p>` : ""}</div></article>`).join("") || '<p>Пока нет подключений.</p>';
  };
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
    event.preventDefault(); button.disabled = true;
    try {
      await request("/api/preorder", {method:"POST", body:JSON.stringify({reference:document.querySelector("#reference").value.trim(), price_kzt:Number(document.querySelector("#price").value), preorder_days:Number(document.querySelector("#days").value), city_id:document.querySelector("#city").value.trim(), zone_id:document.querySelector("#zone").value.trim()})});
      message.textContent = "Задание передано агенту «Тест товара». Здесь появится результат подключения.";
      await load();
    } catch(error) { message.textContent = error.message; }
    finally { button.disabled = false; }
  });
  document.querySelector("#token-form").addEventListener("submit", async event => { event.preventDefault();localStorage.setItem(tokenKey, document.querySelector("#token").value.trim());document.querySelector("#token").value="";await load(); });
  document.querySelector("#refresh").addEventListener("click", load);
  document.addEventListener("visibilitychange", ()=>{if(!document.hidden)load();});
  window.addEventListener("leo:workspace-changed", load);
  setInterval(()=>{if(!document.hidden)load();},10000);
  load();
})();
