// Kiosk: a clock driven by the server's time, live updates over SSE, and a slow pixel shift
// against burn-in. No controls; nothing here reacts to input.
(() => {
  "use strict";
  const root = document.getElementById("kiosk");
  if (!root) return;
  let serverNow = Date.parse(root.dataset.serverNow);
  let base = performance.now();
  const fmt = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit", hourCycle: "h12" });

  const tick = () => {
    const clock = root.querySelector("[data-clock]");
    if (!clock) return;
    const now = new Date(serverNow + (performance.now() - base));
    clock.textContent = fmt.format(now).replace(/\s?[AP]M$/i, "");
  };
  setInterval(tick, 1000);

  const connect = () => {
    const source = new EventSource(root.dataset.stream);
    source.addEventListener("update", (e) => {
      const data = JSON.parse(e.data);
      serverNow = Date.parse(data.now);
      base = performance.now();
      root.innerHTML = data.html;
      tick();
    });
    source.onerror = () => { source.close(); setTimeout(connect, 10000); };
  };
  connect();

  // Burn-in protection: move the whole layout a few pixels every three minutes.
  const shifts = [[0, 0], [4, 2], [-3, 4], [2, -4], [-4, -2], [3, 3]];
  let step = 0;
  setInterval(() => {
    step = (step + 1) % shifts.length;
    root.style.transform = `translate(${shifts[step][0]}px, ${shifts[step][1]}px)`;
  }, 180000);
})();
