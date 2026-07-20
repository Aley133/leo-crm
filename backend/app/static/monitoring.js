const storageKey="leo_crm_service_token";
const authPanel=document.querySelector("#auth-panel");
const page=document.querySelector("#monitoring");
const message=document.querySelector("#message");
const tokenForm=document.querySelector("#token-form");
const tokenInput=document.querySelector("#token");
const refreshButton=document.querySelector("#refresh");
const onlyErrors=document.querySelector("#only-errors");

const escapeHtml=(value)=>String(value??"").replace(/[&<>'"]/g,(char)=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const when=(value)=>value?new Date(value).toLocaleString("ru-RU",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}):"—";
const badge=(value)=>{const text=escapeHtml(value||"unknown");const cls=["succeeded","success","healthy","active"].includes(value)?"ok":["failed","blocked","captcha_required","degraded","auth_required"].includes(value)?"bad":["queued","leased","rate_limited"].includes(value)?"warn":"";return `<span class="badge ${cls}">${text}</span>`;};
const setText=(id,value)=>{const el=document.querySelector(`#${id}`);if(el)el.textContent=String(value??0);};
const api=async(path,token)=>{const response=await fetch(path,{headers:{Authorization:`Bearer ${token}`},cache:"no-store"});if(response.status===401){localStorage.removeItem(storageKey);throw new Error("Токен не принят.");}if(!response.ok)throw new Error(`API вернул ошибку ${response.status}`);return response.json();};
const setLoading=(loading)=>{page.setAttribute("aria-busy",String(loading));refreshButton.disabled=loading;refreshButton.textContent=loading?"Обновление…":"Обновить";};

const renderJobs=(rows)=>{document.querySelector("#jobs-body").innerHTML=rows.map((row)=>`<tr><td>${badge(row.status)}</td><td>${row.product_id?`<a href="/crm/products/${row.product_id}">${escapeHtml(row.product_name||row.kaspi_product_id)}</a>`:"—"}<span class="muted">${escapeHtml(row.kaspi_product_id||"")}</span></td><td><a href="${escapeHtml(row.supplier_product_url)}" target="_blank" rel="noreferrer">${escapeHtml(row.supplier_name||row.supplier_code||"Источник")}</a></td><td>${escapeHtml(row.lease_owner||"—")}<span class="muted">до ${when(row.lease_until)}</span></td><td>${escapeHtml(row.error_code||"—")}<span class="muted">${escapeHtml(row.error_message||"")}</span></td><td>${when(row.created_at)}</td></tr>`).join("");};
const renderAttempts=(rows)=>{document.querySelector("#attempts-body").innerHTML=rows.map((row)=>`<tr><td>${badge(row.outcome)}</td><td><a href="/crm/products/${row.product_id}">${escapeHtml(row.product_name||row.kaspi_product_id)}</a><span class="muted">${escapeHtml(row.kaspi_product_id||"")}</span></td><td>${escapeHtml(row.supplier_code||row.adapter_code)}</td><td>${row.duration_ms==null?"—":`${row.duration_ms} мс`}</td><td>${row.http_status??"—"}</td><td>${escapeHtml(row.error_code||"—")}<span class="muted">${escapeHtml(row.error_message||"")}</span></td><td>${when(row.started_at)}</td></tr>`).join("");};
const renderSources=(rows)=>{document.querySelector("#sources-body").innerHTML=rows.map((row)=>`<tr><td>${escapeHtml(row.supplier_name)}<span class="muted">${escapeHtml(row.supplier_code)}</span></td><td>${escapeHtml(row.access_strategy)}</td><td>${badge(row.status)}</td><td>${row.consecutive_failures}</td><td>${when(row.last_success_at)}</td><td>${when(row.last_failure_at)}<span class="muted">${escapeHtml(row.last_error_code||"")}</span></td></tr>`).join("");};

const loadMonitoring=async()=>{const token=localStorage.getItem(storageKey);if(!token){authPanel.classList.remove("hidden");page.classList.add("hidden");return;}setLoading(true);message.textContent="";try{const [summary,jobs,attempts,sources]=await Promise.all([api("/api/monitoring-center/summary",token),api("/api/monitoring-center/jobs?limit=100",token),api(`/api/monitoring-center/attempts?limit=100&only_errors=${onlyErrors.checked}`,token),api("/api/monitoring-center/sources",token)]);setText("targets-total",summary.targets_total);setText("targets-active",summary.targets_active);setText("jobs-queued",summary.jobs_queued);setText("attempts-failed",summary.attempts_failed);renderJobs(jobs);renderAttempts(attempts);renderSources(sources);document.querySelector("#jobs-updated").textContent=`Обновлено ${new Date().toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit"})}`;authPanel.classList.add("hidden");page.classList.remove("hidden");}catch(error){message.textContent=error instanceof Error?error.message:"Не удалось загрузить мониторинг.";}finally{setLoading(false);}};

tokenForm.addEventListener("submit",(event)=>{event.preventDefault();const token=tokenInput.value.trim();if(!token)return;localStorage.setItem(storageKey,token);tokenInput.value="";loadMonitoring();});
refreshButton.addEventListener("click",loadMonitoring);
onlyErrors.addEventListener("change",loadMonitoring);
loadMonitoring();
