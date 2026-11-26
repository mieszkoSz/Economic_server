const body = document.querySelector("#events");
const count = document.querySelector("#event-count");
const updated = document.querySelector("#updated-at");
const notice = document.querySelector("#notice");
const apiBaseUrl = (window.ECONOMIC_API_BASE_URL || "").replace(/\/+$/, "");

function apiUrl(path) {
  return `${apiBaseUrl}${path}`;
}

function cell(text, className) {
  const element = document.createElement("td");
  element.textContent = text || "—";
  if (className) element.className = className;
  return element;
}

function render(events) {
  body.replaceChildren();
  count.textContent = `${events.length} ${events.length === 1 ? "release" : "releases"}`;
  if (events.length === 0) {
    const row = document.createElement("tr");
    const empty = cell("No events available yet.", "empty");
    empty.colSpan = 5;
    row.append(empty);
    body.append(row);
    return;
  }

  for (const event of events) {
    const row = document.createElement("tr");
    const release = document.createElement("td");
    const name = document.createElement("span");
    name.className = "event-name";
    name.textContent = event.name;
    release.append(name);
    if (event.country) {
      const country = document.createElement("span");
      country.className = "country";
      country.textContent = event.country;
      release.append(country);
    }
    row.append(
      release,
      cell(`${event.date} · ${event.time}`, "date-cell"),
      cell(event.actual, event.actual ? "actual" : ""),
      cell(event.forecast),
      cell(event.previous),
    );
    body.append(row);
  }
}

function renderSnapshot(snapshot) {
  render(snapshot.events);
  notice.textContent = snapshot.persisted
    ? ""
    : "Live data is visible, but its latest update has not been saved to the database.";
  updated.textContent = `Updated ${new Date(snapshot.updated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

async function refresh() {
  if (!apiBaseUrl) return;
  try {
    const response = await fetch(apiUrl("/dashboard-data"), { cache: "no-store" });
    if (!response.ok) throw new Error("Unable to load events");
    renderSnapshot(await response.json());
  } catch {
    notice.textContent = "Calendar data is temporarily unavailable. Retrying shortly.";
    updated.textContent = "Connection issue";
  }
}

if (!apiBaseUrl) {
  notice.textContent = "Dashboard data endpoint is not configured.";
  updated.textContent = "Configuration required";
} else {
  const liveUpdates = new EventSource(apiUrl("/live-events"));
  liveUpdates.onmessage = (message) => {
    try {
      renderSnapshot(JSON.parse(message.data));
    } catch {
      notice.textContent = "Received an invalid live update. Reconnecting…";
    }
  };
  liveUpdates.onerror = () => {
    notice.textContent = "Live connection interrupted. Reconnecting…";
  };

  window.setInterval(() => {
    if (liveUpdates.readyState !== EventSource.OPEN) refresh();
  }, 5000);
}
