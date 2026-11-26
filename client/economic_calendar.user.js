// Run in Chrome DevTools with the calendar iframe selected.
(function () {
  "use strict";

  const API_URL = "http://localhost:443/api/updates";
  const API_TOKEN = "uoGByJ5mKG-OS-cKIF1mqkDs28MZO13YzFfNjodV_Bg";
  const SOURCE_TIME_ZONE = "America/New_York";
  const CHECK_INTERVAL_MS = 25;
  const STATUS_INTERVAL_MS = 3000;
  const FAST_RETRY_OFFSETS_MS = [
    250, 600, 900, 1250, 1600, 1900, 2250, 2600, 2900, 3250, 3600,
  ];
  const FAST_RETRY_STEP_MS = 350;
  const FAST_RETRY_WINDOW_MS = 8000;
  const LATE_RETRY_INTERVAL_MS = 2000;
  const LATE_RETRY_LIMIT = 900;
  const MAX_SOURCE_REQUESTS_IN_FLIGHT = 4;
  const STATE_PREFIX = "economic-calendar-sync-v1:";
  const SOURCE_DATE_FORMATTER = new Intl.DateTimeFormat("en-US", {
    timeZone: SOURCE_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const VALUE_SELECTORS = {
    actual: ".act [class*='-actual']",
    forecast: ".fore [class*='-forecast']",
    previous: ".prev [class*='-previous']",
  };
  const MONTHS = {
    January: "01",
    February: "02",
    March: "03",
    April: "04",
    May: "05",
    June: "06",
    July: "07",
    August: "08",
    September: "09",
    October: "10",
    November: "11",
    December: "12",
  };

  if (!document.querySelector("#ecEventsTable")) {
    const frameUrls = Array.from(document.querySelectorAll("iframe"))
      .map((frame) => frame.src)
      .filter(Boolean);
    console.error(
      `[economic-calendar-sync] This DevTools context (${location.origin}) has no #ecEventsTable. Run the script in the calendar frame's context. Frames on this page: ${frameUrls.join(", ") || "none"}`,
    );
    return;
  }
  console.info(
    `[economic-calendar-sync] Using page origin ${location.origin}; the server must allow this exact origin in ECONOMIC_ALLOWED_ORIGINS.`,
  );
  if (API_TOKEN === "REPLACE_WITH_YOUR_ECONOMIC_API_TOKEN") {
    console.error("[economic-calendar-sync] Set the local API_TOKEN before running.");
    return;
  }

  function fetchText(url, options, timeoutMs) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { ...options, signal: controller.signal })
      .then(async (response) => ({
        status: response.status,
        responseText: await response.text(),
      }))
      .finally(() => window.clearTimeout(timeoutId));
  }

  function dateInSourceZone(timestamp) {
    const values = Object.fromEntries(
      SOURCE_DATE_FORMATTER.formatToParts(timestamp)
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
    return `${values.year}-${values.month}-${values.day}`;
  }

  function dateFromHeader(text) {
    const match = text
      .trim()
      .match(/^(?:[A-Za-z]+,\s+)?([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})$/);
    if (!match || !MONTHS[match[1]]) return null;
    return `${match[3]}-${MONTHS[match[1]]}-${match[2].padStart(2, "0")}`;
  }

  function currentDaySection(root, expectedDate) {
    for (const section of root.querySelectorAll("#ecEventsTable .day-section")) {
      const heading = section.querySelector("h2.theDay");
      if (heading && dateFromHeader(heading.textContent) === expectedDate) return section;
    }
    return null;
  }

  function cleanValue(element) {
    const value = element ? element.textContent.replace(/\u00a0/g, " ").trim() : "";
    return value || null;
  }

  function eventTimestamp(button) {
    const value = button.getAttribute("event_timestamp");
    if (!value) return null;
    const normalized = value.includes("T") ? value : value.replace(" ", "T");
    const withZone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(normalized)
      ? normalized
      : `${normalized}Z`;
    const timestamp = Date.parse(withZone);
    return Number.isFinite(timestamp) ? timestamp : null;
  }

  function readRows(root, day) {
    const section = currentDaySection(root, day);
    if (!section) return null;

    const events = [];
    for (const button of section.querySelectorAll("button.calLine[id^='eventRowId_']")) {
      const timeElement = button.querySelector("time.time");
      const nameElement = button.querySelector(".event");
      const time = timeElement ? timeElement.textContent.trim() : "";
      const name = nameElement ? nameElement.textContent.trim() : "";
      const scheduledAt = eventTimestamp(button);
      if (!name || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time) || scheduledAt === null) {
        continue;
      }

      const countryElement = button.querySelector(".flagCur span[title]");
      events.push({
        sourceId: button.id,
        scheduledAt,
        data: {
          name,
          country: countryElement ? countryElement.title.trim() : "",
          date: day,
          time,
          actual: cleanValue(button.querySelector(VALUE_SELECTORS.actual)),
          forecast: cleanValue(button.querySelector(VALUE_SELECTORS.forecast)),
          previous: cleanValue(button.querySelector(VALUE_SELECTORS.previous)),
        },
      });
    }
    return events;
  }

  function isReported(value) {
    return Boolean(value && !/^(?:[-–—]+|n\/?a|not available)$/i.test(value.trim()));
  }

  function emptyState(day) {
    return {
      day,
      events: {},
      lastSnapshot: null,
      pendingPayload: null,
      lateRetries: {},
    };
  }

  function loadState(day) {
    try {
      const value = sessionStorage.getItem(STATE_PREFIX + day);
      if (!value) return emptyState(day);
      const parsed = JSON.parse(value);
      return parsed && parsed.day === day ? parsed : emptyState(day);
    } catch (error) {
      console.warn("[economic-calendar-sync] Could not restore scheduler state.", error);
      return emptyState(day);
    }
  }

  const today = dateInSourceZone(new Date());
  const storageKey = STATE_PREFIX + today;
  const rows = readRows(document, today);
  if (rows === null) {
    console.error(
      `[economic-calendar-sync] No ${today} section found in the calendar iframe; no data was sent.`,
    );
    return;
  }

  const selectedZone = document.querySelector("select[name='defaulttimezone'] option:checked");
  if (selectedZone && !/Eastern Time \(US & Canada\)/i.test(selectedZone.textContent)) {
    console.error(
      "[economic-calendar-sync] The saved schedule expects Eastern Time. Set the calendar timezone to Eastern Time before running.",
    );
    return;
  }

  const state = loadState(today);
  let sourceRequestsInFlight = 0;
  let apiRequestInFlight = false;
  let apiSendPaused = false;
  let sourceRefreshPaused = false;
  let lastDayCheck = 0;

  function saveState() {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(state));
    } catch (error) {
      console.error("[economic-calendar-sync] Could not save scheduler state.", error);
    }
  }

  function ensureEventState(row) {
    const previous = state.events[row.sourceId];
    if (!previous) {
      state.events[row.sourceId] = {
        scheduledAt: row.scheduledAt,
        fastIndex: 0,
        lateRetries: 0,
        nextLateAt: null,
        done: isReported(row.data.actual),
      };
      return;
    }

    if (Math.abs(previous.scheduledAt - row.scheduledAt) > 1000 && !previous.done) {
      previous.scheduledAt = row.scheduledAt;
      previous.fastIndex = 0;
      previous.lateRetries = 0;
      previous.nextLateAt = null;
    }
    if (isReported(row.data.actual)) previous.done = true;
  }

  function payloadFor(events) {
    return JSON.stringify({
      events: events.map((row) => row.data),
    });
  }

  function sendPendingSnapshot() {
    if (!state.pendingPayload || apiRequestInFlight || apiSendPaused) return;
    apiRequestInFlight = true;
    const sentPayload = state.pendingPayload;
    fetchText(
      API_URL,
      {
        method: "POST",
        mode: "cors",
        credentials: "omit",
        headers: {
          Authorization: `Bearer ${API_TOKEN}`,
          "Content-Type": "application/json",
        },
        body: sentPayload,
      },
      10000,
    )
      .then((response) => {
        apiRequestInFlight = false;
        if (response.status >= 200 && response.status < 300) {
          if (state.pendingPayload === sentPayload) state.pendingPayload = null;
          saveState();
          if (state.pendingPayload) sendPendingSnapshot();
          return;
        }
        if (response.status >= 400 && response.status < 500) {
          apiSendPaused = true;
          console.error(
            `[economic-calendar-sync] Server rejected snapshot (${response.status}); fix the API configuration and reload the calendar frame.`,
          );
        } else {
          console.error(
            `[economic-calendar-sync] Server rejected snapshot (${response.status}); will retry.`,
          );
          window.setTimeout(sendPendingSnapshot, 5000);
        }
      })
      .catch((error) => {
        apiRequestInFlight = false;
        if (error.name === "AbortError") {
          console.error("[economic-calendar-sync] Snapshot request timed out; retrying.");
          window.setTimeout(sendPendingSnapshot, 5000);
          return;
        }
        apiSendPaused = true;
        console.error(
          "[economic-calendar-sync] Browser blocked the API request. Allow Local network access for this page, verify the server, then reload the calendar page.",
          error,
        );
      });
  }

  function queueSnapshot(events) {
    const snapshot = payloadFor(events);
    if (snapshot === state.lastSnapshot && !state.pendingPayload) return;
    state.lastSnapshot = snapshot;
    state.pendingPayload = snapshot;
    saveState();
    sendPendingSnapshot();
  }

  function syncRows(events) {
    for (const row of events) ensureEventState(row);
    saveState();
    queueSnapshot(events);
  }

  function applyFreshValues(events) {
    for (const row of events) {
      const currentButton = document.getElementById(row.sourceId);
      if (!currentButton) continue;
      for (const field of ["actual", "forecast", "previous"]) {
        const freshCell = currentButton.querySelector(VALUE_SELECTORS[field]);
        if (freshCell) freshCell.textContent = row.data[field] || "\u00a0";
      }
    }
  }

  function inspectFreshPage(text) {
    const freshDocument = new DOMParser().parseFromString(text, "text/html");
    const freshRows = readRows(freshDocument, today);
    if (freshRows === null) {
      console.warn("[economic-calendar-sync] Refreshed page did not contain today's event table.");
      return;
    }
    applyFreshValues(freshRows);
    syncRows(freshRows);
  }

  function refreshSource() {
    if (sourceRequestsInFlight >= MAX_SOURCE_REQUESTS_IN_FLIGHT) return;
    sourceRequestsInFlight += 1;
    let completed = false;
    const finish = () => {
      if (completed) return;
      completed = true;
      sourceRequestsInFlight -= 1;
    };

    fetchText(
      window.location.href,
      {
        method: "GET",
        cache: "no-store",
        credentials: "same-origin",
      },
      15000,
    )
      .then((response) => {
        finish();
        if (response.status < 200 || response.status >= 300 || !response.responseText) {
          console.warn(
            `[economic-calendar-sync] Calendar refresh returned HTTP ${response.status}.`,
          );
          if (response.status >= 400 && response.status < 500) {
            sourceRefreshPaused = true;
            console.error(
              "[economic-calendar-sync] Source refreshes paused after a client error; check access and provider limits.",
            );
          }
          return;
        }
        inspectFreshPage(response.responseText);
      })
      .catch((error) => {
        finish();
        console.warn(
          error.name === "AbortError"
            ? "[economic-calendar-sync] Calendar refresh timed out."
            : "[economic-calendar-sync] Calendar refresh failed.",
          error,
        );
      });
  }

  function fastOffsets() {
    const offsets = FAST_RETRY_OFFSETS_MS.slice();
    let next = offsets[offsets.length - 1] + FAST_RETRY_STEP_MS;
    while (next < FAST_RETRY_WINDOW_MS) {
      offsets.push(next);
      next += FAST_RETRY_STEP_MS;
    }
    return offsets;
  }

  const offsets = fastOffsets();

  function nextAttemptAt(eventState, now) {
    if (eventState.done || eventState.scheduledAt > now) return Infinity;
    if (eventState.fastIndex < offsets.length) {
      return eventState.scheduledAt + offsets[eventState.fastIndex];
    }
    if (eventState.lateRetries >= LATE_RETRY_LIMIT) return Infinity;
    return eventState.nextLateAt || eventState.scheduledAt + FAST_RETRY_WINDOW_MS;
  }

  function checkSchedule() {
    const now = Date.now();
    if (now - lastDayCheck >= 1000) {
      lastDayCheck = now;
      if (dateInSourceZone(new Date(now)) !== today) {
        window.location.reload();
        return;
      }
    }

    if (sourceRefreshPaused || sourceRequestsInFlight >= MAX_SOURCE_REQUESTS_IN_FLIGHT) return;
    const due = Object.values(state.events).filter((eventState) => nextAttemptAt(eventState, now) <= now);
    if (due.length === 0) return;

    for (const eventState of due) {
      if (eventState.fastIndex < offsets.length) {
        eventState.fastIndex += 1;
      } else {
        eventState.lateRetries += 1;
        eventState.nextLateAt = now + LATE_RETRY_INTERVAL_MS;
      }
    }
    saveState();
    refreshSource();
  }

  if (state.pendingPayload) sendPendingSnapshot();
  for (const row of rows) ensureEventState(row);
  saveState();
  queueSnapshot(rows);

  const eventTable = document.querySelector("#ecEventsTable");
  if (eventTable) {
    const observer = new MutationObserver(() => {
      const changedRows = readRows(document, today);
      if (changedRows !== null) syncRows(changedRows);
    });
    observer.observe(eventTable, { childList: true, subtree: true, characterData: true });
  }

  window.setInterval(checkSchedule, CHECK_INTERVAL_MS);
  window.setInterval(() => {
    const apiStatus = apiSendPaused
      ? "paused"
      : apiRequestInFlight
        ? "sending"
        : "ready";
    const sourceStatus = sourceRefreshPaused ? "paused" : "active";
    console.info(
      `[economic-calendar-sync] Still running; monitoring ${rows.length} events; API ${apiStatus}; source refresh ${sourceStatus}.`,
    );
  }, STATUS_INTERVAL_MS);
  console.info(
    `[economic-calendar-sync] Monitoring ${rows.length} events for ${today} (${SOURCE_TIME_ZONE}); source refresh limit is ${LATE_RETRY_LIMIT} late attempts per event.`,
  );
})();
