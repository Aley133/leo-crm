(() => {
  const storageKey = "leo_crm_service_token";
  const idleCard = document.querySelector("#leased-empty");
  const runtimeTitle = document.querySelector("#runtime-health-title");
  const runtimeDetail = document.querySelector("#runtime-health-detail");
  const runtimeAction = document.querySelector("#runtime-health-action");
  if (!idleCard) return;

  const heading = idleCard.querySelector("strong");
  const detail = document.querySelector("#agent-idle-detail");
  let lastState = { browserAgents: [], kaspiStatus: null };

  const queuedCount = () => Number(document.querySelector("#jobs-queued")?.textContent || 0);
  const leasedCount = () => Number(document.querySelector("#jobs-leased")?.textContent || 0);

  const render = (browserAgents, kaspiStatus) => {
    lastState = { browserAgents, kaspiStatus };
    const queued = queuedCount();
    const leased = leasedCount();
    const browserOnline = browserAgents.length > 0;
    const kaspiAgent = kaspiStatus?.agents?.find((agent) => agent.online);
    const kaspiOnline = Boolean(kaspiStatus?.online && kaspiAgent);
    const running = browserAgents.find((agent) => agent.status === "running");
    const claiming = browserAgents.find((agent) => agent.status === "claiming");

    if (!browserOnline && !kaspiOnline) {
      if (heading) heading.textContent = "Локальные агенты не подключены";
      if (detail) detail.textContent = queued
        ? `В очереди ${queued} задание. Запустите Ozon/WB Browser Agent и Kaspi Agent.`
        : "CRM не получает heartbeat от Ozon/WB Browser Agent и Kaspi Agent.";
      if (!leased) {
        if (runtimeTitle) runtimeTitle.textContent = queued ? "Очередь ждёт Browser Agent" : "Локальные агенты не подключены";
        if (runtimeDetail) runtimeDetail.textContent = queued
          ? "Агент не подключён к CRM или не прошёл проверку токена."
          : "CRM не получает heartbeat от Ozon/WB и Kaspi.";
        if (runtimeAction) runtimeAction.textContent = "Запустите агенты";
      }
      return;
    }

    const browserAgent = running || claiming || browserAgents[0];
    if (heading) {
      heading.textContent = browserOnline && kaspiOnline
        ? "Ozon/WB и Kaspi подключены"
        : browserOnline
          ? "Ozon/WB подключён, Kaspi офлайн"
          : "Kaspi подключён, Ozon/WB офлайн";
    }
    if (detail) {
      const browserText = browserOnline
        ? `Ozon/WB: ${browserAgent.agent_id} — онлайн`
        : "Ozon/WB: нет связи";
      const kaspiText = kaspiOnline
        ? `Kaspi: ${kaspiAgent.hostname || kaspiAgent.agent_id} — онлайн`
        : "Kaspi: нет связи";
      detail.textContent = `${browserText}. ${kaspiText}.`;
    }
    if (queued && !browserOnline && !leased) {
      if (runtimeTitle) runtimeTitle.textContent = "Очередь ждёт Browser Agent";
      if (runtimeDetail) runtimeDetail.textContent = "Ozon/WB-агент не подключён к CRM.";
      if (runtimeAction) runtimeAction.textContent = "Запустите Browser Agent";
    }
  };

  const poll = async () => {
    const token = localStorage.getItem(storageKey);
    if (!token) return;
    try {
      const headers = { Authorization: `Bearer ${token}` };
      const [browserResponse, kaspiResponse] = await Promise.all([
        fetch("/api/browser-agent/agents", { headers, cache: "no-store" }),
        fetch("/api/kaspi-competitor-agent/agents/status", { headers, cache: "no-store" }),
      ]);
      const browserAgents = browserResponse.ok ? await browserResponse.json() : [];
      const kaspiStatus = kaspiResponse.ok ? await kaspiResponse.json() : null;
      render(browserAgents, kaspiStatus);
    } catch {
      render([], null);
    }
  };

  const observer = new MutationObserver(() => render(lastState.browserAgents, lastState.kaspiStatus));
  const queuedMetric = document.querySelector("#jobs-queued");
  const leasedMetric = document.querySelector("#jobs-leased");
  if (queuedMetric) observer.observe(queuedMetric, { childList: true, subtree: true });
  if (leasedMetric) observer.observe(leasedMetric, { childList: true, subtree: true });

  poll();
  setInterval(poll, 5000);
})();
