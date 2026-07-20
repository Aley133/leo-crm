(() => {
  const originalFetch = window.fetch.bind(window);
  let monitoringReadQueue = Promise.resolve();

  window.fetch = (input, init = {}) => {
    const url = typeof input === "string" ? input : input?.url || "";
    const method = String(init.method || "GET").toUpperCase();
    const isMonitoringRead = method === "GET" && url.startsWith("/api/monitoring-center/");

    if (!isMonitoringRead) {
      return originalFetch(input, init);
    }

    const request = monitoringReadQueue.then(() => originalFetch(input, init));
    monitoringReadQueue = request.then(
      () => undefined,
      () => undefined,
    );
    return request;
  };
})();
