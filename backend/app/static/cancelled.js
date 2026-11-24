(() => {
  const $ = (id) => document.getElementById(id);
  const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const date = s => s ? new Date(s).toLocaleDateString('ru-RU', {timeZone:'Asia/Qyzylorda'}) : '—';
  const money = n => `${Number(n).toLocaleString('ru-RU')} ₸`;
  const stateLabels = {received:'Товар получен', compensated:'Компенсация получена', overdue:'Просрочен', waiting:'Ожидаем возврат'};
  let offset = 0, busy = false;
  const limit = 50;
  async function api(path, options = {}) {
    const response = await fetch(`/api/cancelled-returns${path}`, {...options, headers:{Authorization:`Bearer ${localStorage.getItem('leo_crm_service_token') || ''}`}});
    if (response.status === 401 || response.status === 403) $('auth-panel').classList.remove('hidden');
    if (!response.ok) { let body = {}; try { body = await response.json(); } catch (_) {} throw new Error(body.detail || `Ошибка ${response.status}`); }
    return response.json();
  }
  async function load() {
    if (busy) return;
    busy = true; $('refresh').disabled = true; $('message').textContent = '';
    $('returns-list').setAttribute('aria-busy', 'true');
    try {
      const params = new URLSearchParams({view:$('view').value, query:$('query').value, offset, limit});
      const data = await api(`?${params}`);
      $('auth-panel').classList.add('hidden');
      $('summary').textContent = `${data.total} заказов · ${money(data.total_amount)}`;
      $('returns-list').innerHTML = data.items.length ? data.items.map(o => `<article class="return-card">
        <div class="return-head"><div><strong>Заказ №${escape(o.code)}</strong> · ${escape(o.account)}</div><span class="return-state ${o.state}">${stateLabels[o.state]}</span></div>
        <div class="return-meta"><span>Передан курьеру: ${date(o.handoff_at)}</span><span>Отменён: ${date(o.cancelled_at)}</span><span>Контроль: ${date(o.due_at)}</span></div>
        <div class="return-lines">${o.lines.map(l => `<div>${l.product_id ? `<a href="/crm/products/${Number(l.product_id)}">${escape(l.title)}</a>` : escape(l.title)} × <strong>${Number(l.quantity)}</strong>${l.sku ? ` · SKU ${escape(l.sku)}` : ''}</div>`).join('') || 'Состав заказа пока не загружен'}</div>
        <div class="return-foot"><span>${money(o.amount)} · ${o.received_at ? `Товар получен ${date(o.received_at)}` : o.compensated_at ? `Компенсация получена ${date(o.compensated_at)}` : `Ожидание ${o.days_waiting} дн.`}</span>${o.can_receive ? `<div class="return-actions" data-order-id="${o.id}" data-code="${escape(o.code)}">
          <button class="button" data-outcome="received">Получил товар</button>
          <button class="button secondary" data-outcome="compensated">Получил компенсацию</button>
        </div>` : ''}</div></article>`).join('') : '<div class="empty">Заказов в этой группе нет.</div>';
      $('prev').disabled = offset === 0; $('next').disabled = offset + limit >= data.total;
      $('page').textContent = data.total ? `${offset + 1}–${Math.min(offset + limit,data.total)} из ${data.total}` : '0 заказов';
    } catch (e) { $('message').textContent = e.message; }
    finally { busy = false; $('refresh').disabled = false; $('returns-list').setAttribute('aria-busy','false'); }
  }
  $('returns-list').addEventListener('click', async e => {
    const button = e.target.closest('[data-outcome]');
    if (!button || busy) return;
    const {orderId, code} = button.closest('.return-actions').dataset;
    const outcome = button.dataset.outcome;
    const confirmation = outcome === 'received'
      ? `Подтверждаете получение всех товаров заказа №${code}? CRM вернёт на склад товары, которые ещё числятся за заказом.`
      : `Подтверждаете, что получили компенсацию за утрату товара по заказу №${code}?`;
    if (!window.confirm(confirmation)) return;
    button.disabled = true; busy = true;
    try {
      await api(`/${orderId}/${outcome}`, {method:'POST'});
      if (offset && ['pending', 'overdue'].includes($('view').value)
          && $('returns-list').querySelectorAll('.return-card').length === 1) offset -= limit;
      busy = false;
      await load();
    }
    catch (error) { $('message').textContent = error.message; }
    finally { busy = false; button.disabled = false; }
  });
  $('filters').addEventListener('submit', e => {e.preventDefault(); offset = 0; load();});
  $('view').addEventListener('change', () => {offset=0; load();});
  $('prev').onclick=()=>{offset=Math.max(0,offset-limit);load();};
  $('next').onclick=()=>{offset+=limit;load();};
  $('refresh').onclick=load;
  $('token-form').onsubmit=e=>{e.preventDefault();localStorage.setItem('leo_crm_service_token',$('token').value.trim());load();};
  load();
})();
