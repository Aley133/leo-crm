(() => {
  const storageKey = "leo_crm_service_token";
  const state = document.querySelector("#crm-connection");
  const disconnect = document.querySelector("#disconnect");
  const authPanel = document.querySelector("#auth-panel");
  const page = document.querySelector("#monitoring");
  if (!state || !disconnect || !authPanel) return;

  const render = (mode, text) => {
    state.className = `connection-state ${mode}`;
    const label = state.querySelector("strong");
    if (label) label.textContent = text;
    disconnect.classList.toggle("hidden", mode !== "connected");
  };

  const sync = () => {
    const token = localStorage.getItem(storageKey);
    if (token) {
      render("connected", "CRM подключена");
      authPanel.classList.add("hidden");
    } else {
      render("disconnected", "CRM не подключена");
      authPanel.classList.remove("hidden");
      if (page) page.classList.add("hidden");
    }
  };

  disconnect.addEventListener("click", () => {
    localStorage.removeItem(storageKey);
    sync();
    const input = document.querySelector("#token");
    if (input) input.focus();
  });

  window.addEventListener("storage", sync);
  document.querySelector("#token-form")?.addEventListener("submit", () => setTimeout(sync, 0));
  sync();
})();
