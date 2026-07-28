const state = {
  config: null,
  preferences: {},
  queue: [],
  selectedId: null,
  connection: null,
  workbook: null,
  workbookInspection: null,
  importPreview: null,
  importUploadToken: 0,
  importLoginJobId: null,
  importExpandedGroups: new Set(),
  currentJob: null,
  pollTimer: null,
  connectionHeartbeatTimer: null,
  liveOptionsLoaded: false,
  pasteLocation: null,
  pasteLocationResults: [],
  pasteEntries: [],
  importLocation: null,
  importLocationResults: [],
  locationCache: new Map(),
  locationLoading: new Map(),
  recordedLocationJobs: new Set(),
  newRequest: null,
};

const THEME_STORAGE_KEY = "auto-eudm-theme";
const RECENT_LOCATIONS_STORAGE_KEY = "auto-eudm-recent-locations";
const CONCURRENCY_STORAGE_KEY = "auto-eudm-concurrency";
const IMPORT_COLUMNS_STORAGE_KEY = "auto-eudm-import-columns";
const IMPORT_LOCATION_STORAGE_KEY = "auto-eudm-import-location";
const SPREADSHEET_URL_STORAGE_KEY = "auto-eudm-spreadsheet-url";
const SPREADSHEET_HEADLESS_STORAGE_KEY = "auto-eudm-spreadsheet-headless";
const BULK_SERIAL_VALIDATION_STORAGE_KEY = "auto-eudm-validate-bulk-serials";
const MAX_RECENT_LOCATIONS = 8;
const IMPORT_PREVIEW_ROW_LIMIT = 80;
const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const elements = {
  connectionBadge: $("#connectionBadge"),
  connectButton: $("#connectButton"),
  concurrency: $("#concurrencyInput"),
  queueEmpty: $("#queueEmpty"),
  queueTableWrap: $("#queueTableWrap"),
  queueBody: $("#queueBody"),
  queueCounts: $("#queueCounts"),
  connectionGate: $("#connectionGate"),
  queueValidationNotice: $("#queueValidationNotice"),
  queueValidationMessage: $("#queueValidationMessage"),
  connectionGateTitle: $("#connectionGateTitle"),
  connectionGateMessage: $("#connectionGateMessage"),
  connectionGateButton: $("#connectionGateButton"),
  historyButton: $("#historyButton"),
  historyList: $("#historyList"),
  reviewButton: $("#reviewButton"),
  clearQueueButton: $("#clearQueueButton"),
  inspectorEmpty: $("#inspectorEmpty"),
  inspectorContent: $("#inspectorContent"),
  serialInput: $("#serialInput"),
  serialsInput: $("#serialsInput"),
  serialLabel: $("#serialLabel"),
  serialHint: $("#serialHint"),
  statusInput: $("#statusInput"),
  userFields: $("#userFields"),
  userInput: $("#userInput"),
  locationFields: $("#locationFields"),
  cityInput: $("#cityInput"),
  locationInput: $("#locationInput"),
  locationDetail: $("#locationDetail"),
  returningUserFields: $("#returningUserFields"),
  returningToggle: $("#returningToggle"),
  returningSearch: $("#returningSearch"),
  returningUserInput: $("#returningUserInput"),
  returnConfirmation: $("#returnConfirmation"),
  validationPanel: $("#validationPanel"),
  serialResults: $("#serialResults"),
  userResults: $("#userResults"),
  returningResults: $("#returningResults"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function uid() {
  return crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`;
}

function configureConcurrency(defaultValue) {
  const configured = Math.max(1, Math.min(50, Number(defaultValue) || 1));
  let remembered = null;
  const persisted = Number(state.preferences?.concurrency);
  if (state.preferences?._saved && Number.isInteger(persisted) && persisted >= 1 && persisted <= 50) {
    remembered = persisted;
  }
  try {
    const candidate = Number(localStorage.getItem(CONCURRENCY_STORAGE_KEY));
    if (remembered === null && Number.isInteger(candidate) && candidate >= 1 && candidate <= 50) {
      remembered = candidate;
    }
  } catch (_) {}
  elements.concurrency.innerHTML = Array.from(
    { length: 50 },
    (_, index) => `<option value="${index + 1}">${index + 1}</option>`,
  ).join("");
  elements.concurrency.value = String(remembered ?? configured);
}

function requestIdDisplay(requestId, className) {
  if (!requestId) return "";
  const id = escapeHtml(requestId);
  return `<span class="${className}"><span>Request ID</span><strong>${id}</strong><button class="copy-request-id" type="button" data-copy-request-id="${id}" aria-label="Copy request ID ${id}" title="Copy request ID">Copy</button></span>`;
}

async function copyRequestId(button) {
  const value = button.dataset.copyRequestId || "";
  if (!value) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      const input = document.createElement("textarea");
      input.value = value;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.append(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      if (!copied) throw new Error("Copy was not available.");
    }
    button.textContent = "Copied";
    button.classList.add("copied");
    window.setTimeout(() => {
      button.textContent = "Copy";
      button.classList.remove("copied");
    }, 1200);
  } catch (_) {
    toast("Could not copy the request ID. Select and copy it manually.", "error");
  }
}

function parseSerials(raw) {
  return String(raw || "").split(/[\s,;]+/).map((value) => value.trim()).filter(Boolean);
}

function selectedRequest() {
  return state.newRequest || state.queue.find((request) => request.id === state.selectedId) || null;
}

function toast(message, type = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toastRegion").append(node);
  setTimeout(() => node.remove(), 4300);
}

function savedTheme() {
  const value = document.documentElement.dataset.theme;
  return value === "light" || value === "dark" ? value : null;
}

function effectiveTheme() {
  return savedTheme() || (systemTheme.matches ? "dark" : "light");
}

function updateThemeButton() {
  const dark = effectiveTheme() === "dark";
  const overridden = Boolean(savedTheme());
  $("#themeIcon").textContent = overridden ? "↺" : (dark ? "☀" : "☾");
  $("#themeLabel").textContent = overridden
    ? "Use system"
    : (dark ? "Light mode" : "Dark mode");
  $("#themeToggle").setAttribute(
    "aria-label",
    overridden
      ? "Use system appearance"
      : `Switch to ${dark ? "light" : "dark"} mode`,
  );
  $("#themeToggle").title = overridden
    ? "Return to the system appearance"
    : `Following the system appearance. Switch to ${dark ? "light" : "dark"} mode`;
  $("#themeToggle").setAttribute("aria-pressed", String(dark));
}

function toggleTheme() {
  const root = document.documentElement;
  if (savedTheme()) {
    delete root.dataset.theme;
    try {
      localStorage.removeItem(THEME_STORAGE_KEY);
    } catch (_) {}
  } else {
    const nextTheme = effectiveTheme() === "dark" ? "light" : "dark";
    root.dataset.theme = nextTheme;
    try {
      localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
    } catch (_) {}
  }
  updateThemeButton();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({ error: "The local server returned an unreadable response." }));
  if (!response.ok) {
    if (payload.connection) updateConnection(payload.connection);
    const error = new Error(payload.error || "The request could not be completed.");
    error.payload = payload;
    throw error;
  }
  return payload;
}

function emptyLocation() {
  return { city: "", building: "", floor: "", room: "", cabinet: "" };
}

function recentLocations() {
  try {
    const saved = JSON.parse(localStorage.getItem(RECENT_LOCATIONS_STORAGE_KEY) || "[]");
    return Array.isArray(saved)
      ? saved.filter(hasCompleteLocation).slice(0, MAX_RECENT_LOCATIONS)
      : [];
  } catch (_) {
    return [];
  }
}

function preferredLocation() {
  const configured = state.config?.default_location;
  return structuredClone(
    recentLocations()[0]
      || (hasCompleteLocation(configured) ? configured : emptyLocation()),
  );
}

function importColumns() {
  const persisted = state.preferences?.import_columns;
  if (state.preferences?._saved && persisted && typeof persisted === "object" && Object.keys(persisted).length) {
    return structuredClone(persisted);
  }
  try {
    const saved = JSON.parse(localStorage.getItem(IMPORT_COLUMNS_STORAGE_KEY) || "{}");
    return saved && typeof saved === "object" && Object.keys(saved).length ? saved : null;
  } catch (_) { return null; }
}

function preferredImportLocation() {
  try {
    const saved = JSON.parse(localStorage.getItem(IMPORT_LOCATION_STORAGE_KEY) || "null");
    if (hasCompleteLocation(saved)) return saved;
  } catch (_) {}
  return preferredLocation();
}

function savedSpreadsheetUrl() {
  if (state.preferences?._saved && typeof state.preferences?.workbook_url === "string") {
    return state.preferences.workbook_url.trim();
  }
  try { return String(localStorage.getItem(SPREADSHEET_URL_STORAGE_KEY) || "").trim(); } catch (_) { return ""; }
}

function spreadsheetDownloadHeadless() {
  if (state.preferences?._saved && typeof state.preferences?.workbook_headless === "boolean") {
    return state.preferences.workbook_headless;
  }
  try {
    const saved = localStorage.getItem(SPREADSHEET_HEADLESS_STORAGE_KEY);
    return saved === null ? true : saved === "true";
  } catch (_) {
    return true;
  }
}

function bulkSerialValidationEnabled() {
  if (state.preferences?._saved && typeof state.preferences?.validate_bulk_serials === "boolean") {
    return state.preferences.validate_bulk_serials;
  }
  try { return localStorage.getItem(BULK_SERIAL_VALIDATION_STORAGE_KEY) === "true"; } catch (_) { return false; }
}

function rememberImportLocation(location) {
  if (!hasCompleteLocation(location)) return;
  try { localStorage.setItem(IMPORT_LOCATION_STORAGE_KEY, JSON.stringify(location)); } catch (_) {}
}

function rememberLocation(location) {
  if (!hasCompleteLocation(location)) return;
  const key = locationDisplay(location).toLowerCase();
  const locations = [structuredClone(location), ...recentLocations().filter((item) => locationDisplay(item).toLowerCase() !== key)]
    .slice(0, MAX_RECENT_LOCATIONS);
  try {
    localStorage.setItem(RECENT_LOCATIONS_STORAGE_KEY, JSON.stringify(locations));
  } catch (_) {}
}

function hasCompleteLocation(location) {
  return [location?.city, location?.building, location?.floor, location?.room]
    .every((value) => String(value || "").trim());
}

function locationDisplay(location) {
  return [location?.city, location?.building, location?.floor, location?.room, location?.cabinet]
    .filter(Boolean)
    .join(" → ");
}

function resolveStatus(options, candidate, fallback = "") {
  const wanted = String(candidate || "").trim().toLowerCase();
  const fallbackValue = String(fallback || "").trim().toLowerCase();
  const match = options.find((option) => option.value.toLowerCase() === wanted)
    || options.find((option) => option.label.toLowerCase() === wanted)
    || options.find((option) => option.value.toLowerCase() === fallbackValue)
    || options.find((option) => option.label.toLowerCase() === fallbackValue)
    || options[0];
  return match?.value || "";
}

function normalizeRequestStatus(request) {
  const user = request.kind === "user";
  const options = user ? state.config.user_statuses : state.config.location_statuses;
  const fallback = user ? state.config.default_user_status : state.config.default_location_status;
  request.status = resolveStatus(options, request.status, fallback);
  return request.status;
}

function makeRequest(kind) {
  const user = kind === "user";
  const request = {
    id: uid(),
    kind,
    serials: [],
    status: "",
    user: "",
    returning: false,
    returning_user: "",
    location: user ? null : preferredLocation(),
    group: kind === "bulk_location" ? "Bulk add to location stock" : user ? "Deploy to user" : "Add to location stock",
    source: "",
  };
  normalizeRequestStatus(request);
  return request;
}

function userStatusValues() {
  return new Set(state.config.user_statuses.map((option) => option.value));
}

function locationStatusValues() {
  return new Set(state.config.location_statuses.map((option) => option.value));
}

function singleRequestStatusOptions() {
  return [...state.config.user_statuses, ...state.config.location_statuses];
}

function kindForStatus(status, bulk = false) {
  if (bulk) return "bulk_location";
  return userStatusValues().has(status) ? "user" : "location";
}

function applyInferredKind(request, status, bulk = request.kind === "bulk_location") {
  const kind = kindForStatus(status, bulk);
  const changed = request.kind !== kind;
  const wasUser = request.kind === "user";
  request.kind = kind;
  request.status = status;
  request.group = kind === "user"
    ? "Deploy to user"
    : kind === "bulk_location"
      ? "Bulk add to location stock"
      : "Add to location stock";
  if (kind === "user") {
    request.location = null;
    request.returning = false;
    request.returning_user = "";
    request.returning_user_info = null;
    request.serials = request.serials.slice(0, 1);
  } else if (changed) {
    request.user = "";
    request.location = wasUser || !request.location ? preferredLocation() : request.location;
    if (kind === "location") request.serials = request.serials.slice(0, 1);
    if (kind === "bulk_location") {
      request.returning = false;
      request.returning_user = "";
      request.returning_user_info = null;
    }
  }
}

function validateRequest(request) {
  const errors = [];
  if (!["user", "location", "bulk_location"].includes(request.kind)) {
    return ["Choose Deploy to user, Add to location stock, or Bulk add to location stock."];
  }
  if (!request.serials.length) errors.push("Enter at least one serial number.");
  if (request.kind === "bulk_location" && bulkSerialValidationEnabled() && request.bulk_validation === "failed") {
    errors.push(request.bulk_validation_error || "One or more serial numbers could not be verified in EUDM.");
  }
  if (request.kind !== "bulk_location" && request.serials.length !== 1) {
    errors.push("This request must contain exactly one serial number.");
  }
  const seen = new Set();
  for (const serial of request.serials) {
    if (!/^[A-Za-z0-9._-]{6,}$/.test(serial)) {
      errors.push(`Serial ${serial} must have at least six letters, numbers, dots, underscores, or hyphens.`);
      break;
    }
    const key = serial.toLowerCase();
    if (seen.has(key)) {
      errors.push("Remove duplicate serial numbers from this request.");
      break;
    }
    seen.add(key);
  }
  if (request.kind === "user") {
    if (!userStatusValues().has(request.status)) errors.push("Choose a status for Deploy to user.");
    if (!request.user.trim()) errors.push("Choose the user receiving the device.");
    if (request.user && !/^[A-Za-z][A-Za-z0-9._-]*$/.test(request.user.trim())) {
      errors.push("The receiving username is not in a valid login ID format.");
    }
    if (request.returning || request.returning_user) errors.push("Deploy to user cannot include a returning user.");
    if (request.location) errors.push("Deploy to user cannot include a location.");
  } else {
    if (!locationStatusValues().has(request.status)) errors.push("Choose a status for Add to location stock.");
    if (request.user) errors.push("Add to location stock cannot include a deployed-to user.");
    const location = request.location || {};
    if (![location.city, location.building, location.floor, location.room].every((value) => String(value || "").trim())) {
      errors.push("Choose both the city and the location.");
    }
    if (request.returning && !request.returning_user.trim()) {
      errors.push("Choose the returning user or turn off the return option.");
    }
    if (request.returning_user && !/^[A-Za-z][A-Za-z0-9._-]*$/.test(request.returning_user.trim())) {
      errors.push("The returning username is not in a valid login ID format.");
    }
    if (request.returning_user && !request.returning_user_info && !request.returning_user_loading) {
      errors.push("Search and verify the returning user's details before submitting; an email will be sent to them.");
    }
    if (request.kind === "bulk_location" && request.returning) {
      errors.push("Bulk add to location stock cannot include a returning user.");
    }
  }
  return errors;
}

function queueValidation() {
  const errors = new Map(state.queue.map((request) => [request.id, validateRequest(request)]));
  const owners = new Map();
  for (const request of state.queue) {
    for (const serial of request.serials) {
      const key = serial.toLowerCase();
      if (!owners.has(key)) owners.set(key, []);
      owners.get(key).push(request.id);
    }
  }
  for (const [serial, ids] of owners) {
    if (ids.length > 1) {
      for (const id of ids) {
        errors.get(id).push(`Serial ${serial.toUpperCase()} appears in more than one request.`);
      }
    }
  }
  return errors;
}

async function validateBulkSerials() {
  if (!bulkSerialValidationEnabled()) return true;
  const bulkRequests = state.queue.filter((request) => request.kind === "bulk_location" && request.bulk_validation !== "valid");
  if (!bulkRequests.length) return true;
  const items = bulkRequests.flatMap((request) => request.serials.map((serial) => ({ request, serial })));
  bulkRequests.forEach((request) => {
    request.bulk_validation = "checking";
    request.bulk_validation_error = "";
  });
  renderAll();
  const missing = new Map(bulkRequests.map((request) => [request.id, []]));
  await runConcurrent(items, async ({ request, serial }) => {
    try {
      const payload = await api("/api/search/assets", {
        method: "POST",
        body: JSON.stringify({ query: serial, fresh: true }),
      });
      const found = (payload.results || []).some((item) => bestSerial(item, serial).toLowerCase() === serial.toLowerCase());
      if (!found) missing.get(request.id).push(serial);
    } catch (_) {
      missing.get(request.id).push(serial);
    }
  }, 12);
  bulkRequests.forEach((request) => {
    const invalid = missing.get(request.id);
    request.bulk_validation = invalid.length ? "failed" : "valid";
    request.bulk_validation_error = invalid.length
      ? `Could not verify: ${invalid.slice(0, 3).join(", ")}${invalid.length > 3 ? ` and ${invalid.length - 3} more` : ""}.`
      : "";
  });
  renderAll();
  return !bulkRequests.some((request) => request.bulk_validation === "failed");
}

function statusLabel(request) {
  const options = request.kind === "user" ? state.config.user_statuses : state.config.location_statuses;
  return options.find((option) => option.value === request.status)?.label || request.status || "Not selected";
}

function kindLabel(kind) {
  return ({ user: "Deploy to user", location: "Add to location stock", bulk_location: "Bulk add to location stock" })[kind] || "Unknown";
}

function destinationLabel(request) {
  if (request.kind === "user") return request.user || "No user selected";
  const location = request.location || {};
  const parts = [location.building, location.floor, location.room, location.cabinet].filter(Boolean);
  return parts.length ? parts.join(" → ") : "No location selected";
}

function renderReturningUserInfo(request) {
  const panel = $("#returnUserInfo");
  if (!panel || !request?.returning) return;
  const info = request.returning_user_info;
  if (request.returning_user_loading) {
    panel.className = "return-user-info loading";
    panel.innerHTML = "<strong>Checking returning user…</strong><span>Fetching EUDM details before this request can be submitted.</span>";
    return;
  }
  const values = info?.columns || [];
  if (!request.returning_user || !info || !values.length) {
    panel.className = "return-user-info unknown";
    panel.innerHTML = "<strong>Returning user details unknown</strong><span>Search and select the user before submitting.</span>";
    return;
  }
  const unknown = values.some((value) => !String(value).trim() || /unknown|not available|not found/i.test(value));
  panel.className = `return-user-info ${unknown ? "unknown" : ""}`;
  panel.innerHTML = `<strong>Selected user</strong><span>${escapeHtml(info.login || request.returning_user)}</span><small>${escapeHtml(values.join(" · "))}</small>${unknown ? "<em>Some details are unknown. Verify the user before submitting.</em>" : ""}`;
}

function renderQueue() {
  const validations = queueValidation();
  const requestCount = state.queue.length;
  const deviceCount = state.queue.reduce((sum, request) => sum + request.serials.length, 0);
  const invalidCount = [...validations.values()].filter((errors) => errors.length).length;
  const submittedCount = state.queue.filter((request) => request.result_state === "succeeded").length;
  elements.queueCounts.textContent = `${requestCount} request${requestCount === 1 ? "" : "s"} · ${deviceCount} device${deviceCount === 1 ? "" : "s"}`;
  elements.queueValidationNotice.hidden = invalidCount === 0;
  elements.queueValidationMessage.textContent = invalidCount
    ? `${invalidCount} request${invalidCount === 1 ? " needs" : "s need"} attention`
    : "";
  elements.queueEmpty.hidden = requestCount > 0;
  elements.queueTableWrap.hidden = requestCount === 0;
  const runtimeReady = state.connection?.state === "simulation"
    || state.connection?.state === "connected";
  const requesterReady = Boolean(state.connection?.request_for || state.config?.request_for);
  elements.reviewButton.disabled = requestCount === 0
    || invalidCount > 0
    || submittedCount > 0
    || !runtimeReady
    || !requesterReady;
  elements.reviewButton.title = submittedCount
    ? "Clear completed requests before submitting another run."
    : invalidCount
    ? "Fix every request error before reviewing or submitting."
    : !runtimeReady || !requesterReady
      ? "Connect to EUDM before submitting."
      : "Review every request before submitting.";

  elements.queueBody.innerHTML = state.queue.map((request, index) => {
    const errors = validations.get(request.id) || [];
    const serialDisplay = request.serials.length ? request.serials.join(", ") : "No serial";
    const selected = request.id === state.selectedId;
    const secondary = request.source
      || (request.group && request.group !== kindLabel(request.kind) ? request.group : "");
    const requestId = request.request_id
      ? requestIdDisplay(request.request_id, "cell-request-id")
      : "";
    const resultState = request.result_state === "succeeded" ? "Submitted"
      : request.result_state === "failed" ? "Failed" : "";
    const readinessMarkup = request.result_state === "failed"
      ? '<span class="failed-mark" title="Request failed">!</span><span class="cell-secondary">Failed</span>'
      : request.bulk_validation === "checking"
        ? '<span class="checking-mark" title="Checking serial numbers"></span><span class="cell-secondary">Checking</span>'
        : errors.length
          ? '<span class="invalid-mark">!</span>'
          : request.request_id
            ? `<span class="ready-mark">✓</span><span class="cell-secondary">${resultState}</span>`
            : '<span class="ready-mark">✓</span>';
    return `
      <tr data-id="${escapeHtml(request.id)}" class="${selected ? "selected" : ""} ${errors.length ? "invalid" : ""} ${request.result_state === "failed" ? "failed" : ""}" tabindex="0">
        <td class="index-column">${index + 1}</td>
        <td><span class="cell-primary">${escapeHtml(serialDisplay)}</span>${request.kind === "bulk_location" ? `<span class="cell-secondary">${request.serials.length} devices</span>` : ""}${requestId}</td>
        <td><span class="cell-primary">${escapeHtml(kindLabel(request.kind))}</span>${secondary ? `<span class="cell-secondary">${escapeHtml(secondary)}</span>` : ""}</td>
        <td title="${escapeHtml(statusLabel(request))}">${escapeHtml(statusLabel(request))}</td>
        <td title="${escapeHtml(destinationLabel(request))}"><span class="cell-primary">${escapeHtml(destinationLabel(request))}</span>${request.returning_user ? `<span class="cell-secondary">Returned by ${escapeHtml(request.returning_user)}</span>` : ""}</td>
        <td class="state-column" title="${escapeHtml(errors.join(" "))}">${readinessMarkup}</td>
        <td><button class="row-menu" data-remove="${escapeHtml(request.id)}" aria-label="Remove request" title="Remove request"><span class="trash-icon" aria-hidden="true"></span></button></td>
      </tr>`;
  }).join("");

  elements.queueBody.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("[data-remove], [data-copy-request-id]")) return;
      state.selectedId = row.dataset.id;
      renderAll();
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        state.selectedId = row.dataset.id;
        renderAll();
      }
    });
  });
  elements.queueBody.querySelectorAll("[data-remove]").forEach((button) => {
    button.addEventListener("click", () => removeRequest(button.dataset.remove));
  });
  refreshSelectedValidation();
}

function refreshSelectedValidation() {
  const request = selectedRequest();
  if (!request) return;
  if (request.kind !== "user") {
    $("#confirmSerial").textContent = request.serials[0] || "Not selected";
    $("#confirmUser").textContent = request.returning_user || "Not selected";
    $("#confirmLocation").textContent = destinationLabel(request);
    renderReturningUserInfo(request);
  }
  const errors = state.newRequest === request
    ? validateRequest(request)
    : queueValidation().get(request.id) || [];
  elements.validationPanel.hidden = !errors.length;
  elements.validationPanel.innerHTML = errors.length
    ? `<ul>${errors.map((error) => `<li>${escapeHtml(error)}</li>`).join("")}</ul>`
    : "";
  const saveButton = $("#saveNewRequestButton");
  if (saveButton) saveButton.disabled = state.newRequest === request && errors.length > 0;
}

function fillSelect(select, options, selected, placeholder = null) {
  select.innerHTML = [
    ...(placeholder !== null ? [{ label: placeholder, value: "" }] : []),
    ...options,
  ].map((option) => `<option value="${escapeHtml(option.value)}" ${option.value === selected ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("");
}

function renderInspector() {
  const request = selectedRequest();
  elements.inspectorEmpty.hidden = Boolean(request);
  elements.inspectorContent.hidden = !request;
  hideSearchResults();
  if (!request) return;

  const bulk = request.kind === "bulk_location";
  const user = request.kind === "user";
  $("#requestSizeInput").value = bulk ? "bulk" : "single";
  elements.serialInput.hidden = bulk;
  elements.serialsInput.hidden = !bulk;
  $("#searchSerialButton").hidden = bulk;
  elements.serialLabel.textContent = bulk ? "Serial numbers" : "Serial number";
  elements.serialInput.value = bulk ? "" : (request.serials[0] || "");
  elements.serialsInput.value = bulk ? request.serials.join("\n") : "";
  elements.serialHint.textContent = bulk ? `${request.serials.length} serial${request.serials.length === 1 ? "" : "s"}` : "";

  const statusOptions = bulk ? state.config.location_statuses : singleRequestStatusOptions();
  normalizeRequestStatus(request);
  fillSelect(elements.statusInput, statusOptions, request.status, "Choose a status");
  elements.userFields.hidden = !user;
  elements.locationFields.hidden = user;
  elements.userInput.value = request.user || "";

  if (!user) {
    fillSelect(elements.cityInput, locationCities(request.location), request.location?.city || "", "Choose a city");
    const location = request.location || {};
    const results = locationResults(location.city);
    const hasExact = hasCompleteLocation(location);
    populateLocationPicker(elements.locationInput, location, results, locationEmptyText(location.city));
    elements.locationDetail.textContent = hasExact ? "" : "Choose a location.";
    elements.returningUserFields.hidden = bulk;
    elements.returningToggle.checked = Boolean(request.returning);
    elements.returningSearch.hidden = !request.returning;
    elements.returningUserInput.value = request.returning_user || "";
    elements.returnConfirmation.hidden = !request.returning;
    $("#confirmSerial").textContent = request.serials[0] || "Not selected";
    $("#confirmUser").textContent = request.returning_user || "Not selected";
    $("#confirmLocation").textContent = destinationLabel(request);
    ensureLocationsLoaded(location.city);
  }

  refreshSelectedValidation();
}

function renderAll() {
  renderQueue();
  renderInspector();
}

function restoreInspector() {
  const content = elements.inspectorContent;
  const inspector = $("#inspector");
  if (content.parentElement !== inspector) inspector.append(content);
}

function changeRequestSize(size) {
  const request = selectedRequest();
  if (!request) return;
  if (size === "bulk") {
    const locationStatus = state.config.location_statuses.some((option) => option.value === request.status)
      ? request.status
      : (state.config.default_location_status || state.config.location_statuses[0]?.value || "");
    applyInferredKind(request, locationStatus);
    request.kind = "bulk_location";
    request.group = "Bulk add to location stock";
    request.user = "";
    request.returning = false;
    request.returning_user = "";
    request.returning_user_info = null;
    request.location = request.location || preferredLocation();
  } else if (request.kind === "bulk_location") {
    request.serials = request.serials.slice(0, 1);
    applyInferredKind(request, request.status, false);
  }
  renderAll();
  setTimeout(() => (size === "bulk" ? elements.serialsInput : elements.serialInput).focus(), 0);
}

function discardNewRequest() {
  state.newRequest = null;
  restoreInspector();
  renderAll();
}

function startNewRequest() {
  const kind = "user";
  state.newRequest = makeRequest(kind);
  const dialog = $("#newRequestDialog");
  $("#newRequestEditorMount").append(elements.inspectorContent);
  dialog.showModal();
  renderAll();
  setTimeout(() => (kind === "bulk_location" ? elements.serialsInput : elements.serialInput).focus(), 0);
}

function saveNewRequest() {
  const request = state.newRequest;
  if (!request) return;
  const errors = validateRequest(request);
  if (errors.length) {
    refreshSelectedValidation();
    return;
  }
  state.queue.push(request);
  state.selectedId = request.id;
  state.newRequest = null;
  $("#newRequestDialog").close();
  restoreInspector();
  renderAll();
  toast("Request added to the queue.", "success");
}

function removeRequest(id) {
  const index = state.queue.findIndex((request) => request.id === id);
  if (index < 0) return;
  state.queue.splice(index, 1);
  if (state.selectedId === id) {
    state.selectedId = state.queue[index]?.id || state.queue[index - 1]?.id || null;
  }
  renderAll();
}

function duplicateSelected() {
  const request = selectedRequest();
  if (!request) return;
  const copy = structuredClone(request);
  copy.id = uid();
  copy.source = request.source ? `${request.source} · copy` : "Copy";
  const index = state.queue.findIndex((item) => item.id === request.id);
  state.queue.splice(index + 1, 0, copy);
  state.selectedId = copy.id;
  renderAll();
}

function hideSearchResults() {
  [elements.serialResults, elements.userResults, elements.returningResults].forEach((node) => {
    node.hidden = true;
    node.replaceChildren();
  });
}

function renderSearchResults(container, results, onSelect, primaryIndex = 0) {
  if (!results.length) {
    container.innerHTML = '<div class="search-empty">No matches returned</div>';
  } else {
    container.innerHTML = results.map((result, index) => {
      const columns = result.columns || [];
      const primary = columns[primaryIndex] || columns[0] || result.value;
      const secondary = columns.filter((_, columnIndex) => columnIndex !== primaryIndex).join(" · ");
      return `<button class="search-result" type="button" data-index="${index}"><strong>${escapeHtml(primary)}</strong><small>${escapeHtml(secondary)}</small></button>`;
    }).join("");
    container.querySelectorAll("[data-index]").forEach((button) => {
      button.addEventListener("click", () => {
        const selected = results[Number(button.dataset.index)];
        hideSearchResults();
        onSelect(selected);
      });
    });
  }
  container.hidden = false;
}

function locationResults(city) {
  return state.locationCache.get(city) || [];
}

function hasLoadedLocations(city) {
  return Boolean(city) && state.locationCache.has(city);
}

function locationEmptyText(city) {
  if (!city) return "Choose a city to load locations";
  return hasLoadedLocations(city)
    ? "No locations are available for this city"
    : "Loading locations…";
}

function fetchLocationResults(city, { force = false } = {}) {
  if (!city) return Promise.resolve([]);
  if (!force && hasLoadedLocations(city)) {
    return Promise.resolve(locationResults(city));
  }
  if (state.locationLoading.has(city)) {
    return state.locationLoading.get(city);
  }
  const request = api("/api/search/locations", {
    method: "POST",
    body: JSON.stringify({ city }),
  }).then((payload) => {
    const results = payload.results || [];
    state.locationCache.set(city, results);
    return results;
  }).finally(() => {
    state.locationLoading.delete(city);
  });
  state.locationLoading.set(city, request);
  return request;
}

function populateLocationPicker(input, location, results, emptyText) {
  const exact = hasCompleteLocation(location);
  const selectedIndex = results.findIndex((result) => {
    const [building = "", floor = "", room = "", cabinet = ""] = result.columns || [];
    return building === location?.building
      && floor === location?.floor
      && room === location?.room
      && cabinet === (location?.cabinet || "");
  });
  if (results.length) {
    input.innerHTML = [
      '<option value="">Choose a location</option>',
      ...results.map((result, index) => `<option value="${index}" ${index === selectedIndex ? "selected" : ""}>${escapeHtml(result.columns.filter(Boolean).join(" → "))}</option>`),
    ].join("");
    input.dataset.results = JSON.stringify(results);
  } else if (exact) {
    input.innerHTML = `<option value="current">${escapeHtml(locationDisplay(location))}</option>`;
    input.dataset.results = "";
  } else {
    input.innerHTML = `<option value="">${escapeHtml(emptyText)}</option>`;
    input.dataset.results = "";
  }
}

function locationCities(location) {
  return [...new Set([
    ...(state.config.cities || []),
    ...recentLocations().map((item) => item.city),
    location?.city,
  ].filter(Boolean))].map((city) => ({ label: city, value: city }));
}

function bestLogin(result, query) {
  const columns = result.columns || [];
  const exact = columns.find((value) => value.toLowerCase() === query.toLowerCase());
  if (exact) return exact;
  return [...columns].reverse().find((value) => /^[A-Za-z][A-Za-z0-9._-]*$/.test(value) && !value.includes(" ")) || query;
}

function bestSerial(result, query) {
  const columns = result.columns || [];
  return columns.find((value) => value.toLowerCase() === query.toLowerCase())
    || columns.find((value) => /^[A-Za-z0-9._-]{6,}$/.test(value))
    || query;
}

async function searchAssets() {
  const request = selectedRequest();
  if (!request || request.kind === "bulk_location") return;
  const query = elements.serialInput.value.trim();
  if (query.length < 2) return toast("Enter at least two serial characters.", "error");
  $("#searchSerialButton").disabled = true;
  try {
    const payload = await api("/api/search/assets", { method: "POST", body: JSON.stringify({ query }) });
    renderSearchResults(elements.serialResults, payload.results, (result) => {
      request.serials = [bestSerial(result, query)];
      renderAll();
    }, 1);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    $("#searchSerialButton").disabled = false;
  }
}

async function searchUsers(returning = false) {
  const request = selectedRequest();
  if (!request) return;
  const input = returning ? elements.returningUserInput : elements.userInput;
  const button = returning ? $("#searchReturningButton") : $("#searchUserButton");
  const container = returning ? elements.returningResults : elements.userResults;
  const query = input.value.trim();
  if (query.length < 2) return toast("Enter at least two username characters.", "error");
  button.disabled = true;
  try {
    const payload = await api("/api/search/users", {
      method: "POST",
      body: JSON.stringify({ query, returning }),
    });
    renderSearchResults(container, payload.results, (result) => {
      const login = bestLogin(result, query);
      if (returning) {
        request.returning_user = login;
        request.returning_user_info = {
          login,
          columns: (Array.isArray(result.columns) ? result.columns : [result.value]).map(String).filter(Boolean),
        };
      }
      else request.user = login;
      renderAll();
    }, 0);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function loadLocations({ city: requestedCity, force = false, quiet = false } = {}) {
  const request = selectedRequest();
  if (!request || request.kind === "user") return;
  const requestId = request.id;
  const city = requestedCity || elements.cityInput.value;
  if (!city) {
    if (!quiet) toast("Choose a city first.", "error");
    return;
  }
  if (!force && hasLoadedLocations(city)) {
    populateLocationPicker(elements.locationInput, request.location || emptyLocation(), locationResults(city), locationEmptyText(city));
    elements.locationDetail.textContent = hasCompleteLocation(request.location) ? "" : "Choose a location.";
    return;
  }
  $("#loadLocationsButton").disabled = true;
  elements.locationInput.innerHTML = '<option>Loading locations…</option>';
  try {
    const results = await fetchLocationResults(city, { force });
    const current = state.queue.find((item) => item.id === requestId);
    if (current && current.kind !== "user" && current.location?.city === city) {
      if (state.selectedId === requestId) renderInspector();
      renderQueue();
    }
  } catch (error) {
    if (state.selectedId === requestId) {
      elements.locationInput.innerHTML = '<option value="">Could not load locations</option>';
    }
    if (!quiet) toast(error.message, "error");
  } finally {
    $("#loadLocationsButton").disabled = false;
  }
}

function ensureLocationsLoaded(city) {
  if (!city || hasLoadedLocations(city)) return;
  if (!["connected", "simulation"].includes(state.connection?.state)) return;
  loadLocations({ city, quiet: true });
}

function updateConnection(status) {
  const previousState = state.connection?.state;
  state.connection = status;
  if (status.state === "connected" && previousState && previousState !== "connected") {
    // Results from an expired session are not trustworthy. Reload them after
    // a successful reconnection rather than retaining stale choices.
    state.locationCache.clear();
    state.locationLoading.clear();
  }
  elements.connectionBadge.className = `connection-badge ${status.state}`;
  const requester = status.request_for || state.config.request_for || "";
  const label = status.state === "simulation" ? "Simulating EUDM API"
    : status.state === "connected" ? requester ? `Connected to EUDM as ${requester}` : "Connected to EUDM"
    : status.state === "connecting" ? "Connecting"
    : status.state === "expired" ? "Reconnect to EUDM"
    : status.state === "error" ? "Connection failed"
    : "Not connected";
  elements.connectionBadge.querySelector("span:last-child").textContent = label;
  elements.connectionBadge.title = status.message || "";
  elements.connectButton.hidden = status.state === "simulation";
  elements.connectButton.disabled = status.state === "connecting";
  elements.connectButton.textContent = status.state === "connected"
    ? "Refresh connection"
    : status.state === "expired"
      ? "Reconnect to EUDM"
    : status.state === "error" ? "Try again"
      : status.state === "connecting" ? "Connecting…" : "Connect to EUDM";
  const needsConnection = !state.config.simulation && !["connected", "simulation"].includes(status.state);
  elements.connectionGate.hidden = !needsConnection;
  elements.connectionGateTitle.textContent = status.state === "connecting"
    ? "Connecting to EUDM…"
    : status.state === "expired"
      ? "Your EUDM session has expired"
    : status.state === "error"
      ? "EUDM connection needed before submitting"
      : "Connect to EUDM before submitting";
  elements.connectionGateMessage.textContent = status.state === "error"
    ? status.message || "The authenticated EUDM session could not be established."
    : status.state === "expired"
      ? "Reconnect to EUDM. Complete SSO in Chrome if it opens, then return here to continue."
    : status.state === "connecting"
      ? "Complete authentication if prompted. The queue will unlock when EUDM is ready."
      : "An authenticated EUDM connection is required. Your prepared queue is saved here.";
  elements.connectionGateButton.hidden = status.state === "connecting";
  elements.connectionGateButton.disabled = status.state === "connecting";
  if (status.state === "connected" && !state.liveOptionsLoaded) {
    refreshFormOptions();
  }
  renderQueue();
}

async function refreshFormOptions() {
  try {
    const payload = await api("/api/options");
    const userStatuses = payload.statuses.filter((option) => option.label.startsWith("Deployed -"));
    const locationStatuses = payload.statuses.filter((option) => !option.label.startsWith("Deployed -"));
    if (userStatuses.length) state.config.user_statuses = userStatuses;
    if (locationStatuses.length) state.config.location_statuses = locationStatuses;
    if (payload.cities.length) state.config.cities = payload.cities;
    state.queue.forEach(normalizeRequestStatus);
    state.liveOptionsLoaded = true;
    renderAll();
  } catch (error) {
    toast(`Live form options could not be refreshed: ${error.message}`, "error");
  }
}

async function refreshConnection() {
  try {
    const status = await api("/api/status");
    updateConnection(status);
    if (status.state === "connecting") setTimeout(refreshConnection, 900);
    if (status.state === "error") toast(status.message, "error");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function checkConnection() {
  if (!state.connection || state.connection.state === "simulation") return;
  try {
    const status = await api("/api/connection/health", { method: "POST", body: "{}" });
    updateConnection(status);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function connect() {
  if (state.connection?.state === "connected") {
    elements.connectButton.disabled = true;
    await checkConnection();
    elements.connectButton.disabled = false;
    return;
  }
  elements.connectButton.disabled = true;
  try {
    const status = await api("/api/connect", { method: "POST", body: "{}" });
    updateConnection(status);
    setTimeout(refreshConnection, 700);
  } catch (error) {
    toast(error.message, "error");
    elements.connectButton.disabled = false;
  }
}

function openPasteDialog() {
  $("#pairsInput").value = "";
  $("#pairsError").hidden = true;
  state.pasteLocation = preferredLocation();
  state.pasteLocationResults = [];
  state.pasteEntries = [];
  $("#pairsAddSerial").value = "";
  $("#pairsAddUsername").value = "";
  $("#pairsEntry").hidden = false;
  $("#pairsEntry .quick-import-add").hidden = false;
  $("#pairsReview").hidden = false;
  $("#pairsTextMode").hidden = true;
  $("#pairsTextModeButton").textContent = "Add a list instead";
  $("#pasteDialog").showModal();
  renderQuickImportReview();
  setTimeout(() => $("#pairsAddUsername").focus(), 0);
}

function openSettings() {
  const columns = importColumns() || {};
  $("#spreadsheetUrlInput").value = savedSpreadsheetUrl();
  $("#spreadsheetUsernameColumnInput").value = columns.username || "Username";
  $("#spreadsheetDeploymentColumnInput").value = columns.deployment_serial || "SN";
  $("#spreadsheetReturnedColumnInput").value = columns.returned_device || "";
  $("#spreadsheetPendingColumnInput").value = columns.pending_return || "OLD Device SN";
  $("#spreadsheetEnabledColumnInput").value = columns.enabled || "";
  $("#spreadsheetHeadlessInput").checked = spreadsheetDownloadHeadless();
  $("#validateBulkSerialsInput").checked = bulkSerialValidationEnabled();
  $("#settingsDialog").showModal();
}

function openAlmWorkbookImport() {
  resetImportDialog();
  $("#importDialog").showModal();
}

function openShortcuts() {
  $("#shortcutsDialog").showModal();
}

function focusSelectedSerial() {
  const request = selectedRequest();
  if (!request) return;
  (request.kind === "bulk_location" ? elements.serialsInput : elements.serialInput).focus();
}

function renderPasteLocationFields() {
  const location = state.pasteLocation || preferredLocation();
  fillSelect($("#pairsCityInput"), locationCities(location), location.city, "Choose a city");
  state.pasteLocationResults = locationResults(location.city).map((result) => ({ ...result, city: location.city }));
  populateLocationPicker($("#pairsLocationInput"), location, state.pasteLocationResults, locationEmptyText(location.city));
  const locationNotice = $("#pairsLocation");
  const complete = hasCompleteLocation(location);
  locationNotice.classList.toggle("incomplete", !complete);
  locationNotice.textContent = complete
    ? locationDisplay(location)
    : location.city
      ? hasLoadedLocations(location.city)
        ? "No locations are available for the selected city."
        : "Loading locations for the selected city…"
      : "Choose a city to load locations.";
  ensurePasteLocationsLoaded(location.city);
}

async function findPasteLocations({ force = false, quiet = false } = {}) {
  const city = $("#pairsCityInput").value;
  if (!city) {
    if (!quiet) toast("Choose a city first.", "error");
    return;
  }
  const button = $("#pairsFindLocationsButton");
  button.disabled = true;
  $("#pairsLocationInput").innerHTML = '<option>Loading locations…</option>';
  try {
    const results = await fetchLocationResults(city, { force });
    state.pasteLocationResults = results.map((result) => ({ ...result, city }));
    if (state.pasteLocation?.city !== city) {
      state.pasteLocation = { city, building: "", floor: "", room: "", cabinet: "" };
    }
    renderPasteLocationFields();
    if (!hasCompleteLocation(state.pasteLocation)) {
      $("#pairsLocation").textContent = results.length
        ? `${results.length} location${results.length === 1 ? "" : "s"} ready to choose.`
        : "No locations are available for the selected city.";
    }
  } catch (error) {
    state.pasteLocationResults = [];
    $("#pairsLocationInput").innerHTML = '<option value="">Could not load locations</option>';
    if (!quiet) toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function ensurePasteLocationsLoaded(city) {
  if (!city || hasLoadedLocations(city)) return;
  if (!["connected", "simulation"].includes(state.connection?.state)) return;
  findPasteLocations({ quiet: true });
}

function makeQuickImportEntry(serial, username) {
  return {
    serial,
    username,
    returningUserInfo: null,
    returningUserChecked: false,
    validationChecked: false,
    kind: username ? "user" : "location",
    userStatus: resolveStatus(state.config.user_statuses, state.config.default_user_status),
    locationStatus: resolveStatus(state.config.location_statuses, state.config.default_location_status),
  };
}

function parseQuickImportLines() {
  const lines = $("#pairsInput").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const entries = [];
  const errors = [];
  const seenSerials = new Set();
  lines.forEach((line, index) => {
    const parts = line.split(/\s+/);
    if (parts.length < 1 || parts.length > 2) {
      errors.push(`Line ${index + 1} needs a serial, with an optional username.`);
      return;
    }
    const [serial, username = ""] = parts;
    if (!/^[A-Za-z0-9._-]{6,}$/.test(serial)) {
      errors.push(`Line ${index + 1} has an invalid serial number.`);
      return;
    }
    if (username && !/^[A-Za-z][A-Za-z0-9._-]*$/.test(username)) {
      errors.push(`Line ${index + 1} has an invalid username.`);
      return;
    }
    const serialKey = serial.toLowerCase();
    if (seenSerials.has(serialKey)) {
      errors.push(`Line ${index + 1} repeats serial ${serial}.`);
      return;
    }
    seenSerials.add(serialKey);
    entries.push(makeQuickImportEntry(serial, username));
  });
  if (!entries.length && !errors.length) errors.push("Enter at least one serial number.");
  return { entries, errors };
}

function renderQuickImportReview() {
  populateQuickImportBulkOptions();
  const list = $("#pairsReviewList");
  list.innerHTML = state.pasteEntries.map((entry, index) => {
    const username = entry.kind === "user"
      ? `To ${escapeHtml(entry.username)}`
      : entry.username
        ? `Returned by ${escapeHtml(entry.username)}`
        : "No returning user";
    const statusOptions = singleRequestStatusOptions();
    const selectedStatus = entry.kind === "location" ? entry.locationStatus : entry.userStatus;
    const deploymentStatus = `<label class="quick-import-status">Status
          <select data-pairs-status="${index}" aria-label="Deployment status for ${escapeHtml(entry.serial)}">
            ${statusOptions.map((option) => `<option value="${escapeHtml(option.value)}" ${option.value === selectedStatus ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}
          </select>
        </label>`;
    const returnInfo = entry.kind === "location" && entry.username
      ? `<div class="quick-import-return ${entry.returningUserInfo ? "" : "unknown"}"><strong>Returning user</strong><span>${escapeHtml(entry.returningUserInfo?.login || entry.username)}</span><small>${entry.returningUserInfo?.columns?.length ? escapeHtml(entry.returningUserInfo.columns.join(" · ")) : "Details unknown — search and verify before submitting. An email will be sent to this user."}</small></div>`
      : "";
    const checking = entry.validationState === "checking" ? '<small class="import-checking">Checking EUDM…</small>' : entry.validationState === "failed" ? `<small class="import-check-failed">${escapeHtml(entry.validationError || "Not found in EUDM")}</small>` : "";
    return `<div class="quick-import-row">
      <div><strong>${escapeHtml(entry.serial)}</strong><small>${username}</small>${returnInfo}${checking}</div>
      <div class="quick-import-row-actions">
        ${deploymentStatus}
        <button class="row-menu" type="button" data-pairs-remove="${index}" aria-label="Remove ${escapeHtml(entry.serial)}" title="Remove device"><span class="trash-icon" aria-hidden="true"></span></button>
      </div>
    </div>`;
  }).join("");
  $$("[data-pairs-status]").forEach((select) => select.addEventListener("change", () => {
    const entry = state.pasteEntries[Number(select.dataset.pairsStatus)];
    const kind = kindForStatus(select.value);
    entry.kind = kind;
    if (kind === "location") {
      entry.locationStatus = select.value;
      if (entry.username) {
        entry.validationChecked = false;
        entry.validationState = "";
        entry.returningUserInfo = null;
      }
    } else entry.userStatus = select.value;
    renderQuickImportReview();
    resolveQuickImportReturningUsers();
  }));
  $$("[data-pairs-remove]").forEach((button) => button.addEventListener("click", () => {
    state.pasteEntries.splice(Number(button.dataset.pairsRemove), 1);
    renderQuickImportReview();
  }));
  const locationNeeded = state.pasteEntries.some((entry) => entry.kind === "location");
  $("#pairsLocationFields").hidden = !locationNeeded;
  $("#addPairsButton").disabled = state.pasteEntries.length === 0;
  if (locationNeeded) renderPasteLocationFields();
}

function populateQuickImportBulkOptions() {
  const select = $("#pairsBulkKind");
  const selected = select.value;
  const statusOptions = singleRequestStatusOptions().map((option) =>
    `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`,
  );
  select.innerHTML = [
    '<option value="">Choose a status</option>',
    ...statusOptions,
  ].join("");
  if ([...select.options].some((option) => option.value === selected)) select.value = selected;
}

async function resolveQuickImportReturningUsers() {
  const entries = state.pasteEntries.filter((entry) => !entry.validationChecked);
  if (!entries.length || !["connected", "simulation"].includes(state.connection?.state)) return;
  entries.forEach((entry) => { entry.validationChecked = true; entry.validationState = "checking"; });
  renderQuickImportReview();
  await runConcurrent(entries, async (entry) => {
    try {
      const [assets, users] = await Promise.all([
        api("/api/search/assets", { method: "POST", body: JSON.stringify({ query: entry.serial, fresh: true }) }),
        entry.username ? api("/api/search/users", { method: "POST", body: JSON.stringify({ query: entry.username, returning: entry.kind === "location", fresh: true }) }) : Promise.resolve({ results: [] }),
      ]);
      const asset = (assets.results || []).find((item) => bestSerial(item, entry.serial).toLowerCase() === entry.serial.toLowerCase());
      const result = entry.username ? (users.results || []).find((item) => bestLogin(item, entry.username).toLowerCase() === entry.username.toLowerCase()) : null;
      if (!asset || (entry.username && !result)) throw new Error(!asset ? "Serial number was not found in EUDM." : "Username was not found in EUDM.");
      entry.returningUserInfo = result ? { login: bestLogin(result, entry.username), columns: (result.columns || [result.value]).map(String).filter(Boolean) } : null;
      entry.validationState = "valid";
    } catch (error) {
      entry.returningUserInfo = null;
      entry.validationState = "failed";
      entry.validationError = error.message || "Could not validate this entry.";
    }
  });
  renderQuickImportReview();
}

async function resolveQueueReturningUsers(requests) {
  const entries = requests.filter((request) => request.kind === "location" && request.returning_user && !request.returning_user_info);
  if (!entries.length || !["connected", "simulation"].includes(state.connection?.state)) return;
  entries.forEach((request) => { request.returning_user_loading = true; });
  renderAll();
  await runConcurrent(entries, async (request) => {
    try {
      const payload = await api("/api/search/users", { method: "POST", body: JSON.stringify({ query: request.returning_user, returning: true }) });
      const result = (payload.results || []).find((item) => bestLogin(item, request.returning_user).toLowerCase() === request.returning_user.toLowerCase());
      request.returning_user_info = result ? { login: bestLogin(result, request.returning_user), columns: (result.columns || [result.value]).map(String).filter(Boolean) } : null;
    } catch (_) {
      request.returning_user_info = null;
    } finally {
      request.returning_user_loading = false;
    }
  });
  renderAll();
}

function applyQuickImportKind() {
  const status = $("#pairsBulkKind").value;
  if (!status) return;
  const kind = kindForStatus(status);
  state.pasteEntries.forEach((entry) => {
    entry.kind = kind;
    if (kind === "location") {
      entry.locationStatus = resolveStatus(state.config.location_statuses, status);
      if (entry.username) {
        entry.validationChecked = false;
        entry.validationState = "";
        entry.returningUserInfo = null;
      }
    } else {
      entry.userStatus = resolveStatus(state.config.user_statuses, status);
    }
  });
  renderQuickImportReview();
  resolveQuickImportReturningUsers();
}

function addQuickImportEntry() {
  const serial = $("#pairsAddSerial").value.trim();
  const username = $("#pairsAddUsername").value.trim();
  const errors = [];
  if (!/^[A-Za-z0-9._-]{6,}$/.test(serial)) errors.push("Enter a valid serial number.");
  if (username && !/^[A-Za-z][A-Za-z0-9._-]*$/.test(username)) errors.push("Enter a valid username.");
  if (state.pasteEntries.some((entry) => entry.serial.toLowerCase() === serial.toLowerCase())) {
    errors.push(`${serial} is already in this import.`);
  }
  if (errors.length) {
    $("#pairsError").textContent = errors.join(" ");
    $("#pairsError").hidden = false;
    return;
  }
  state.pasteEntries.push(makeQuickImportEntry(serial, username));
  $("#pairsAddSerial").value = "";
  $("#pairsAddUsername").value = "";
  $("#pairsError").hidden = true;
  renderQuickImportReview();
  resolveQuickImportReturningUsers();
  $("#pairsAddUsername").focus();
}

function addQuickImportList() {
  const { entries, errors } = parseQuickImportLines();
  const existing = new Set(state.pasteEntries.map((entry) => entry.serial.toLowerCase()));
  entries.forEach((entry) => {
    if (existing.has(entry.serial.toLowerCase())) errors.push(`${entry.serial} is already in this import.`);
    else { existing.add(entry.serial.toLowerCase()); state.pasteEntries.push(entry); }
  });
  if (errors.length) {
    $("#pairsError").textContent = errors.slice(0, 4).join(" ");
    $("#pairsError").hidden = false;
  } else {
    $("#pairsInput").value = "";
    $("#pairsError").hidden = true;
  }
  renderQuickImportReview();
  resolveQuickImportReturningUsers();
}

function addPairs() {
  const errors = [];
  if (!state.pasteEntries.length) errors.push("Choose deployments before adding them to the queue.");
  if (state.pasteEntries.some((entry) => entry.kind === "location")) {
    const location = state.pasteLocation || preferredLocation();
    if (!hasCompleteLocation(location)) {
      errors.push("Choose a complete city and location before adding these requests.");
    }
  }
  if (state.pasteEntries.some((entry) => entry.validationState === "checking")) errors.push("Wait for EUDM validation to finish.");
  if (state.pasteEntries.some((entry) => entry.validationState === "failed")) errors.push("Correct the entries EUDM could not validate.");
  if (state.pasteEntries.some((entry) => entry.kind === "user" && !entry.username)) errors.push("A username is required for a deployed status.");
  if (errors.length) {
    $("#pairsError").textContent = errors.join(" ");
    $("#pairsError").hidden = false;
    return;
  }
  const requests = state.pasteEntries.map(({ serial, username, kind, userStatus, locationStatus, returningUserInfo }) => {
    const locationMode = kind === "location";
    const request = makeRequest(locationMode ? "location" : "user");
    request.serials = [serial];
    request.status = locationMode
      ? resolveStatus(
        state.config.location_statuses,
        locationStatus,
        state.config.default_location_status,
      )
      : resolveStatus(state.config.user_statuses, userStatus, state.config.default_user_status);
    request.source = "Quick import";
    if (locationMode) {
      request.location = structuredClone(state.pasteLocation || preferredLocation());
      request.returning = Boolean(username);
      request.returning_user = username;
      request.returning_user_info = returningUserInfo || null;
      request.group = "Quick import · Add to location stock";
    } else {
      request.user = username;
      request.group = "Quick import · Deploy to user";
    }
    return request;
  });
  state.queue.push(...requests);
  state.selectedId = requests[0].id;
  $("#pasteDialog").close();
  renderAll();
  resolveQueueReturningUsers(requests);
  toast(`${requests.length} request${requests.length === 1 ? "" : "s"} added.`, "success");
}

function resetImportDialog() {
  state.importUploadToken += 1;
  state.workbook = null;
  state.workbookInspection = null;
  state.importPreview = null;
  state.importExpandedGroups.clear();
  state.importLoginJobId = null;
  $("#importLoginCompleteButton").hidden = true;
  $("#workbookInput").value = "";
  $("#importChoose").hidden = false;
  $("#importFileChooser").hidden = false;
  $("#importConfigure").hidden = true;
  $("#importMapColumns").hidden = true;
  $("#importPreview").hidden = true;
  $("#backImportButton").hidden = true;
  $("#prepareImportButton").disabled = true;
  $("#prepareImportButton").textContent = "Review import";
  $("#importError").hidden = true;
  setImportBusy(false);
  state.importLocation = preferredImportLocation();
  state.importLocationResults = [];
  const defaultMode = $('input[name="importMode"][value="deployments"]');
  if (defaultMode) defaultMode.checked = true;
  setImportStep(1);
}

function setImportStep(step) {
  [1, 2, 3].forEach((number) => {
    const item = $(`#importStep${number}`);
    item.classList.toggle("active", number === step);
    item.classList.toggle("complete", number < step);
  });
}

function workbookSheet(name) {
  return state.workbook?.sheets.find((sheet) => sheet.name === name);
}

function localDate(value) {
  const [year, month, day] = String(value).split("-").map(Number);
  return new Date(year, month - 1, day);
}

function mondayOfWeek(value) {
  const date = new Date(value.getFullYear(), value.getMonth(), value.getDate());
  const mondayOffset = (date.getDay() + 6) % 7;
  date.setDate(date.getDate() - mondayOffset);
  return date;
}

function relativeDateLabel(value) {
  const target = localDate(value);
  const today = new Date();
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const dayDifference = Math.round((target - todayStart) / 86400000);
  if (dayDifference === 0) return "Today";
  if (dayDifference === 1) return "Tomorrow";
  if (dayDifference === -1) return "Yesterday";
  const weekDifference = Math.round(
    (mondayOfWeek(target) - mondayOfWeek(todayStart)) / 604800000,
  );
  if (weekDifference === 1) return "Next Week";
  if (weekDifference === -1) return "Last Week";
  if (dayDifference > 1) return `In ${dayDifference} days`;
  return `${Math.abs(dayDifference)} days ago`;
}

function updateImportDates() {
  const sheet = workbookSheet($("#sheetInput").value);
  const dates = sheet?.dates || [];
  const previous = $("#dateInput").value;
  const today = new Date();
  const todayValue = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  const selected = dates.some((entry) => entry.value === todayValue) ? todayValue : dates.some((entry) => entry.value === previous) ? previous : dates[0]?.value;
  $("#dateInput").innerHTML = dates.map((entry) => `<option value="${escapeHtml(entry.value)}" ${entry.value === selected ? "selected" : ""}>${escapeHtml(entry.label)} [${escapeHtml(relativeDateLabel(entry.value))}]</option>`).join("");
  updateImportCounts();
}

function updateImportCounts() {
  const sheet = workbookSheet($("#sheetInput").value);
  const selected = sheet?.dates.find((entry) => entry.value === $("#dateInput").value);
  const deploymentCount = selected?.deployment_count || 0;
  const returnedDeviceCount = selected?.returned_device_count || 0;
  const pendingReturnCount = selected?.pending_return_count || 0;
  $("#deploymentImportCount").textContent = `${deploymentCount} request${deploymentCount === 1 ? "" : "s"}`;
  $("#returnedDeviceImportCount").textContent = `${returnedDeviceCount} request${returnedDeviceCount === 1 ? "" : "s"}`;
  $("#pendingReturnImportCount").textContent = `${pendingReturnCount} request${pendingReturnCount === 1 ? "" : "s"}`;
  $("#allImportCount").textContent = `${deploymentCount + returnedDeviceCount + pendingReturnCount} requests`;
  const mode = $('input[name="importMode"]:checked')?.value || "deployments";
  const selectedCount = mode === "deployments" ? deploymentCount : mode === "returned_devices" ? returnedDeviceCount : mode === "pending_returns" ? pendingReturnCount : deploymentCount + returnedDeviceCount + pendingReturnCount;
  const needsLocation = mode === "returned_devices" || mode === "all";
  $("#importLocationFields").hidden = !needsLocation;
  if (needsLocation) renderImportLocationFields();
  $("#prepareImportButton").disabled = !selected || selectedCount === 0;
}

function renderImportLocationFields() {
  const location = state.importLocation || preferredImportLocation();
  fillSelect($("#importCityInput"), locationCities(location), location.city, "Choose a city");
  state.importLocationResults = locationResults(location.city).map((result) => ({ ...result, city: location.city }));
  populateLocationPicker($("#importLocationInput"), location, state.importLocationResults, locationEmptyText(location.city));
  if (location.city && !hasLoadedLocations(location.city) && ["connected", "simulation"].includes(state.connection?.state)) {
    fetchImportLocations(location.city);
  }
}

async function fetchImportLocations(city, force = false) {
  if (!city) return;
  $("#importLocationInput").innerHTML = '<option>Loading locations…</option>';
  try {
    const results = await fetchLocationResults(city, { force });
    state.importLocationResults = results.map((result) => ({ ...result, city }));
    renderImportLocationFields();
  } catch (error) {
    $("#importLocationInput").innerHTML = '<option value="">Could not load locations</option>';
  }
}

function setImportBusy(visible, { percent = 0, title = "", detail = "" } = {}) {
  const busy = $("#importBusy");
  busy.hidden = !visible;
  $("#importChoose").classList.toggle("is-busy", visible);
  if (!visible) return;
  const safePercent = Math.max(0, Math.min(100, Math.round(percent)));
  $("#importBusyTitle").textContent = title;
  $("#importBusyDetail").textContent = detail;
  $("#importBusyBar").style.width = `${safePercent}%`;
  $("#importBusyPercent").textContent = `${safePercent}%`;
}

function fileToBase64(file, onProgress = () => {}) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
    reader.onerror = () => reject(new Error("The ALM Workbook could not be read."));
    reader.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    reader.readAsDataURL(file);
  });
}

function pause(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function runConcurrent(items, worker, limit = 8) {
  let next = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const index = next;
      next += 1;
      await worker(items[index]);
    }
  });
  await Promise.all(runners);
}

async function waitForWorkbookImport(jobId, token) {
  while (token === state.importUploadToken) {
    const status = await api(`/api/imports/${encodeURIComponent(jobId)}`);
    const total = Number(status.total_rows) || 0;
    const completed = Number(status.processed_rows) || 0;
    const scanProgress = total ? completed / total : .03;
    const detail = total
      ? `${status.sheet || "ALM Workbook"} · ${completed.toLocaleString()} of ${total.toLocaleString()} rows`
      : "Opening ALM Workbook…";
    const downloading = ["downloading", "waiting_for_login"].includes(status.state);
    state.importLoginJobId = status.needs_login_confirmation ? jobId : null;
    $("#importLoginCompleteButton").hidden = !status.needs_login_confirmation;
    $("#importLoginCompleteButton").disabled = false;
    setImportBusy(true, {
      percent: downloading ? 18 : 28 + scanProgress * 70,
      title: downloading ? "Downloading ALM Workbook…" : (status.message || "Reading ALM Workbook…"),
      detail: status.needs_login_confirmation ? "Sign in in Chrome, then continue." : (downloading ? "" : detail),
    });
    if (status.state === "ready") return status.workbook;
    if (status.state === "failed") throw new Error(status.error || "The ALM Workbook could not be imported.");
    await pause(250);
  }
  return null;
}

function workbookMappingMatches(workbook, saved) {
  if (!saved?.username || !saved?.deployment_serial || !saved?.pending_return) return false;
  const sheet = workbook.sheets.find((item) => item.name === workbook.default_sheet);
  const headings = new Set(
    (sheet?.headings || []).map((heading) => String(heading).trim().toLowerCase()),
  );
  return [
    saved.username,
    saved.deployment_serial,
    saved.returned_device,
    saved.pending_return,
    saved.enabled,
  ].filter(Boolean).every((heading) => headings.has(String(heading).trim().toLowerCase()));
}

function openImportColumnMapping() {
  const workbook = state.workbookInspection;
  if (!workbook) return;
  $("#importChoose").hidden = false;
  $("#importFileChooser").hidden = true;
  $("#importConfigure").hidden = true;
  $("#importPreview").hidden = true;
  $("#importMapColumns").hidden = false;
  $("#importMapFilename").textContent = workbook.filename;
  const preferredSheet = $("#sheetInput").value || workbook.default_sheet;
  $("#importMapSheet").innerHTML = workbook.sheets.map((sheet) => (
    `<option value="${escapeHtml(sheet.name)}" ${sheet.name === preferredSheet ? "selected" : ""}>${escapeHtml(sheet.name)}</option>`
  )).join("");
  renderImportColumnMap();
  $("#backImportButton").hidden = true;
  $("#prepareImportButton").textContent = "Use columns";
  setImportStep(1);
}

function showImportedWorkbook(workbook) {
  if (workbook.needs_mapping) {
    state.workbook = workbook;
    state.workbookInspection = workbook;
    const saved = importColumns();
    if (workbookMappingMatches(workbook, saved)) {
      $("#importMapSheet").innerHTML = `<option value="${escapeHtml(workbook.default_sheet)}">${escapeHtml(workbook.default_sheet)}</option>`;
      renderImportColumnMap();
      mapWorkbookColumns().catch((error) => {
        $("#importError").textContent = error.message;
        $("#importError").hidden = false;
        openImportColumnMapping();
      });
      return;
    }
    openImportColumnMapping();
    return;
  }
  state.workbook = workbook;
  $("#importChoose").hidden = true;
  $("#importFileChooser").hidden = false;
  $("#importMapColumns").hidden = true;
  $("#importConfigure").hidden = false;
  $("#importPreview").hidden = true;
  $("#backImportButton").hidden = true;
  $("#prepareImportButton").textContent = "Review import";
  setImportStep(2);
  $("#importFilename").textContent = workbook.filename;
  $("#importFileSummary").textContent = `${workbook.sheets.length} dated sheet${workbook.sheets.length === 1 ? "" : "s"}`;
  $("#sheetInput").innerHTML = workbook.sheets.map((sheet) => `<option value="${escapeHtml(sheet.name)}" ${sheet.name === workbook.default_sheet ? "selected" : ""}>${escapeHtml(sheet.name)}</option>`).join("");
  updateImportDates();
}

function renderImportColumnMap() {
  const sheet = state.workbookInspection?.sheets.find((item) => item.name === $("#importMapSheet").value);
  const headings = sheet?.headings || [];
  const saved = importColumns() || {};
  const select = (element, selected) => {
    const matching = headings.find((heading) => String(heading).trim().toLowerCase() === String(selected || "").trim().toLowerCase());
    element.innerHTML = `<option value="">Choose a column</option>${headings.map((heading) => `<option value="${escapeHtml(heading)}" ${heading === matching ? "selected" : ""}>${escapeHtml(heading)}</option>`).join("")}`;
  };
  select($("#importMapUsername"), saved.username || "Username");
  select($("#importMapDeployment"), saved.deployment_serial || "SN");
  select($("#importMapReturned"), saved.returned_device || "Returned Device SN");
  select($("#importMapPending"), saved.pending_return || "OLD Device SN");
  select($("#importMapEnabled"), saved.enabled || "");
  updateImportColumnMapButton();
}

function selectedImportColumns() {
  return {
    username: $("#importMapUsername").value,
    deployment_serial: $("#importMapDeployment").value,
    returned_device: $("#importMapReturned").value,
    pending_return: $("#importMapPending").value,
    enabled: $("#importMapEnabled").value,
  };
}

function importColumnMapError(columns = selectedImportColumns()) {
  if (!columns.username || !columns.deployment_serial || !columns.pending_return) {
    return "Choose username, deployment serial, and pending-return columns.";
  }
  const selected = Object.values(columns).filter(Boolean);
  if (new Set(selected).size !== selected.length) {
    return "Use a different workbook column for each field.";
  }
  return "";
}

function updateImportColumnMapButton() {
  if ($("#importMapColumns").hidden) return;
  $("#prepareImportButton").disabled = Boolean(importColumnMapError());
}

async function saveImportColumnPreferences(columns) {
  const preferences = {
    concurrency: Number(elements.concurrency.value),
    validate_bulk_serials: bulkSerialValidationEnabled(),
    workbook_url: savedSpreadsheetUrl(),
    workbook_headless: spreadsheetDownloadHeadless(),
    import_columns: columns,
  };
  state.preferences = await api("/api/preferences", {
    method: "POST",
    body: JSON.stringify(preferences),
  });
  try {
    localStorage.setItem(IMPORT_COLUMNS_STORAGE_KEY, JSON.stringify(columns));
  } catch (_) {}
}

async function mapWorkbookColumns({ persist = true } = {}) {
  const columns = selectedImportColumns();
  const mappingError = importColumnMapError(columns);
  if (mappingError) {
    throw new Error(mappingError);
  }
  const inspection = state.workbookInspection;
  if (!inspection?.import_id) {
    throw new Error("Choose the ALM Workbook again.");
  }
  $("#importError").hidden = true;
  $("#importMapColumns").hidden = true;
  $("#importConfigure").hidden = true;
  $("#importChoose").hidden = false;
  $("#importFileChooser").hidden = true;
  setImportBusy(true, { percent: 25, title: "Reading ALM Workbook…", detail: "Matching workbook columns" });
  try {
    const job = await api("/api/import/map", {
      method: "POST",
      body: JSON.stringify({ import_id: inspection.import_id, columns }),
    });
    const token = state.importUploadToken;
    const workbook = await waitForWorkbookImport(job.job_id, token);
    if (!workbook) return;
    if (persist) await saveImportColumnPreferences(columns);
    showImportedWorkbook(workbook);
  } catch (error) {
    $("#importChoose").hidden = true;
    $("#importFileChooser").hidden = false;
    openImportColumnMapping();
    throw error;
  } finally {
    setImportBusy(false);
  }
}

async function uploadWorkbook(file) {
  const token = state.importUploadToken + 1;
  state.importUploadToken = token;
  $("#importError").hidden = true;
  $("#prepareImportButton").disabled = true;
  setImportBusy(true, {
    percent: 0,
    title: "Reading ALM Workbook…",
    detail: `${file.name} · ${Math.ceil(file.size / 1024 / 1024)} MB`,
  });
  try {
    const data = await fileToBase64(file, (progress) => {
      if (token === state.importUploadToken) {
        setImportBusy(true, {
          percent: progress * 22,
          title: "Reading ALM Workbook…",
          detail: `${file.name} · preparing upload`,
        });
      }
    });
    if (token !== state.importUploadToken) return;
    setImportBusy(true, {
      percent: 25,
      title: "Sending ALM Workbook…",
      detail: "Starting the import",
    });
    const job = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ filename: file.name, data }),
    });
    const workbook = await waitForWorkbookImport(job.job_id, token);
    if (workbook && token === state.importUploadToken) showImportedWorkbook(workbook);
  } catch (error) {
    if (token === state.importUploadToken) {
      $("#importError").textContent = error.message;
      $("#importError").hidden = false;
    }
  } finally {
    if (token === state.importUploadToken) setImportBusy(false);
  }
}

async function downloadSavedWorkbook() {
  const url = savedSpreadsheetUrl();
  if (!url) return toast("Add an ALM Workbook link in Settings.", "error");
  const token = state.importUploadToken + 1;
  state.importUploadToken = token;
  $("#importError").hidden = true;
  $("#prepareImportButton").disabled = true;
  $("#downloadSheetButton").disabled = true;
  setImportBusy(true, { percent: 8, title: "Downloading ALM Workbook…", detail: "" });
  try {
    const job = await api("/api/import/download", {
      method: "POST",
      body: JSON.stringify({ url, headless: spreadsheetDownloadHeadless() }),
    });
    const workbook = await waitForWorkbookImport(job.job_id, token);
    if (workbook && token === state.importUploadToken) showImportedWorkbook(workbook);
  } catch (error) {
    if (token === state.importUploadToken) {
      $("#importError").textContent = error.message;
      $("#importError").hidden = false;
    }
  } finally {
    if (token === state.importUploadToken) {
      state.importLoginJobId = null;
      $("#importLoginCompleteButton").hidden = true;
      setImportBusy(false);
      $("#downloadSheetButton").disabled = false;
    }
  }
}

function importReturnDetails(request, isReturnedDevice) {
  if (!isReturnedDevice) return "";
  if (request.import_validation === "checking") {
    return '<div class="import-return-details loading"><strong>Checking returning user…</strong><small>Confirming the details before this request is added.</small></div>';
  }
  const info = request.returning_user_info;
  if (!info?.columns?.length) {
    return '<div class="import-return-details unknown"><strong>Returning user details unknown</strong><small>Correct the username and retry before adding this request.</small></div>';
  }
  return '<div class="import-return-details"><strong>Returning user</strong><small>An email will be sent to this user. Verify the details.</small><span>'
    + escapeHtml(info.login || request.returning_user)
    + '</span><em>' + escapeHtml(info.columns.join(" · ")) + '</em></div>';
}

function renderImportPreview() {
  const payload = state.importPreview;
  if (!payload) return;
  const included = payload.requests.filter((request) => request.included !== false);
  const deploymentCount = included.filter((request) => request.group === "Deployments").length;
  const returnedDeviceCount = included.filter((request) => request.group === "Returned devices").length;
  const pendingReturnCount = included.filter((request) => request.group === "Pending returns").length;
  $("#importPreviewTitle").textContent = `${deploymentCount} deployments · ${returnedDeviceCount} returned devices · ${pendingReturnCount} pending returns`;
  $("#importPreviewSubtitle").textContent = `${$("#sheetInput").value} · ${$("#dateInput option:checked").textContent}`;
  $("#importPreviewCount").textContent = `${included.length} selected`;

  const groups = [
    {
      key: "Deployments",
      title: "Deploy to user",
      detail: "Deployment serials",
    },
    {
      key: "Returned devices",
      title: "Add returned devices to location",
      detail: "Shared location",
    },
    {
      key: "Pending returns",
      title: "Pending returns",
      detail: "Pending return serials",
    },
  ];
  $("#importPreviewList").innerHTML = groups.map((group) => {
    const requests = payload.requests.filter((request) => request.group === group.key);
    if (!requests.length) return "";
    const selectedCount = requests.filter((request) => request.included !== false).length;
    const expanded = state.importExpandedGroups.has(group.key);
    const visibleRequests = expanded ? requests : requests.slice(0, IMPORT_PREVIEW_ROW_LIMIT);
    const rows = visibleRequests.map((request, index) => {
      const isDeployment = request.group === "Deployments";
      const isReturnedDevice = request.group === "Returned devices";
      const isIncluded = request.included !== false;
      const statusControl = isDeployment
        ? `<select data-import-status="${escapeHtml(request.id)}" aria-label="Status for ${escapeHtml(request.serials[0])}">
            <option value="Deployed - New Stock" ${request.status === "Deployed - New Stock" ? "selected" : ""}>Deployed - New Stock</option>
            <option value="Deployed - Existing Stock" ${request.status === "Deployed - Existing Stock" ? "selected" : ""}>Deployed - Existing Stock</option>
          </select>`
        : isReturnedDevice
          ? `<select data-import-status="${escapeHtml(request.id)}" aria-label="Status for ${escapeHtml(request.serials[0])}">
               <option value="Used Stock" ${request.status === "Used Stock" ? "selected" : ""}>Used Stock</option>
               <option value="Pending Decom" ${request.status === "Pending Decom" ? "selected" : ""}>Pending Decom</option>
             </select>`
          : `<span class="fixed-status">Deployed - Pending Return</span>`;
      const validation = request.import_validation === "checking"
        ? '<small class="import-checking">Checking EUDM…</small>'
        : request.import_validation === "failed"
          ? `<small class="import-check-failed">${escapeHtml(request.import_error || "Not found in EUDM")}</small>`
          : "";
      const returnDetails = importReturnDetails(request, isReturnedDevice);
      const editable = request.import_validation === "failed" ? `<div class="import-inline-edit"><input data-import-serial="${escapeHtml(request.id)}" value="${escapeHtml(request.serials[0])}" aria-label="Serial number"><input data-import-user="${escapeHtml(request.id)}" value="${escapeHtml(request.user || request.returning_user)}" aria-label="Username"><button class="text-button" data-import-retry="${escapeHtml(request.id)}" type="button">Retry</button></div>` : "";
      return `<div class="import-preview-row ${isIncluded ? "" : "excluded"}">
        <label class="include-control" title="${isIncluded ? "Included" : "Do not deploy"}">
          <input type="checkbox" data-import-include="${escapeHtml(request.id)}" ${isIncluded ? "checked" : ""}>
          <span>${index + 1}</span>
        </label>
        <div><strong>${escapeHtml(request.serials[0])}</strong><small>${isDeployment ? "Deployment serial" : isReturnedDevice ? "Returned device" : "Pending return"}</small></div>
        <div><strong>${escapeHtml(request.user || request.returning_user || "No user")}</strong><small>${isReturnedDevice ? "Returning user" : "Receiving user"}</small></div>
        <div>${statusControl}${isIncluded ? validation : "<small>Do not deploy</small>"}${returnDetails}${editable}</div>
      </div>`;
    }).join("");
    return `<section class="import-preview-section">
      <div class="import-group-heading">
        <div><strong>${group.title}</strong><small>${group.detail} · ${selectedCount} of ${requests.length} selected</small></div>
        <div class="import-group-actions">
          <button class="text-button" type="button" data-import-group="${escapeHtml(group.key)}" data-include="true">All</button>
          <button class="text-button" type="button" data-import-group="${escapeHtml(group.key)}" data-include="false">None</button>
        </div>
      </div>
      ${rows}
      ${visibleRequests.length < requests.length ? `<button class="import-show-more" type="button" data-import-expand="${escapeHtml(group.key)}">Show ${requests.length - visibleRequests.length} more</button>` : ""}
    </section>`;
  }).join("");
  $("#importPreviewList").querySelectorAll("[data-import-status]").forEach((select) => {
    select.addEventListener("change", () => {
      const request = payload.requests.find((item) => item.id === select.dataset.importStatus);
      if (request) request.status = select.value;
    });
  });
  $("#importPreviewList").querySelectorAll("[data-import-include]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const request = payload.requests.find((item) => item.id === checkbox.dataset.importInclude);
      if (request) request.included = checkbox.checked;
      renderImportPreview();
      const selected = payload.requests.filter((item) => item.included !== false).length;
      $("#prepareImportButton").disabled = selected === 0;
      $("#prepareImportButton").textContent = `Add ${selected} to queue`;
    });
  });
  $("#importPreviewList").querySelectorAll("[data-import-group]").forEach((button) => {
    button.addEventListener("click", () => {
      const included = button.dataset.include === "true";
      payload.requests
        .filter((request) => request.group === button.dataset.importGroup)
        .forEach((request) => { request.included = included; });
      renderImportPreview();
      const selected = payload.requests.filter((request) => request.included !== false).length;
      $("#prepareImportButton").disabled = selected === 0;
      $("#prepareImportButton").textContent = `Add ${selected} to queue`;
    });
  });
  $("#importPreviewList").querySelectorAll("[data-import-expand]").forEach((button) => {
    button.addEventListener("click", () => {
      state.importExpandedGroups.add(button.dataset.importExpand);
      renderImportPreview();
    });
  });
  $("#importPreviewList").querySelectorAll("[data-import-retry]").forEach((button) => button.addEventListener("click", () => {
    const request = payload.requests.find((item) => item.id === button.dataset.importRetry);
    if (!request) return;
    const serial = $(`[data-import-serial="${button.dataset.importRetry}"]`);
    const user = $(`[data-import-user="${button.dataset.importRetry}"]`);
    request.serials = [serial.value.trim()];
    if (request.kind === "location") {
      request.returning_user = user.value.trim();
      request.returning_user_info = null;
    } else request.user = user.value.trim();
    validateImportPreview([request]);
  }));

  $("#importIgnored").hidden = !payload.ignored.length;
  $("#importIgnoredList").innerHTML = payload.ignored.map((item) => `<li>${item.count} × ${escapeHtml(item.reason)}</li>`).join("");
}

async function validateImportPreview(retryRequests = null) {
  const payload = state.importPreview;
  if (!payload || !["connected", "simulation"].includes(state.connection?.state)) return;
  const requests = (retryRequests || payload.requests).filter((request) => request.included !== false);
  requests.forEach((request) => {
    request.import_validation = "checking";
    request.import_error = "";
    if (request.kind === "location") request.returning_user_loading = true;
  });
  renderImportPreview();
  $("#prepareImportButton").disabled = true;
  await runConcurrent(requests, async (request) => {
    const serial = request.serials[0];
    const username = request.user || request.returning_user;
    try {
      const [assets, users] = await Promise.all([
        api("/api/search/assets", { method: "POST", body: JSON.stringify({ query: serial, fresh: true }) }),
        api("/api/search/users", { method: "POST", body: JSON.stringify({ query: username, returning: request.kind === "location", fresh: true }) }),
      ]);
      const asset = (assets.results || []).find((item) => bestSerial(item, serial).toLowerCase() === serial.toLowerCase());
      const user = (users.results || []).find((item) => bestLogin(item, username).toLowerCase() === username.toLowerCase());
      if (!asset || !user) throw new Error(!asset ? "Serial number was not found in EUDM." : "Username was not found in EUDM.");
      if (request.kind === "location") {
        request.returning_user_info = { login: bestLogin(user, username), columns: (user.columns || [user.value]).map(String).filter(Boolean) };
      }
      request.import_validation = "valid";
    } catch (error) {
      request.import_validation = "failed";
      request.import_error = error.message || "Could not validate this request.";
    } finally {
      request.returning_user_loading = false;
    }
  }, 12);
  renderImportPreview();
  const selectable = payload.requests.filter((request) => request.included !== false && request.import_validation === "valid").length;
  $("#prepareImportButton").disabled = selectable === 0;
  const totalSelected = payload.requests.filter((request) => request.included !== false).length;
  $("#prepareImportButton").textContent = selectable === totalSelected ? `Add ${selectable} to queue` : `Add ${selectable} checked to queue`;
}

function backToImportSelection() {
  state.importPreview = null;
  $("#importPreview").hidden = true;
  $("#importConfigure").hidden = false;
  $("#backImportButton").hidden = true;
  $("#prepareImportButton").textContent = "Review import";
  $("#prepareImportButton").disabled = false;
  $("#importError").hidden = true;
  setImportStep(2);
}

async function prepareImport() {
  const button = $("#prepareImportButton");
  if (state.importPreview) {
    const requests = state.importPreview.requests
      .filter((request) => request.included !== false && request.import_validation === "valid")
      .map((request) => {
        const cleanRequest = { ...request };
        delete cleanRequest.included;
        return cleanRequest;
      });
    if (!requests.length) {
      $("#importError").textContent = "Select at least one deployment to add.";
      $("#importError").hidden = false;
      return;
    }
    state.queue.push(...requests);
    state.selectedId = requests[0]?.id || state.selectedId;
    const ignored = state.importPreview.ignored.reduce((sum, item) => sum + item.count, 0);
    $("#importDialog").close();
    renderAll();
    resolveQueueReturningUsers(requests);
    toast(`${requests.length} request${requests.length === 1 ? "" : "s"} added${ignored ? `; ${ignored} serial entries excluded` : ""}.`, "success");
    return;
  }
  button.disabled = true;
  try {
    if (!$("#importMapColumns").hidden) {
      await mapWorkbookColumns();
      return;
    }
    const mode = $('input[name="importMode"]:checked')?.value || "deployments";
    const payload = await api("/api/import/prepare", {
      method: "POST",
      body: JSON.stringify({
        import_id: state.workbook.import_id,
        sheet: $("#sheetInput").value,
        date: $("#dateInput").value,
        mode,
        location: state.importLocation,
      }),
    });
    payload.requests.forEach((request) => {
      request.included = true;
      normalizeRequestStatus(request);
    });
    state.importPreview = payload;
    state.importExpandedGroups.clear();
    $("#importConfigure").hidden = true;
    $("#importPreview").hidden = false;
    $("#backImportButton").hidden = false;
    button.textContent = `Add ${payload.counts.requests} to queue`;
    setImportStep(3);
    renderImportPreview();
    validateImportPreview();
  } catch (error) {
    $("#importError").textContent = error.message;
    $("#importError").hidden = false;
  } finally {
    if (state.importPreview) {
      button.disabled = false;
    } else {
      updateImportCounts();
    }
  }
}

async function openReview() {
  if (!state.queue.length || elements.reviewButton.disabled) return;
  if (bulkSerialValidationEnabled()) {
    const bulkOkay = await validateBulkSerials();
    if (!bulkOkay) {
      toast("Correct the bulk serial numbers EUDM could not verify.", "error");
      return;
    }
  }
  const validations = queueValidation();
  const invalid = [...validations.values()].filter((errors) => errors.length);
  $("#reviewList").innerHTML = state.queue.map((request) => {
    const errors = validations.get(request.id) || [];
    const secondary = request.source
      || (request.group && request.group !== kindLabel(request.kind) ? request.group : "");
    return `<div class="review-row">
      <span class="${errors.length ? "invalid-mark" : "ready-mark"}">${errors.length ? "!" : "✓"}</span>
      <div><strong>${escapeHtml(request.serials.join(", ") || "No serial")}</strong><small>${escapeHtml(kindLabel(request.kind))}${request.kind === "bulk_location" ? ` · ${request.serials.length} devices` : ""}</small></div>
      <div><strong>${escapeHtml(statusLabel(request))}</strong>${secondary ? `<small>${escapeHtml(secondary)}</small>` : ""}</div>
      <div><strong>${escapeHtml(destinationLabel(request))}</strong><small>${errors.length ? escapeHtml(errors[0]) : request.returning_user ? `Returned by ${escapeHtml(request.returning_user)}` : "Ready"}</small></div>
    </div>`;
  }).join("");
  $("#submitQueueButton").disabled = invalid.length > 0;
  $("#reviewDialog").showModal();
}

function progressStateSymbol(entry) {
  if (entry.state === "succeeded") return "✓";
  if (entry.state === "failed") return "!";
  if (entry.state === "running") return "…";
  return "·";
}

function progressStateLabel(entry) {
  if (entry.state === "succeeded") return "Deployed";
  if (entry.state === "failed") return "Failed";
  if (entry.state === "running") return "Deploying";
  return "Pending";
}

function recordSuccessfulLocations(job) {
  if (job.simulation || job.state !== "finished" || state.recordedLocationJobs.has(job.job_id)) return;
  job.entries
    .filter((entry) => entry.state === "succeeded" && entry.location)
    .forEach((entry) => rememberLocation(entry.location));
  state.recordedLocationJobs.add(job.job_id);
}

function renderProgress(job) {
  state.currentJob = job;
  const entriesById = new Map((job.entries || []).map((entry) => [entry.id, entry]));
  state.queue.forEach((request) => {
    const entry = entriesById.get(request.id);
    if (!entry) return;
    request.request_id = entry.request_id || request.request_id || "";
    request.order_id = entry.order_id || request.order_id || "";
    request.result_state = entry.state;
    request.result_message = entry.message || "";
  });
  renderQueue();
  recordSuccessfulLocations(job);
  const done = job.counts.succeeded + job.counts.failed;
  const percentage = job.counts.total ? (done / job.counts.total) * 100 : 0;
  $("#progressBar").style.width = `${percentage}%`;
  $("#progressCounts").textContent = `${done} of ${job.counts.total} complete · ${job.counts.devices} devices`;
  const spinnerDelay = -(performance.now() % 720);
  $("#progressList").innerHTML = job.entries.map((entry) => {
    return `
    <div class="progress-row ${entry.state}">
      <span class="progress-state" aria-label="${progressStateLabel(entry)}">${entry.state === "running" ? `<i class="activity-spinner" style="animation-delay:${spinnerDelay}ms"></i>` : progressStateSymbol(entry)}</span>
      <div class="progress-device"><strong>${escapeHtml(entry.serials.join(", "))}</strong><small>${escapeHtml(kindLabel(entry.kind))} · ${escapeHtml(entry.status)}</small></div>
      <div class="progress-message"><div class="progress-message-title"><span class="progress-status ${entry.state}">${progressStateLabel(entry)}</span><strong>${escapeHtml(entry.message)}</strong></div><small>${escapeHtml(entry.destination)}${entry.returning_user ? ` · returned by ${escapeHtml(entry.returning_user)}` : ""}</small></div>
      <div class="request-id">${entry.request_id
        ? requestIdDisplay(entry.request_id, "progress-request-id")
        : entry.state === "queued" ? "Queued" : "Preparing"}</div>
    </div>`;
  }).join("");
  const finished = job.state === "finished";
  $("#progressHeading").textContent = finished
    ? `${job.counts.succeeded} deployed, ${job.counts.failed} failed`
    : "Submitting requests";
  $("#progressActions").hidden = !finished;
  $("#closeProgressButton").hidden = !finished;
  $("#downloadResultsLink").href = `/api/jobs/${job.job_id}/results.txt`;
}

function formatHistoryDate(value) {
  if (!value) return "Unknown time";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function renderHistory(runs) {
  if (!runs.length) {
    elements.historyList.innerHTML = '<div class="history-empty">No request runs yet.</div>';
    return;
  }
  elements.historyList.innerHTML = runs.map((run) => {
    const succeeded = run.counts?.succeeded || 0;
    const failed = run.counts?.failed || 0;
    const stateLabel = run.state === "finished"
      ? `${succeeded} deployed · ${failed} failed`
      : run.state;
    const entries = (run.entries || []).map((entry) => {
      const requestLink = entry.request_id
        ? requestIdDisplay(entry.request_id, "history-request-id")
        : '<strong class="history-request-id">No request ID</strong>';
      return `<div class="history-entry ${entry.state === "failed" ? "failed" : ""}">
        <div><strong>${escapeHtml(entry.serials.join(", ") || "No serial")}</strong><small>${escapeHtml(kindLabel(entry.kind))} · ${escapeHtml(entry.status)}</small></div>
        <div><strong>${escapeHtml(entry.destination || "No destination")}</strong><small>${escapeHtml(entry.message || "")}</small></div>
        <div>${requestLink}</div>
      </div>`;
    }).join("");
    return `<section class="history-run">
      <div class="history-run-header">
        <div><strong>${escapeHtml(formatHistoryDate(run.created_at))}</strong><small>${run.simulation ? "Debug simulation · " : ""}${escapeHtml(run.request_for || "Unknown requester")} · ${run.counts?.devices || 0} devices</small></div>
        <span class="history-run-state ${failed ? "failed" : ""}">${escapeHtml(stateLabel)}</span>
      </div>
      <div class="history-entries">${entries}</div>
    </section>`;
  }).join("");
}

async function openHistory() {
  elements.historyButton.disabled = true;
  try {
    const payload = await api("/api/history");
    renderHistory(payload.runs || []);
    $("#historyDialog").showModal();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    elements.historyButton.disabled = false;
  }
}

async function pollJob(jobId) {
  let job;
  try {
    job = await api(`/api/jobs/${jobId}`);
  } catch (error) {
    toast("Could not refresh request status. Trying again…", "error");
    state.pollTimer = setTimeout(() => pollJob(jobId), 1500);
    return;
  }
  try {
    renderProgress(job);
  } catch (error) {
    console.error("Could not render submission status", error);
    $("#progressCounts").textContent = "Submission is running. Refreshing status…";
    state.pollTimer = setTimeout(() => pollJob(jobId), 1500);
    return;
  }
  if (job.state !== "finished") {
    state.pollTimer = setTimeout(() => pollJob(jobId), 650);
  } else {
    refreshConnection();
    const type = job.counts.failed ? "error" : "success";
    toast(`${job.counts.succeeded} request${job.counts.succeeded === 1 ? "" : "s"} submitted; ${job.counts.failed} failed.`, type);
  }
}

async function submitQueue() {
  const button = $("#submitQueueButton");
  button.disabled = true;
  let job;
  try {
    job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        requests: state.queue,
        concurrency: Number(elements.concurrency.value),
      }),
    });
  } catch (error) {
    if (error.payload?.validation) {
      toast("Some requests need attention. Return to the queue to correct them.", "error");
    } else {
      toast(error.message, "error");
    }
    button.disabled = false;
    return;
  }
  $("#reviewDialog").close();
  $("#progressDialog").showModal();
  try {
    renderProgress(job);
  } catch (error) {
    console.error("Could not render initial submission status", error);
    $("#progressHeading").textContent = "Submitting requests";
    $("#progressCounts").textContent = "Request accepted. Loading status…";
  }
  pollJob(job.job_id);
}

function bindEvents() {
  elements.concurrency.addEventListener("change", () => {
    try {
      localStorage.setItem(CONCURRENCY_STORAGE_KEY, elements.concurrency.value);
    } catch (_) {}
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-copy-request-id]");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    copyRequestId(button);
  });
  $("#themeToggle").addEventListener("click", toggleTheme);
  $("#shortcutsButton").addEventListener("click", openShortcuts);
  $("#newRequestButton").addEventListener("click", startNewRequest);
  $("#saveNewRequestButton").addEventListener("click", saveNewRequest);
  $("#cancelNewRequestButton").addEventListener("click", () => $("#newRequestDialog").close());
  $("#discardNewRequestButton").addEventListener("click", () => $("#newRequestDialog").close());
  $("#newRequestDialog").addEventListener("close", () => {
    if (state.newRequest) discardNewRequest();
  });
  $("#pastePairsButton").addEventListener("click", openPasteDialog);
  $("#addPairsButton").addEventListener("click", addPairs);
  $("#pairsApplyBulkButton").addEventListener("click", applyQuickImportKind);
  $("#pairsConfirmAddButton").addEventListener("click", addQuickImportEntry);
  $("#pairsAddListButton").addEventListener("click", addQuickImportList);
  $("#pairsTextModeButton").addEventListener("click", () => {
    const visible = !$("#pairsTextMode").hidden;
    $("#pairsTextMode").hidden = visible;
    $("#pairsEntry .quick-import-add").hidden = !visible;
    $("#pairsTextModeButton").textContent = visible ? "Add a list instead" : "Use one-at-a-time entry";
    if (!visible) $("#pairsInput").focus();
  });
  $("#pairsAddUsername").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addQuickImportEntry();
    }
  });
  $("#pairsAddSerial").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addQuickImportEntry();
    }
  });
  $("#pairsAddSerial").addEventListener("input", () => { $("#pairsError").hidden = true; });
  $("#pairsAddUsername").addEventListener("input", () => { $("#pairsError").hidden = true; });
  $("#pairsFindLocationsButton").addEventListener("click", findPasteLocations);
  $("#pairsCityInput").addEventListener("change", () => {
    state.pasteLocation = {
      city: $("#pairsCityInput").value,
      building: "",
      floor: "",
      room: "",
      cabinet: "",
    };
    state.pasteLocationResults = [];
    renderPasteLocationFields();
    findPasteLocations({ quiet: true });
  });
  $("#pairsLocationInput").addEventListener("change", () => {
    const value = $("#pairsLocationInput").value;
    if (value === "current") return;
    if (!value) {
      state.pasteLocation = {
        city: $("#pairsCityInput").value,
        building: "",
        floor: "",
        room: "",
        cabinet: "",
      };
      renderPasteLocationFields();
      return;
    }
    const result = state.pasteLocationResults[Number(value)];
    if (!result) return;
    const [building = "", floor = "", room = "", cabinet = ""] = result.columns;
    state.pasteLocation = {
      city: $("#pairsCityInput").value,
      building,
      floor,
      room,
      cabinet,
    };
    renderPasteLocationFields();
  });
  $("#pairsInput").addEventListener("input", () => { $("#pairsError").hidden = true; });
  $("#settingsButton").addEventListener("click", openSettings);
  $$('[data-settings-tab]').forEach((tab) => tab.addEventListener("click", () => {
    $$('[data-settings-tab]').forEach((item) => item.classList.toggle("active", item === tab));
    $$('[data-settings-panel]').forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== tab.dataset.settingsTab; });
  }));
  $("#saveSettingsButton").addEventListener("click", async () => {
    const button = $("#saveSettingsButton");
    const url = $("#spreadsheetUrlInput").value.trim();
    const columns = {
      username: $("#spreadsheetUsernameColumnInput").value.trim(),
      deployment_serial: $("#spreadsheetDeploymentColumnInput").value.trim(),
      returned_device: $("#spreadsheetReturnedColumnInput").value.trim(),
      pending_return: $("#spreadsheetPendingColumnInput").value.trim(),
      enabled: $("#spreadsheetEnabledColumnInput").value.trim(),
    };
    if (!columns.username || !columns.deployment_serial || !columns.pending_return) {
      toast("Set the username, deployment serial, and pending return columns.", "error");
      return;
    }
    if (url && !/^https:\/\//i.test(url)) { toast("Use a full https ALM Workbook link.", "error"); return; }
    const preferences = {
      concurrency: Number(elements.concurrency.value),
      validate_bulk_serials: $("#validateBulkSerialsInput").checked,
      workbook_url: url,
      workbook_headless: $("#spreadsheetHeadlessInput").checked,
      import_columns: columns,
    };
    button.disabled = true;
    try {
      state.preferences = await api("/api/preferences", {
        method: "POST",
        body: JSON.stringify(preferences),
      });
      if (url) localStorage.setItem(SPREADSHEET_URL_STORAGE_KEY, url);
      else localStorage.removeItem(SPREADSHEET_URL_STORAGE_KEY);
      localStorage.setItem(IMPORT_COLUMNS_STORAGE_KEY, JSON.stringify(columns));
      localStorage.setItem(BULK_SERIAL_VALIDATION_STORAGE_KEY, String($("#validateBulkSerialsInput").checked));
      localStorage.setItem(SPREADSHEET_HEADLESS_STORAGE_KEY, String($("#spreadsheetHeadlessInput").checked));
      localStorage.setItem(CONCURRENCY_STORAGE_KEY, elements.concurrency.value);
      $("#downloadSheetButton").hidden = !state.config.spreadsheet_import_enabled || !url;
      $("#settingsDialog").close();
      toast("Settings saved.", "success");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
  $("#importSheetButton").addEventListener("click", openAlmWorkbookImport);
  $("#downloadSheetButton").addEventListener("click", () => {
    if (!savedSpreadsheetUrl()) {
      openSettings();
      toast("Add the ALM Workbook link in Settings first.", "error");
      return;
    }
    openAlmWorkbookImport();
    downloadSavedWorkbook();
  });
  $("#importDialog").addEventListener("close", () => {
    state.importUploadToken += 1;
    state.importLoginJobId = null;
    $("#importLoginCompleteButton").hidden = true;
    $("#downloadSheetButton").disabled = false;
    setImportBusy(false);
  });
  $("#importLoginCompleteButton").addEventListener("click", async () => {
    const jobId = state.importLoginJobId;
    if (!jobId) return;
    $("#importLoginCompleteButton").disabled = true;
    try {
      await api(`/api/imports/${encodeURIComponent(jobId)}/continue`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      state.importLoginJobId = null;
      $("#importLoginCompleteButton").hidden = true;
    } catch (error) {
      $("#importLoginCompleteButton").disabled = false;
      toast(error.message, "error");
    }
  });
  $("#workbookInput").addEventListener("change", (event) => {
    if (event.target.files[0]) uploadWorkbook(event.target.files[0]);
  });
  $("#changeFileButton").addEventListener("click", resetImportDialog);
  $("#changeMappedFileButton").addEventListener("click", resetImportDialog);
  $("#changeColumnsButton").addEventListener("click", openImportColumnMapping);
  $("#importMapSheet").addEventListener("change", renderImportColumnMap);
  [
    "#importMapUsername",
    "#importMapDeployment",
    "#importMapReturned",
    "#importMapPending",
    "#importMapEnabled",
  ].forEach((selector) => $(selector).addEventListener("change", updateImportColumnMapButton));
  $("#sheetInput").addEventListener("change", updateImportDates);
  $("#dateInput").addEventListener("change", updateImportCounts);
  $$('input[name="importMode"]').forEach((radio) => radio.addEventListener("change", updateImportCounts));
  $("#importCityInput").addEventListener("change", () => {
    state.importLocation = { city: $("#importCityInput").value, building: "", floor: "", room: "", cabinet: "" };
    state.importLocationResults = [];
    fetchImportLocations(state.importLocation.city);
  });
  $("#importLocationInput").addEventListener("change", () => {
    const result = state.importLocationResults[Number($("#importLocationInput").value)];
    if (!result) return;
    const [building = "", floor = "", room = "", cabinet = ""] = result.columns;
    state.importLocation = { city: $("#importCityInput").value, building, floor, room, cabinet };
    rememberImportLocation(state.importLocation);
    renderImportLocationFields();
  });
  $("#requestSizeInput").addEventListener("change", () => changeRequestSize($("#requestSizeInput").value));
  $("#prepareImportButton").addEventListener("click", prepareImport);
  $("#backImportButton").addEventListener("click", backToImportSelection);
  elements.reviewButton.addEventListener("click", openReview);
  $("#submitQueueButton").addEventListener("click", submitQueue);
  elements.clearQueueButton.addEventListener("click", () => {
    if (!state.queue.length || confirm(`Remove all ${state.queue.length} prepared requests?`)) {
      state.queue = [];
      state.selectedId = null;
      renderAll();
    }
  });
  elements.connectButton.addEventListener("click", connect);
  elements.connectionGateButton.addEventListener("click", connect);
  elements.historyButton.addEventListener("click", openHistory);
  $("#duplicateButton").addEventListener("click", duplicateSelected);
  $("#removeButton").addEventListener("click", () => removeRequest(state.selectedId));
  $("#searchSerialButton").addEventListener("click", searchAssets);
  $("#searchUserButton").addEventListener("click", () => searchUsers(false));
  $("#searchReturningButton").addEventListener("click", () => searchUsers(true));
  $("#loadLocationsButton").addEventListener("click", loadLocations);
  elements.serialInput.addEventListener("input", () => {
    hideSearchResults();
    const request = selectedRequest();
    if (!request) return;
    request.serials = elements.serialInput.value.trim() ? [elements.serialInput.value.trim()] : [];
    renderQueue();
  });
  elements.serialsInput.addEventListener("input", () => {
    const request = selectedRequest();
    if (!request) return;
    request.serials = parseSerials(elements.serialsInput.value);
    request.bulk_validation = "stale";
    request.bulk_validation_error = "";
    elements.serialHint.textContent = `${request.serials.length} serial${request.serials.length === 1 ? "" : "s"}`;
    renderQueue();
  });
  elements.statusInput.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request) return;
    applyInferredKind(request, elements.statusInput.value);
    renderAll();
  });
  elements.userInput.addEventListener("input", () => {
    hideSearchResults();
    const request = selectedRequest();
    if (!request) return;
    request.user = elements.userInput.value.trim();
    renderQueue();
  });
  elements.cityInput.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request) return;
    request.location = { city: elements.cityInput.value, building: "", floor: "", room: "", cabinet: "" };
    elements.locationInput.innerHTML = '<option value="">Loading locations…</option>';
    elements.locationDetail.textContent = "";
    renderQueue();
    loadLocations({ quiet: true });
  });
  elements.locationInput.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request || elements.locationInput.value === "current" || !elements.locationInput.value) return;
    const results = JSON.parse(elements.locationInput.dataset.results || "[]");
    const result = results[Number(elements.locationInput.value)];
    if (!result) return;
    const [building = "", floor = "", room = "", cabinet = ""] = result.columns;
    request.location = { city: elements.cityInput.value, building, floor, room, cabinet };
    elements.locationDetail.textContent = "";
    renderQueue();
  });
  elements.returningToggle.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request) return;
    request.returning = elements.returningToggle.checked;
    if (!request.returning) {
      request.returning_user = "";
      request.returning_user_info = null;
    }
    elements.returningSearch.hidden = !elements.returningToggle.checked;
    elements.returnConfirmation.hidden = !elements.returningToggle.checked;
    renderQueue();
  });
  elements.returningUserInput.addEventListener("input", () => {
    hideSearchResults();
    const request = selectedRequest();
    if (!request) return;
    request.returning_user = elements.returningUserInput.value.trim();
    request.returning_user_info = null;
    renderReturningUserInfo(request);
    renderQueue();
  });
  elements.serialInput.addEventListener("keydown", (event) => { if (event.key === "Enter") searchAssets(); });
  elements.userInput.addEventListener("keydown", (event) => { if (event.key === "Enter") searchUsers(false); });
  elements.returningUserInput.addEventListener("keydown", (event) => { if (event.key === "Enter") searchUsers(true); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && [elements.serialResults, elements.userResults, elements.returningResults].some((node) => !node.hidden)) {
      hideSearchResults();
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest(".search-control, .search-results")) hideSearchResults();
  });
  $("#doneButton").addEventListener("click", () => $("#progressDialog").close());
  $("#closeProgressButton").addEventListener("click", () => $("#progressDialog").close());
  $("#progressDialog").addEventListener("close", () => {
    if (state.currentJob?.state === "finished") renderAll();
  });
  document.addEventListener("keydown", (event) => {
    const mac = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
    const shortcut = mac
      ? (event.metaKey && event.ctrlKey)
      : (event.ctrlKey && !event.metaKey);
    const editable = event.target instanceof Element
      && Boolean(event.target.closest("input, textarea, select, [contenteditable='true']"));
    const dialogOpen = Boolean($("dialog[open]"));

    if (!shortcut && !event.altKey && !editable && !dialogOpen && event.key === "?") {
      event.preventDefault();
      openShortcuts();
      return;
    }
    if (!shortcut || event.altKey || dialogOpen) return;

    if (event.key === "Enter" && !elements.reviewButton.disabled) {
      event.preventDefault();
      openReview();
      return;
    }
    if (editable) return;

    const key = event.key.toLowerCase();
    if (event.shiftKey && key === "d" && !$("#downloadSheetButton").hidden && !$("#downloadSheetButton").disabled) {
      event.preventDefault();
      $("#downloadSheetButton").click();
      return;
    }
    if (event.shiftKey && key === "h") {
      event.preventDefault();
      openHistory();
      return;
    }
    if (event.shiftKey) return;

    if (key === "n") {
      event.preventDefault();
      startNewRequest();
    } else if (key === "i") {
      event.preventDefault();
      openPasteDialog();
    } else if (key === "o") {
      event.preventDefault();
      openAlmWorkbookImport();
    } else if (key === ",") {
      event.preventDefault();
      openSettings();
    }
  });
  document.addEventListener("keydown", (event) => {
    const editable = event.target instanceof Element
      && Boolean(event.target.closest("input, textarea, select, [contenteditable='true']"));
    if (!editable && !$("dialog[open]") && !event.metaKey && !event.ctrlKey && !event.altKey && event.key === "/") {
      event.preventDefault();
      focusSelectedSerial();
    }
  });
}

async function init() {
  try {
    updateThemeButton();
    const updateForSystemTheme = () => {
      if (!savedTheme()) updateThemeButton();
    };
    if (systemTheme.addEventListener) {
      systemTheme.addEventListener("change", updateForSystemTheme);
    } else {
      systemTheme.addListener(updateForSystemTheme);
    }
    [state.config, state.preferences] = await Promise.all([
      api("/api/config"),
      api("/api/preferences"),
    ]);
    const spreadsheetEnabled = Boolean(state.config.spreadsheet_import_enabled);
    $("#importSheetButton").hidden = !spreadsheetEnabled;
    $("#downloadSheetButton").hidden = !spreadsheetEnabled || !savedSpreadsheetUrl();
    const spreadsheetSettings = $('[data-settings-tab="spreadsheet"]');
    if (spreadsheetSettings) spreadsheetSettings.hidden = !spreadsheetEnabled;
    configureConcurrency(state.config.concurrency);
    bindEvents();
    await refreshConnection();
    state.connectionHeartbeatTimer = window.setInterval(checkConnection, 30_000);
    renderAll();
  } catch (error) {
    document.body.innerHTML = `<main class="empty-state"><h1>AutoEUDM could not start</h1><p>${escapeHtml(error.message)}</p></main>`;
  }
}

init();
