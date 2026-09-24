"use strict";

/*
 * Small JSON-lines bridge for the optional PC Toolkit Puppeteer transport.
 *
 * It authenticates in the user's visible Chrome profile, then keeps device
 * requests in a headless Chrome page using that same profile. This preserves
 * the portal's fetch context without leaving a browser window open.
 */

const fs = require("node:fs");
const readline = require("node:readline");

const PORTAL_URL = "https://portal.platform.infraportal.syd.c1.macquarie.com/details/45sf2q7-07c";
const PORTAL_ORIGIN = "https://portal.platform.infraportal.syd.c1.macquarie.com";
const DEVICE_URL = "https://autoscalecomponent.prod-eapi-devices.wkpautoapps.iptauto.syd.c1.macquarie.com/v1/Computers";
const ROLE_URL = `${PORTAL_ORIGIN}/auth/session/maxroles`;
const HEARTBEAT_URL = `${PORTAL_ORIGIN}/auth/session/heartbeat`;
const DEFAULT_ROLE = "maxrole:personal";

let puppeteer;
let browser = null;
let page = null;
let role = "";
let accessToken = "";
let options = {};
let closing = false;

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function progress(stage, details = {}) {
  send({ type: "progress", stage, ...details });
}

function clean(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function safeError(error) {
  const message = clean(error && error.message ? error.message : error);
  return (message || "Puppeteer reported an unknown error.").slice(0, 600);
}

function isBlankUrl(value) {
  const url = String(value || "").toLowerCase();
  return !url || url === "about:blank" || url.startsWith("chrome-error://") || url.startsWith("data:");
}

function isPortalUrl(value) {
  try {
    return new URL(String(value || "")).origin === PORTAL_ORIGIN;
  } catch (_) {
    return false;
  }
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function chromeExecutable(explicit) {
  const candidates = [
    explicit,
    process.env.AUTO_EUDM_CHROME_PATH,
    process.env.CHROME_PATH,
    process.platform === "darwin" ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" : "",
    process.platform === "darwin" ? "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" : "",
    process.platform === "win32" && process.env.PROGRAMFILES ? `${process.env.PROGRAMFILES}\\Google\\Chrome\\Application\\chrome.exe` : "",
    process.platform === "win32" && process.env["PROGRAMFILES(X86)"] ? `${process.env["PROGRAMFILES(X86)"]}\\Google\\Chrome\\Application\\chrome.exe` : "",
    process.platform === "linux" ? "/usr/bin/google-chrome" : "",
    process.platform === "linux" ? "/usr/bin/google-chrome-stable" : "",
    process.platform === "linux" ? "/usr/bin/chromium" : "",
    process.platform === "linux" ? "/usr/bin/chromium-browser" : "",
  ].filter(Boolean);
  return candidates.find((candidate) => {
    try {
      return fs.existsSync(candidate);
    } catch (_) {
      return false;
    }
  }) || "";
}

async function livePages() {
  if (!browser) return [];
  const pages = await browser.pages();
  return pages.filter((candidate) => {
    try {
      return !candidate.isClosed();
    } catch (_) {
      return false;
    }
  });
}

async function cleanBlankPages(keep = null) {
  for (const candidate of await livePages()) {
    if (candidate === keep) continue;
    let candidateUrl = "";
    try {
      candidateUrl = candidate.url();
    } catch (_) {
      continue;
    }
    if (!isBlankUrl(candidateUrl)) continue;
    try {
      await candidate.close();
    } catch (_) {
      // Chrome may close a startup page while a redirect is being committed.
    }
  }
}

async function findPortalPage() {
  for (const candidate of await livePages()) {
    let candidateUrl = "";
    try {
      candidateUrl = candidate.url();
    } catch (_) {
      continue;
    }
    if (isPortalUrl(candidateUrl)) return candidate;
  }
  return null;
}

async function ensurePage() {
  if (page) {
    try {
      if (!page.isClosed()) return page;
    } catch (_) {
      // Select a replacement below.
    }
  }
  const pages = await livePages();
  for (const candidate of pages) {
    try {
      if (isPortalUrl(candidate.url())) {
        page = candidate;
        return page;
      }
    } catch (_) {
      // A page can close between the snapshot and url() call.
    }
  }
  for (const candidate of pages) {
    try {
      if (!isBlankUrl(candidate.url())) {
        page = candidate;
        return page;
      }
    } catch (_) {
      // Try the next page.
    }
  }
  page = pages[0] || await browser.newPage();
  return page;
}

async function navigateToPortal() {
  let candidate = await ensurePage();
  let lastError = null;
  progress("navigation_started", { url: PORTAL_URL });
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    if (closing) throw new Error("Puppeteer connection was cancelled.");
    try {
      if (candidate.isClosed()) {
        candidate = await browser.newPage();
      }
      await candidate.goto(PORTAL_URL, {
        waitUntil: "domcontentloaded",
        timeout: Number(options.navigationTimeoutMs || 30000),
      });
    } catch (error) {
      lastError = error;
      progress("navigation_attempt_failed", { attempt, error: safeError(error) });
    }
    await delay(250);
    const portalPage = await findPortalPage();
    if (portalPage) {
      page = portalPage;
      await cleanBlankPages(page);
      progress("navigation_completed", { url: page.url() });
      return page;
    }
    let currentUrl = "";
    try {
      currentUrl = candidate.url();
    } catch (_) {
      currentUrl = "";
    }
    if (!isBlankUrl(currentUrl)) {
      // This is usually the identity-provider page. Keep the same tab and
      // wait for SSO to redirect it back to the portal.
      page = candidate;
      progress("waiting_for_portal", { url: currentUrl });
      return page;
    }
    if (attempt < 3) {
      progress("retrying_blank_page", { attempt });
      try {
        if (!candidate.isClosed()) await candidate.reload({ waitUntil: "domcontentloaded", timeout: 5000 });
      } catch (_) {
        // The next iteration will navigate the same tab again.
      }
    }
  }
  await cleanBlankPages(null);
  throw new Error(`Chrome opened but the Puppeteer authentication tab remained blank${lastError ? ` (${safeError(lastError)})` : ""}.`);
}

async function waitForPortal(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (closing) throw new Error("Puppeteer connection was cancelled.");
    const portalPage = await findPortalPage();
    if (portalPage) {
      page = portalPage;
      await cleanBlankPages(page);
      return page;
    }
    await delay(500);
  }
  const currentUrl = page ? page.url() : "";
  throw new Error(`PC Toolkit sign-in did not return to the portal${currentUrl ? ` (currently ${currentUrl})` : ""}.`);
}

function decodeCookie(value) {
  try {
    return decodeURIComponent(String(value || ""));
  } catch (_) {
    return String(value || "");
  }
}

async function xsrfToken() {
  if (!page) return "";
  try {
    const cookies = await page.browserContext().cookies(HEARTBEAT_URL);
    for (const cookie of cookies) {
      const name = clean(cookie.name).toLowerCase().replace(/-/g, "_");
      if (name === "xsrf_token" || name === "x_xsrf_token") return decodeCookie(cookie.value);
    }
  } catch (_) {
    return "";
  }
  return "";
}

async function pageFetch(url, request = {}) {
  if (!page || page.isClosed()) throw new Error("The Puppeteer page is no longer available.");
  return page.evaluate(async ({ url: requestUrl, method, headers, body, credentials }) => {
    const response = await fetch(requestUrl, {
      method,
      headers,
      body,
      credentials,
      cache: "no-store",
    });
    return {
      status: response.status,
      url: response.url,
      headers: Object.fromEntries(response.headers.entries()),
      body: await response.text(),
    };
  }, {
    url,
    method: request.method || "GET",
    headers: request.headers || {},
    body: request.body || undefined,
    credentials: request.credentials || "include",
  });
}

function jsonBody(response) {
  try {
    return JSON.parse(response.body || "");
  } catch (_) {
    return null;
  }
}

async function heartbeat(roleName = "") {
  const csrf = await xsrfToken();
  const headers = {
    Accept: "application/json, text/plain, */*",
    "Content-Type": "application/json",
  };
  if (csrf) headers["X-XSRF-Token"] = csrf;
  if (clean(roleName)) headers["X-Max-Elevated-Role"] = clean(roleName);
  const response = await pageFetch(HEARTBEAT_URL, {
    method: "POST",
    headers,
    body: "{}",
    credentials: "include",
  });
  const payload = jsonBody(response);
  const token = payload && typeof payload.token === "string" ? payload.token.trim() : "";
  progress("heartbeat_completed", {
    status: response.status,
    token_present: Boolean(token),
    token_length: token.length,
    role_supplied: Boolean(clean(roleName)),
  });
  return { response, token };
}

async function authenticate(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  if (accessToken && role && page && !page.isClosed() && isPortalUrl(page.url())) return;
  if (!page || page.isClosed() || !isPortalUrl(page.url())) {
    await waitForPortal(Math.max(500, deadline - Date.now()));
  }
  let lastStatus = null;
  let lastHeartbeat = 0;
  progress("authentication_started", { timeout_ms: timeoutMs });
  while (Date.now() < deadline) {
    if (closing) throw new Error("Puppeteer connection was cancelled.");
    if (!isPortalUrl(page.url())) {
      await waitForPortal(Math.min(3000, Math.max(500, deadline - Date.now())));
    }
    try {
      if (Date.now() - lastHeartbeat > 2500) {
        await heartbeat("");
        lastHeartbeat = Date.now();
      }
      const csrf = await xsrfToken();
      const headers = {
        Accept: "application/json, text/plain, */*",
        Referer: page.url() || PORTAL_URL,
        "X-Max-Elevated-Role": DEFAULT_ROLE,
      };
      if (csrf) headers["X-XSRF-Token"] = csrf;
      const roleResponse = await pageFetch(ROLE_URL, {
        headers,
        credentials: "include",
      });
      lastStatus = roleResponse.status;
      const payload = jsonBody(roleResponse);
      const roles = payload && Array.isArray(payload.maxRoles) ? payload.maxRoles : [];
      const selectedRole = roles.map(clean).find(Boolean) || "";
      progress("role_response", {
        status: roleResponse.status,
        role_count: roles.length,
        role_present: Boolean(selectedRole),
      });
      if (selectedRole) {
        const refreshed = await heartbeat(selectedRole);
        if (refreshed.token) {
          role = selectedRole;
          accessToken = refreshed.token;
          progress("authentication_completed", {
            role,
            token_present: true,
            page_url: page.url(),
          });
          return;
        }
      }
    } catch (error) {
      progress("authentication_attempt_failed", { error: safeError(error), status: lastStatus });
    }
    await delay(750);
  }
  throw new Error(`PC Toolkit sign-in did not complete${lastStatus ? ` (portal returned HTTP ${lastStatus})` : ""}.`);
}

async function deviceLookup(query) {
  await authenticate(Number(options.lookupAuthTimeoutMs || 30000));
  const encoded = encodeURIComponent(clean(query));
  const url = `${DEVICE_URL}/${encoded}?sources=cmdb,sccm`;
  const headers = {
    Accept: "application/json, text/plain, */*",
    Authorization: `Bearer ${accessToken}`,
    "X-Max-Elevated-Role": role,
  };
  let response = await pageFetch(url, {
    headers,
    credentials: "omit",
  });
  if (response.status === 401 || response.status === 403) {
    progress("device_api_auth_retry", { status: response.status });
    accessToken = "";
    role = "";
    await authenticate(Number(options.lookupAuthTimeoutMs || 30000));
    response = await pageFetch(url, {
      headers: {
        Accept: "application/json, text/plain, */*",
        Authorization: `Bearer ${accessToken}`,
        "X-Max-Elevated-Role": role,
      },
      credentials: "omit",
    });
  }
  progress("device_api_completed", {
    status: response.status,
    response_body_bytes: String(response.body || "").length,
  });
  return response;
}

async function start(message) {
  if (browser) return { role, page_url: page ? page.url() : "" };
  try {
    puppeteer = require("puppeteer-core");
  } catch (error) {
    throw new Error("Puppeteer is not installed. Run npm install in the AutoEUDM folder, then try again.");
  }
  options = message.options || {};
  const executablePath = chromeExecutable(options.chromeExecutable);
  if (!executablePath) {
    throw new Error("Google Chrome could not be found for Puppeteer. Install Chrome or set AUTO_EUDM_CHROME_PATH.");
  }
  const profile = clean(options.browserProfile);
  if (!profile) throw new Error("Puppeteer needs the dedicated Chrome profile used for Helix.");
  progress("launch_started", {
    executable_path: executablePath,
    browser_profile_configured: true,
    headless: Boolean(options.headless),
  });
  browser = await puppeteer.launch({
    executablePath,
    userDataDir: profile,
    headless: Boolean(options.headless),
    defaultViewport: null,
    args: ["--no-first-run", "--no-default-browser-check"],
  });
  await navigateToPortal();
  await authenticate(Number(options.authTimeoutMs || (options.headless ? 20000 : 120000)));
  if (!options.headless) {
    progress("visible_auth_window_close_started", { reason: "authentication_completed" });
    await closeBrowserSession();
    options.headless = true;
    browser = await puppeteer.launch({
      executablePath,
      userDataDir: profile,
      headless: true,
      defaultViewport: null,
      args: ["--no-first-run", "--no-default-browser-check"],
    });
    await navigateToPortal();
    await authenticate(Number(options.lookupAuthTimeoutMs || 30000));
    progress("headless_lookup_session_ready", {
      role,
      page_url: page ? page.url() : "",
    });
  }
  return {
    role,
    page_url: page ? page.url() : "",
    authenticated: true,
    visible_window_closed: true,
  };
}

async function closeBrowserSession() {
  const current = browser;
  browser = null;
  page = null;
  accessToken = "";
  role = "";
  if (current) {
    try {
      await current.close();
    } catch (_) {
      // The process is exiting; there is nothing useful left to report.
    }
  }
}

async function closeBrowser() {
  closing = true;
  await closeBrowserSession();
}

async function handle(message) {
  const id = message && message.id;
  const command = message && message.command;
  try {
    if (command === "start") {
      send({ id, ok: true, result: await start(message) });
      return;
    }
    if (command === "lookup") {
      const response = await deviceLookup(message.query);
      send({ id, ok: true, result: response });
      return;
    }
    if (command === "health") {
      const refreshed = await heartbeat(role);
      if (refreshed.token) accessToken = refreshed.token;
      send({ id, ok: true, result: { authenticated: Boolean(refreshed.token) } });
      return;
    }
    if (command === "close") {
      await closeBrowser();
      send({ id, ok: true });
      process.exit(0);
      return;
    }
    throw new Error("Unknown Puppeteer command.");
  } catch (error) {
    send({ id, ok: false, error: safeError(error) });
    if (command === "start") await closeBrowser();
  }
}

let commandChain = Promise.resolve();
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch (_) {
    send({ ok: false, error: "Puppeteer bridge received invalid JSON." });
    return;
  }
  commandChain = commandChain.then(() => handle(message));
});
input.on("close", () => {
  commandChain.then(() => closeBrowser()).finally(() => process.exit(0));
});
process.on("SIGTERM", () => {
  closeBrowser().finally(() => process.exit(0));
});
process.on("SIGINT", () => {
  closeBrowser().finally(() => process.exit(0));
});
