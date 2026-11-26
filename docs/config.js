const isGitHubPages = window.location.hostname.endsWith(".github.io");
window.ECONOMIC_API_BASE_URL = isGitHubPages
  ? "https://officially-entrance-hurricane-amendments.trycloudflare.com"
  : window.location.origin;
