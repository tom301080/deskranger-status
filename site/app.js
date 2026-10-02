// Refreshes the status from the probe's latest data (the `data` branch) every
// minute; the page itself is complete without this script.
(() => {
  const main = document.querySelector("main[data-raw]");
  if (!main || !window.fetch) return;
  const labels = JSON.parse(main.dataset.labels);
  const lang = main.dataset.lang;
  const rank = { up: 0, degraded: 1, maintenance: 1, down: 2, unknown: -1 };
  const banners = { up: "all_up", degraded: "some_degraded", down: "some_down", maintenance: "maintenance", unknown: "no_data" };

  const format = (iso) => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat(lang === "de" ? "de-DE" : "en-GB", {
      timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short",
    }).formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
    return lang === "de"
      ? `${parts.day}.${parts.month}.${parts.year}, ${parts.hour}:${parts.minute} Uhr`
      : `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} ${parts.timeZoneName}`;
  };

  const apply = (current) => {
    if (!current || !current.components) return;
    let top = main.dataset.floor || "up";
    for (const [id, component] of Object.entries(current.components)) {
      const row = main.querySelector(`[data-component="${CSS.escape(id)}"] [data-state]`);
      if (row) {
        row.className = `state ${component.status}`;
        row.textContent = labels[component.status] || labels.unknown;
      }
      if (rank[component.status] > rank[top]) top = component.status;
    }
    const banner = main.querySelector("[data-banner]");
    banner.className = `banner ${top}`;
    main.querySelector("[data-banner-text]").textContent = labels[banners[top]];
    main.querySelector("[data-checked]").textContent = format(current.checked_at);
  };

  const refresh = () =>
    fetch(`${main.dataset.raw}?t=${Date.now()}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then(apply)
      .catch(() => {});
  refresh();
  setInterval(refresh, 60_000);
})();
