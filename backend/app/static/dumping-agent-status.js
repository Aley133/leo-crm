(() => {
  const storageKey = "leo_crm_service_token";
  const card = document.querySelector("#agent-source");
  const title = document.querySelector("#agent-source-title");
  const meta = document.querySelector("#agent-source-meta");
  const status = document.querySelector("#agent-source-status");
  if (!card || !title || !meta || !status) return;

  const formatDate = (value) => value ? new Date(value).toLocaleString("ru-RU") : "никогда";

  const render = (payload) => {
    const agent = payload.agents?.[0];
    const online = Boolean(payload.online && agent?.online);
    card.classList.toggle("ready", online);
    card.classList.toggle("missing", !online);
    status.textContent = online ? "Онлайн" : "Офлайн";

    if (!agent) {
      title.textContent = "Агент ещё не подключался";
      meta.textContent = "Запусти LEO-Kaspi-Competitor-Agent.exe и один раз введи SERVICE_API_TOKEN.";
      return;
    }

    title.textContent = online ? `Подключён: ${agent.hostname || agent.agent_id}` : `Нет связи: ${agent.hostname || agent.agent_id}`;
    const version = agent.version ? `версия ${agent.version}` : "версия неизвестна";
    const workers = agent.concurrency ? `потоков ${agent.concurrency}` : "число потоков неизвестно";
    meta.textContent = `${version} · ${workers} · последний heartbeat ${formatDate(agent.last_seen_at)}`;
  };

  const poll = async () => {
    const token = localStorage.getItem(storageKey);
    if (!token) return;
    try {
      const response = await fetch("/api/kaspi-competitor-agent/agents/status", {
        cache: "no-store",
        headers: {Authorization: `Bearer ${token}`},
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (_) {
      card.classList.remove("ready");
      card.classList.add("missing");
      title.textContent = "Не удалось проверить агента";
      meta.textContent = "CRM временно не получила статус локального Kaspi Competitor Agent.";
      status.textContent = "Нет связи";
    }
  };

  poll();
  window.setInterval(poll, 10000);
})();
