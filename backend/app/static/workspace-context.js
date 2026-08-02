(() => {
  "use strict";

  const workspaceStorageKey = "leo_crm_workspace_id";
  const tokenStorageKey = "leo_crm_service_token";
  const workspaceHeader = "X-Workspace-ID";
  const nativeFetch = window.fetch.bind(window);

  const selectedWorkspaceId = () => {
    const value = Number.parseInt(localStorage.getItem(workspaceStorageKey) || "1", 10);
    return Number.isInteger(value) && value > 0 ? value : 1;
  };

  const isApiRequest = (input) => {
    try {
      const raw = input instanceof Request ? input.url : String(input);
      const url = new URL(raw, window.location.href);
      return url.origin === window.location.origin && url.pathname.startsWith("/api/");
    } catch (_) {
      return false;
    }
  };

  window.fetch = (input, init = {}) => {
    if (!isApiRequest(input)) return nativeFetch(input, init);
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init.headers || {}).forEach((value, key) => headers.set(key, value));
    headers.set(workspaceHeader, String(selectedWorkspaceId()));
    if (input instanceof Request) {
      return nativeFetch(new Request(input, {...init, headers}));
    }
    return nativeFetch(input, {...init, headers});
  };

  window.LEOWorkspace = Object.freeze({
    header: workspaceHeader,
    selectedId: selectedWorkspaceId,
  });

  const escapeHtml = (value) => String(value ?? "").replace(
    /[&<>'"]/g,
    (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]),
  );

  const styles = document.createElement("style");
  styles.textContent = `
    .leo-account-button{display:inline-flex;align-items:center;gap:8px;min-height:40px;padding:8px 14px;border:1px solid rgba(109,128,159,.35);border-radius:10px;background:#fff;color:#18243a;font:600 13px/1.2 Inter,system-ui,sans-serif;cursor:pointer;white-space:nowrap;box-shadow:0 4px 14px rgba(24,36,58,.07)}
    .leo-account-button:before{content:"";width:8px;height:8px;border-radius:50%;background:#19a974;box-shadow:0 0 0 3px rgba(25,169,116,.14)}
    .leo-account-dialog{width:min(620px,calc(100vw - 32px));max-height:calc(100vh - 48px);padding:0;border:0;border-radius:16px;color:#18243a;box-shadow:0 24px 80px rgba(15,25,45,.28)}
    .leo-account-dialog::backdrop{background:rgba(12,22,40,.55);backdrop-filter:blur(2px)}
    .leo-account-card{padding:22px;font:14px/1.45 Inter,system-ui,sans-serif}
    .leo-account-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}.leo-account-head h2{margin:0 0 4px;font-size:22px}.leo-account-head p{margin:0;color:#63708a}.leo-account-close{border:0;background:#eef2f7;border-radius:9px;width:36px;height:36px;font-size:23px;cursor:pointer}
    .leo-account-field{display:grid;gap:6px;margin:12px 0}.leo-account-field span{font-weight:650}.leo-account-field input,.leo-account-field select{width:100%;box-sizing:border-box;border:1px solid #ccd5e3;border-radius:9px;padding:10px 11px;background:#fff;color:#18243a;font:inherit}
    .leo-account-actions{display:flex;gap:9px;flex-wrap:wrap;margin:14px 0}.leo-account-actions button{border:0;border-radius:9px;padding:10px 14px;background:#2457e6;color:#fff;font:650 13px/1.2 inherit;cursor:pointer}.leo-account-actions button.secondary{background:#e9eef8;color:#203150}
    .leo-account-dialog details{border-top:1px solid #e2e8f1;padding-top:12px;margin-top:14px}.leo-account-dialog summary{font-weight:700;cursor:pointer}.leo-account-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}.leo-account-grid .wide{grid-column:1/-1}
    .leo-account-status{min-height:20px;margin:8px 0 0;color:#52617a}.leo-account-status.error{color:#b42318}.leo-account-status.ok{color:#087a55}
    @media(max-width:700px){.leo-account-grid{grid-template-columns:1fr}.leo-account-grid .wide{grid-column:auto}.leo-account-button{max-width:180px;overflow:hidden;text-overflow:ellipsis}}
  `;
  document.head.appendChild(styles);

  const authHeaders = (json = false) => {
    const headers = {Authorization: `Bearer ${localStorage.getItem(tokenStorageKey) || ""}`};
    if (json) headers["Content-Type"] = "application/json";
    return headers;
  };

  const responseError = async (response) => {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || detail);
    } catch (_) {}
    return new Error(detail);
  };

  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers: {...authHeaders(Boolean(options.body)), ...(options.headers || {})},
    });
    if (!response.ok) throw await responseError(response);
    return response.status === 204 ? null : response.json();
  };

  document.addEventListener("DOMContentLoaded", () => {
    const topbar = document.querySelector(".topbar");
    if (!topbar) return;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "leo-account-button";
    button.textContent = "Аккаунты Kaspi";
    button.title = "Переключить или подключить аккаунт Kaspi";
    topbar.appendChild(button);

    const dialog = document.createElement("dialog");
    dialog.className = "leo-account-dialog";
    dialog.innerHTML = `
      <div class="leo-account-card">
        <div class="leo-account-head"><div><h2>Аккаунты Kaspi</h2><p>Заказы, склад, демпинг и XML полностью разделены.</p></div><button class="leo-account-close" type="button" aria-label="Закрыть">×</button></div>
        <label class="leo-account-field"><span>Рабочий аккаунт</span><select id="leo-account-select"></select></label>
        <div class="leo-account-actions"><button id="leo-account-switch" type="button">Переключить</button><button id="leo-account-test" class="secondary" type="button">Проверить API</button></div>
        <details id="leo-account-edit"><summary>Настройки выбранного аккаунта</summary><form id="leo-account-edit-form" class="leo-account-grid">
          <label class="leo-account-field"><span>Название</span><input name="name" maxlength="255" required></label>
          <label class="leo-account-field"><span>Kaspi Partner ID</span><input name="partner_id" maxlength="128" required></label>
          <label class="leo-account-field"><span>Часовой пояс</span><input name="timezone" value="Asia/Almaty" maxlength="64" required></label>
          <label class="leo-account-field"><span>Новый API-токен</span><input name="api_token" type="password" maxlength="4096" placeholder="Оставьте пустым, чтобы не менять"></label>
          <div class="leo-account-actions wide"><button type="submit">Сохранить настройки</button></div>
        </form></details>
        <details><summary>Подключить ещё один аккаунт</summary><form id="leo-account-create-form" class="leo-account-grid">
          <label class="leo-account-field"><span>Название магазина</span><input name="name" maxlength="255" required></label>
          <label class="leo-account-field"><span>Kaspi Partner ID</span><input name="partner_id" maxlength="128" required></label>
          <label class="leo-account-field"><span>Часовой пояс</span><input name="timezone" value="Asia/Almaty" maxlength="64" required></label>
          <label class="leo-account-field"><span>Kaspi API-токен</span><input name="api_token" type="password" maxlength="4096" required autocomplete="off"></label>
          <div class="leo-account-actions wide"><button type="submit">Подключить аккаунт</button></div>
        </form></details>
        <p id="leo-account-status" class="leo-account-status" role="status"></p>
      </div>`;
    document.body.appendChild(dialog);

    const select = dialog.querySelector("#leo-account-select");
    const status = dialog.querySelector("#leo-account-status");
    const editForm = dialog.querySelector("#leo-account-edit-form");
    const createForm = dialog.querySelector("#leo-account-create-form");
    let accounts = [];

    const setStatus = (message, kind = "") => {
      status.textContent = message;
      status.className = `leo-account-status ${kind}`.trim();
    };

    const selectedAccount = () => accounts.find((item) => item.id === Number(select.value));
    const fillEditForm = () => {
      const account = selectedAccount();
      if (!account) return;
      editForm.elements.name.value = account.name || "";
      editForm.elements.partner_id.value = account.partner_id || "";
      editForm.elements.timezone.value = account.timezone || "Asia/Almaty";
      editForm.elements.api_token.value = "";
    };

    const renderAccounts = () => {
      const current = selectedWorkspaceId();
      select.innerHTML = accounts.map((account) => `<option value="${account.id}" ${account.id === current ? "selected" : ""}>${escapeHtml(account.name)}${account.configured ? "" : " — не настроен"}</option>`).join("");
      const active = accounts.find((account) => account.id === current) || accounts[0];
      if (active && active.id !== current) localStorage.setItem(workspaceStorageKey, String(active.id));
      button.textContent = active ? `Аккаунт: ${active.name}` : "Аккаунты Kaspi";
      fillEditForm();
    };

    const loadAccounts = async () => {
      if (!localStorage.getItem(tokenStorageKey)) {
        setStatus("Сначала подключитесь к CRM через SERVICE_API_TOKEN.", "error");
        return;
      }
      setStatus("Загружаю аккаунты…");
      try {
        accounts = await api("/api/workspaces");
        renderAccounts();
        setStatus("");
      } catch (error) {
        setStatus(error instanceof Error ? error.message : "Не удалось загрузить аккаунты.", "error");
      }
    };

    button.addEventListener("click", async () => {
      dialog.showModal();
      await loadAccounts();
    });
    dialog.querySelector(".leo-account-close").addEventListener("click", () => dialog.close());
    select.addEventListener("change", fillEditForm);

    dialog.querySelector("#leo-account-switch").addEventListener("click", () => {
      const account = selectedAccount();
      if (!account) return;
      if (account.id === selectedWorkspaceId()) {
        setStatus("Этот аккаунт уже выбран.", "ok");
        return;
      }
      localStorage.setItem(workspaceStorageKey, String(account.id));
      window.location.reload();
    });

    dialog.querySelector("#leo-account-test").addEventListener("click", async () => {
      const account = selectedAccount();
      if (!account) return;
      setStatus("Проверяю соединение с Kaspi…");
      try {
        await api(`/api/workspaces/${account.id}/test`, {method: "POST"});
        setStatus("Kaspi API отвечает, подключение исправно.", "ok");
      } catch (error) {
        setStatus(error instanceof Error ? error.message : "Kaspi API не отвечает.", "error");
      }
    });

    createForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = Object.fromEntries(new FormData(createForm).entries());
      setStatus("Проверяю и подключаю аккаунт…");
      try {
        const created = await api("/api/workspaces", {method: "POST", body: JSON.stringify(payload)});
        localStorage.setItem(workspaceStorageKey, String(created.id));
        window.location.reload();
      } catch (error) {
        setStatus(error instanceof Error ? error.message : "Не удалось подключить аккаунт.", "error");
      }
    });

    editForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const account = selectedAccount();
      if (!account) return;
      const payload = Object.fromEntries(new FormData(editForm).entries());
      if (!payload.api_token) delete payload.api_token;
      setStatus("Проверяю и сохраняю настройки…");
      try {
        await api(`/api/workspaces/${account.id}`, {method: "PUT", body: JSON.stringify(payload)});
        await loadAccounts();
        setStatus("Настройки сохранены.", "ok");
      } catch (error) {
        setStatus(error instanceof Error ? error.message : "Не удалось сохранить настройки.", "error");
      }
    });

    if (localStorage.getItem(tokenStorageKey)) loadAccounts();
  });
})();
