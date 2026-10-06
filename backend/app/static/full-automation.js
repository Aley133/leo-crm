(() => {
'use strict';
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = v => v == null ? '—' : `${Number(v).toLocaleString('ru-RU')} ₸`;
let items=[], selected=null, timer, searchTimer, pending=false;
async function request(url, options={}) {
 const response=await fetch(url,{cache:'no-store',...options,headers:{Authorization:`Bearer ${localStorage.getItem('leo_crm_service_token')||''}`,'Content-Type':'application/json',...options.headers}});
 const data=await response.json(); if(!response.ok) throw new Error(data.detail||'Ошибка CRM'); return data;
}
function message(text) {$('message').textContent=text;}
function edit(row) {
 selected=row.product_id; $('selected').textContent=row.name;
 const config={monitor_seconds:120,premium_per_day_kzt:500,premium_cap_kzt:2000,delivery_advantage_days:4,observation_hours:12,maximum_price_kzt:"",...(row.automation?.settings||{})};
 for(const field of ['monitor_seconds','premium_per_day_kzt','premium_cap_kzt','delivery_advantage_days','observation_hours','maximum_price_kzt']) {
  if(config[field]!=null) $(field).value=config[field];
 }
 $('minimum_profit_kzt').value=row.policy?.minimum_profit_kzt??1000;
 $('enabled').checked=true; $('settings').hidden=false;
}
async function load() {
 if(pending) return; pending=true;
 try {
  const [data,agents]=await Promise.all([request('/api/full-automation'),request('/api/fast-dumping-agent/agents/status')]); items=data.items;
  const connected=(agents.agents||[]).filter(a=>a.online); $('agent-info').textContent=connected.length?`Агент подключён: ${connected.map(a=>a.version||'версия неизвестна').join(', ')}. Для автоматизации нужна 1.2.8.`:'Агент не подключён. Проверки начнутся после его запуска.';
  $('auth-panel').hidden=true; $('page').hidden=false;
  const active=items.filter(r=>r.automation.enabled);
  $('count').textContent=`${active.length} / ${data.capacity}`;
  $('cards').innerHTML=active.length?active.map(row=>{
   const s=row.state||{}, o=row.automation.observation||{};
   return `<article class="fast-section"><h2>${escape(row.name)}</h2><p>SKU ${escape(row.merchant_sku)} · FIFO ${escape(row.current_inventory_on_hand)}</p><div class="metrics"><article class="metric"><span>Наша цена</span><strong>${money(s.own_price_kzt)}</strong></article><article class="metric"><span>Целевая цена</span><strong>${money(s.target_price_kzt)}</strong></article><article class="metric"><span>Безопасный порог</span><strong>${money(s.safe_floor_kzt)}</strong></article></div><p>${escape(s.status)} · ${escape(s.status_reason)}</p><p>Проверка рынка: ${escape(s.last_scanned_at?new Date(s.last_scanned_at).toLocaleString('ru-RU'):'ожидается')} · продавцов: ${escape(s.seller_count)} · цель TOP-${escape(o.target_position||3)}</p><p>${escape(o.plan_reason||'Первый расчёт ещё не выполнен.')}</p><p>${escape(o.sales_note||'Наблюдение за заказами начнётся после проверки рынка.')}</p><p>Заказы за наблюдение: ${escape(o.orders_units||0)} ед. · минимальный интервал записи цены: 5 минут.</p><button class="button secondary" data-edit="${row.product_id}">Настройки</button> <button class="button secondary" data-stop="${row.product_id}">Выключить автоматизацию</button> ${s.automatic_writes_paused?`<button class="button" data-resume="${row.product_id}">Возобновить после проверки</button>`:''}</article>`;
  }).join(''):'<p>Подключите выбранные товары. Автоматизация включается для каждого SKU отдельно.</p>';
  $('cards').querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>edit(items.find(r=>r.product_id===Number(b.dataset.edit))));
  $('cards').querySelectorAll('[data-resume]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await request(`/api/fast-dumping/products/${b.dataset.resume}/resume`,{method:'POST'});await load();}catch(e){message(e.message);}finally{b.disabled=false;}});
  $('cards').querySelectorAll('[data-stop]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await request(`/api/full-automation/products/${b.dataset.stop}`,{method:'PUT',body:JSON.stringify({enabled:false})});message('Автоматизация выключена. Прежние настройки восстановлены.');await load();}catch(e){message(e.message);}finally{b.disabled=false;}});
 } catch(e){message(e.message);} finally {pending=false;}
}
$('search').oninput=()=>{
 clearTimeout(searchTimer); searchTimer=setTimeout(async()=>{
  const q=$('search').value.trim(); if(q.length<2){$('results').innerHTML='';return;}
  try {const rows=await request(`/api/product-registry/products?q=${encodeURIComponent(q)}&limit=20`);if($('search').value.trim()!==q)return;
   $('results').innerHTML=rows.map(r=>`<button class="product-result" type="button" data-id="${r.product_id}">${escape(r.name)} · ${escape(r.merchant_sku)}</button>`).join('');
   $('results').querySelectorAll('[data-id]').forEach(b=>b.onclick=()=>{const r=rows.find(r=>r.product_id===Number(b.dataset.id));edit(items.find(i=>i.product_id===r.product_id)||r);$('results').innerHTML='';});
  } catch(e){message(e.message);}
 },300);
};
$('settings').onsubmit=async event=>{
 event.preventDefault(); const button=$('save');button.disabled=true;
 const payload={enabled:$('enabled').checked};
 for(const field of ['minimum_profit_kzt','monitor_seconds','premium_per_day_kzt','premium_cap_kzt','delivery_advantage_days','observation_hours','maximum_price_kzt']) payload[field]=$(field).value===''?null:Number($(field).value);
 try{await request(`/api/full-automation/products/${selected}`,{method:'PUT',body:JSON.stringify(payload)});message('Режим сохранён; товар поставлен на приоритетную проверку.');$('settings').hidden=true;await load();}catch(e){message(e.message);}finally{button.disabled=false;}
};
$('token-form').onsubmit=e=>{e.preventDefault();localStorage.setItem('leo_crm_service_token',$('token').value.trim());$('token').value='';load();};
$('refresh').onclick=load;
if(localStorage.getItem('leo_crm_service_token'))load();
function poll(){clearInterval(timer);if(!document.hidden)timer=setInterval(load,30000);}
document.addEventListener('visibilitychange',poll);poll();
})();
