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

  const render = (agents) => {
    lastAgents = agents;
    const queued = queuedCount();
    const leased = leasedCount();
    const online = agents.length > 0;
    const running = agents.find((agent) => agent.status === "running");
    const claiming = agents.find((agent) => agent.status === "claiming");

    if (!online) {
      if (heading) heading.textContent = "Browser Agent не подключён";
      if (detail) detail.textContent = queued
        ? `В очереди ${queued} задание. Запустите или перезапустите LEO Browser Agent.`
        : "Агент не присылает heartbeat. Запустите LEO Browser Agent.";
      if (queued && !leased) {
        if (runtimeTitle) runtimeTitle.textContent = "Очередь ждёт Browser Agent";
        if (runtimeDetail) runtimeDetail.textContent = "Агент не подключён к CRM или не прошёл проверку токена.";
        if (runtimeAction) runtimeAction.textContent = "Запустите агент";
      }
      return;
    }

    const agent = running || claiming || agents[0];
    if (heading) heading.textContent = `Browser Agent подключён: ${agent.agent_id}`;
    if (detail) {
      if (running) detail.textContent = `Выполняет Job #${running.current_job_id || "—"}.`;
      else if (claiming) detail.textContent = "Проверяет очередь и запрашивает lease.";
      else if (queued) detail.textContent = `Агент онлайн. В очереди ${queued} задание; ожидается получение lease.`;
      else detail.textContent = "Агент онлайн и ожидает новые задания.";
    }
  };

  const poll = async () => {
    const token = localStorage.getItem(storageKey);
    if (!token) return;
    try {
      const response = await fetch("/api/browser-agent/agents", {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      });
      if (!response.ok) return;
      render(await response.json());
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
