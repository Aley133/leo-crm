(() => {
  const storageKey = "leo_crm_service_token";
  const idleCard = document.querySelector("#leased-empty");
  const runtimeTitle = document.querySelector("#runtime-health-title");
  const runtimeDetail = document.querySelector("#runtime-health-detail");
  const runtimeAction = document.querySelector("#runtime-health-action");
  if (!idleCard) return;

  const heading = idleCard.querySelector("strong");
  const detail = document.querySelector("#agent-idle-detail");
  let lastAgents = [];

  const queuedCount = () => Number(document.querySelector("#jobs-queued")?.textContent || 0);
  const leasedCount = () => Number(document.querySelector("#jobs-leased")?.textContent || 0);

  const render = (browserAgents) => {
    lastAgents = browserAgents;
    const queued = queuedCount();
    const leased = leasedCount();
    const browserOnline = browserAgents.length > 0;
    const running = browserAgents.find((agent) => agent.status === "running");
    const claiming = browserAgents.find((agent) => agent.status === "claiming");

    if (!browserOnline) {
      if (heading) heading.textContent = "Ozon/WB Browser Agent не подключён";
      if (detail) detail.textContent = queued
        ? `В очереди ${queued} задание. Запустите Ozon/WB Browser Agent.`
        : "CRM не получает heartbeat от Ozon/WB Browser Agent.";
      if (!leased) {
        if (runtimeTitle) runtimeTitle.textContent = queued ? "Очередь ждёт Browser Agent" : "Локальные агенты не подключены";
        if (runtimeDetail) runtimeDetail.textContent = queued
          ? "Агент не подключён к CRM или не прошёл проверку токена."
          : "CRM не получает heartbeat от Ozon/WB Browser Agent.";
        if (runtimeAction) runtimeAction.textContent = "Запустите Browser Agent";
      }
      return;
    }

    const browserAgent = running || claiming || browserAgents[0];
    if (heading) heading.textContent = "Ozon/WB Browser Agent подключён";
    if (detail) detail.textContent = `${browserAgent.agent_id} — онлайн.`;
  };

  const poll = async () => {
    const token = localStorage.getItem(storageKey);
    if (!token) return;
    try {
      const headers = { Authorization: `Bearer ${token}` };
      const browserResponse = await fetch("/api/browser-agent/agents", { headers, cache: "no-store" });
      const browserAgents = browserResponse.ok ? await browserResponse.json() : [];
      render(browserAgents);
    } catch {
      render([]);
    }
  };

  const observer = new MutationObserver(() => render(lastAgents));
  const queuedMetric = document.querySelector("#jobs-queued");
  const leasedMetric = document.querySelector("#jobs-leased");
  if (queuedMetric) observer.observe(queuedMetric, { childList: true, subtree: true });
  if (leasedMetric) observer.observe(leasedMetric, { childList: true, subtree: true });

  poll();
  setInterval(poll, 5000);
})();
