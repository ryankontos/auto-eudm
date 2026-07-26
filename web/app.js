const state = {
  config: null,
  queue: [],
  selectedId: null,
  connection: null,
  workbook: null,
  importPreview: null,
  currentJob: null,
  pollTimer: null,
  liveOptionsLoaded: false,
};

const THEME_STORAGE_KEY = "auto-eudm-theme";
const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const elements = {
  connectionBadge: $("#connectionBadge"),
  connectButton: $("#connectButton"),
  requestForValue: $("#requestForValue"),
  requestForSource: $("#requestForSource"),
  concurrency: $("#concurrencyInput"),
  queueEmpty: $("#queueEmpty"),
  queueTableWrap: $("#queueTableWrap"),
  queueBody: $("#queueBody"),
  queueCounts: $("#queueCounts"),
  connectionGate: $("#connectionGate"),
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

function parseSerials(raw) {
  return String(raw || "").split(/[\s,;]+/).map((value) => value.trim()).filter(Boolean);
}

function selectedRequest() {
  return state.queue.find((request) => request.id === state.selectedId) || null;
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
    const error = new Error(payload.error || "The request could not be completed.");
    error.payload = payload;
    throw error;
  }
  return payload;
}

function defaultLocation() {
  return { ...state.config.default_location };
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
    location: user ? null : defaultLocation(),
    group: kind === "bulk_location" ? "Bulk location" : user ? "User deployments" : "Location deployments",
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

function validateRequest(request) {
  const errors = [];
  if (!["user", "location", "bulk_location"].includes(request.kind)) {
    return ["Choose a deployment type."];
  }
  if (!request.serials.length) errors.push("Enter at least one serial number.");
  if (request.kind !== "bulk_location" && request.serials.length !== 1) {
    errors.push("This request must contain exactly one serial number.");
  }
  const seen = new Set();
  for (const serial of request.serials) {
    if (!/^[A-Za-z0-9._-]+$/.test(serial)) {
      errors.push(`Serial ${serial} contains unsupported characters.`);
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
    if (!userStatusValues().has(request.status)) errors.push("Choose a user deployment status.");
    if (!request.user.trim()) errors.push("Choose the user receiving the device.");
    if (request.returning || request.returning_user) errors.push("A user deployment cannot include a returning user.");
    if (request.location) errors.push("A user deployment cannot include a location.");
  } else {
    if (!locationStatusValues().has(request.status)) errors.push("Choose a location deployment status.");
    if (request.user) errors.push("A location deployment cannot include a deployed-to user.");
    const location = request.location || {};
    if (![location.city, location.building, location.floor, location.room].every((value) => String(value || "").trim())) {
      errors.push("Choose both the city and the exact location.");
    }
    if (request.returning && !request.returning_user.trim()) {
      errors.push("Choose the returning user or turn off the return option.");
    }
    if (request.kind === "bulk_location" && request.returning) {
      errors.push("Bulk location requests cannot include a returning user.");
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

function statusLabel(request) {
  const options = request.kind === "user" ? state.config.user_statuses : state.config.location_statuses;
  return options.find((option) => option.value === request.status)?.label || request.status || "Not selected";
}

function kindLabel(kind) {
  return ({ user: "User", location: "Location", bulk_location: "Bulk location" })[kind] || "Unknown";
}

function destinationLabel(request) {
  if (request.kind === "user") return request.user || "No user selected";
  const location = request.location || {};
  const parts = [location.building, location.floor, location.room, location.cabinet].filter(Boolean);
  return parts.length ? parts.join(" → ") : "No location selected";
}

function renderQueue() {
  const validations = queueValidation();
  const requestCount = state.queue.length;
  const deviceCount = state.queue.reduce((sum, request) => sum + request.serials.length, 0);
  const invalidCount = [...validations.values()].filter((errors) => errors.length).length;
  elements.queueCounts.textContent = `${requestCount} request${requestCount === 1 ? "" : "s"} · ${deviceCount} device${deviceCount === 1 ? "" : "s"}${invalidCount ? ` · ${invalidCount} needs attention` : ""}`;
  elements.queueEmpty.hidden = requestCount > 0;
  elements.queueTableWrap.hidden = requestCount === 0;
  const runtimeReady = state.connection?.state === "simulation"
    || state.connection?.state === "connected";
  const requesterReady = Boolean(state.connection?.request_for || state.config?.request_for);
  elements.reviewButton.disabled = requestCount === 0
    || invalidCount > 0
    || !runtimeReady
    || !requesterReady;

  elements.queueBody.innerHTML = state.queue.map((request, index) => {
    const errors = validations.get(request.id) || [];
    const serialDisplay = request.serials.length ? request.serials.join(", ") : "No serial";
    const selected = request.id === state.selectedId;
    return `
      <tr data-id="${escapeHtml(request.id)}" class="${selected ? "selected" : ""} ${errors.length ? "invalid" : ""}" tabindex="0">
        <td class="index-column">${index + 1}</td>
        <td><span class="cell-primary">${escapeHtml(serialDisplay)}</span><span class="cell-secondary">${request.serials.length} device${request.serials.length === 1 ? "" : "s"}</span></td>
        <td><span class="cell-primary">${escapeHtml(kindLabel(request.kind))}</span><span class="cell-secondary">${escapeHtml(request.group)}</span></td>
        <td title="${escapeHtml(statusLabel(request))}">${escapeHtml(statusLabel(request))}</td>
        <td title="${escapeHtml(destinationLabel(request))}"><span class="cell-primary">${escapeHtml(destinationLabel(request))}</span>${request.returning_user ? `<span class="cell-secondary">Returned by ${escapeHtml(request.returning_user)}</span>` : ""}</td>
        <td class="state-column" title="${escapeHtml(errors.join(" "))}">${errors.length ? '<span class="invalid-mark">!</span>' : '<span class="ready-mark">✓</span>'}</td>
        <td><button class="row-menu" data-remove="${escapeHtml(request.id)}" aria-label="Remove request" title="Remove request"><span class="trash-icon" aria-hidden="true"></span></button></td>
      </tr>`;
  }).join("");

  elements.queueBody.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("[data-remove]")) return;
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
  }
  const errors = queueValidation().get(request.id) || [];
  elements.validationPanel.hidden = !errors.length;
  elements.validationPanel.innerHTML = errors.length
    ? `<ul>${errors.map((error) => `<li>${escapeHtml(error)}</li>`).join("")}</ul>`
    : "";
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

  $$('input[name="requestKind"]').forEach((radio) => {
    const selected = radio.value === request.kind;
    radio.checked = selected;
    radio.parentElement.classList.toggle("is-selected", selected);
    radio.setAttribute("aria-checked", String(selected));
  });
  const bulk = request.kind === "bulk_location";
  const user = request.kind === "user";
  elements.serialInput.hidden = bulk;
  elements.serialsInput.hidden = !bulk;
  $("#searchSerialButton").hidden = bulk;
  elements.serialLabel.textContent = bulk ? "Serial numbers" : "Serial number";
  elements.serialInput.value = bulk ? "" : (request.serials[0] || "");
  elements.serialsInput.value = bulk ? request.serials.join("\n") : "";
  elements.serialHint.textContent = bulk ? `${request.serials.length} serial${request.serials.length === 1 ? "" : "s"}` : "";

  const statusOptions = user ? state.config.user_statuses : state.config.location_statuses;
  normalizeRequestStatus(request);
  fillSelect(elements.statusInput, statusOptions, request.status, "Choose a status");
  elements.userFields.hidden = !user;
  elements.locationFields.hidden = user;
  elements.userInput.value = request.user || "";

  if (!user) {
    fillSelect(elements.cityInput, state.config.cities.map((city) => ({ label: city, value: city })), request.location?.city || "", "Choose a city");
    const location = request.location || {};
    const hasExact = [location.building, location.floor, location.room].every(Boolean);
    elements.locationInput.innerHTML = hasExact
      ? `<option value="current">${escapeHtml([location.building, location.floor, location.room, location.cabinet].filter(Boolean).join(" → "))}</option>`
      : '<option value="">Choose a city, then find locations</option>';
    elements.locationDetail.textContent = hasExact ? `City: ${location.city}` : "";
    elements.returningUserFields.hidden = bulk;
    elements.returningToggle.checked = Boolean(request.returning);
    elements.returningSearch.hidden = !request.returning;
    elements.returningUserInput.value = request.returning_user || "";
    elements.returnConfirmation.hidden = !request.returning;
    $("#confirmSerial").textContent = request.serials[0] || "Not selected";
    $("#confirmUser").textContent = request.returning_user || "Not selected";
    $("#confirmLocation").textContent = destinationLabel(request);
  }

  refreshSelectedValidation();
}

function renderAll() {
  renderQueue();
  renderInspector();
}

function addRequest(kind) {
  const request = makeRequest(kind);
  state.queue.push(request);
  state.selectedId = request.id;
  renderAll();
  if (kind === "bulk_location") {
    setTimeout(() => elements.serialsInput.focus(), 0);
  } else {
    setTimeout(() => elements.serialInput.focus(), 0);
  }
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

function changeKind(kind) {
  const request = selectedRequest();
  if (!request || request.kind === kind) return;
  const wasUser = request.kind === "user";
  request.kind = kind;
  request.group = kind === "user" ? "User deployments" : kind === "location" ? "Location deployments" : "Bulk location";
  if (kind === "user") {
    request.location = null;
    request.returning = false;
    request.returning_user = "";
    request.status = resolveStatus(
      state.config.user_statuses,
      request.status,
      state.config.default_user_status,
    );
    request.serials = request.serials.slice(0, 1);
  } else {
    request.user = "";
    request.location = wasUser || !request.location ? defaultLocation() : request.location;
    request.status = resolveStatus(
      state.config.location_statuses,
      request.status,
      state.config.default_location_status,
    );
    if (kind === "location") request.serials = request.serials.slice(0, 1);
    if (kind === "bulk_location") {
      request.returning = false;
      request.returning_user = "";
    }
  }
  renderAll();
}

function hideSearchResults() {
  [elements.serialResults, elements.userResults, elements.returningResults].forEach((node) => { node.hidden = true; });
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
        onSelect(results[Number(button.dataset.index)]);
        container.hidden = true;
      });
    });
  }
  container.hidden = false;
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

async function loadLocations() {
  const request = selectedRequest();
  if (!request || request.kind === "user") return;
  const city = elements.cityInput.value;
  if (!city) return toast("Choose a city first.", "error");
  $("#loadLocationsButton").disabled = true;
  elements.locationInput.innerHTML = '<option>Loading locations…</option>';
  try {
    const payload = await api("/api/search/locations", {
      method: "POST",
      body: JSON.stringify({ city }),
    });
    elements.locationInput.innerHTML = [
      '<option value="">Choose an exact location</option>',
      ...payload.results.map((result, index) => `<option value="${index}">${escapeHtml(result.columns.filter(Boolean).join(" → "))}</option>`),
    ].join("");
    elements.locationInput.dataset.results = JSON.stringify(payload.results);
    request.location = { city, building: "", floor: "", room: "", cabinet: "" };
    renderQueue();
    elements.locationDetail.textContent = `${payload.results.length} location${payload.results.length === 1 ? "" : "s"} found`;
  } catch (error) {
    elements.locationInput.innerHTML = '<option value="">Could not load locations</option>';
    toast(error.message, "error");
  } finally {
    $("#loadLocationsButton").disabled = false;
  }
}

function updateConnection(status) {
  state.connection = status;
  elements.connectionBadge.className = `connection-badge ${status.state}`;
  const label = status.state === "simulation" ? "Simulation ready"
    : status.state === "connected" ? "EUDM connected"
    : status.state === "connecting" ? "Connecting"
    : status.state === "error" ? "Connection failed"
    : "Not connected";
  elements.connectionBadge.querySelector("span:last-child").textContent = label;
  elements.connectionBadge.title = status.message || "";
  elements.connectButton.hidden = ["simulation", "connected"].includes(status.state);
  elements.connectButton.disabled = status.state === "connecting";
  elements.connectButton.textContent = status.state === "error" ? "Try again" : status.state === "connecting" ? "Connecting…" : "Connect to EUDM";
  const needsConnection = !state.config.simulation && !["connected", "simulation"].includes(status.state);
  elements.connectionGate.hidden = !needsConnection;
  elements.connectionGateTitle.textContent = status.state === "connecting"
    ? "Connecting to EUDM…"
    : status.state === "error"
      ? "EUDM connection needed before submitting"
      : "Connect to EUDM before submitting";
  elements.connectionGateMessage.textContent = status.state === "error"
    ? status.message || "The authenticated EUDM session could not be established."
    : status.state === "connecting"
      ? "Complete authentication if prompted. The queue will unlock when EUDM is ready."
      : "A real run needs an authenticated EUDM connection. Your prepared queue is saved here.";
  elements.connectionGateButton.hidden = status.state === "connecting";
  elements.connectionGateButton.disabled = status.state === "connecting";
  const requester = status.request_for || state.config.request_for || "";
  elements.requestForValue.textContent = requester || "Waiting for EUDM";
  elements.requestForSource.textContent = status.state === "simulation"
    ? "From the shared simulation environment"
    : status.request_for_source === "EUDM signed-in account"
      ? "Detected from the signed-in EUDM session"
      : requester
        ? "From the shared environment"
        : "Resolved automatically after connection";
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

async function connect() {
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
  const defaultMode = $('input[name="pairsMode"][value="user"]');
  if (defaultMode) defaultMode.checked = true;
  updatePasteMode();
  $("#pasteDialog").showModal();
  setTimeout(() => $("#pairsInput").focus(), 0);
}

function updatePasteMode() {
  const locationMode = $('input[name="pairsMode"]:checked')?.value === "location";
  const options = locationMode ? state.config.location_statuses : state.config.user_statuses;
  const defaultStatus = locationMode
    ? state.config.default_location_status
    : state.config.default_user_status;
  fillSelect(
    $("#pairsStatus"),
    options,
    resolveStatus(options, defaultStatus, defaultStatus),
  );
  $("#pairsFormat").textContent = locationMode
    ? "Enter one device and returning user per line"
    : "Enter one device and receiving user per line";
  $("#pairsHelp").innerHTML = locationMode
    ? 'Format: <code>SERIAL USERNAME</code>. Each line deploys a device to the configured location and records who returned it.'
    : 'Format: <code>SERIAL USERNAME</code>. Each line becomes one user deployment.';
  $("#addPairsButton").textContent = locationMode
    ? "Add location returns"
    : "Add user deployments";

  const locationNotice = $("#pairsLocation");
  locationNotice.hidden = !locationMode;
  if (!locationMode) return;
  const location = defaultLocation();
  const complete = [location.city, location.building, location.floor, location.room]
    .every((value) => String(value || "").trim());
  locationNotice.classList.toggle("incomplete", !complete);
  locationNotice.textContent = complete
    ? `Destination: ${[location.city, location.building, location.floor, location.room, location.cabinet].filter(Boolean).join(" → ")}`
    : "No complete default location is configured. Set the city, building, floor and room in .env before adding these requests.";
}

function addPairs() {
  const locationMode = $('input[name="pairsMode"]:checked')?.value === "location";
  const lines = $("#pairsInput").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const parsed = [];
  const errors = [];
  lines.forEach((line, index) => {
    const parts = line.split(/\s+/);
    if (parts.length !== 2) errors.push(`Line ${index + 1} needs exactly one serial and one username.`);
    else parsed.push({ serial: parts[0], username: parts[1] });
  });
  if (!parsed.length && !errors.length) errors.push("Paste at least one serial and username.");
  if (locationMode) {
    const location = defaultLocation();
    if (![location.city, location.building, location.floor, location.room]
      .every((value) => String(value || "").trim())) {
      errors.push("Configure a complete default location in .env before adding location returns.");
    }
  }
  if (errors.length) {
    $("#pairsError").textContent = errors.slice(0, 4).join(" ");
    $("#pairsError").hidden = false;
    return;
  }
  const status = $("#pairsStatus").value;
  const requests = parsed.map(({ serial, username }) => {
    const request = makeRequest(locationMode ? "location" : "user");
    request.serials = [serial];
    request.status = status;
    request.source = "Pasted device and user pairs";
    if (locationMode) {
      request.returning = true;
      request.returning_user = username;
      request.group = "Pasted location returns";
    } else {
      request.user = username;
      request.group = "Pasted user deployments";
    }
    return request;
  });
  state.queue.push(...requests);
  state.selectedId = requests[0].id;
  $("#pasteDialog").close();
  renderAll();
  toast(`${requests.length} request${requests.length === 1 ? "" : "s"} added.`, "success");
}

function resetImportDialog() {
  state.workbook = null;
  state.importPreview = null;
  $("#workbookInput").value = "";
  $("#importChoose").hidden = false;
  $("#importConfigure").hidden = true;
  $("#importPreview").hidden = true;
  $("#backImportButton").hidden = true;
  $("#prepareImportButton").disabled = true;
  $("#prepareImportButton").textContent = "Review import";
  $("#importError").hidden = true;
  const defaultMode = $('input[name="importMode"][value="new"]');
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
  $("#dateInput").innerHTML = dates.map((entry) => `<option value="${escapeHtml(entry.value)}">${escapeHtml(entry.label)} [${escapeHtml(relativeDateLabel(entry.value))}]</option>`).join("");
  updateImportCounts();
}

function updateImportCounts() {
  const sheet = workbookSheet($("#sheetInput").value);
  const selected = sheet?.dates.find((entry) => entry.value === $("#dateInput").value);
  const newCount = selected?.new_count || 0;
  const returnCount = selected?.return_count || 0;
  $("#newImportCount").textContent = `${newCount} request${newCount === 1 ? "" : "s"}`;
  $("#returnImportCount").textContent = `${returnCount} request${returnCount === 1 ? "" : "s"}`;
  $("#bothImportCount").textContent = `${newCount + returnCount} requests`;
  const mode = $('input[name="importMode"]:checked')?.value || "new";
  const selectedCount = mode === "new"
    ? newCount
    : mode === "returns"
      ? returnCount
      : newCount + returnCount;
  $("#prepareImportButton").disabled = !selected || selectedCount === 0;
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
    reader.onerror = () => reject(new Error("The workbook could not be read."));
    reader.readAsDataURL(file);
  });
}

async function uploadWorkbook(file) {
  $("#importError").hidden = true;
  $("#prepareImportButton").disabled = true;
  try {
    const data = await fileToBase64(file);
    const workbook = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ filename: file.name, data }),
    });
    state.workbook = workbook;
    $("#importChoose").hidden = true;
    $("#importConfigure").hidden = false;
    $("#importPreview").hidden = true;
    setImportStep(2);
    $("#importFilename").textContent = workbook.filename;
    $("#importFileSummary").textContent = `${workbook.sheets.length} dated sheet${workbook.sheets.length === 1 ? "" : "s"}`;
    $("#sheetInput").innerHTML = workbook.sheets.map((sheet) => `<option value="${escapeHtml(sheet.name)}" ${sheet.name === workbook.default_sheet ? "selected" : ""}>${escapeHtml(sheet.name)}</option>`).join("");
    updateImportDates();
  } catch (error) {
    $("#importError").textContent = error.message;
    $("#importError").hidden = false;
  }
}

function renderImportPreview() {
  const payload = state.importPreview;
  if (!payload) return;
  const included = payload.requests.filter((request) => request.included !== false);
  const newCount = included.filter((request) => request.group === "New deployments").length;
  const returnCount = included.filter((request) => request.group === "Pending returns").length;
  $("#importPreviewTitle").textContent = `${newCount} new deployment${newCount === 1 ? "" : "s"} · ${returnCount} return${returnCount === 1 ? "" : "s"}`;
  $("#importPreviewSubtitle").textContent = `${$("#sheetInput").value} · ${$("#dateInput option:checked").textContent}`;
  $("#importPreviewCount").textContent = `${included.length} selected`;

  const groups = [
    {
      key: "New deployments",
      title: "New deployments",
      detail: "Column J",
    },
    {
      key: "Pending returns",
      title: "Pending returns",
      detail: "Column L",
    },
  ];
  $("#importPreviewList").innerHTML = groups.map((group) => {
    const requests = payload.requests.filter((request) => request.group === group.key);
    if (!requests.length) return "";
    const selectedCount = requests.filter((request) => request.included !== false).length;
    const rows = requests.map((request, index) => {
      const isNew = request.group === "New deployments";
      const isIncluded = request.included !== false;
      const statusControl = isNew
        ? `<select data-import-status="${escapeHtml(request.id)}" aria-label="Status for ${escapeHtml(request.serials[0])}">
            <option value="Deployed - New Stock" ${request.status === "Deployed - New Stock" ? "selected" : ""}>Deployed - New Stock</option>
            <option value="Deployed - Existing Stock" ${request.status === "Deployed - Existing Stock" ? "selected" : ""}>Deployed - Existing Stock</option>
          </select>`
        : `<span class="fixed-status">Deployed - Pending Return</span>`;
      return `<div class="import-preview-row ${isIncluded ? "" : "excluded"}">
        <label class="include-control" title="${isIncluded ? "Included" : "Do not deploy"}">
          <input type="checkbox" data-import-include="${escapeHtml(request.id)}" ${isIncluded ? "checked" : ""}>
          <span>${index + 1}</span>
        </label>
        <div><strong>${escapeHtml(request.serials[0])}</strong><small>${isNew ? "New serial" : "Old serial"}</small></div>
        <div><strong>${escapeHtml(request.user)}</strong><small>Receiving user</small></div>
        <div>${statusControl}<small>${isIncluded ? "" : "Do not deploy"}</small></div>
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

  $("#importIgnored").hidden = !payload.ignored.length;
  $("#importIgnoredList").innerHTML = payload.ignored.map((item) => `<li>${item.count} × ${escapeHtml(item.reason)}</li>`).join("");
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
      .filter((request) => request.included !== false)
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
    toast(`${requests.length} request${requests.length === 1 ? "" : "s"} added${ignored ? `; ${ignored} serial entries excluded` : ""}.`, "success");
    return;
  }
  button.disabled = true;
  try {
    const mode = $('input[name="importMode"]:checked')?.value || "new";
    const payload = await api("/api/import/prepare", {
      method: "POST",
      body: JSON.stringify({
        import_id: state.workbook.import_id,
        sheet: $("#sheetInput").value,
        date: $("#dateInput").value,
        mode,
      }),
    });
    payload.requests.forEach((request) => {
      request.included = true;
      normalizeRequestStatus(request);
    });
    state.importPreview = payload;
    $("#importConfigure").hidden = true;
    $("#importPreview").hidden = false;
    $("#backImportButton").hidden = false;
    button.textContent = `Add ${payload.counts.requests} to queue`;
    setImportStep(3);
    renderImportPreview();
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

function openReview() {
  if (!state.queue.length || elements.reviewButton.disabled) return;
  const validations = queueValidation();
  const invalid = [...validations.values()].filter((errors) => errors.length);
  const devices = state.queue.reduce((sum, request) => sum + request.serials.length, 0);
  $("#reviewSummary").innerHTML = `
    <div class="summary-metric"><strong>${state.queue.length}</strong><span>EUDM requests</span></div>
    <div class="summary-metric"><strong>${devices}</strong><span>Devices</span></div>
    <div class="summary-metric"><strong>${invalid.length}</strong><span>Need attention</span></div>`;
  $("#reviewList").innerHTML = state.queue.map((request) => {
    const errors = validations.get(request.id) || [];
    return `<div class="review-row">
      <span class="${errors.length ? "invalid-mark" : "ready-mark"}">${errors.length ? "!" : "✓"}</span>
      <div><strong>${escapeHtml(request.serials.join(", ") || "No serial")}</strong><small>${escapeHtml(kindLabel(request.kind))}${request.kind === "bulk_location" ? ` · ${request.serials.length} devices` : ""}</small></div>
      <div><strong>${escapeHtml(statusLabel(request))}</strong><small>${escapeHtml(request.group)}</small></div>
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

function renderProgress(job) {
  state.currentJob = job;
  const done = job.counts.succeeded + job.counts.failed;
  const percentage = job.counts.total ? (done / job.counts.total) * 100 : 0;
  $("#progressBar").style.width = `${percentage}%`;
  $("#progressCounts").textContent = `${done} of ${job.counts.total} complete · ${job.counts.devices} devices`;
  $("#progressList").innerHTML = job.entries.map((entry) => `
    <div class="progress-row ${entry.state}">
      <span class="progress-state">${progressStateSymbol(entry)}</span>
      <div class="progress-device"><strong>${escapeHtml(entry.serials.join(", "))}</strong><small>${escapeHtml(kindLabel(entry.kind))} · ${escapeHtml(entry.status)}</small></div>
      <div class="progress-message"><strong>${escapeHtml(entry.message)}</strong><small>${escapeHtml(entry.destination)}${entry.returning_user ? ` · returned by ${escapeHtml(entry.returning_user)}` : ""}</small></div>
      <div class="request-id">${entry.request_id
        ? entry.request_page_url
          ? `<a href="${escapeHtml(entry.request_page_url)}" target="_blank" rel="noopener">Open ${escapeHtml(entry.request_id)}</a>`
          : `Request ${escapeHtml(entry.request_id)}`
        : entry.state === "queued" ? "Queued" : "Preparing"}</div>
    </div>`).join("");
  const finished = job.state === "finished";
  $("#progressHeading").textContent = finished
    ? `${job.counts.succeeded} submitted, ${job.counts.failed} failed`
    : "Submitting requests";
  $("#progressActions").hidden = !finished;
  $("#closeProgressButton").hidden = !finished;
  $("#downloadResultsLink").href = `/api/jobs/${job.job_id}/results.txt`;
  const requestPages = job.entries.filter((entry) => entry.request_page_url);
  const openAll = $("#openAllRequestsButton");
  openAll.hidden = !finished || requestPages.length === 0;
  openAll.disabled = requestPages.length === 0;
}

function openAllRequestPages() {
  const pages = (state.currentJob?.entries || [])
    .map((entry) => entry.request_page_url)
    .filter(Boolean);
  if (!pages.length) {
    toast("Request pages are available after a real EUDM submission.", "error");
    return;
  }
  pages.forEach((url) => window.open(url, "_blank", "noopener"));
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
      ? `${succeeded} submitted · ${failed} failed`
      : run.state;
    const entries = (run.entries || []).map((entry) => {
      const requestLink = entry.request_page_url
        ? `<a href="${escapeHtml(entry.request_page_url)}" target="_blank" rel="noopener">Open ${escapeHtml(entry.request_id || "request")}</a>`
        : `<span>${escapeHtml(entry.request_id ? `Request ${entry.request_id}` : "No request ID")}</span>`;
      return `<div class="history-entry ${entry.state === "failed" ? "failed" : ""}">
        <div><strong>${escapeHtml(entry.serials.join(", ") || "No serial")}</strong><small>${escapeHtml(kindLabel(entry.kind))} · ${escapeHtml(entry.status)}</small></div>
        <div><strong>${escapeHtml(entry.destination || "No destination")}</strong><small>${escapeHtml(entry.message || "")}</small></div>
        <div>${requestLink}</div>
      </div>`;
    }).join("");
    return `<section class="history-run">
      <div class="history-run-header">
        <div><strong>${escapeHtml(formatHistoryDate(run.created_at))}</strong><small>${run.simulation ? "Simulation" : "Real EUDM"} · ${escapeHtml(run.request_for || "Unknown requester")} · ${run.counts?.devices || 0} devices</small></div>
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

function clearQueueAfterFlow() {
  state.queue = [];
  state.selectedId = null;
  renderAll();
}

async function pollJob(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    renderProgress(job);
    if (job.state !== "finished") {
      state.pollTimer = setTimeout(() => pollJob(jobId), 650);
    } else {
      const type = job.counts.failed ? "error" : "success";
      toast(`${job.counts.succeeded} request${job.counts.succeeded === 1 ? "" : "s"} submitted; ${job.counts.failed} failed.`, type);
    }
  } catch (error) {
    toast(error.message, "error");
    state.pollTimer = setTimeout(() => pollJob(jobId), 1500);
  }
}

async function submitQueue() {
  const button = $("#submitQueueButton");
  button.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        requests: state.queue,
        concurrency: Number(elements.concurrency.value),
      }),
    });
    $("#reviewDialog").close();
    renderProgress(job);
    $("#progressDialog").showModal();
    pollJob(job.job_id);
  } catch (error) {
    if (error.payload?.validation) {
      toast("Some requests need attention. Return to the queue to correct them.", "error");
    } else {
      toast(error.message, "error");
    }
    button.disabled = false;
  }
}

function bindEvents() {
  $("#themeToggle").addEventListener("click", toggleTheme);
  $("#addUserButton").addEventListener("click", () => addRequest("user"));
  $("#addLocationButton").addEventListener("click", () => addRequest("location"));
  $("#addBulkButton").addEventListener("click", () => addRequest("bulk_location"));
  $("#pastePairsButton").addEventListener("click", openPasteDialog);
  $("#addPairsButton").addEventListener("click", addPairs);
  $$('input[name="pairsMode"]').forEach((radio) => radio.addEventListener("change", () => {
    $("#pairsError").hidden = true;
    updatePasteMode();
  }));
  $("#importSheetButton").addEventListener("click", () => {
    resetImportDialog();
    $("#importDialog").showModal();
  });
  $("#workbookInput").addEventListener("change", (event) => {
    if (event.target.files[0]) uploadWorkbook(event.target.files[0]);
  });
  $("#changeFileButton").addEventListener("click", resetImportDialog);
  $("#sheetInput").addEventListener("change", updateImportDates);
  $("#dateInput").addEventListener("change", updateImportCounts);
  $$('input[name="importMode"]').forEach((radio) => radio.addEventListener("change", updateImportCounts));
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
  $$('input[name="requestKind"]').forEach((radio) => radio.addEventListener("change", () => changeKind(radio.value)));

  elements.serialInput.addEventListener("input", () => {
    const request = selectedRequest();
    if (!request) return;
    request.serials = elements.serialInput.value.trim() ? [elements.serialInput.value.trim()] : [];
    renderQueue();
  });
  elements.serialsInput.addEventListener("input", () => {
    const request = selectedRequest();
    if (!request) return;
    request.serials = parseSerials(elements.serialsInput.value);
    elements.serialHint.textContent = `${request.serials.length} serial${request.serials.length === 1 ? "" : "s"}`;
    renderQueue();
  });
  elements.statusInput.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request) return;
    request.status = elements.statusInput.value;
    renderAll();
  });
  elements.userInput.addEventListener("input", () => {
    const request = selectedRequest();
    if (!request) return;
    request.user = elements.userInput.value.trim();
    renderQueue();
  });
  elements.cityInput.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request) return;
    request.location = { city: elements.cityInput.value, building: "", floor: "", room: "", cabinet: "" };
    elements.locationInput.innerHTML = '<option value="">Find an exact location</option>';
    elements.locationDetail.textContent = "";
    renderQueue();
  });
  elements.locationInput.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request || elements.locationInput.value === "current" || !elements.locationInput.value) return;
    const results = JSON.parse(elements.locationInput.dataset.results || "[]");
    const result = results[Number(elements.locationInput.value)];
    if (!result) return;
    const [building = "", floor = "", room = "", cabinet = ""] = result.columns;
    request.location = { city: elements.cityInput.value, building, floor, room, cabinet };
    elements.locationDetail.textContent = [building, floor, room, cabinet].filter(Boolean).join(" → ");
    renderQueue();
  });
  elements.returningToggle.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request) return;
    request.returning = elements.returningToggle.checked;
    if (!request.returning) request.returning_user = "";
    elements.returningSearch.hidden = !elements.returningToggle.checked;
    elements.returnConfirmation.hidden = !elements.returningToggle.checked;
    renderQueue();
  });
  elements.returningUserInput.addEventListener("input", () => {
    const request = selectedRequest();
    if (!request) return;
    request.returning_user = elements.returningUserInput.value.trim();
    renderQueue();
  });
  elements.serialInput.addEventListener("keydown", (event) => { if (event.key === "Enter") searchAssets(); });
  elements.userInput.addEventListener("keydown", (event) => { if (event.key === "Enter") searchUsers(false); });
  elements.returningUserInput.addEventListener("keydown", (event) => { if (event.key === "Enter") searchUsers(true); });
  $("#doneButton").addEventListener("click", () => $("#progressDialog").close());
  $("#closeProgressButton").addEventListener("click", () => $("#progressDialog").close());
  $("#progressDialog").addEventListener("close", () => {
    if (state.currentJob?.state === "finished") clearQueueAfterFlow();
  });
  $("#openAllRequestsButton").addEventListener("click", openAllRequestPages);
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !elements.reviewButton.disabled) {
      event.preventDefault();
      openReview();
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
    state.config = await api("/api/config");
    elements.requestForValue.textContent = state.config.request_for || "Waiting for EUDM";
    elements.concurrency.value = String(state.config.concurrency);
    bindEvents();
    await refreshConnection();
    renderAll();
  } catch (error) {
    document.body.innerHTML = `<main class="empty-state"><h1>AutoEUDM could not start</h1><p>${escapeHtml(error.message)}</p></main>`;
  }
}

init();
