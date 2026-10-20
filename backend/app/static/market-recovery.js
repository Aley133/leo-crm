(() => {
  "use strict";
  const panel = document.querySelector("#market-recovery");
  if (!panel) return;
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const money = value => value == null ? "—" : `${Number(value).toLocaleString("ru-RU")} ₸`;
  const when = value => value ? new Date(value).toLocaleString("ru-RU") : "ещё не проверено";
  const status = panel.querySelector("#market-status");
  let offset = 0, total = 0, inFlight = false, dirty = false;
  const api = async (path = "", options = {}) => {
    const token = localStorage.getItem("leo_crm_service_token");
    const response = await fetch(`/api/monitoring-center/market-recovery${path}`, {cache:"no-store", ...options,
      headers:{Authorization:`Bearer ${token}`, ...(options.body ? {"Content-Type":"application/json"} : {})}});
    const data = await response.json();
    if (!response.ok) throw Error(data.detail || `Ошибка ${response.status}`);
    return data;
  };
  async function load() {
    if (inFlight || document.hidden || !localStorage.getItem("leo_crm_service_token")) return;
    inFlight = true;
    try {
      const data = await api(`?offset=${offset}&limit=100`);
      panel.classList.remove("hidden");
      total = data.total;
      panel.querySelector("#market-summary").textContent = `Карточек: ${data.total} · Проверено: ${data.checked} · Ошибок последней проверки: ${data.errors}`;
      const compatible = (data.agent?.agents || []).some(agent => agent.online && /^\d+\.\d+\.\d+$/.test(agent.version || "") && agent.version.split(".").reduce((n,v)=> n*1000+Number(v),0) >= 1001027);
      status.textContent = !data.enabled ? "Наблюдение приостановлено" : compatible ? "Product Test Agent в сети · проверяем весь ассортимент раз в сутки" : "Для проверки рынка запустите Product Test Agent 1.1.27 или новее";
      if (!dirty) {
        panel.querySelector("#market-enabled").checked = data.enabled;
        panel.querySelector("#market-rise").value = String(data.rise_percent);
      }
      panel.querySelector("#market-signals").innerHTML = (data.signals || []).map(signal => `<article class="market-signal"><strong>${signal.reasons.includes("no_sellers") ? "🟢 Продавцов больше нет" : `📈 Цена выросла на ${esc(signal.rise_percent)}%`}</strong><a href="${esc(signal.kaspi_url)}" target="_blank" rel="noopener">${esc(signal.product_name)} ↗</a><span>${money(signal.baseline_price)} → ${money(signal.current_price)} · Продавцов: ${signal.seller_count}</span><small>${when(signal.checked_at)} · ${signal.sent ? "Отправлено в Telegram" : signal.delivery_error ? esc(signal.delivery_error) : "Ожидает отправки в Telegram"}</small></article>`).join("") || '<p class="muted">Сигналов пока нет. Первый проход запоминает цены и находит карточки без продавцов.</p>';
      panel.querySelector("#market-cards").innerHTML = (data.cards || []).map(card => `<tr><td><a href="${esc(card.kaspi_url)}" target="_blank" rel="noopener">${esc(card.name)}</a><small class="market-id">${esc(card.kaspi_product_id)}</small></td><td>${money(card.baseline_price)}</td><td>${money(card.current_price)}${card.baseline_price && card.current_price ? `<small class="market-id">+${Math.max(0, (Number(card.current_price)/Number(card.baseline_price)-1)*100).toFixed(1)}%</small>` : ""}</td><td>${card.seller_count == null ? "—" : `${card.sellers_complete ? "" : "≥"}${card.seller_count}`}</td><td>${when(card.last_checked_at)}${card.last_error ? `<small class="market-id market-error">${esc(card.last_error)} · повтор через час</small>` : ""}</td></tr>`).join("") || '<tr><td colspan="5">В каталоге пока нет карточек с определённым Kaspi ID.</td></tr>';
      panel.querySelector("#market-page").textContent = total ? `${offset + 1}–${Math.min(offset + 100, total)} из ${total}` : "0 карточек";
      panel.querySelector("#market-prev").disabled = offset === 0;
      panel.querySelector("#market-next").disabled = offset + 100 >= total;
    } catch (error) {
      panel.classList.remove("hidden");
      status.textContent = `${error.message}. Повторим загрузку автоматически.`;
    } finally { inFlight = false; }
  }
  panel.querySelector("#market-settings").addEventListener("change",()=>{dirty=true;});
  panel.querySelector("#market-settings").addEventListener("submit", async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    try {
      await api("/settings",{method:"PATCH",body:JSON.stringify({enabled:panel.querySelector("#market-enabled").checked,rise_percent:Number(panel.querySelector("#market-rise").value)})});
      dirty = false; await load();
    } catch(error) { status.textContent=error.message; }
    finally { button.disabled=false; }
  });
  panel.querySelector("#market-sync").addEventListener("click", async event => {
    const button = event.currentTarget; button.disabled=true;
    try { await api("/sync",{method:"POST"}); await load(); }
    catch(error) { status.textContent=error.message; }
    finally { button.disabled=false; }
  });
  panel.querySelector("#market-prev").addEventListener("click",()=>{offset=Math.max(0,offset-100);load();});
  panel.querySelector("#market-next").addEventListener("click",()=>{if(offset+100<total){offset+=100;load();}});
  document.querySelector("#refresh")?.addEventListener("click",load);
  document.querySelector("#token-form")?.addEventListener("submit",()=>setTimeout(load,0));
  document.querySelector("#disconnect")?.addEventListener("click",()=>panel.classList.add("hidden"));
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)load();});
  setInterval(load,60000);
  load();
})();
