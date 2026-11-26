# Economic events server

A dependency-free Python service with a token-protected HTTP update endpoint on port 443 and a public, read-only HTTP dashboard on port 8080. Connected dashboards receive pushed updates as soon as the validated request is published to memory; the browser falls back to a five-second refresh only while the live connection is unavailable.

**Temporary HTTP mode is unencrypted.** Bearer tokens and event data are sent in plaintext and can be observed or modified by parties on the network path. Use this only temporarily on a trusted network; restrict inbound access with a firewall and do not reuse the token elsewhere. Re-enable HTTPS before sending data over untrusted networks.

## Update payload

Send a complete replacement snapshot to `POST /api/updates`:

```json
{
  "events": [
    {
      "name": "Japan CPI",
      "country": "Japan",
      "date": "2026-11-27",
      "time": "18:30",
      "actual": "103.3",
      "forecast": "103.1",
      "previous": "102.9"
    }
  ]
}
```

`name`, ISO `date` (`YYYY-MM-DD`), and 24-hour `time` (`HH:MM`) are required. `country`, `actual`, `forecast`, and `previous` are optional; numeric or string values are accepted for the three reported values. Send the entire current list each time; an empty `events` array clears it. Requests must include `Content-Type: application/json` and `Authorization: Bearer <token>`. JSON bodies are limited to 1 MiB and 1,000 events. The endpoint rejects unexpected fields and malformed values, uses parameterized SQL, and never executes input as code or a command.

## Live update flow

The server loads SQLite once at startup. For each valid update, it first publishes the new snapshot to its in-memory state and pushes it over Server-Sent Events to connected dashboards; only afterward does it replace the SQLite snapshot. Dashboard reads use that in-memory state, not a database query, so disk I/O is not between ingestion and display. SQLite remains the durable restart source. If a database write fails, the dashboard still shows the live update and indicates that it was not saved; the ingestion request returns an error. On restart, only the last successfully saved snapshot can be restored.

There is no periodic SQLite polling: when this service is the only writer, polling the same database every five seconds adds disk work without improving freshness. The five-second browser fallback reads the in-memory snapshot only and runs only if SSE is disconnected.

## Host the dashboard on GitHub Pages

The dashboard files live in `docs/`, which GitHub Pages can publish directly from a branch. In the repository, open **Settings → Pages → Build and deployment**, choose **Deploy from a branch**, select `main` and the `/docs` folder, and save. GitHub will publish it at `https://OWNER.github.io/REPOSITORY/`. Updates to the dashboard are published when you push changes to `docs/`; no Actions workflow or build step is needed. If Pages is currently configured to publish the repository root instead, the root `index.html` redirects visitors to `docs/`; selecting `/docs` in Pages settings is still recommended.

The deployed dashboard reads live data from the configured Cloudflare HTTPS tunnel at `https://officially-entrance-hurricane-amendments.trycloudflare.com`, using `/dashboard-data` and `/live-events`. The tunnel must remain running and forward to this server's dashboard handler on port 8080. Because GitHub Pages is HTTPS, it cannot fetch from the LAN's plain-HTTP address. Add the lowercase Pages origin, `https://OWNER.github.io`, to `ECONOMIC_ALLOWED_ORIGINS` in the server's `.env`, preserving the existing calendar origin as a comma-separated entry, then restart with `bash START.sh`. Replace `OWNER` with your GitHub username. If using a custom Pages domain, allow that exact HTTPS origin instead. The tunnel hostname is part of `docs/config.js`; if it changes, update that file and push the change. GitHub Pages hosts only the static dashboard, not the Python server or SQLite database.

## HTTP deployment with Docker

Run the launcher from Bash in this folder:

```bash
bash START.sh
```

The launcher checks that Docker Compose is running, creates a private `.env` file with a random token on first run, and starts the containers. On Windows, use Git Bash or WSL with Docker Desktop running. The `.env` file is excluded from Git and the Docker build context. To configure your private DevTools copy, read the token locally with `grep '^ECONOMIC_API_TOKEN=' .env` and replace its placeholder; never share or commit that value.

On the current local network, the API is available at `http://192.168.1.174:443/api/updates` and the dashboard at `http://192.168.1.174:8080/`. These addresses are reachable only from devices that can access this LAN address. Allow inbound TCP ports 443 and 8080 in the host firewall; Docker publishes both ports on all host interfaces. Internet access requires separate router port forwarding and use of the router's public IP.

The container runs as an unprivileged user, has a read-only root filesystem, drops Linux capabilities, and mounts only `data/` as writable. This limits access to the host filesystem but is not an absolute security guarantee against container-runtime or kernel vulnerabilities. For stronger isolation, run it in a dedicated, patched VM with no unrelated host folders or credentials mounted.

## Local execution

The application requires Python 3.10+ and no third-party packages. Set `ECONOMIC_API_TOKEN` (at least 32 characters) and `ECONOMIC_ALLOWED_ORIGINS`, then run `python server.py` from this folder. The defaults are API port 443 and dashboard port 8080; port 443 may require an elevated shell. Environment variables `ECONOMIC_BIND_HOST`, `ECONOMIC_API_PORT`, `ECONOMIC_DASHBOARD_PORT`, and `ECONOMIC_ALLOWED_ORIGINS` control the runtime settings. For an unprivileged local run, set the API port to 8081 and dashboard port to 8082; update the collector's `API_URL` to match.

The SQLite database is stored under `data/`. The public dashboard can view event data but cannot change it. Browser-origin requests are accepted only from origins listed in `ECONOMIC_ALLOWED_ORIGINS`; non-browser clients still need the bearer token.

## Chrome DevTools collector

Select the calendar iframe in Chrome DevTools' execution-context menu, then paste a private copy of [client/economic_calendar.user.js](./client/economic_calendar.user.js) into the Console. The script detects the calendar by its `#ecEventsTable` element rather than requiring a particular hostname; if the page origin differs from the configured CORS origin, the console reports which origin the server must allow. Replace `REPLACE_WITH_YOUR_ECONOMIC_API_TOKEN` in the private copy with the server token. The collector uses browser `fetch`, so it needs no userscript manager. It posts to `http://localhost:443`; Chrome and the server must be on the same computer. In Chrome, allow **Local network access** for the calendar site under the site information icon → **Site settings**. After granting permission, reload the page and run the script again. The API request is unencrypted HTTP. If Chrome is on a different device, use HTTPS or a browser extension instead; page JavaScript cannot reliably bypass mixed-content and local-network protections to reach a private HTTP address.

The saved page's selected calendar timezone is Eastern Time. The collector uses the IANA zone `America/New_York` (including daylight-saving changes) and stops if the page's timezone selector is changed. It samples the local clock every 25 ms, but source-page HTTP requests are made only at the configured release offsets. The first offsets are the supplied sequence, followed by 350 ms intervals until 8 seconds; after that, it retries every 2 seconds up to 900 times per event. Simultaneous due events share one refresh request. It requests a fresh copy of the iframe page without navigating away, updates the visible values, and sends a complete today's snapshot on startup and when data changes. Browser background-tab throttling and network delays mean these times cannot be guaranteed to millisecond precision. Confirm the refresh cadence is permitted by the source provider; the script pauses after source HTTP 4xx responses.

## Tests

Run `python -m unittest discover -s tests`.
