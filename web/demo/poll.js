// Demo state requests omit cookies and leave the browser connection free between polls.
export function pollDemoState({ path, accept, connection }) {
  let request;
  let timer;
  let failures = 0;
  let connected = false;
  const unreachable = `Can't reach the demo server at ${location.host}`;
  const firstAnswer = setTimeout(() => connection("Waiting for demo state...", unreachable), 5000);

  async function poll() {
    clearTimeout(timer);
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const deadline = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(`/demo/api/${path()}`, {
        credentials: "omit", cache: "no-store", signal: controller.signal
      });
      if (!response.ok) throw new Error("Demo state request failed.");
      const next = await response.json();
      if (request !== controller) return;
      if (next?.type !== "state") throw new Error("Invalid demo state.");
      accept(next);
      clearTimeout(firstAnswer);
      failures = 0;
      connected = true;
      connection("Live", "");
    } catch {
      if (request !== controller) return;
      clearTimeout(firstAnswer);
      failures += 1;
      if (failures >= 2) connection("Reconnecting...", unreachable);
      else if (!connected) connection("Waiting for demo state...", unreachable);
    } finally {
      clearTimeout(deadline);
      if (request === controller) {
        request = null;
        timer = setTimeout(poll, document.hidden ? 3000 : 750);
      }
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
    else if (!request) {
      clearTimeout(timer);
      timer = setTimeout(poll, 3000);
    }
  });
  poll();
  return poll;
}
