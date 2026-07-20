const storageKey = "leo_crm_service_token";
const authPanel = document.querySelector("#auth-panel");
const page = document.querySelector("#monitoring");
const message = document.querySelector("#message");
const tokenForm = document.querySelector("#token-form");
const tokenInput = document.querySelector("#token");
const refreshButton = document.querySelector("#refresh");
const onlyErrors = document.querySelector("#only-errors");
const jobStatus = document.querySelector("#job-status");
const jobSource = document.querySelector("#job-source");
const attemptSource = document.querySelector("#attempt-source");
const attemptPeriod = document.querySelector("#attempt-period");
const jobDialog = document.querySelector("#job-dialog");
const jobDialogContent = document.querySelector("#job-dialog-content");
const jobDialogTitle = document.querySelector("#job-dialog-title");

let cachedJobs = [];
let cachedAttempts = [];
let cachedSources = [];

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const when = (value) => value ? new Date(value).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "—";
const setText = (id, value) => { const el=document.querySelector(`#${id}`); if(el) el.textContent=String(value??0); };
const labels = {
  queued:"В очереди", leased:"В работе", succeeded:"Успешно", failed:"Ошибка",
  waiting_for_agent:"Ожидает Browser Agent", processing:"Выполняется", lease_expired:"Lease истёк",
  finished:"Завершено", cancelled:"Отменено оператором", healthy:"Работает", active:"Активен",
  degraded:"Нестабильно", blocked:"Заблокирован", captcha_required:"Нужна CAPTCHA",
  auth_required:"Нужна авторизация", rate_limited:"Ограничение запросов", success:"Успешно"
};
const labelFor = (value) => labels[value] || value || "Неизвестно";
const badge = (value) => {
  const cls = ["succeeded","success","healthy","active","finished"].includes(value) ? "ok" : ["failed","blocked","captcha_required","degraded","auth_required","cancelled","lease_expired"].includes(value) ? "bad" : ["queued","leased","rate_limited","waiting_for_agent","processing"].includes(value) ? "warn" : "";
  return `<span class="badge ${cls}">${escapeHtml(labelFor(value))}</span>`;
};
const errorLabels = {
  AdapterNetworkError:"Ошибка сети", adapter_parse_error:"Ошибка разбора страницы",
  operator_cancelled:"Отменено оператором", timeout:"Превышено время ожидания",
  captcha_required:"Требуется CAPTCHA", auth_required:"Требуется авторизация"
};
const api = async (path, token, options={}) => {
  const response=await fetch(path,{headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/json"},cache:"no-store",...options});
  if(response.status===401){localStorage.removeItem(storageKey);throw new Error("Токен не принят.");}
  if([502,503,504].includes(response.status))throw new Error("Сервис Render временно недоступен или перезапускается.");
  if(!response.ok){let detail=`API вернул ошибку ${response.status}`;try{const body=await response.json();if(body.detail)detail=body.detail;}catch{}throw new Error(detail);}
  return response.json();
};
const setLoading = (loading) => { page.setAttribute("aria-busy",String(loading));refreshButton.disabled=loading;refreshButton.textContent=loading?"Обновление…":"Обновить"; };
const formatDuration = (milliseconds) => { if(milliseconds==null)return"—";const value=Number(milliseconds);if(!Number.isFinite(value)||value<0)return"—";if(value<1000)return`${Math.round(value)} мс`;if(value<60000)return`${(value/1000).toFixed(value<10000?1:0)} с`;const minutes=Math.floor(value/60000);const seconds=Math.round((value%60000)/1000);if(minutes<60)return`${minutes} мин ${seconds} с`;return`${Math.floor(minutes/60)} ч ${minutes%60} мин`; };
const elapsed = (value) => value ? formatDuration(Math.max(0,Date.now()-new Date(value).getTime())) : "—";
const errorCell = (code,text) => {
  if(!code&&!text)return"—";
  const title=errorLabels[code]||"Техническая ошибка";
  return `<details class="error-details"><summary><span>${escapeHtml(title)}</span><small>${escapeHtml(code||"Ошибка")}</small></summary><div>${escapeHtml(text||"Нет подробностей")}</div></details>`;
};
const lifecycleCell = (row) => `${badge(row.lifecycle_state)}${row.wait_reason?`<span class="muted">${escapeHtml(row.wait_reason)}</span>`:""}`;

const filteredJobs=()=>{const status=jobStatus.value;const source=jobSource.value.trim().toLowerCase();return cachedJobs.filter((row)=>(!status||row.status===status)&&(!source||String(row.supplier_code||row.supplier_name||"").toLowerCase().includes(source)));};
const filteredAttempts=()=>{const source=attemptSource.value.trim().toLowerCase();const since=Date.now()-24*60*60*1000;return cachedAttempts.filter((row)=>{if(onlyErrors.checked&&!row.error_code)return false;if(source&&!String(row.supplier_code||row.adapter_code||"").toLowerCase().includes(source))return false;if(attemptPeriod.value==="24h"&&new Date(row.started_at).getTime()<since)return false;return true;});};
const recentAttemptStats=()=>{const since=Date.now()-24*60*60*1000;const recent=cachedAttempts.filter((row)=>new Date(row.started_at).getTime()>=since);return {recent,success:recent.filter((row)=>!row.error_code&&["success","succeeded"].includes(row.outcome)).length,failed:recent.filter((row)=>Boolean(row.error_code)||row.outcome==="failed").length};};

const renderRuntimeHealth=()=>{
  const queued=cachedJobs.filter((row)=>row.status==="queued").length;
  const leased=cachedJobs.filter((row)=>row.status==="leased").length;
  const unhealthy=cachedSources.filter((row)=>row.status!=="healthy");
  const {failed}=recentAttemptStats();
  const panel=document.querySelector("#runtime-health");
  let state="ok",title="Runtime работает нормально",detail="Очередь и источники не требуют вмешательства.",action="Наблюдение не требуется";
  if(unhealthy.length){state="bad";title=`Проблема с источником: ${unhealthy[0].supplier_name}`;detail=`Статус: ${labelFor(unhealthy[0].status)}. Ошибок подряд: ${unhealthy[0].consecutive_failures}.`;action="Проверьте источник";}
  else if(failed>=5){state="bad";title=`${failed} ошибок за последние 24 часа`;detail="Runtime работает, но частота ошибок требует внимания оператора.";action="Откройте последние ошибки";}
  else if(leased){state="active";title=`Browser Agent выполняет ${leased} ${leased===1?"задание":"задания"}`;detail="Lease активен, результат появится после завершения проверки.";action="Runtime занят";}
  else if(queued){state="warn";title=`В очереди ${queued} ${queued===1?"задание":"задания"}`;detail="Задания ожидают свободный Browser Agent и получение lease.";action="Ожидает агента";}
  panel.dataset.state=state;setText("runtime-health-title",title);setText("runtime-health-detail",detail);setText("runtime-health-action",action);
};
const renderLeased=()=>{const rows=cachedJobs.filter((row)=>row.status==="leased");const body=document.querySelector("#leased-body");const table=document.querySelector("#leased-table");const empty=document.querySelector("#leased-empty");body.innerHTML=rows.map((row)=>`<tr><td><strong>#${row.id}</strong></td><td>${row.product_id?`<a href="/crm/products/${row.product_id}">${escapeHtml(row.product_name||row.kaspi_product_id)}</a>`:"—"}<span class="muted">${escapeHtml(row.kaspi_product_id||"")}</span></td><td><a href="${escapeHtml(row.supplier_product_url)}" target="_blank" rel="noreferrer">${escapeHtml(row.supplier_name||row.supplier_code||"Источник")}</a></td><td>${escapeHtml(row.lease_owner||"—")}</td><td>${when(row.lease_until)}</td><td>${elapsed(row.updated_at||row.created_at)}<span class="muted">${escapeHtml(row.wait_reason||"")}</span></td></tr>`).join("");table.classList.toggle("hidden",rows.length===0);empty.classList.toggle("hidden",rows.length>0);setText("leased-label",`${rows.length} активных`);setText("agent-queue-count",cachedJobs.filter((row)=>row.status==="queued").length);setText("agent-last-work",cachedAttempts.length?when(cachedAttempts[0].started_at):"Нет данных");const durations=cachedAttempts.map((row)=>Number(row.duration_ms)).filter((value)=>Number.isFinite(value)&&value>=0);setText("agent-average-time",durations.length?formatDuration(durations.reduce((sum,value)=>sum+value,0)/durations.length):"Нет данных");setText("agent-idle-detail",cachedJobs.some((row)=>row.status==="queued")?"Свободный агент пока не забрал задания из очереди.":"Очередь пуста, агент ожидает новые задания.");};
const actionButtons=(row)=>{const retry=["failed","succeeded"].includes(row.status)?`<button class="table-action" data-action="retry" data-job-id="${row.id}" type="button">Повторить</button>`:"";const cancel=row.status==="queued"?`<button class="table-action danger" data-action="cancel" data-job-id="${row.id}" type="button">Отменить</button>`:"";return `<div class="job-actions"><button class="table-action" data-action="inspect" data-job-id="${row.id}" type="button">Подробнее</button>${retry}${cancel}</div>`;};
const renderJobs=()=>{const rows=filteredJobs();document.querySelector("#jobs-body").innerHTML=rows.map((row)=>`<tr><td><strong>#${row.id}</strong></td><td>${badge(row.status)}</td><td>${lifecycleCell(row)}</td><td>${row.product_id?`<a href="/crm/products/${row.product_id}">${escapeHtml(row.product_name||row.kaspi_product_id)}</a>`:"—"}<span class="muted">${escapeHtml(row.kaspi_product_id||"")}</span></td><td><a href="${escapeHtml(row.supplier_product_url)}" target="_blank" rel="noreferrer">${escapeHtml(row.supplier_name||row.supplier_code||"Источник")}</a></td><td>${escapeHtml(row.lease_owner||"—")}<span class="muted">до ${when(row.lease_until)}</span></td><td>${errorCell(row.error_code,row.error_message)}</td><td>${when(row.created_at)}</td><td>${actionButtons(row)}</td></tr>`).join("");};
const renderAttempts=()=>{const rows=filteredAttempts();document.querySelector("#attempts-body").innerHTML=rows.map((row)=>`<tr><td>${badge(row.outcome)}</td><td>${row.product_id?`<a href="/crm/products/${row.product_id}">${escapeHtml(row.product_name||row.kaspi_product_id)}</a>`:"—"}<span class="muted">${escapeHtml(row.kaspi_product_id||"")}</span></td><td>${escapeHtml(row.supplier_code||row.adapter_code)}</td><td>${formatDuration(row.duration_ms)}</td><td>${row.http_status??"—"}</td><td>${errorCell(row.error_code,row.error_message)}</td><td>${when(row.started_at)}</td></tr>`).join("");setText("attempts-label",`${rows.length} проверок`);};
const renderSources=(rows)=>{document.querySelector("#sources-body").innerHTML=rows.map((row)=>`<tr><td>${escapeHtml(row.supplier_name)}<span class="muted">${escapeHtml(row.supplier_code)}</span></td><td>${escapeHtml(row.access_strategy)}</td><td>${badge(row.status)}</td><td>${row.consecutive_failures}</td><td>${when(row.last_success_at)}</td><td>${when(row.last_failure_at)}<span class="muted">${escapeHtml(row.last_error_code||"")}</span></td></tr>`).join("");};
const renderAttemptMetrics=()=>{const {success,failed}=recentAttemptStats();const durations=cachedAttempts.map((row)=>Number(row.duration_ms)).filter((value)=>Number.isFinite(value)&&value>=0);const average=durations.length?durations.reduce((sum,value)=>sum+value,0)/durations.length:null;setText("attempts-success-24h",success);setText("attempts-failed-24h",failed);setText("attempts-average",formatDuration(average));setText("last-attempt",cachedAttempts.length?when(cachedAttempts[0].started_at):"—");};
const rerender=()=>{renderRuntimeHealth();renderLeased();renderJobs();renderAttempts();renderAttemptMetrics();};

const inspectJob=async(jobId)=>{const token=localStorage.getItem(storageKey);const [job,events]=await Promise.all([api(`/api/monitoring-center/jobs/${jobId}`,token),api(`/api/monitoring-center/jobs/${jobId}/events`,token)]);jobDialogTitle.textContent=`Job #${job.id}`;jobDialogContent.innerHTML=`<dl class="job-inspector"><div><dt>Статус</dt><dd>${badge(job.status)}</dd></div><div><dt>Этап</dt><dd>${badge(job.lifecycle_state)}</dd></div><div><dt>Причина ожидания</dt><dd>${escapeHtml(job.wait_reason||"—")}</dd></div><div><dt>Monitor target</dt><dd>${job.monitor_target_id??"—"}</dd></div><div><dt>Supplier product</dt><dd>${job.supplier_product_id}</dd></div><div><dt>Lease owner</dt><dd>${escapeHtml(job.lease_owner||"—")}</dd></div><div><dt>Lease до</dt><dd>${when(job.lease_until)}</dd></div><div><dt>Создан</dt><dd>${when(job.created_at)}</dd></div><div><dt>Завершён</dt><dd>${when(job.finished_at)}</dd></div><div><dt>Ошибка</dt><dd>${errorCell(job.error_code,job.error_message)}</dd></div><div><dt>URL</dt><dd><a href="${escapeHtml(job.url)}" target="_blank" rel="noreferrer">Открыть источник</a></dd></div></dl><h3>Хронология Job</h3><ol class="runtime-events">${events.map((event)=>`<li><strong>${escapeHtml(labelFor(event.event))}</strong><span>${when(event.occurred_at)}</span><p>${escapeHtml(event.detail||"")}</p></li>`).join("")}</ol>`;jobDialog.showModal();};
const mutateJob=async(jobId,action)=>{const token=localStorage.getItem(storageKey);if(action==="cancel"&&!confirm(`Отменить Job #${jobId}?`))return;await api(`/api/monitoring-center/jobs/${jobId}/${action}`,token,{method:"POST",body:"{}"});message.textContent=action==="retry"?`Создан повтор для Job #${jobId}.`:`Job #${jobId} отменён.`;await loadMonitoring();};
const loadMonitoring=async()=>{const token=localStorage.getItem(storageKey);if(!token){authPanel.classList.remove("hidden");page.classList.add("hidden");return;}setLoading(true);message.textContent="";try{const [summary,jobs,attempts,sources]=await Promise.all([api("/api/monitoring-center/summary",token),api("/api/monitoring-center/jobs?limit=100",token),api("/api/monitoring-center/attempts?limit=100",token),api("/api/monitoring-center/sources",token)]);cachedJobs=jobs;cachedAttempts=attempts;cachedSources=sources;setText("targets-total",summary.targets_total);setText("targets-active",summary.targets_active);setText("jobs-queued",summary.jobs_queued);setText("jobs-leased",summary.jobs_leased);rerender();renderSources(sources);document.querySelector("#jobs-updated").textContent=`Обновлено ${new Date().toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit"})}`;authPanel.classList.add("hidden");page.classList.remove("hidden");}catch(error){message.textContent=error instanceof Error?error.message:"Не удалось загрузить мониторинг.";}finally{setLoading(false);}};

tokenForm.addEventListener("submit",(event)=>{event.preventDefault();const token=tokenInput.value.trim();if(!token)return;localStorage.setItem(storageKey,token);tokenInput.value="";loadMonitoring();});
refreshButton.addEventListener("click",loadMonitoring);
[onlyErrors,jobStatus,jobSource,attemptSource,attemptPeriod].forEach((control)=>control.addEventListener(control.tagName==="INPUT"&&control.type==="search"?"input":"change",rerender));
document.querySelector("#jobs-body").addEventListener("click",async(event)=>{const button=event.target.closest("[data-action]");if(!button)return;try{button.disabled=true;const {action,jobId}=button.dataset;if(action==="inspect")await inspectJob(jobId);else await mutateJob(jobId,action);}catch(error){message.textContent=error instanceof Error?error.message:"Операция не выполнена.";}finally{button.disabled=false;}});
document.querySelector("#job-dialog-close").addEventListener("click",()=>jobDialog.close());
jobDialog.addEventListener("click",(event)=>{if(event.target===jobDialog)jobDialog.close();});
loadMonitoring();
