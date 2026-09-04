const state = {
  config: null,
  preferences: {},
  queue: [],
  queueLoaded: false,
  persistedQueueSnapshot: null,
  queuePersistTimer: null,
  queueSearch: "",
  selectedId: null,
  connection: null,
  connectionSheetEventsBound: false,
  connectionDismissTimer: null,
  workbook: null,
  workbookInspection: null,
  importPreview: null,
  importMode: "deploy",
  importDraftId: null,
  importDrafts: [],
  importSelectedDates: [],
  importUploadToken: 0,
  importExpandedGroups: new Set(),
  currentJob: null,
  pollTimer: null,
  pollInFlight: false,
  pollFailures: 0,
  pollStatusMessage: "",
  submissionStarting: false,
  notifiedJobs: new Set(),
  celebratedJobs: new Set(),
  connectionHeartbeatTimer: null,
  liveOptionsLoaded: false,
  pasteLocation: null,
  pasteLocationResults: [],
  pasteEntries: [],
  importLocation: null,
  importLocationResults: [],
  locationCache: new Map(),
  locationLoading: new Map(),
  locationRetryCities: new Set(),
  locationRetryTimer: null,
  recordedLocationJobs: new Set(),
  newRequest: null,
  importUndoStack: [],
  importRedoStack: [],
  appUndoStack: [],
  appRedoStack: [],
  appHistoryApplying: false,
  appInputHistoryTimers: new Map(),
  commandPaletteIndex: 0,
  queueSortable: null,
  requestStatusSortable: null,
  queueDropDepth: 0,
  validationTimers: new Map(),
  backlogValidationIds: new Set(),
  bulkValidationRenderFrame: null,
  bulkValidationNeedsFullRender: false,
  quickImportRenderFrame: null,
  importPreviewRenderFrame: null,
  importPreviewNeedsFullRender: false,
  historyRuns: [],
};

const THEME_STORAGE_KEY = "auto-eudm-theme";
const RECENT_LOCATIONS_STORAGE_KEY = "auto-eudm-recent-locations";
const CONCURRENCY_STORAGE_KEY = "auto-eudm-concurrency";
const IMPORT_COLUMNS_STORAGE_KEY = "auto-eudm-import-columns";
const IMPORT_LOCATION_STORAGE_KEY = "auto-eudm-import-location";
const VALIDATION_DEBOUNCE_MS = 300;
// Verification is a read-only fan-out; the server-side submission limit stays
// separate so a large import can finish checking without serialising the UI.
const VALIDATION_CONCURRENCY = 200;
const MAX_RECENT_LOCATIONS = 8;
const IMPORT_PREVIEW_ROW_LIMIT = 80;
const MAX_WORKBOOK_BYTES = 100 * 1024 * 1024;
const ALM_IMPORT_STATUS_OPTIONS = {
  Deployments: [
    { value: "Deployed - New Stock", label: "Deployed - New Stock" },
    { value: "Deployed - Existing Stock", label: "Deployed - Existing Stock" },
  ],
  "Returned devices": [
    { value: "Pending Decom", label: "Pending Decom" },
    { value: "Pending Rebuild", label: "Pending Rebuild" },
    { value: "Used Stock", label: "Used Stock" },
  ],
};
const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function iconMarkup(name, className = "") {
  const classes = className ? `ui-icon ${className}` : "ui-icon";
  return `<i data-lucide="${escapeHtml(name)}" class="${classes}" aria-hidden="true"></i>`;
}

function refreshIcons(root = document) {
  if (!root) return;
  if (window.lucide?.createIcons) {
    try {
      window.lucide.createIcons({ root });
    } catch (error) {
      // Icons are decorative. A failed enhancement should never stop the UI.
      console.warn("Could not enhance interface symbols.", error);
    }
  }
  refreshTooltips(root);
}

function refreshTooltips(root = document) {
  if (typeof window.tippy !== "function" || !root?.querySelectorAll) return;
  const selector = "button[title], button[aria-label], [role='button'][title], [role='button'][aria-label], .queue-drag-handle[title], [data-tooltip]";
  const targets = [
    ...(root.matches?.(selector) ? [root] : []),
    ...root.querySelectorAll(selector),
  ].filter((target) => !target.disabled);
  if (!targets.length) return;
  const options = {
    theme: "autoeudm",
    animation: "shift-away",
    arrow: false,
    delay: [420, 0],
    duration: [130, 100],
    placement: "top",
    maxWidth: 250,
    appendTo: () => document.body,
  };
  targets.forEach((target) => {
    const title = target.getAttribute("title") || target.dataset.tooltip || target.getAttribute("aria-label");
    if (!title) return;
    // Keep a copy because Tippy removes the native title after enhancement.
    // This also lets dynamic disabled/error labels stay accurate on rerender.
    target.dataset.tooltip = title;
    if (target._tippy) {
      target._tippy.setContent(title);
      return;
    }
    window.tippy(target, { ...options, content: title });
  });
}

function setButtonLabel(buttonOrSelector, text) {
  const button = typeof buttonOrSelector === "string" ? $(buttonOrSelector) : buttonOrSelector;
  if (!button) return;
  const label = button.querySelector("[data-button-label]");
  if (label) label.textContent = text;
  else button.textContent = text;
}

function commandPaletteActions() {
  const reviewAvailable = Boolean(elements.reviewButton && !elements.reviewButton.disabled);
  const themeIsDark = effectiveTheme() === "dark";
  const actions = [
    { label: "New request", detail: "Prepare a single-device request", icon: "plus", shortcut: "N", run: startNewRequest },
    { label: "New bulk request", detail: "Prepare a multi-device request", icon: "layers-2", shortcut: "B", run: startNewBulkRequest },
    { label: "Quick import", detail: "Add devices from a list", icon: "list-plus", shortcut: "I", run: openPasteDialog },
    ...(reviewAvailable ? [{ label: "Review & submit", detail: `Review ${state.queue.length} queued request${state.queue.length === 1 ? "" : "s"}`, icon: "send", shortcut: "⌥ Enter", run: openReview }] : []),
    { label: "Filter request queue", detail: "Find a serial, user, status, or location", icon: "search", shortcut: "", run: focusQueueSearch },
    { label: "Request history", detail: "Search submitted requests", icon: "history", shortcut: "⌥ ⇧ H", run: openHistory },
    { label: "Settings", detail: "Change app and import settings", icon: "settings-2", shortcut: "⌥ ,", run: openSettings },
    { label: themeIsDark ? "Use light appearance" : "Use dark appearance", detail: "Change the interface theme", icon: themeIsDark ? "sun" : "moon", shortcut: "", run: toggleTheme },
    { label: "Keyboard shortcuts", detail: "See the quick actions", icon: "keyboard", shortcut: "?", run: openShortcuts },
  ];
  if (state.config?.spreadsheet_import_enabled) {
    actions.splice(3, 0, { label: "Import ALM Workbook", detail: "Review an ALM spreadsheet", icon: "file-spreadsheet", shortcut: "A", run: openAlmWorkbookImport });
  }
  return actions;
}

function filteredCommandPaletteActions() {
  const query = String($("#commandPaletteInput")?.value || "").trim().toLowerCase();
  return commandPaletteActions().filter((action) => !query
    || `${action.label} ${action.detail}`.toLowerCase().includes(query));
}

function renderCommandPalette() {
  const list = $("#commandPaletteList");
  if (!list) return;
  const actions = filteredCommandPaletteActions();
  if (!actions.length) {
    state.commandPaletteIndex = 0;
    list.innerHTML = '<div class="command-palette-empty">No matching actions</div>';
    return;
  }
  state.commandPaletteIndex = Math.min(state.commandPaletteIndex, actions.length - 1);
  list.innerHTML = actions.map((action, index) => `
    <button class="command-item ${index === state.commandPaletteIndex ? "selected" : ""}" type="button" role="option" aria-selected="${index === state.commandPaletteIndex}" data-command-index="${index}">
      ${iconMarkup(action.icon, "command-item-icon")}
      <span class="command-item-copy"><strong>${escapeHtml(action.label)}</strong><small>${escapeHtml(action.detail)}</small></span>
      ${action.shortcut ? `<kbd>${escapeHtml(action.shortcut)}</kbd>` : ""}
    </button>`).join("");
  list.querySelectorAll("[data-command-index]").forEach((button) => {
    button.addEventListener("click", () => runCommandPaletteItem(Number(button.dataset.commandIndex)));
  });
  refreshIcons(list);
}

function runCommandPaletteItem(index) {
  const action = filteredCommandPaletteActions()[index];
  if (!action) return;
  $("#commandPaletteDialog")?.close();
  action.run();
}

function openCommandPalette() {
  if ($("#connectionDialog")?.open) return;
  const dialog = $("#commandPaletteDialog");
  if (!dialog) return;
  state.commandPaletteIndex = 0;
  $("#commandPaletteInput").value = "";
  renderCommandPalette();
  if (!dialog.open) dialog.showModal();
  requestAnimationFrame(() => $("#commandPaletteInput")?.focus());
}

function setupOptionalListAnimation() {
  if (typeof window.autoAnimate !== "function"
    || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  ["#commandPaletteList", "#pairsReviewList", "#bulkSerialList", "#importDraftList", "#historyList", "#toastRegion"].forEach((selector) => {
    const target = $(selector);
    if (!target || target.dataset.autoAnimateReady) return;
    try {
      window.autoAnimate(target, { duration: 180, easing: "ease-out" });
      target.dataset.autoAnimateReady = "true";
    } catch (error) {
      console.warn(`Could not animate ${selector}.`, error);
    }
  });
}

const elements = {
  workspace: $(".workspace"),
  concurrency: $("#concurrencyInput"),
  queueEmpty: $("#queueEmpty"),
  queueTableWrap: $("#queueTableWrap"),
  queueBody: $("#queueBody"),
  queueCounts: $("#queueCounts"),
  queueSearch: $("#queueSearchInput"),
  queueFilterEmpty: $("#queueFilterEmpty"),
  connectionStatus: $("#connectionStatus"),
  queueValidationNotice: $("#queueValidationNotice"),
  queueValidationMessage: $("#queueValidationMessage"),
  submissionNotice: $("#submissionNotice"),
  submissionNoticeState: $(".submission-notice-state"),
  submissionNoticeTitle: $("#submissionNoticeTitle"),
  submissionNoticeDetail: $("#submissionNoticeDetail"),
  connectionDialog: $("#connectionDialog"),
  connectionSheetTitle: $("#connectionSheetTitle"),
  connectionVisual: $("#connectionVisual"),
  connectionLinkIcon: $("#connectionLinkIcon"),
  connectionLoading: $("#connectionLoading"),
  connectionAuthenticateButton: $("#connectionAuthenticateButton"),
  historyButton: $("#historyButton"),
  historyList: $("#historyList"),
  reviewButton: $("#reviewButton"),
  clearQueueButton: $("#clearQueueButton"),
  inspector: $("#inspector"),
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
  return `<span class="${className}"><span>Request ID</span><strong>${id}</strong><button class="copy-request-id" type="button" data-copy-request-id="${id}" aria-label="Copy request ID ${id}" title="Copy request ID">${iconMarkup("copy")}<span class="copy-request-label">Copy</span></button></span>`;
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
    const label = button.querySelector(".copy-request-label");
    if (label) label.textContent = "Copied";
    button.classList.add("copied");
    window.setTimeout(() => {
      if (label) label.textContent = "Copy";
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

function queueSearchText(request) {
  const userInfo = request?.user_info && typeof request.user_info === "object"
    ? Object.values(request.user_info)
    : [];
  const location = request?.location && typeof request.location === "object"
    ? Object.values(request.location)
    : [];
  return [
    ...(request?.serials || []),
    request?.user,
    request?.returning_user,
    request?.status,
    request?.group,
    request?.source,
    request?.device_allocation,
    request?.request_id,
    ...userInfo,
    ...location,
  ].filter(Boolean).join(" ").toLowerCase();
}

function focusQueueSearch() {
  const input = $("#queueSearchInput");
  if (!input) return;
  input.focus();
  input.select();
}

function toast(message, type = "") {
  const region = $("#toastRegion");
  const existing = [...region.children].find((node) =>
    node.dataset.message === String(message) && node.dataset.type === type,
  );
  if (existing) {
    window.clearTimeout(existing.toastTimer);
    existing.classList.remove("repeated");
    void existing.offsetWidth;
    existing.classList.add("repeated");
    existing.toastTimer = window.setTimeout(() => existing.remove(), 4300);
    return;
  }
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  const iconName = type === "success" ? "circle-check" : type === "error" ? "triangle-alert" : "info";
  node.innerHTML = `${iconMarkup(iconName, "toast-icon")}<span>${escapeHtml(message)}</span>`;
  node.dataset.message = String(message);
  node.dataset.type = type;
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.tabIndex = 0;
  node.title = "Click to dismiss";
  const dismiss = () => {
    window.clearTimeout(node.toastTimer);
    node.remove();
  };
  node.addEventListener("click", dismiss);
  node.addEventListener("keydown", (event) => {
    if (["Enter", " ", "Escape"].includes(event.key)) dismiss();
  });
  region.append(node);
  refreshIcons(node);
  node.toastTimer = window.setTimeout(() => node.remove(), 4300);
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
  $("#themeIcon").innerHTML = iconMarkup(overridden ? "sun-moon" : (dark ? "sun" : "moon"));
  refreshIcons($("#themeIcon"));
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

async function forEachWithConcurrency(items, limit, worker) {
  if (!items.length) return;
  let nextIndex = 0;
  const runner = async () => {
    while (nextIndex < items.length) {
      const index = nextIndex;
      nextIndex += 1;
      await worker(items[index], index);
    }
  };
  const runners = Array.from(
    { length: Math.min(Math.max(1, limit), items.length) },
    () => runner(),
  );
  await Promise.all(runners);
}

function queueSnapshot() {
  try {
    return JSON.stringify(state.queue);
  } catch (_) {
    return null;
  }
}

function appStateSnapshot() {
  return {
    queue: structuredClone(state.queue),
    selectedId: state.selectedId,
    newRequest: state.newRequest ? structuredClone(state.newRequest) : null,
    pasteEntries: structuredClone(state.pasteEntries),
    pasteLocation: state.pasteLocation ? structuredClone(state.pasteLocation) : null,
  };
}

function appStateSnapshotKey(snapshot) {
  try {
    return JSON.stringify(snapshot);
  } catch (_) {
    return "";
  }
}

function recordAppEdit() {
  if (state.appHistoryApplying) return;
  state.appInputHistoryTimers.forEach((timer) => window.clearTimeout(timer));
  state.appInputHistoryTimers.clear();
  const snapshot = appStateSnapshot();
  const key = appStateSnapshotKey(snapshot);
  if (key && key === appStateSnapshotKey(state.appUndoStack.at(-1))) return;
  state.appUndoStack.push(snapshot);
  if (state.appUndoStack.length > 100) state.appUndoStack.shift();
  state.appRedoStack = [];
}

function recordAppInputEdit(key) {
  const existing = state.appInputHistoryTimers.get(key);
  if (!existing) recordAppEdit();
  if (existing) window.clearTimeout(existing);
  state.appInputHistoryTimers.set(key, window.setTimeout(() => {
    state.appInputHistoryTimers.delete(key);
  }, 650));
}

function restoreAppState(snapshot) {
  if (!snapshot) return;
  state.appHistoryApplying = true;
  state.queue = structuredClone(snapshot.queue || []).map(resetPersistedValidationState);
  state.selectedId = snapshot.selectedId || null;
  state.newRequest = snapshot.newRequest
    ? resetPersistedValidationState(structuredClone(snapshot.newRequest))
    : null;
  state.pasteEntries = structuredClone(snapshot.pasteEntries || []);
  state.pasteLocation = snapshot.pasteLocation ? structuredClone(snapshot.pasteLocation) : null;
  if (state.selectedId && !state.queue.some((request) => request.id === state.selectedId)) {
    state.selectedId = null;
  }
  renderAll();
  if ($("#pasteDialog").open) {
    renderQuickImportReview();
    renderPasteLocationFields();
  }
  state.appHistoryApplying = false;
}

function undoAppEdit() {
  if (!state.appUndoStack.length || submissionBusy()) return;
  state.appRedoStack.push(appStateSnapshot());
  restoreAppState(state.appUndoStack.pop());
}

function redoAppEdit() {
  if (!state.appRedoStack.length || submissionBusy()) return;
  state.appUndoStack.push(appStateSnapshot());
  restoreAppState(state.appRedoStack.pop());
}

function resetPersistedValidationState(request) {
  if (!request || typeof request !== "object") return request;
  if (request.kind === "location" && request.returning_user && !request.returning) {
    // Older queue entries inferred the return branch from the username alone.
    request.returning = true;
  }
  if (request.kind === "location" && request.returning_user_info && request.returning_user_selected === undefined) {
    request.returning_user_selected = true;
  }
  if (request.kind === "location" && request.returning_user && request.returning_user_validation === undefined) {
    request.returning_user_validation = request.returning_user_info ? "valid" : "pending";
  }
  if (request.serial_validation === "checking") {
    request.serial_validation = request.serials?.length ? "pending" : "empty";
  }
  if (request.user_validation === "checking") {
    request.user_validation = request.user ? "pending" : "empty";
  }
  if (request.returning_user_validation === "checking") {
    request.returning_user_validation = request.returning_user ? "pending" : "empty";
  }
  if (!request.returning_user && request.returning_user_validation === undefined) {
    request.returning_user_validation = "empty";
  }
  if (request.kind === "bulk_location" && request.bulk_serial_mode === "individual") {
    request.bulk_serial_states = request.bulk_serial_states || {};
    let pending = request.bulk_validation === "checking";
    request.serials?.forEach((serial) => {
      if (["checking", "pending"].includes(request.bulk_serial_states[serial])) {
        request.bulk_serial_states[serial] = "pending";
        pending = true;
      }
    });
    if (pending) request.bulk_validation = request.serials?.length ? "pending" : "empty";
  }
  return request;
}

async function loadPersistedQueue() {
  const payload = await api("/api/queue");
  state.queue = Array.isArray(payload.requests)
    ? payload.requests.map(resetPersistedValidationState)
    : [];
  state.selectedId = state.queue[0]?.id || null;
  state.persistedQueueSnapshot = queueSnapshot();
  state.queueLoaded = true;
}

function persistQueueSoon() {
  if (!state.queueLoaded) return;
  const snapshot = queueSnapshot();
  if (!snapshot || snapshot === state.persistedQueueSnapshot) return;
  if (state.queuePersistTimer) window.clearTimeout(state.queuePersistTimer);
  state.queuePersistTimer = window.setTimeout(async () => {
    state.queuePersistTimer = null;
    const queuedSnapshot = queueSnapshot();
    if (!queuedSnapshot || queuedSnapshot === state.persistedQueueSnapshot) return;
    try {
      const payload = await api("/api/queue", {
        method: "POST",
        body: JSON.stringify({ requests: state.queue }),
      });
      if (queuedSnapshot === queueSnapshot()) {
        state.persistedQueueSnapshot = JSON.stringify(payload.requests || []);
      } else {
        persistQueueSoon();
      }
    } catch (error) {
      toast(`Could not save the request queue: ${error.message}`, "error");
    }
  }, 250);
}

function verifyCachedValueInBackground(kind, query, returning, onResult, onError = null) {
  const path = kind === "serial" ? "/api/search/assets" : "/api/search/users";
  const body = kind === "serial"
    ? { query, fresh: true, bypass_cache: true }
    : { query, returning, fresh: true, bypass_cache: true };
  void api(path, { method: "POST", body: JSON.stringify(body) })
    .then(onResult)
    .catch((error) => {
      if (onError) onError(error);
      // The cached verification remains usable when the background refresh
      // cannot reach Helix. A later foreground verification can retry it.
    });
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

function validationEnabled(key, fallback = true) {
  if (state.preferences?._saved && typeof state.preferences?.[key] === "boolean") {
    return state.preferences[key];
  }
  return fallback;
}

function requestStatusSettingOrder() {
  const all = allRequestStatusOptions();
  const configured = state.preferences?.request_statuses;
  const legacyConfigured = configured && !Array.isArray(configured)
    ? [...(configured.user || []), ...(configured.location || [])]
    : configured;
  const configuredValues = Array.isArray(legacyConfigured) && legacyConfigured.length
    ? legacyConfigured.filter((value, index) => all.some((option) => option.value === value) && legacyConfigured.indexOf(value) === index)
    : all.map((option) => option.value);
  const included = new Set(configuredValues);
  return [
    ...configuredValues.map((value) => all.find((option) => option.value === value)).filter(Boolean),
    ...all.filter((option) => !included.has(option.value)),
  ];
}

function renderRequestStatusSettings() {
  const list = $("[data-request-status-list]");
  if (!list) return;
  const visible = new Set(visibleRequestStatusValues());
  list.innerHTML = requestStatusSettingOrder().map((option) => {
    const kind = allRequestStatusOptions("user").some((item) => item.value === option.value)
      ? "User"
      : "Location";
    return `
      <div class="request-status-row" data-request-status-row data-status-value="${escapeHtml(option.value)}">
        <span class="request-status-drag-handle" title="Drag to reorder" aria-label="Drag to reorder">${iconMarkup("grip-vertical")}</span>
        <label class="request-status-choice">
          <input type="checkbox" data-request-status-visible ${visible.has(option.value) ? "checked" : ""}>
          <span>${escapeHtml(option.label)}<small>${kind} deployment</small></span>
        </label>
        <div class="request-status-order">
          <button class="icon-button" type="button" data-request-status-move="up" aria-label="Move ${escapeHtml(option.label)} up" title="Move up">${iconMarkup("chevron-up")}</button>
          <button class="icon-button" type="button" data-request-status-move="down" aria-label="Move ${escapeHtml(option.label)} down" title="Move down">${iconMarkup("chevron-down")}</button>
        </div>
      </div>`;
  }).join("");
  refreshIcons(list);
  syncRequestStatusSorting();
}

function syncRequestStatusSorting() {
  const list = $("[data-request-status-list]");
  if (!list || !window.Sortable) return;
  if (!state.requestStatusSortable) {
    state.requestStatusSortable = new window.Sortable(list, {
      animation: 160,
      easing: "cubic-bezier(.2,.8,.2,1)",
      handle: ".request-status-drag-handle",
      ghostClass: "request-status-row-ghost",
      chosenClass: "request-status-row-chosen",
    });
  }
  state.requestStatusSortable.option("disabled", false);
}

function readRequestStatusSettings() {
  const list = $("[data-request-status-list]");
  return [...(list?.querySelectorAll("[data-request-status-row]") || [])]
    .filter((row) => row.querySelector("[data-request-status-visible]")?.checked)
    .map((row) => row.dataset.statusValue || "")
    .filter(Boolean);
}

function validateRequestStatusSettings(settings) {
  const userStatuses = new Set(allRequestStatusOptions("user").map((option) => option.value));
  const locationStatuses = new Set(allRequestStatusOptions("location").map((option) => option.value));
  if (!settings.some((value) => userStatuses.has(value))) return "Keep at least one user deployment status visible.";
  if (!settings.some((value) => locationStatuses.has(value))) return "Keep at least one location deployment status visible.";
  return "";
}

function moveRequestStatus(button) {
  const row = button.closest("[data-request-status-row]");
  const list = row?.parentElement;
  if (!row || !list) return;
  const direction = button.dataset.requestStatusMove;
  const sibling = direction === "up" ? row.previousElementSibling : row.nextElementSibling;
  if (!sibling) return;
  if (direction === "up") list.insertBefore(row, sibling);
  else list.insertBefore(sibling, row);
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

function allRequestStatusOptions(kind) {
  if (kind === "user") return state.config?.user_statuses || [];
  if (kind === "location") return state.config?.location_statuses || [];
  return [...(state.config?.user_statuses || []), ...(state.config?.location_statuses || [])];
}

function visibleRequestStatusValues(kind) {
  const all = allRequestStatusOptions(kind);
  const configured = state.preferences?.request_statuses;
  const configuredValues = Array.isArray(configured)
    ? configured
    : configured && typeof configured === "object"
      ? [...(configured.user || []), ...(configured.location || [])]
      : null;
  if (!configuredValues?.length) return all.map((option) => option.value);
  const available = new Set(all.map((option) => option.value));
  return configuredValues.filter((value, index) => available.has(value) && configuredValues.indexOf(value) === index);
}

function requestStatusOptions(kind, current = "") {
  const all = allRequestStatusOptions(kind);
  const visible = new Set(visibleRequestStatusValues(kind));
  const options = visibleRequestStatusValues(kind)
    .map((value) => all.find((option) => option.value === value))
    .filter(Boolean);
  const currentOption = all.find((option) => option.value === current);
  if (currentOption && !visible.has(currentOption.value)) return [currentOption, ...options];
  return options;
}

function normalizeRequestStatus(request) {
  const kind = request.kind === "user" ? "user" : "location";
  const all = allRequestStatusOptions(kind);
  const current = all.find((option) => option.value === request.status);
  if (current) return request.status;
  const fallback = kind === "user" ? state.config.default_user_status : state.config.default_location_status;
  request.status = resolveStatus(requestStatusOptions(kind), request.status, fallback);
  return request.status;
}

function makeRequest(kind) {
  const user = kind === "user";
  const request = {
    id: uid(),
    kind,
    serials: [],
    serial_validation: "empty",
    serial_validation_error: "",
    user_validation: "empty",
    user_validation_error: "",
    bulk_validation: "empty",
    bulk_validation_error: "",
    bulk_validation_missing: [],
    bulk_serial_mode: kind === "bulk_location" ? "individual" : "",
    bulk_serial_states: {},
    bulk_serial_errors: {},
    status: "",
    user: "",
    returning: false,
    returning_user: "",
    returning_user_selected: false,
    returning_user_info: null,
    returning_user_validation: "empty",
    returning_user_validation_error: "",
    returning_user_loading: false,
    location: user ? null : preferredLocation(),
    group: kind === "bulk_location" ? "Bulk add to location stock" : user ? "Deploy to user" : "Add to location stock",
    source: "",
  };
  normalizeRequestStatus(request);
  return request;
}

function userStatusValues() {
  return new Set(allRequestStatusOptions("user").map((option) => option.value));
}

function locationStatusValues() {
  return new Set(allRequestStatusOptions("location").map((option) => option.value));
}

function singleRequestStatusOptions(current = "") {
  return requestStatusOptions("all", current);
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
    request.serials = request.serials.slice(0, 1);
  } else if (changed) {
    request.user = "";
    request.user_selected = false;
    request.user_info = null;
    request.user_validation = "empty";
    request.user_validation_error = "";
    request.location = wasUser || !request.location ? preferredLocation() : request.location;
    if (kind === "location") request.serials = request.serials.slice(0, 1);
  }
}

function validateRequest(request) {
  const errors = [];
  if (!["user", "location", "bulk_location"].includes(request.kind)) {
    return ["Choose Deploy to user, Add to location stock, or Bulk add to location stock."];
  }
  if (!request.serials.length) errors.push(
    request.kind === "bulk_location" ? "Enter one or more serial numbers." : "Enter a serial number.",
  );
  // Serial verification is shown inline beside the field, not repeated in the
  // request error summary.
  if (request.kind !== "bulk_location" && request.serials.length && request.serial_validation !== "valid") {
    errors.push("__field_serial__");
  }
  if (request.kind !== "bulk_location" && request.serials.length > 1) {
    errors.push("This request must contain exactly one serial number.");
  }
  const seen = new Set();
  for (const serial of request.serials) {
    if (!/^[A-Za-z0-9._-]{6,}$/.test(serial)) {
      errors.push("__field_serial__");
      break;
    }
    const key = serial.toLowerCase();
    if (seen.has(key)) {
      errors.push("Remove duplicate serial numbers from this request.");
      break;
    }
    seen.add(key);
  }
  if (request.kind === "bulk_location"
    && bulkSerialMode(request) === "individual"
    && request.serials.length
    && request.bulk_validation !== "valid") {
    errors.push("__field_serial__");
  }
  if (request.kind === "user") {
    if (!userStatusValues().has(request.status)) errors.push("Choose a status for Deploy to user.");
    // The empty user state is shown by the editor field itself.
    if (!request.user.trim()) errors.push("__field_user__");
    if (request.user && !/^[A-Za-z][A-Za-z0-9._-]*$/.test(request.user.trim())) errors.push("__field_user__");
    if (request.user && /^[A-Za-z][A-Za-z0-9._-]*$/.test(request.user.trim()) && request.user_validation !== "valid") {
      errors.push("__field_user__");
    }
    if (request.location) errors.push("Deploy to user cannot include a location.");
  } else {
    if (!locationStatusValues().has(request.status)) errors.push("Choose a status for Add to location stock.");
    if (request.user) errors.push("Add to location stock cannot include a deployed-to user.");
    const location = request.location || {};
    if (![location.city, location.building, location.floor, location.room].every((value) => String(value || "").trim())) {
      errors.push("Choose both the city and the location.");
    }
  }
  return errors;
}

function isVerificationPending(validation) {
  return validation === "pending" || validation === "checking";
}

function canDeferNewRequestValidation(request, error) {
  if (error === "__field_serial__") {
    const serialsAreValid = request.serials.length
      && request.serials.every((serial) => /^[A-Za-z0-9._-]{6,}$/.test(String(serial || "").trim()));
    if (!serialsAreValid) return false;
    if (request.kind === "bulk_location") {
      return bulkSerialMode(request) === "individual" && isVerificationPending(request.bulk_validation);
    }
    return isVerificationPending(request.serial_validation);
  }
  if (error === "__field_user__") {
    return Boolean(request.user.trim() && isVerificationPending(request.user_validation));
  }
  return false;
}

function newRequestValidationErrors(request) {
  return validateRequest(request).filter((error) => !canDeferNewRequestValidation(request, error));
}

function validationErrorText(request, error) {
  if (error === "__field_serial__") {
    const serialsAreValid = request.serials.length
      && request.serials.every((serial) => /^[A-Za-z0-9._-]{6,}$/.test(String(serial || "").trim()));
    if (!serialsAreValid) return "Enter a valid serial number.";
    const detail = request.kind === "bulk_location"
      ? request.bulk_validation_error
      : request.serial_validation_error;
    return detail || (isVerificationPending(request.kind === "bulk_location"
      ? request.bulk_validation
      : request.serial_validation)
      ? "Serial verification is still in progress."
      : "Verify the serial number before submitting.");
  }
  if (error === "__field_user__") {
    if (!request.user.trim()) return "Choose a receiving user.";
    return request.user_validation_error
      || (isVerificationPending(request.user_validation)
        ? "User verification is still in progress."
        : "Choose a verified receiving user.");
  }
  return error;
}

function validationErrorTexts(request, errors) {
  return [...new Set(errors.map((error) => validationErrorText(request, error)))];
}

function queueValidation() {
  const errors = new Map(state.queue.map((request) => [request.id, validateRequest(request)]));
  const owners = new Map();
  for (const request of state.queue) {
    const uniqueSerials = new Set(request.serials.map((serial) => serial.toLowerCase()));
    for (const key of uniqueSerials) {
      if (!owners.has(key)) owners.set(key, []);
      owners.get(key).push(request.id);
    }
  }
  for (const [serial, ids] of owners) {
    if (ids.length > 1) {
      for (const id of new Set(ids)) {
        const requestErrors = errors.get(id);
        if (requestErrors) requestErrors.push(`Serial ${serial.toUpperCase()} appears in more than one request.`);
      }
    }
  }
  return errors;
}

function serialResultMatches(result, serial) {
  const wanted = String(serial || "").trim().toLowerCase();
  return [result?.value, ...(Array.isArray(result?.columns) ? result.columns : [])]
    .map((value) => String(value || "").trim().toLowerCase())
    .includes(wanted);
}

function userResultMatches(result, query) {
  const wanted = normalisedLookupValue(query);
  return [result?.value, ...(Array.isArray(result?.columns) ? result.columns : [])]
    .map(normalisedLookupValue)
    .includes(wanted);
}

function verifiedUserInfo(result, query) {
  if (!userResultMatches(result, query)) return null;
  const login = bestLogin(result, query);
  if (!/^[A-Za-z][A-Za-z0-9._-]*$/.test(login)) return null;
  const columns = Array.isArray(result?.columns) ? result.columns : [result?.value];
  return {
    login,
    columns: columns.map((value) => String(value || "").trim()).filter(Boolean),
  };
}

function normalisedLookupValue(value) {
  return String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
}

function userInfoMatchesQuery(info, query) {
  if (!info || !normalisedLookupValue(query)) return false;
  const values = [
    info.login,
    ...(Array.isArray(info.columns) ? info.columns : []),
  ];
  return values.some((value) => normalisedLookupValue(value) === normalisedLookupValue(query));
}

function importCachedVerificationFields(request) {
  const fields = [];
  if (request.cached_serial_verification && request.serials?.[0]) fields.push("serial");
  const username = request.user || request.username;
  const userInfo = request.user_info;
  if (request.cached_user_verification && userInfoMatchesQuery(userInfo, username)) fields.push("user");
  return fields;
}

function setBulkSerialState(request, serial, stateName, error = "") {
  request.bulk_serial_states = request.bulk_serial_states || {};
  request.bulk_serial_errors = request.bulk_serial_errors || {};
  request.bulk_serial_states[serial] = stateName;
  if (error) request.bulk_serial_errors[serial] = error;
  else delete request.bulk_serial_errors[serial];
}

function scheduleBulkValidationRender(full = true) {
  state.bulkValidationNeedsFullRender ||= full;
  if (state.bulkValidationRenderFrame) return;
  state.bulkValidationRenderFrame = window.requestAnimationFrame(() => {
    state.bulkValidationRenderFrame = null;
    const renderFull = state.bulkValidationNeedsFullRender;
    state.bulkValidationNeedsFullRender = false;
    if (renderFull) renderAll();
    else renderQueue();
  });
}

function updateBulkValidationSummary(request) {
  const serials = request.serials || [];
  if (!serials.length) {
    request.bulk_validation = "empty";
    request.bulk_validation_missing = [];
    request.bulk_validation_error = "";
    return;
  }
  const states = serials.map((serial) => bulkSerialState(request, serial));
  const missing = serials.filter((serial, index) => states[index] === "failed");
  const checking = states.some((stateName) => stateName === "checking" || stateName === "pending");
  request.bulk_validation = checking ? "checking" : missing.length ? "failed" : "valid";
  request.bulk_validation_missing = missing;
  request.bulk_validation_error = missing.length
    ? `Could not verify: ${missing.slice(0, 3).join(", ")}${missing.length > 3 ? ` and ${missing.length - 3} more` : ""}.`
    : "";
}

function requestHasSerial(request, serial) {
  const wanted = String(serial || "").toLowerCase();
  return request.serials.some((value) => String(value || "").toLowerCase() === wanted);
}

async function validateBulkSerials({ force = false, requests = null, render = true, serialsByRequest = null } = {}) {
  const candidates = requests || [
    ...state.queue,
    ...(state.newRequest ? [state.newRequest] : []),
  ];
  const bulkRequests = candidates.filter((request) => request.kind === "bulk_location"
    && request.serials.length
    && (force || request.bulk_validation !== "valid"));
  if (!bulkRequests.length) return true;
  const snapshots = new Map(bulkRequests.map((request) => [request.id, request.serials.join("\u0000")]));
  const targets = new Map(bulkRequests.map((request) => [
    request.id,
    (serialsByRequest?.get(request.id) || request.serials).filter((serial) => requestHasSerial(request, serial)),
  ]));
  const items = bulkRequests.flatMap((request) => (targets.get(request.id) || []).map((serial) => ({ request, serial })));
  bulkRequests.forEach((request) => {
    request.bulk_validation = "checking";
    request.bulk_validation_error = "";
    request.bulk_serial_states = request.bulk_serial_states || {};
    request.bulk_serial_errors = request.bulk_serial_errors || {};
    if (!serialsByRequest) {
      request.bulk_validation_missing = [];
      request.bulk_serial_states = {};
      request.bulk_serial_errors = {};
    }
    (targets.get(request.id) || []).forEach((serial) => setBulkSerialState(request, serial, "checking"));
    if (request === selectedRequest()) refreshBulkValidationButton(request);
  });
  if (render) renderAll();
  await forEachWithConcurrency(items, VALIDATION_CONCURRENCY, async ({ request, serial }) => {
    try {
      const payload = await api("/api/search/assets", {
        method: "POST",
        body: JSON.stringify({ query: serial, fresh: true }),
      });
      if (!requestHasSerial(request, serial)) return;
      const asset = (payload.results || []).find((item) => serialResultMatches(item, serial));
      if (asset) {
        setBulkSerialState(request, serial, "valid");
      } else {
        setBulkSerialState(request, serial, "failed", "Serial number was not found in Helix.");
      }
      updateBulkValidationSummary(request);
      scheduleBulkValidationRender(render);
      if (payload.cached) {
        verifyCachedValueInBackground("serial", serial, false, (freshPayload) => {
          if (!requestHasSerial(request, serial)) return;
          const freshAsset = (freshPayload.results || []).find((item) => serialResultMatches(item, serial));
          if (freshAsset) {
            setBulkSerialState(request, serial, "valid");
          } else {
            setBulkSerialState(request, serial, "failed", "Serial number was not found in Helix.");
          }
          updateBulkValidationSummary(request);
          if (request === selectedRequest()) refreshBulkValidationButton(request);
          scheduleBulkValidationRender(render);
        });
      }
    } catch (_) {
      if (!requestHasSerial(request, serial)) return;
      setBulkSerialState(request, serial, "failed", "Could not verify the serial number in Helix.");
      updateBulkValidationSummary(request);
      scheduleBulkValidationRender(render);
    }
  });
  bulkRequests.forEach((request) => {
    if (snapshots.get(request.id) !== request.serials.join("\u0000")) return;
    updateBulkValidationSummary(request);
    if (request === selectedRequest()) refreshBulkValidationButton(request);
  });
  if (render) renderAll();
  else {
    refreshSelectedValidation();
    renderQueue();
  }
  return true;
}

function resumePendingBulkValidation() {
  if (!connectionIsReady()) return;
  const requests = [
    ...state.queue,
    ...(state.newRequest ? [state.newRequest] : []),
  ].filter((request) => request.kind === "bulk_location"
    && request.bulk_serial_mode === "individual"
    && request.serials?.length
    && request.bulk_validation !== "valid");
  if (requests.length) void validateBulkSerials({ requests, render: true });
}

function statusLabel(request) {
  const options = allRequestStatusOptions(request.kind === "user" ? "user" : "location");
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

function requestIsInCurrentJob(request) {
  return Boolean(
    state.currentJob
    && (state.currentJob.entries || []).some((entry) => entry.id === request.id),
  );
}

function clearRequestSubmissionMetadata(request) {
  if (!request) return;
  delete request.request_id;
  delete request.order_id;
  delete request.result_state;
  delete request.result_message;
}

function submissionBusy() {
  return state.submissionStarting || Boolean(state.currentJob);
}

function renderReturningUserInfo(request) {
  const panel = $("#returnUserInfo");
  if (!panel) return;
  const verified = Boolean(
    request?.returning
    && request.returning_user
    && request.returning_user_selected
    && request.returning_user_validation === "valid"
    && request.returning_user_info,
  );
  panel.hidden = !verified;
  if (!verified) {
    panel.replaceChildren();
    return;
  }
  const info = request.returning_user_info;
  const values = info?.columns || [];
  const unknown = values.some((value) => !String(value).trim() || /unknown|not available|not found/i.test(value));
  panel.className = `return-user-info ${unknown ? "unknown" : ""}`;
  panel.innerHTML = `<strong>Selected user</strong><span>${escapeHtml(info.login || request.returning_user)}</span><small>${escapeHtml(values.join(" · "))}</small>${unknown ? "<em>Some details are unknown. Verify the user before submitting.</em>" : ""}`;
}

function syncQueueSorting() {
  if (!elements.queueBody || !window.Sortable) return;
  if (!state.queueSortable) {
    state.queueSortable = new window.Sortable(elements.queueBody, {
      animation: 180,
      easing: "cubic-bezier(.2,.8,.2,1)",
      handle: ".queue-drag-handle",
      ghostClass: "queue-row-ghost",
      chosenClass: "queue-row-chosen",
      dragClass: "queue-row-dragging",
      onStart: () => elements.queueBody.classList.add("is-reordering"),
      onEnd: (event) => {
        elements.queueBody.classList.remove("is-reordering");
        const oldIndex = Number(event.oldIndex);
        const newIndex = Number(event.newIndex);
        if (!Number.isInteger(oldIndex) || !Number.isInteger(newIndex)
          || oldIndex === newIndex || submissionBusy()) return;
        const request = state.queue[oldIndex];
        if (!request) return;
        recordAppEdit();
        state.queue.splice(oldIndex, 1);
        state.queue.splice(newIndex, 0, request);
        renderAll();
      },
    });
  }
  state.queueSortable.option("disabled", submissionBusy() || state.queue.length < 2 || Boolean(state.queueSearch.trim()));
}

function renderQueue() {
  const validations = queueValidation();
  const requestCount = state.queue.length;
  const invalidCount = [...validations.values()].filter((errors) => errors.length).length;
  const submittedCount = state.queue.filter((request) => request.result_state === "succeeded").length;
  const submissionLocked = submissionBusy();
  const query = String(state.queueSearch || "").trim().toLowerCase();
  const visibleRequests = query
    ? state.queue.filter((request) => queueSearchText(request).includes(query))
    : state.queue;
  const visibleCount = visibleRequests.length;
  const currentJobIds = new Set((state.currentJob?.entries || []).map((entry) => entry.id));
  const requestCountLabel = `${requestCount} request${requestCount === 1 ? "" : "s"}`;
  elements.queueCounts.textContent = query ? `${visibleCount} of ${requestCountLabel}` : requestCountLabel;
  elements.queueValidationNotice.hidden = invalidCount === 0;
  elements.queueValidationMessage.textContent = invalidCount
    ? `${invalidCount} request${invalidCount === 1 ? " needs" : "s need"} attention`
    : "";
  elements.queueEmpty.hidden = requestCount > 0;
  elements.queueFilterEmpty.hidden = !query || visibleCount > 0 || requestCount === 0;
  elements.queueTableWrap.hidden = requestCount === 0 || visibleCount === 0;
  const runtimeReady = state.connection?.state === "simulation"
    || state.connection?.state === "connected";
  const requesterReady = Boolean(state.connection?.request_for || state.config?.request_for);
  elements.reviewButton.disabled = requestCount === 0
    || invalidCount > 0
    || submittedCount > 0
    || submissionLocked
    || !runtimeReady
    || !requesterReady;
  elements.clearQueueButton.disabled = requestCount === 0 || submissionLocked;
  elements.reviewButton.title = submittedCount
    ? "Clear completed requests before submitting another run."
    : submissionLocked
      ? "Finish or review the current submission before starting another."
    : invalidCount
    ? "Fix every request error before reviewing or submitting."
    : !runtimeReady || !requesterReady
      ? "Connect to Helix before submitting."
      : "Review every request before submitting.";

  elements.queueBody.innerHTML = visibleRequests.map((request) => {
    const index = state.queue.indexOf(request);
    const errors = validations.get(request.id) || [];
    const errorText = validationErrorTexts(request, errors).join(" ");
    const submitting = currentJobIds.has(request.id)
      && ["queued", "running"].includes(request.result_state);
    const serialDisplay = request.serials.length ? request.serials.join(", ") : "No serial";
    const selected = request.id === state.selectedId;
    const secondary = request.source
      || (request.group && request.group !== kindLabel(request.kind) ? request.group : "");
    const requestId = request.request_id
      ? requestIdDisplay(request.request_id, "cell-request-id")
      : "";
    const resultState = request.result_state === "succeeded" ? "Submitted"
      : request.result_state === "failed" ? "Failed" : "";
    const stateTitle = request.result_state === "failed"
      ? request.result_message || "Request failed."
      : submitting ? "Request is being submitted." : errorText;
    const readinessMarkup = request.result_state === "failed"
      ? `<span class="failed-mark" title="Request failed">${iconMarkup("circle-x")}</span><span class="cell-secondary">Failed</span>`
      : submitting
        ? `<span class="activity-spinner" ${spinnerPhaseStyle(720)} aria-hidden="true"></span><span class="cell-secondary">${request.result_state === "running" ? "Submitting" : "Waiting"}</span>`
      : errors.length
          ? `<span class="invalid-mark">${iconMarkup("circle-alert")}</span>`
          : request.request_id
            ? `<span class="ready-mark">${iconMarkup("check")}</span><span class="cell-secondary">${resultState}</span>`
            : `<span class="ready-mark">${iconMarkup("check")}</span>`;
    return `
      <tr data-id="${escapeHtml(request.id)}" class="${selected ? "selected" : ""} ${errors.length ? "invalid" : ""} ${submitting ? "submitting" : ""} ${request.result_state === "failed" ? "failed" : ""}" tabindex="0">
        <td class="index-column"><span class="queue-drag-handle" title="Drag to reorder" aria-label="Drag to reorder">${iconMarkup("grip-vertical")}</span><span class="queue-index">${index + 1}</span></td>
        <td><span class="cell-primary ${request.kind === "bulk_location" ? "bulk-serial-summary" : ""}">${escapeHtml(serialDisplay)}</span>${request.device_allocation ? `<span class="cell-secondary">${escapeHtml(request.device_allocation)}</span>` : ""}${requestId}</td>
        <td><span class="cell-primary">${escapeHtml(kindLabel(request.kind))}</span>${secondary ? `<span class="cell-secondary">${escapeHtml(secondary)}</span>` : ""}</td>
        <td title="${escapeHtml(statusLabel(request))}">${escapeHtml(statusLabel(request))}</td>
        <td title="${escapeHtml(destinationLabel(request))}"><span class="cell-primary">${escapeHtml(destinationLabel(request))}</span>${request.returning ? `<span class="cell-secondary">Return from ${escapeHtml(request.returning_user || "user")}</span>` : ""}</td>
        <td class="state-column" title="${escapeHtml(stateTitle)}">${readinessMarkup}</td>
        <td><button class="row-menu" data-remove="${escapeHtml(request.id)}" aria-label="Remove request" title="${submitting ? "This request is being submitted" : "Remove request"}" ${submitting ? "disabled" : ""}>${iconMarkup("trash-2")}</button></td>
      </tr>`;
  }).join("");
  refreshIcons(elements.queueBody);
  syncQueueSorting();

  elements.queueBody.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("[data-remove], [data-copy-request-id], .queue-drag-handle")) return;
      // Selecting text in a row also produces a click. Re-rendering here would
      // replace the row and clear the selection before it can be copied.
      const selection = window.getSelection?.();
      if (selection && !selection.isCollapsed && selection.toString()) return;
      state.selectedId = state.selectedId === row.dataset.id ? null : row.dataset.id;
      renderAll();
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        state.selectedId = state.selectedId === row.dataset.id ? null : row.dataset.id;
        renderAll();
      }
    });
  });
  elements.queueBody.querySelectorAll("[data-remove]").forEach((button) => {
    button.addEventListener("click", () => removeRequest(button.dataset.remove));
  });
  refreshSelectedValidation();
  renderSubmissionNotice();
  persistQueueSoon();
}

function refreshSelectedValidation() {
  const request = selectedRequest();
  if (!request) return;
  const errors = state.newRequest === request
    ? newRequestValidationErrors(request)
    : queueValidation().get(request.id) || [];
  const visibleErrors = [
    ...(request.result_state === "failed" && request.result_message
      ? [`Last submission failed: ${request.result_message}`]
      : []),
    ...errors.filter((error) =>
    !error.startsWith("__field_") && !/^Verifying .+ Please wait\.$/.test(error)
    ),
  ];
  elements.validationPanel.hidden = !visibleErrors.length;
  elements.validationPanel.innerHTML = visibleErrors.length
    ? `<div class="validation-panel-heading">${iconMarkup("triangle-alert")}<strong>Check before submitting</strong></div><ul>${visibleErrors.map((error) => `<li>${escapeHtml(error)}</li>`).join("")}</ul>`
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

function bulkSerialMode(request) {
  if (request.bulk_serial_mode === "individual" || request.bulk_serial_mode === "text") {
    return request.bulk_serial_mode;
  }
  // Bulk requests saved before the individual-entry editor was added already
  // contain a text-list value, so keep those requests in the compatible mode.
  request.bulk_serial_mode = "text";
  return request.bulk_serial_mode;
}

function bulkSerialState(request, serial) {
  const key = String(serial || "").trim();
  const states = request.bulk_serial_states || {};
  const stateKey = Object.keys(states).find((value) => value.toLowerCase() === key.toLowerCase());
  if (stateKey) return states[stateKey];
  if ((request.bulk_validation_missing || []).some((value) => value.toLowerCase() === key.toLowerCase())) return "failed";
  if (request.bulk_validation === "checking") return "checking";
  if (request.bulk_validation === "valid") return "valid";
  return "pending";
}

function bulkSerialStateText(request, serial) {
  const stateName = bulkSerialState(request, serial);
  if (stateName === "checking") return "Verifying…";
  if (stateName === "valid") return "Verified";
  if (stateName === "failed") return request.bulk_serial_errors?.[serial] || "Could not verify";
  return "Waiting to verify";
}

function renderBulkSerialList(request) {
  const list = $("#bulkSerialList");
  if (!list || request.kind !== "bulk_location" || bulkSerialMode(request) !== "individual") return;
  if (!request.serials.length) {
    list.innerHTML = '<div class="bulk-serial-empty">No serials added yet.</div>';
    return;
  }
  list.innerHTML = request.serials.map((serial, index) => {
    const stateName = bulkSerialState(request, serial);
    const stateIcon = stateName === "valid"
      ? iconMarkup("check")
      : stateName === "failed"
        ? iconMarkup("circle-alert")
        : stateName === "checking"
          ? '<span class="activity-spinner" aria-hidden="true"></span>'
          : iconMarkup("circle-dashed");
    return `<div class="bulk-serial-row">
      <span class="bulk-serial-state ${stateName}" aria-label="${escapeHtml(bulkSerialStateText(request, serial))}">${stateIcon}</span>
      <span class="bulk-serial-value">${escapeHtml(serial)}</span>
      <small class="bulk-serial-detail ${stateName}">${escapeHtml(bulkSerialStateText(request, serial))}</small>
      <button class="row-menu" data-bulk-serial-remove="${index}" type="button" aria-label="Remove ${escapeHtml(serial)}" title="Remove serial">${iconMarkup("trash-2")}</button>
    </div>`;
  }).join("");
}

function renderBulkSerialEditor(request) {
  const editor = $("#bulkSerialEditor");
  if (!editor) return;
  const bulk = request.kind === "bulk_location";
  editor.hidden = !bulk;
  if (!bulk) {
    elements.serialsInput.hidden = true;
    $("#validateBulkSerialButton").hidden = true;
    $("#bulkSerialPrefixWarning").hidden = true;
    return;
  }
  const mode = bulkSerialMode(request);
  const individual = mode === "individual";
  const entryButton = $("#bulkSerialEntryModeButton");
  const textButton = $("#bulkSerialTextModeButton");
  entryButton.classList.toggle("active", individual);
  entryButton.setAttribute("aria-pressed", String(individual));
  textButton.classList.toggle("active", !individual);
  textButton.setAttribute("aria-pressed", String(!individual));
  $("#bulkSerialEntryMode").hidden = !individual;
  $("#bulkSerialTextMode").hidden = individual;
  elements.serialsInput.hidden = individual;
  $("#validateBulkSerialButton").hidden = individual;
  const keys = request.serials.map((serial) => String(serial).toLowerCase());
  const uniqueCount = new Set(keys).size;
  const duplicateCount = keys.length - uniqueCount;
  $("#bulkSerialUniqueCount").textContent = `${uniqueCount} unique serial${uniqueCount === 1 ? "" : "s"}`;
  $("#bulkSerialDuplicateWarning").hidden = duplicateCount === 0;
  $("#bulkSerialDuplicateWarning").textContent = duplicateCount
    ? `${duplicateCount} duplicate ${duplicateCount === 1 ? "entry" : "entries"}`
    : "";
  const prefixCount = removableBulkSerialPrefixCount(request.serials);
  $("#bulkSerialPrefixWarning").hidden = prefixCount === 0;
  renderBulkSerialList(request);
}

function removableBulkSerialPrefixCount(serials) {
  if (serials.length <= 5) return 0;
  const matching = serials.filter((serial) => /^S[A-Za-z0-9]{10}$/i.test(String(serial).trim()));
  return matching.length >= Math.ceil(serials.length * .8) ? matching.length : 0;
}

function removeBulkSerialPrefixes() {
  const request = selectedRequest();
  if (!request || request.kind !== "bulk_location" || !removableBulkSerialPrefixCount(request.serials)) return;
  recordAppEdit();
  request.serials = request.serials.map((serial) => {
    const value = String(serial).trim();
    return /^S[A-Za-z0-9]{10}$/i.test(value) ? value.slice(1) : value;
  });
  request.bulk_validation_epoch = Number(request.bulk_validation_epoch || 0) + 1;
  request.bulk_validation = request.serials.length ? "pending" : "empty";
  request.bulk_validation_error = "";
  request.bulk_validation_missing = [];
  request.bulk_serial_states = {};
  request.bulk_serial_errors = {};
  renderAll();
  if (bulkSerialMode(request) === "individual") {
    void validateBulkSerials({ requests: [request], render: true });
  }
  toast("Removed the leading S from MacBook serials.", "success");
}

function focusRequestSerialInput(request = selectedRequest()) {
  const input = request?.kind === "bulk_location"
    ? bulkSerialMode(request) === "individual" ? $("#bulkSerialAddInput") : elements.serialsInput
    : elements.serialInput;
  input?.focus({ preventScroll: true });
}

function setBulkSerialEntryError(message = "") {
  const error = $("#bulkSerialEntryError");
  if (!error) return;
  error.hidden = !message;
  error.textContent = message;
}

function setBulkSerialMode(mode) {
  const request = selectedRequest();
  if (!request || request.kind !== "bulk_location" || !["individual", "text"].includes(mode)) return;
  if (request.bulk_serial_mode === mode) return;
  recordAppEdit();
  request.bulk_serial_mode = mode;
  if (mode === "individual") {
    request.bulk_serial_states = request.bulk_serial_states || {};
    request.bulk_serial_errors = request.bulk_serial_errors || {};
    request.serials.forEach((serial) => {
      if (!request.bulk_serial_states[serial]) setBulkSerialState(request, serial, "pending");
    });
    updateBulkValidationSummary(request);
  } else {
    request.bulk_serial_states = {};
    request.bulk_serial_errors = {};
    request.bulk_validation = request.serials.length ? "pending" : "empty";
    request.bulk_validation_missing = [];
    request.bulk_validation_error = "";
  }
  setBulkSerialEntryError();
  renderAll();
  if (mode === "individual" && request.serials.length) {
    const pendingSerials = request.serials.filter((serial) => bulkSerialState(request, serial) !== "valid");
    if (pendingSerials.length) {
      void validateBulkSerials({
        requests: [request],
        render: true,
        serialsByRequest: new Map([[request.id, pendingSerials]]),
      });
    }
  }
  setTimeout(() => focusRequestSerialInput(request), 0);
}

function addBulkSerial() {
  const request = selectedRequest();
  const input = $("#bulkSerialAddInput");
  if (!request || request.kind !== "bulk_location" || !input) return;
  const serial = input.value.trim();
  if (!/^[A-Za-z0-9._-]{6,}$/.test(serial)) {
    setBulkSerialEntryError("Enter a valid serial number.");
    input.focus();
    return;
  }
  if (request.serials.some((value) => value.toLowerCase() === serial.toLowerCase())) {
    setBulkSerialEntryError(`${serial} is already in this request.`);
    input.focus();
    return;
  }
  recordAppEdit();
  request.serials.push(serial);
  request.bulk_serial_states = request.bulk_serial_states || {};
  request.bulk_serial_errors = request.bulk_serial_errors || {};
  setBulkSerialState(request, serial, "pending");
  request.bulk_validation = "pending";
  request.bulk_validation_missing = [];
  request.bulk_validation_error = "";
  input.value = "";
  setBulkSerialEntryError();
  renderAll();
  void validateBulkSerials({
    requests: [request],
    render: true,
    serialsByRequest: new Map([[request.id, [serial]]]),
  });
  setTimeout(() => $("#bulkSerialAddInput")?.focus(), 0);
}

function removeBulkSerial(index) {
  const request = selectedRequest();
  if (!request || request.kind !== "bulk_location") return;
  recordAppEdit();
  const [serial] = request.serials.splice(index, 1);
  if (serial) {
    if (request.bulk_serial_states) delete request.bulk_serial_states[serial];
    if (request.bulk_serial_errors) delete request.bulk_serial_errors[serial];
  }
  updateBulkValidationSummary(request);
  renderAll();
}

function renderInspector() {
  const request = selectedRequest();
  const open = Boolean(request);
  const newRequest = open && state.newRequest === request;
  const sidebarOpen = open && !newRequest;
  const createDialog = $("#requestCreateDialog");
  const contentHost = newRequest ? $("#requestCreateHost") : $("#inspectorEditorHost");
  if (elements.inspectorContent.parentElement !== contentHost) contentHost.append(elements.inspectorContent);
  if (newRequest && !createDialog.open) createDialog.showModal();
  if (!newRequest && createDialog.open) createDialog.close();
  elements.workspace.classList.toggle("inspector-closed", !sidebarOpen);
  elements.inspector.classList.toggle("is-closed", !sidebarOpen);
  elements.inspector.setAttribute("aria-hidden", String(!sidebarOpen));
  elements.inspectorEmpty.hidden = sidebarOpen;
  elements.inspectorContent.hidden = !open;
  $("#inspectorHeading").textContent = newRequest ? "New request" : "Request details";
  $("#duplicateButton").hidden = newRequest;
  $("#removeButton").hidden = newRequest;
  $("#discardNewRequestButton").hidden = !newRequest;
  $("#saveNewRequestButton").hidden = !newRequest;
  $("#closeInspectorButton").setAttribute("aria-label", newRequest ? "Cancel new request" : "Close request editor");
  $("#closeInspectorButton").title = newRequest ? "Cancel new request" : "Close request editor";
  const requestLocked = Boolean(request && requestIsInCurrentJob(request));
  $("#requestEditorFields").disabled = requestLocked;
  $("#inspectorSubmittingNotice").hidden = !requestLocked;
  $("#duplicateButton").disabled = requestLocked;
  hideSearchResults();
  setLookupStatus("serial", "");
  setLookupStatus("user", "");
  setLookupStatus("returning", "");
  if (!request) return;

  const bulk = request.kind === "bulk_location";
  const user = request.kind === "user";
  $("#requestSizeInput").value = bulk ? "bulk" : "single";
  $("#serialSearchControl").hidden = bulk;
  elements.serialsInput.hidden = !bulk;
  const bulkValidateButton = $("#validateBulkSerialButton");
  bulkValidateButton.hidden = !bulk;
  bulkValidateButton.disabled = !request.serials.length || request.bulk_validation === "checking";
  bulkValidateButton.textContent = request.bulk_validation === "checking" ? "Verifying…" : "Verify";
  elements.serialLabel.textContent = bulk ? "Serial numbers" : "Serial number";
  elements.serialInput.value = bulk ? "" : (request.serials[0] || "");
  elements.serialsInput.value = bulk ? request.serials.join("\n") : "";
  elements.serialHint.textContent = bulk ? `${request.serials.length} serial${request.serials.length === 1 ? "" : "s"}` : "";
  renderBulkSerialEditor(request);
  refreshBulkValidationButton(request);

  normalizeRequestStatus(request);
  const statusOptions = bulk
    ? requestStatusOptions("location", request.status)
    : singleRequestStatusOptions(request.status);
  fillSelect(elements.statusInput, statusOptions, request.status, "Choose a status");
  // Keep the complete single-device editor visible while a serial is being
  // looked up. Validation still prevents an incomplete request from being
  // saved or submitted, but the user/location fields should not be staged
  // behind serial verification.
  $("#statusFields").hidden = false;
  elements.userFields.hidden = !user;
  elements.locationFields.hidden = user;
  // The return-from-user control belongs to single location deployments only.
  // Set it on every render so it cannot remain hidden after switching the
  // request between deployment destinations.
  elements.returningUserFields.hidden = request.kind !== "location";
  elements.userInput.value = request.user || "";
  renderLookupConfirmations(request, { bulk, user });

  if (!user) {
    fillSelect(elements.cityInput, locationCities(request.location), request.location?.city || "", "Choose a city");
    const location = request.location || {};
    const results = locationResults(location.city);
    const hasExact = hasCompleteLocation(location);
    populateLocationPicker(elements.locationInput, location, results, locationEmptyText(location.city));
    elements.locationDetail.textContent = hasExact ? "" : "Choose a location.";
    elements.returningToggle.checked = Boolean(request.returning);
    elements.returningSearch.hidden = !request.returning;
    elements.returningUserInput.value = request.returning_user || "";
    elements.returnConfirmation.hidden = !(
      request.returning
      && request.returning_user
      && request.returning_user_selected
      && request.returning_user_validation === "valid"
      && request.returning_user_info
    );
    $("#confirmSerial").textContent = request.serials[0] || "Not selected";
    $("#confirmUser").textContent = request.returning_user || "Not selected";
    $("#confirmLocation").textContent = destinationLabel(request);
    renderReturningUserInfo(request);
    ensureLocationsLoaded(location.city);
  }

  refreshSelectedValidation();
  updateLookupControlStates(request);
}

function refreshBulkValidationButton(request = selectedRequest()) {
  const button = $("#validateBulkSerialButton");
  if (!button || !request) return;
  const bulk = request.kind === "bulk_location";
  button.hidden = !bulk || bulkSerialMode(request) !== "text";
  button.disabled = !request.serials.length || request.bulk_validation === "checking";
  button.textContent = request.bulk_validation === "checking" ? "Verifying…" : "Verify list";
  const status = $("#bulkValidationStatus");
  const alert = $("#bulkValidationAlert");
  status.hidden = !bulk || request.bulk_validation !== "valid";
  status.textContent = request.bulk_validation === "valid"
    ? `✓ Verified ${request.serials.length} serial${request.serials.length === 1 ? "" : "s"}`
    : "";
  const missing = request.bulk_validation_missing || [];
  alert.hidden = !bulk || request.bulk_validation !== "failed" || !missing.length;
  alert.innerHTML = missing.length
    ? `<div class="validation-panel-heading">${iconMarkup("triangle-alert")}<strong>Could not verify</strong></div><ul>${missing.map((serial) => `<li>${escapeHtml(serial)}</li>`).join("")}</ul>`
    : "";
}

function renderAll() {
  renderQueue();
  renderInspector();
  renderLatestImportDraft();
  refreshIcons();
}

function changeRequestSize(size) {
  const request = selectedRequest();
  if (!request) return;
  recordAppEdit();
  if (size === "bulk") {
    const locationStatus = state.config.location_statuses.some((option) => option.value === request.status)
      ? request.status
      : (state.config.default_location_status || state.config.location_statuses[0]?.value || "");
    applyInferredKind(request, locationStatus);
    request.kind = "bulk_location";
    request.group = "Bulk add to location stock";
    request.user = "";
    request.user_validation = "empty";
    request.user_validation_error = "";
    request.returning = false;
    request.returning_user = "";
    request.returning_user_selected = false;
    request.returning_user_info = null;
    request.returning_user_validation = "empty";
    request.returning_user_validation_error = "";
    request.returning_user_loading = false;
    request.location = request.location || preferredLocation();
    request.bulk_validation = request.serials.length ? "pending" : "empty";
    request.bulk_validation_error = "";
    request.bulk_serial_mode = "individual";
    request.bulk_serial_states = {};
    request.bulk_serial_errors = {};
  } else if (request.kind === "bulk_location") {
    request.serials = request.serials.slice(0, 1);
    request.serial_validation = request.serials.length ? "pending" : "empty";
    request.serial_validation_error = "";
    applyInferredKind(request, request.status, false);
  }
  renderAll();
  if (size === "bulk" && request.serials.length) {
    void validateBulkSerials({ requests: [request], render: true });
  }
  setTimeout(() => focusRequestSerialInput(request), 0);
}

function discardNewRequest() {
  if (!state.newRequest) return;
  recordAppEdit();
  state.newRequest = null;
  renderAll();
}

function startNewRequest() {
  if (state.newRequest) {
    focusRequestSerialInput(state.newRequest);
    return;
  }
  recordAppEdit();
  const kind = "user";
  state.selectedId = null;
  state.newRequest = makeRequest(kind);
  renderAll();
  setTimeout(() => focusRequestSerialInput(state.newRequest), 0);
}

function startNewBulkRequest() {
  startNewRequest();
  if (state.newRequest) changeRequestSize("bulk");
}

function saveNewRequest() {
  const request = state.newRequest;
  if (!request) return;
  const errors = newRequestValidationErrors(request);
  if (errors.length) {
    refreshSelectedValidation();
    return;
  }
  recordAppEdit();
  state.queue.push(request);
  state.selectedId = request.id;
  state.newRequest = null;
  renderAll();
  toast("Request added to the queue.", "success");
}

function handleInspectorDefaultKey(event) {
  if (event.key !== "Enter" || event.defaultPrevented || event.isComposing
    || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
  const target = event.target;
  if (!(target instanceof HTMLElement)
    || !["INPUT", "SELECT"].includes(target.tagName)
    || target.matches("input[type='checkbox'], input[type='radio']")) return;
  const request = selectedRequest();
  const saveButton = $("#saveNewRequestButton");
  if (!request || request !== state.newRequest || saveButton.hidden || saveButton.disabled) return;
  event.preventDefault();
  saveNewRequest();
}

function removeRequest(id) {
  const index = state.queue.findIndex((request) => request.id === id);
  if (index < 0) return;
  if (requestIsInCurrentJob(state.queue[index])) {
    toast("Wait for this request submission to finish before removing it.", "error");
    return;
  }
  recordAppEdit();
  state.queue.splice(index, 1);
  if (state.selectedId === id) {
    state.selectedId = state.queue[index]?.id || state.queue[index - 1]?.id || null;
  }
  renderAll();
}

function duplicateSelected() {
  const request = selectedRequest();
  if (!request) return;
  if (requestIsInCurrentJob(request)) return;
  recordAppEdit();
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
    if (!node) return;
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
    state.locationRetryCities.delete(city);
    return results;
  }).catch((error) => {
    scheduleLocationRetry(city);
    throw error;
  }).finally(() => {
    state.locationLoading.delete(city);
  });
  state.locationLoading.set(city, request);
  return request;
}

function refreshLocationConsumers(city) {
  const selected = selectedRequest();
  if (selected && selected.kind !== "user" && selected.location?.city === city) {
    renderInspector();
    renderQueue();
  }
  if (state.pasteLocation?.city === city) {
    state.pasteLocationResults = locationResults(city).map((result) => ({ ...result, city }));
    renderPasteLocationFields();
  }
  if (state.importLocation?.city === city) {
    state.importLocationResults = locationResults(city).map((result) => ({ ...result, city }));
    renderImportLocationFields();
  }
}

function scheduleLocationRetry(city) {
  if (!city) return;
  state.locationRetryCities.add(city);
  if (state.locationRetryTimer || !connectionIsReady()) return;
  state.locationRetryTimer = window.setTimeout(() => {
    state.locationRetryTimer = null;
    void retryPendingLocationLoads();
  }, 5000);
}

async function retryPendingLocationLoads() {
  if (!state.locationRetryCities.size || !connectionIsReady()) return;
  const cities = [...state.locationRetryCities];
  await Promise.all(cities.map(async (city) => {
    try {
      await fetchLocationResults(city, { force: true });
      refreshLocationConsumers(city);
    } catch (_) {
      // fetchLocationResults keeps the city queued for the next retry.
    }
  }));
  if (state.locationRetryCities.size) scheduleLocationRetry([...state.locationRetryCities][0]);
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
  const columns = (Array.isArray(result?.columns) ? result.columns : [])
    .map((value) => String(value || "").trim());
  const valid = (value) => /^[A-Za-z][A-Za-z0-9._-]*$/.test(String(value || "").trim());
  const exact = [result?.login, result?.value, ...columns]
    .map((value) => String(value || "").trim())
    .find((value) => value.toLowerCase() === String(query || "").trim().toLowerCase() && valid(value));
  if (exact) return exact;
  const candidates = [result?.login, result?.value, ...columns]
    .map((value) => String(value || "").trim());
  return candidates.find(valid) || String(query || "").trim();
}

function bestSerial(result, query) {
  const columns = (result.columns || []).map(String);
  const exact = columns.find((value) => value.toLowerCase() === query.toLowerCase());
  return exact
    || columns.find((value) => /^[A-Za-z0-9._-]{6,}$/.test(value))
    || String(result.value || "")
    || query;
}

function scheduleValidation(request, field, work) {
  const key = request.id + ":" + field;
  const existing = state.validationTimers.get(key);
  if (existing) clearTimeout(existing);
  const epochKey = field + "_validation_epoch";
  const epoch = Number(request[epochKey] || 0) + 1;
  request[epochKey] = epoch;
  const timer = setTimeout(() => {
    state.validationTimers.delete(key);
    work(epoch).catch(() => {});
  }, VALIDATION_DEBOUNCE_MS);
  state.validationTimers.set(key, timer);
}

function validationStillCurrent(request, field, epoch, value) {
  if (!request || request[field + "_validation_epoch"] !== epoch) return false;
  if (field === "serial") {
    const current = String(request.serials[0] || "");
    return current === value
      || (Boolean(request.serial_selected) && current.toLowerCase() === String(value || "").toLowerCase());
  }
  if (field === "bulk") return request.serials.join("\u0000") === value;
  if (field === "user" || field === "returning_user") {
    const current = String(request[field] || "");
    const selectedLogin = String(request[`${field}_info`]?.login || "");
    return current === value
      || (Boolean(request[`${field}_selected`]) && Boolean(selectedLogin) && current === selectedLogin);
  }
  return true;
}

function setLookupStatus(kind, message = "", busy = false) {
  const node = $("#" + kind + "LookupStatus");
  if (!node) return;
  node.hidden = !message;
  node.classList.toggle("busy", Boolean(message && busy));
  if (message && busy) node.style.setProperty("--spinner-delay", `${-(performance.now() % 700)}ms`);
  else node.style.removeProperty("--spinner-delay");
  node.textContent = message;
}

function cachedVerificationMessage(...fields) {
  const labels = fields.filter(Boolean);
  return labels.length ? `Verifying cached ${labels.join(" and ")}…` : "";
}

function spinnerPhaseStyle(duration) {
  return `style="--spinner-delay: -${Math.floor(performance.now() % duration)}ms"`;
}

function setLookupInputStatus(kind, value) {
  if (!value) return setLookupStatus(kind, "");
  if (value.length < 2) return setLookupStatus(kind, "Type at least 2 characters to search.");
  setLookupStatus(kind, "Checking Helix automatically…", true);
}

function updateLookupControlStates(request = selectedRequest()) {
  const serialButton = $("#searchSerialButton");
  const userButton = $("#searchUserButton");
  const returningButton = $("#searchReturningButton");
  if (!request) {
    [serialButton, userButton, returningButton].forEach((button) => { if (button) button.disabled = true; });
    return;
  }
  serialButton.disabled = request.kind === "bulk_location"
    || elements.serialInput.value.trim().length < 2
    || request.serial_validation === "checking";
  userButton.disabled = request.kind !== "user"
    || elements.userInput.value.trim().length < 2
    || request.user_validation === "checking";
  if (returningButton) {
    returningButton.disabled = request.kind !== "location"
      || !request.returning
      || elements.returningUserInput.value.trim().length < 2
      || request.returning_user_validation === "checking";
  }
}

function confirmedPersonLabel(login, info) {
  const values = (info?.columns || []).map((value) => String(value).trim()).filter(Boolean);
  const fullName = values.find((value) => value !== login && /\s/.test(value)) || login;
  return { fullName, login: fullName === login ? "" : login };
}

function renderLookupConfirmations(request, { bulk, user }) {
  const serialConfirmed = !bulk && Boolean(request.serial_selected && request.serials[0]);
  $("#serialSearchControl").hidden = bulk || serialConfirmed;
  $("#serialConfirmed").hidden = !serialConfirmed;
  $("#serialConfirmedValue").textContent = request.serials[0] || "";
  if (serialConfirmed) {
    elements.serialResults.hidden = true;
    elements.serialResults.replaceChildren();
  }

  const userConfirmed = user && Boolean(request.user_selected && request.user);
  $("#userSearchControl").hidden = !user || userConfirmed;
  $("#userConfirmed").hidden = !userConfirmed;
  const selectedUser = confirmedPersonLabel(request.user || "", request.user_info);
  $("#userConfirmedValue").textContent = selectedUser.fullName;
  $("#userConfirmedUsername").textContent = selectedUser.login;
  if (userConfirmed) {
    elements.userResults.hidden = true;
    elements.userResults.replaceChildren();
  }

  const returningConfirmed = !user && !bulk && request.returning
    && Boolean(request.returning_user_selected && request.returning_user);
  $("#returningSearchControl").hidden = !request.returning || returningConfirmed;
  $("#returningConfirmed").hidden = !returningConfirmed;
  const selectedReturningUser = confirmedPersonLabel(request.returning_user || "", request.returning_user_info);
  $("#returningConfirmedValue").textContent = selectedReturningUser.fullName;
  $("#returningConfirmedUsername").textContent = selectedReturningUser.login;
  if (returningConfirmed && elements.returningResults) {
    elements.returningResults.hidden = true;
    elements.returningResults.replaceChildren();
  }

}

function resetLookupSelection(kind) {
  const request = selectedRequest();
  if (!request) return;
  recordAppEdit();
  const field = kind === "serial" ? "serial" : kind === "user" ? "user" : "returning_user";
  const value = field === "serial" ? request.serials[0] || "" : request[field] || "";
  request[`${field}_selected`] = false;
  request[`${field}_validation_epoch`] = Number(request[`${field}_validation_epoch`] || 0) + 1;
  request[`${field}_validation`] = value ? "pending" : "empty";
  request[`${field}_validation_error`] = "";
  if (field === "returning_user") request.returning_user_loading = false;
  renderInspector();
  const input = field === "serial"
    ? elements.serialInput
    : field === "user" ? elements.userInput : elements.returningUserInput;
  input.focus();
  input.select();
  setLookupInputStatus(field === "returning_user" ? "returning" : kind, value);
  updateLookupControlStates(request);
  renderQueue();
}

async function loadSerialSuggestions(request, query, { requireSelection = true } = {}) {
  const value = query.trim();
  if (!value || value.length < 2) return;
  const epoch = Number(request.serial_validation_epoch || 0);
  request.serial_validation = "checking";
  request.serial_validation_error = "";
  setLookupStatus("serial", "Searching Helix for serial numbers…", true);
  updateLookupControlStates(request);
  refreshSelectedValidation();
  renderQueue();
  try {
    const payload = await api("/api/search/assets", {
      method: "POST",
      body: JSON.stringify({ query: value, fresh: true }),
    });
    if (!validationStillCurrent(request, "serial", epoch, value)) return;
    const results = payload.results || [];
    const exactAsset = results.find((item) => serialResultMatches(item, value));
    const cachedExact = Boolean(payload.cached && exactAsset);
    // A serial is unique. If Helix also returns fuzzy matches, an exact serial
    // is still safe to select immediately instead of making the user search
    // again or click a result.
    const autoSelectedExact = Boolean(exactAsset);
    if (cachedExact || autoSelectedExact) {
      request.serials = [bestSerial(exactAsset, value)];
      request.serial_selected = true;
      request.serial_validation = "valid";
      request.serial_validation_error = "";
      hideSearchResults();
      if (cachedExact) {
        if (selectedRequest() === request) {
          renderInspector();
          setLookupStatus("serial", cachedVerificationMessage("serial"), true);
        }
        verifyCachedValueInBackground("serial", value, false, (freshPayload) => {
          if (!validationStillCurrent(request, "serial", epoch, value)) return;
          const freshAsset = (freshPayload.results || []).find((item) => serialResultMatches(item, value));
          if (freshAsset) {
            request.serial_validation = "valid";
            request.serial_validation_error = "";
          } else {
            request.serial_validation = "failed";
            request.serial_validation_error = "Serial number was not found in Helix.";
          }
          if (selectedRequest() === request) setLookupStatus("serial", "");
          refreshSelectedValidation();
          updateLookupControlStates(request);
          renderQueue();
        }, () => {
          if (selectedRequest() === request) setLookupStatus("serial", "");
        });
      } else {
        request.serial_validation_epoch = Number(request.serial_validation_epoch || 0) + 1;
        if (selectedRequest() === request) renderInspector();
      }
    } else if (selectedRequest() === request) renderSearchResults(elements.serialResults, results, (result) => {
      if (!validationStillCurrent(request, "serial", epoch, value)) return;
      recordAppEdit();
      hideSearchResults();
      request.serial_validation_epoch = Number(request.serial_validation_epoch || 0) + 1;
      request.serials = [bestSerial(result, value)];
      request.serial_selected = true;
      request.serial_validation = "valid";
      request.serial_validation_error = "";
      renderInspector();
      renderQueue();
    }, 1);
    if (selectedRequest() === request && !cachedExact && !autoSelectedExact) setLookupStatus("serial", results.length ? "Select a matching serial number:" : "No matching serial numbers found.");
    const exact = results.some((item) => serialResultMatches(item, value));
    if (!exact) {
      request.serial_validation = "failed";
      request.serial_validation_error = "Serial number was not found in Helix.";
    } else if (requireSelection && !cachedExact && !autoSelectedExact) {
      request.serial_validation = "unselected";
      request.serial_validation_error = "Serial number is not verified.";
    } else {
      request.serial_validation = "valid";
    }
    refreshSelectedValidation();
    updateLookupControlStates(request);
    renderQueue();
  } catch (error) {
    if (!validationStillCurrent(request, "serial", epoch, value)) return;
    request.serial_validation = "failed";
    request.serial_validation_error = error.message || "Could not verify the serial number in Helix.";
    if (selectedRequest() === request) setLookupStatus("serial", "Serial search failed.");
    refreshSelectedValidation();
    updateLookupControlStates(request);
    renderQueue();
  }
}

async function validateUserAfterPause(request, returning = false) {
  const field = returning ? "returning_user" : "user";
  const value = (returning ? request.returning_user : request.user).trim();
  if (!value || value.length < 2) return;
  const epoch = Number(request[field + "_validation_epoch"] || 0);
  if (returning) {
    request.returning_user_validation = "checking";
    request.returning_user_validation_error = "";
    request.returning_user_loading = true;
  } else {
    request.user_validation = "checking";
    request.user_validation_error = "";
  }
  setLookupStatus(returning ? "returning" : "user", "Searching Helix for users…", true);
  updateLookupControlStates(request);
  refreshSelectedValidation();
  renderQueue();
  try {
    const payload = await api("/api/search/users", {
      method: "POST",
      body: JSON.stringify({ query: value, returning, fresh: true }),
    });
    if (!validationStillCurrent(request, field, epoch, value)) return;
    const results = payload.results || [];
    const cachedResult = payload.cached
      ? results.find((item) => userResultMatches(item, value))
      : null;
    const exactUser = results.find((item) => userResultMatches(item, value));
    // Full names and usernames can both be exact matches. Prefer that exact
    // result even when Helix includes additional fuzzy suggestions.
    const autoSelectedResult = cachedResult || exactUser;
    const container = returning ? elements.returningResults : elements.userResults;
    if (autoSelectedResult) {
      const login = bestLogin(autoSelectedResult, value);
      const info = { login, columns: (autoSelectedResult.columns || [autoSelectedResult.value]).map(String).filter(Boolean) };
      request[field] = login;
      request[`${field}_selected`] = true;
      request[`${field}_info`] = info;
      request[`${field}_validation`] = "valid";
      request[`${field}_validation_error`] = "";
      hideSearchResults();
      if (cachedResult) {
        if (selectedRequest() === request) setLookupStatus(returning ? "returning" : "user", cachedVerificationMessage("user"), true);
        verifyCachedValueInBackground("username", value, returning, (freshPayload) => {
          if (!validationStillCurrent(request, field, epoch, value)) return;
          const freshResult = (freshPayload.results || []).find((item) => userResultMatches(item, value));
          if (freshResult) {
            const freshLogin = bestLogin(freshResult, value);
            request[field] = freshLogin;
            request[`${field}_selected`] = true;
            request[`${field}_info`] = {
              login: freshLogin,
              columns: (freshResult.columns || [freshResult.value]).map(String).filter(Boolean),
            };
            request[`${field}_validation`] = "valid";
            request[`${field}_validation_error`] = "";
          } else {
            request[`${field}_validation`] = "failed";
            request[`${field}_validation_error`] = "User was not found in Helix.";
          }
          if (selectedRequest() === request) setLookupStatus(returning ? "returning" : "user", "");
          refreshSelectedValidation();
          updateLookupControlStates(request);
          renderQueue();
        }, () => {
          if (selectedRequest() === request) setLookupStatus(returning ? "returning" : "user", "");
        });
      } else {
        request[`${field}_validation_epoch`] = Number(request[`${field}_validation_epoch`] || 0) + 1;
        if (selectedRequest() === request) renderInspector();
      }
    } else if (selectedRequest() === request) renderSearchResults(container, results, (result) => {
      if (!validationStillCurrent(request, field, epoch, value)) return;
      recordAppEdit();
      hideSearchResults();
      request[`${field}_validation_epoch`] = Number(request[`${field}_validation_epoch`] || 0) + 1;
      const login = bestLogin(result, value);
      const info = { login, columns: (result.columns || [result.value]).map(String).filter(Boolean) };
      if (returning) {
        request.returning_user = login;
        request.returning_user_selected = true;
        request.returning_user_info = info;
        request.returning_user_validation = "valid";
        request.returning_user_validation_error = "";
        request.returning_user_loading = false;
      } else {
        request.user = login;
        request.user_selected = true;
        request.user_info = info;
        request.user_validation = "valid";
        request.user_validation_error = "";
      }
      renderInspector();
      renderQueue();
    }, 0);
    if (selectedRequest() === request && !cachedResult && !autoSelectedResult) setLookupStatus(returning ? "returning" : "user", results.length ? "Select a matching user:" : "No matching users found.");
    const hasResults = results.length > 0;
    if (returning) {
      request.returning_user_loading = false;
      request.returning_user_validation = autoSelectedResult ? "valid" : hasResults ? "suggested" : "failed";
      request.returning_user_validation_error = autoSelectedResult
        ? ""
        : hasResults ? "Choose the verified user from the suggestions." : "User was not found in Helix.";
    } else {
      request.user_validation = autoSelectedResult ? "valid" : hasResults ? "suggested" : "failed";
      request.user_validation_error = autoSelectedResult
        ? ""
        : hasResults ? "Choose the verified user from the suggestions." : "User was not found in Helix.";
    }
    refreshSelectedValidation();
    updateLookupControlStates(request);
    renderQueue();
  } catch (error) {
    if (!validationStillCurrent(request, field, epoch, value)) return;
    if (returning) {
      request.returning_user_loading = false;
      request.returning_user_validation = "failed";
      request.returning_user_validation_error = error.message || "Could not verify the user in Helix.";
    } else {
      request.user_validation = "failed";
      request.user_validation_error = error.message || "Could not verify the user in Helix.";
    }
    if (selectedRequest() === request) setLookupStatus(returning ? "returning" : "user", "User search failed.");
    refreshSelectedValidation();
    updateLookupControlStates(request);
    renderQueue();
  }
}

async function searchAssets() {
  const request = selectedRequest();
  if (!request || request.kind === "bulk_location") return;
  const query = elements.serialInput.value.trim();
  if (query.length < 2) return toast("Enter at least two serial characters.", "error");
  request.serial_validation_epoch = Number(request.serial_validation_epoch || 0) + 1;
  try { await loadSerialSuggestions(request, query, { requireSelection: true }); }
  catch (error) { toast(error.message, "error"); }
  finally { updateLookupControlStates(request); }
}

async function searchUsers(returning = false) {
  const request = selectedRequest();
  if (!request) return;
  const input = returning ? elements.returningUserInput : elements.userInput;
  const button = returning ? $("#searchReturningButton") : $("#searchUserButton");
  const container = returning ? elements.returningResults : elements.userResults;
  const query = input.value.trim();
  if (query.length < 2) return toast("Enter at least two name or username characters.", "error");
  setLookupStatus(returning ? "returning" : "user", "Searching Helix for users…", true);
  button.disabled = true;
  try {
    const payload = await api("/api/search/users", {
      method: "POST",
      body: JSON.stringify({ query, returning }),
    });
    const results = payload.results || [];
    const cachedResult = payload.cached
      ? results.find((item) => userResultMatches(item, query))
      : null;
    if (cachedResult) {
      const field = returning ? "returning_user" : "user";
      const login = bestLogin(cachedResult, query);
      request[field] = login;
      request[`${field}_selected`] = true;
      request[`${field}_info`] = {
        login,
        columns: (cachedResult.columns || [cachedResult.value]).map(String).filter(Boolean),
      };
      request[`${field}_validation`] = "valid";
      request[`${field}_validation_error`] = "";
      request.returning_user_loading = false;
      hideSearchResults();
      verifyCachedValueInBackground("username", query, returning, (freshPayload) => {
        const freshResult = (freshPayload.results || []).find((item) => userResultMatches(item, query));
        if (!freshResult || (request[field] !== login && request[field] !== query)) return;
        const freshLogin = bestLogin(freshResult, query);
        request[field] = freshLogin;
        request[`${field}_info`] = {
          login: freshLogin,
          columns: (freshResult.columns || [freshResult.value]).map(String).filter(Boolean),
        };
        request[`${field}_validation`] = "valid";
        request[`${field}_validation_error`] = "";
        renderInspector();
        renderQueue();
        if (selectedRequest() === request) setLookupStatus(returning ? "returning" : "user", "");
      }, () => {
        if (selectedRequest() === request) setLookupStatus(returning ? "returning" : "user", "");
      });
      renderInspector();
      renderQueue();
      if (selectedRequest() === request) setLookupStatus(returning ? "returning" : "user", cachedVerificationMessage("user"), true);
      return;
    }
    const exactUser = results.find((item) => userResultMatches(item, query));
    if (results.length === 1 && exactUser) {
      const field = returning ? "returning_user" : "user";
      request[`${field}_validation_epoch`] = Number(request[`${field}_validation_epoch`] || 0) + 1;
      const login = bestLogin(exactUser, query);
      request[field] = login;
      request[`${field}_selected`] = true;
      request[`${field}_info`] = {
        login,
        columns: (Array.isArray(exactUser.columns) ? exactUser.columns : [exactUser.value]).map(String).filter(Boolean),
      };
      request[`${field}_validation`] = "valid";
      request[`${field}_validation_error`] = "";
      request.returning_user_loading = false;
      hideSearchResults();
      renderInspector();
      renderQueue();
      return;
    }
    renderSearchResults(container, results, (result) => {
      recordAppEdit();
      hideSearchResults();
      const field = returning ? "returning_user" : "user";
      request[`${field}_validation_epoch`] = Number(request[`${field}_validation_epoch`] || 0) + 1;
      const login = bestLogin(result, query);
      if (returning) {
        request.returning_user = login;
        request.returning_user_selected = true;
        request.returning_user_info = {
          login,
          columns: (Array.isArray(result.columns) ? result.columns : [result.value]).map(String).filter(Boolean),
        };
        request.returning_user_validation = "valid";
        request.returning_user_validation_error = "";
        request.returning_user_loading = false;
      } else {
        request.user = login;
        request.user_selected = true;
        request.user_info = {
          login,
          columns: (Array.isArray(result.columns) ? result.columns : [result.value]).map(String).filter(Boolean),
        };
        request.user_validation = "valid";
        request.user_validation_error = "";
      }
      renderInspector();
      renderQueue();
    }, 0);
    setLookupStatus(returning ? "returning" : "user", results.length ? "Select a matching user:" : "No matching users found.");
  } catch (error) {
    setLookupStatus(returning ? "returning" : "user", "User search failed.");
    toast(error.message, "error");
  } finally { updateLookupControlStates(request); }
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
    const current = selectedRequest();
    if (current?.id === requestId && current.kind !== "user" && current.location?.city === city) {
      renderInspector();
      renderQueue();
    }
  } catch (error) {
    if (selectedRequest()?.id === requestId) {
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

function connectionIsReady(status = state.connection) {
  return ["connected", "simulation"].includes(status?.state);
}

function renderConnectionSheet(status = state.connection) {
  const ready = connectionIsReady(status);
  const stateName = status?.state || "checking";
  const visualState = ready ? "connected" : stateName === "connecting" ? "connecting" : "disconnected";
  if (state.connectionDismissTimer) {
    window.clearTimeout(state.connectionDismissTimer);
    state.connectionDismissTimer = null;
  }
  elements.connectionStatus.hidden = stateName !== "connected";
  elements.connectionSheetTitle.textContent = "Helix Authentication Required";
  elements.connectionLoading.hidden = !["checking", "connecting"].includes(stateName);
  elements.connectionAuthenticateButton.textContent = "Authenticate in Helix";
  elements.connectionDialog.dataset.state = visualState;
  elements.connectionVisual.dataset.state = visualState;
  elements.connectionVisual.setAttribute(
    "aria-label",
    ready
      ? "AutoEUDM is connected to Helix"
      : visualState === "connecting"
        ? "AutoEUDM is connecting to Helix"
        : "AutoEUDM is waiting for Helix authentication",
  );
  const linkIcon = visualState === "connected" ? "link-2" : "link-2-off";
  if (elements.connectionLinkIcon.dataset.icon !== linkIcon) {
    elements.connectionLinkIcon.dataset.icon = linkIcon;
    elements.connectionLinkIcon.innerHTML = iconMarkup(linkIcon);
  }
  refreshIcons(elements.connectionVisual);
  if (ready) {
    if (elements.connectionDialog.open) {
      state.connectionDismissTimer = window.setTimeout(() => {
        state.connectionDismissTimer = null;
        if (connectionIsReady() && elements.connectionDialog.open) elements.connectionDialog.close();
      }, 420);
    }
    return;
  }
  if (!elements.connectionDialog.open) elements.connectionDialog.showModal();
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
  renderConnectionSheet(status);
  if (status.state === "connected" && !state.liveOptionsLoaded) {
    refreshFormOptions();
  }
  if (status.state === "connected") {
    renderAll();
    window.setTimeout(() => { void retryPendingLocationLoads(); }, 0);
  }
  else renderQueue();
  if (connectionIsReady(status) && !["connected", "simulation"].includes(previousState)) {
    window.setTimeout(resumePendingImportValidation, 0);
    window.setTimeout(resumePendingBulkValidation, 0);
    window.setTimeout(resumePendingBacklogValidation, 0);
  }
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

async function refreshConnection({ verify = false } = {}) {
  try {
    const status = await api("/api/status");
    updateConnection(status);
    if (status.state === "connecting") setTimeout(() => refreshConnection({ verify }), 900);
    if (verify && status.state === "connected") await checkConnection();
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
  try {
    const status = await api("/api/connect", { method: "POST", body: "{}" });
    updateConnection(status);
    setTimeout(() => refreshConnection({ verify: true }), 700);
  } catch (error) {
    toast(error.message, "error");
  }
}

function bindConnectionSheetEvents() {
  if (state.connectionSheetEventsBound) return;
  state.connectionSheetEventsBound = true;
  elements.connectionAuthenticateButton.addEventListener("click", connect);
  elements.connectionDialog.addEventListener("cancel", (event) => event.preventDefault());
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
  $("#spreadsheetUsernameColumnInput").value = columns.username || "Username";
  $("#spreadsheetDeploymentColumnInput").value = columns.deployment_serial || "SN";
  $("#spreadsheetReturnedColumnInput").value = columns.returned_device || "";
  $("#spreadsheetPendingColumnInput").value = columns.pending_return || "OLD Device SN";
  $("#spreadsheetEnabledColumnInput").value = columns.enabled || "";
  $("#spreadsheetDeviceAllocationColumnInput").value = columns.device_allocation || "Device(s) Allocation";
  $("#spreadsheetNewAssetStatusColumnInput").value = columns.new_asset_status || "New Asset Status";
  $("#spreadsheetFirstNameColumnInput").value = columns.first_name || "First Name";
  $("#spreadsheetLastNameColumnInput").value = columns.last_name || "Last Name";
  $("#validateQuickImportInput").checked = validationEnabled("validate_quick_import");
  $("#validateWorkbookImportInput").checked = validationEnabled("validate_workbook_import");
  $("#saveAlmImportDraftsInput").checked = state.preferences.save_alm_import_drafts !== false;
  renderRequestStatusSettings();
  $("#settingsDialog").showModal();
}

function openAlmWorkbookImport() {
  resetImportDialog("deploy");
  $("#importDialog").showModal();
}

function openAlmBacklogImport() {
  resetImportDialog("backlog");
  $("#importDialog").showModal();
}

function openBacklogForCurrentWorkbook() {
  const workbook = state.workbook;
  if (!workbook?.import_id) return openAlmBacklogImport();
  state.importMode = "backlog";
  state.importPreview = null;
  state.importDraftId = newImportDraftId();
  state.importUndoStack = [];
  state.importRedoStack = [];
  $("#importDialog").showModal();
  showImportedWorkbook(workbook);
}

function workbookFile(file) {
  return file && /\.(xlsx|xlsm)$/i.test(file.name || "");
}

function setQueueWorkbookDropActive(active) {
  const drop = $("#queueWorkbookDrop");
  if (!drop) return;
  drop.hidden = !active;
  drop.setAttribute("aria-hidden", String(!active));
}

function importDroppedWorkbook(file) {
  if (!state.config?.spreadsheet_import_enabled) return;
  if (!workbookFile(file)) {
    toast("Drop an .xlsx or .xlsm ALM Workbook.", "error");
    return;
  }
  openAlmWorkbookImport();
  uploadWorkbook(file);
}

function setImportDialogDropActive(active) {
  const target = $("#importFileChooser");
  if (target) target.classList.toggle("is-dragging", active);
}

function importDroppedIntoDialog(file) {
  if (!workbookFile(file)) {
    toast("Drop an .xlsx or .xlsm ALM Workbook.", "error");
    return;
  }
  if (!$("#importDialog").open) openAlmWorkbookImport();
  else resetImportDialog("deploy");
  uploadWorkbook(file);
}

function openShortcuts() {
  $("#shortcutsDialog").showModal();
}

function focusSelectedSerial() {
  const request = selectedRequest();
  if (!request) return;
  focusRequestSerialInput(request);
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
    serialValidationState: "",
    serialValidationError: "",
    userValidationState: username ? "" : "valid",
    userValidationError: "",
    serialCacheVerification: false,
    userCacheVerification: false,
    validationChecked: false,
    validationEpoch: 0,
    kind: username ? "user" : "location",
    userStatus: resolveStatus(requestStatusOptions("user"), state.config.default_user_status),
    locationStatus: resolveStatus(requestStatusOptions("location"), state.config.default_location_status),
  };
}

function syncQuickImportValidation(entry) {
  const serialFailed = entry.serialValidationState === "failed";
  const requiresUser = entry.kind === "user";
  const userFailed = requiresUser && entry.userValidationState === "failed";
  const checking = entry.serialValidationState === "checking" || (requiresUser && entry.userValidationState === "checking");
  const valid = entry.serialValidationState === "valid"
    && (!requiresUser || (entry.username && entry.userValidationState === "valid"));
  entry.validationState = checking ? "checking" : serialFailed || userFailed ? "failed" : valid ? "valid" : "";
  entry.validationError = serialFailed
    ? entry.serialValidationError
    : userFailed
      ? entry.userValidationError
      : "";
  return entry.validationState;
}

function resetQuickImportUserValidation(entry) {
  entry.validationEpoch = Number(entry.validationEpoch || 0) + 1;
  entry.userValidationState = entry.kind === "user" && entry.username ? "" : "valid";
  entry.userValidationError = "";
  entry.userCacheVerification = false;
  entry.validationChecked = false;
  syncQuickImportValidation(entry);
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
    const validationState = syncQuickImportValidation(entry);
    const usernameRequired = entry.kind === "user" && !entry.username;
    const username = entry.kind === "user"
      ? entry.username ? `To ${escapeHtml(entry.username)}` : "Username required"
      : "Add to selected location";
    const statusOptions = singleRequestStatusOptions();
    const selectedStatus = entry.kind === "location" ? entry.locationStatus : entry.userStatus;
    const deploymentStatus = `<label class="quick-import-status">Status
          <select data-pairs-status="${index}" aria-label="Deployment status for ${escapeHtml(entry.serial)}">
            ${statusOptions.map((option) => `<option value="${escapeHtml(option.value)}" ${option.value === selectedStatus ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}
          </select>
        </label>`;
    const cachedFields = [
      entry.serialCacheVerification ? "serial" : "",
      entry.userCacheVerification ? "user" : "",
    ].filter(Boolean);
    const checking = validationState === "checking"
      ? verificationStatusMarkup("checking", entry.serialValidationState === "valid" ? "Serial verified · Verifying the user in Helix…" : "Verifying the serial and user in Helix…")
      : validationState === "failed"
        ? verificationStatusMarkup("failed", entry.validationError || "Not found in Helix")
        : cachedFields.length
          ? verificationStatusMarkup("checking", cachedVerificationMessage(...cachedFields))
        : validationState === "valid"
          ? verificationStatusMarkup("valid", `Serial verified${entry.username ? " · User verified" : ""}`)
          : "";
    const usernameError = usernameRequired
      ? `<div class="quick-import-row-error"><input data-pairs-username="${index}" type="text" autocomplete="off" spellcheck="false" placeholder="Enter username" aria-label="Username for ${escapeHtml(entry.serial)}"><small>Username required for Deploy to user</small></div>`
      : "";
    return `<div class="quick-import-row">
      <div><strong>${escapeHtml(entry.serial)}</strong><small>${username}</small>${usernameError}${checking}</div>
      <div class="quick-import-row-actions">
        ${deploymentStatus}
        <button class="row-menu" type="button" data-pairs-remove="${index}" aria-label="Remove ${escapeHtml(entry.serial)}" title="Remove device">${iconMarkup("trash-2")}</button>
      </div>
    </div>`;
  }).join("");
  $$("[data-pairs-status]").forEach((select) => select.addEventListener("change", () => {
    const entry = state.pasteEntries[Number(select.dataset.pairsStatus)];
    recordAppEdit();
    const kind = kindForStatus(select.value);
    const kindChanged = entry.kind !== kind;
    entry.kind = kind;
    if (kind === "location") entry.locationStatus = select.value;
    else entry.userStatus = select.value;
    if (kindChanged && entry.username) resetQuickImportUserValidation(entry);
    else syncQuickImportValidation(entry);
    renderQuickImportReview();
    resolveQuickImportReturningUsers();
  }));
  $$("[data-pairs-remove]").forEach((button) => button.addEventListener("click", () => {
    recordAppEdit();
    state.pasteEntries.splice(Number(button.dataset.pairsRemove), 1);
    renderQuickImportReview();
  }));
  $$("[data-pairs-username]").forEach((input) => input.addEventListener("change", () => {
    const entry = state.pasteEntries[Number(input.dataset.pairsUsername)];
    if (!entry) return;
    recordAppEdit();
    entry.username = input.value.trim();
    resetQuickImportUserValidation(entry);
    renderQuickImportReview();
    resolveQuickImportReturningUsers();
  }));
  const locationNeeded = state.pasteEntries.some((entry) => entry.kind === "location");
  $("#pairsLocationFields").hidden = !locationNeeded;
  const validationRequired = validationEnabled("validate_quick_import");
  const checking = validationRequired
    && state.pasteEntries.some((entry) => !["valid", "failed"].includes(entry.validationState));
  const failed = validationRequired
    && state.pasteEntries.some((entry) => entry.validationState === "failed");
  const missingUser = state.pasteEntries.some((entry) => entry.kind === "user" && !entry.username);
  const missingLocation = locationNeeded && !hasCompleteLocation(state.pasteLocation || preferredLocation());
  const completed = state.pasteEntries.filter((entry) => ["valid", "failed"].includes(entry.validationState)).length;
  const addButton = $("#addPairsButton");
  addButton.disabled = state.pasteEntries.length === 0 || missingUser || missingLocation || checking || failed;
  setButtonLabel(addButton, checking
    ? `Verifying ${completed}/${state.pasteEntries.length}…`
    : failed ? "Fix validation errors"
      : state.pasteEntries.length ? `Add ${state.pasteEntries.length} to queue` : "Add to queue");
  if (locationNeeded) renderPasteLocationFields();
  refreshIcons($("#pasteDialog"));
}

function scheduleQuickImportReview() {
  if (state.quickImportRenderFrame) return;
  state.quickImportRenderFrame = window.requestAnimationFrame(() => {
    state.quickImportRenderFrame = null;
    renderQuickImportReview();
  });
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
  if (!validationEnabled("validate_quick_import")) {
    entries.forEach((entry) => {
      entry.validationChecked = true;
      entry.serialValidationState = "valid";
      entry.serialValidationError = "";
      entry.userValidationState = "valid";
      entry.userValidationError = "";
      entry.serialCacheVerification = false;
      entry.userCacheVerification = false;
      syncQuickImportValidation(entry);
    });
    renderQuickImportReview();
    return;
  }
  if (!entries.length || !["connected", "simulation"].includes(state.connection?.state)) return;
  entries.forEach((entry) => {
    entry.validationEpoch = Number(entry.validationEpoch || 0) + 1;
    entry.validationChecked = true;
    entry.serialCacheVerification = false;
    entry.userCacheVerification = false;
    if (entry.serialValidationState !== "valid") {
      entry.serialValidationState = "checking";
      entry.serialValidationError = "";
    }
    if (entry.kind === "user" && entry.username && entry.userValidationState !== "valid") {
      entry.userValidationState = "checking";
      entry.userValidationError = "";
    } else if (!entry.username) {
      entry.userValidationState = "valid";
    }
    syncQuickImportValidation(entry);
  });
  renderQuickImportReview();
  await forEachWithConcurrency(entries, VALIDATION_CONCURRENCY, async (entry) => {
    const validationEpoch = entry.validationEpoch;
    const serialChecking = entry.serialValidationState === "checking";
    const userChecking = entry.userValidationState === "checking";
    const settled = (promise) => promise.then((value) => ({ value })).catch((error) => ({ error }));
    try {
      const [assetsResult, usersResult] = await Promise.all([
        serialChecking
          ? settled(api("/api/search/assets", { method: "POST", body: JSON.stringify({ query: entry.serial, fresh: true }) }))
          : Promise.resolve({ value: null }),
        userChecking
          ? settled(api("/api/search/users", { method: "POST", body: JSON.stringify({ query: entry.username, fresh: true }) }))
          : Promise.resolve({ value: null }),
      ]);
      if (entry.validationEpoch !== validationEpoch) return;

      if (serialChecking) {
        const asset = assetsResult.error
          ? null
          : (assetsResult.value?.results || []).find((item) => serialResultMatches(item, entry.serial));
        if (assetsResult.error || !asset) {
          entry.serialValidationState = "failed";
          entry.serialValidationError = assetsResult.error?.message || "Serial number was not found in Helix.";
        } else {
          entry.serialValidationState = "valid";
          entry.serialValidationError = "";
          if (assetsResult.value?.cached) {
            entry.serialCacheVerification = true;
            verifyCachedValueInBackground("serial", entry.serial, false, (freshPayload) => {
              if (entry.validationEpoch !== validationEpoch) return;
              const freshAsset = (freshPayload.results || []).find((item) => serialResultMatches(item, entry.serial));
              entry.serialCacheVerification = false;
              if (freshAsset) {
              } else {
                entry.serialValidationState = "failed";
                entry.serialValidationError = "Serial number was not found in Helix.";
              }
              syncQuickImportValidation(entry);
              scheduleQuickImportReview();
            }, () => {
              if (entry.validationEpoch !== validationEpoch) return;
              entry.serialCacheVerification = false;
              scheduleQuickImportReview();
            });
          }
        }
      }

      if (userChecking) {
        const result = usersResult.error
          ? null
          : (usersResult.value?.results || []).find((item) => userResultMatches(item, entry.username));
        if (usersResult.error || !result) {
          entry.userValidationState = "failed";
          entry.userValidationError = usersResult.error?.message || "Username was not found in Helix.";
        } else {
          entry.returningUserInfo = { login: bestLogin(result, entry.username), columns: (result.columns || [result.value]).map(String).filter(Boolean) };
          entry.userValidationState = "valid";
          entry.userValidationError = "";
          if (usersResult.value?.cached) {
            entry.userCacheVerification = true;
            verifyCachedValueInBackground("username", entry.username, false, (freshPayload) => {
              if (entry.validationEpoch !== validationEpoch) return;
              const freshResult = (freshPayload.results || []).find((item) => userResultMatches(item, entry.username));
              entry.userCacheVerification = false;
              if (freshResult) {
                entry.returningUserInfo = { login: bestLogin(freshResult, entry.username), columns: (freshResult.columns || [freshResult.value]).map(String).filter(Boolean) };
              } else {
                entry.userValidationState = "failed";
                entry.userValidationError = "Username was not found in Helix.";
                entry.returningUserInfo = null;
              }
              syncQuickImportValidation(entry);
              scheduleQuickImportReview();
            }, () => {
              if (entry.validationEpoch !== validationEpoch) return;
              entry.userCacheVerification = false;
              scheduleQuickImportReview();
            });
          }
        }
      }
      syncQuickImportValidation(entry);
    } catch (error) {
      const message = error.message || "Could not validate this entry.";
      if (serialChecking) {
        entry.serialValidationState = "failed";
        entry.serialValidationError = message;
      }
      if (userChecking) {
        entry.userValidationState = "failed";
        entry.userValidationError = message;
      }
      syncQuickImportValidation(entry);
    } finally {
      scheduleQuickImportReview();
    }
  });
  renderQuickImportReview();
}

function applyQuickImportKind() {
  const status = $("#pairsBulkKind").value;
  if (!status) return;
  const kind = kindForStatus(status);
  state.pasteEntries.forEach((entry) => {
    const kindChanged = entry.kind !== kind;
    entry.kind = kind;
    if (kind === "location") {
      entry.locationStatus = resolveStatus(state.config.location_statuses, status);
    } else {
      entry.userStatus = resolveStatus(state.config.user_statuses, status);
    }
    if (kindChanged && entry.username) resetQuickImportUserValidation(entry);
    else syncQuickImportValidation(entry);
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
  recordAppEdit();
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
  if (entries.length) recordAppEdit();
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
  state.pasteEntries.forEach(syncQuickImportValidation);
  if (!state.pasteEntries.length) errors.push("Choose deployments before adding them to the queue.");
  if (state.pasteEntries.some((entry) => entry.kind === "location")) {
    const location = state.pasteLocation || preferredLocation();
    if (!hasCompleteLocation(location)) {
      errors.push("Choose a complete city and location before adding these requests.");
    }
  }
  if (validationEnabled("validate_quick_import") && state.pasteEntries.some((entry) => !["valid", "failed"].includes(entry.validationState))) errors.push("Wait for Helix validation to finish.");
  if (validationEnabled("validate_quick_import") && state.pasteEntries.some((entry) => entry.validationState === "failed")) errors.push("Correct the entries Helix could not validate.");
  if (state.pasteEntries.some((entry) => entry.kind === "user" && !entry.username)) errors.push("A username is required for a deployed status.");
  if (errors.length) {
    $("#pairsError").textContent = errors.join(" ");
    $("#pairsError").hidden = false;
    return;
  }
  const requests = state.pasteEntries.map(({ serial, username, kind, userStatus, locationStatus, returningUserInfo, serialValidationState, serialValidationError, userValidationState, userValidationError, validationState, validationError }) => {
    const locationMode = kind === "location";
    const request = makeRequest(locationMode ? "location" : "user");
    const serialValidated = serialValidationState === "valid";
    const userValidated = !username || userValidationState === "valid";
    request.serials = [serial];
    request.serial_selected = serialValidated;
    request.serial_validation = serialValidated ? "valid" : (serialValidationState || validationState || "pending");
    request.serial_validation_error = serialValidationError || validationError || "";
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
      request.group = "Quick import · Add to location stock";
    } else {
      request.user = username;
      request.user_selected = Boolean(username && userValidated);
      request.user_info = returningUserInfo || null;
      request.user_validation = username
        ? (userValidated ? "valid" : (userValidationState || validationState || "pending"))
        : "empty";
      request.user_validation_error = username ? (userValidationError || validationError || "") : "";
      request.group = "Quick import · Deploy to user";
    }
    return request;
  });
  recordAppEdit();
  state.queue.push(...requests);
  state.selectedId = requests[0].id;
  $("#pasteDialog").close();
  renderAll();
  toast(`${requests.length} request${requests.length === 1 ? "" : "s"} added.`, "success");
}

function resetImportDialog(mode = state.importMode || "deploy") {
  saveCurrentImportDraft({ immediate: true });
  state.importMode = mode;
  state.importSelectedDates = [];
  state.importUploadToken += 1;
  state.workbook = null;
  state.workbookInspection = null;
  state.importPreview = null;
  state.importPreviewNeedsFullRender = false;
  state.importDraftId = null;
  state.importUndoStack = [];
  state.importRedoStack = [];
  state.importExpandedGroups.clear();
  state.backlogValidationIds.clear();
  $("#workbookInput").value = "";
  $("#importFileChooser").hidden = false;
  setImportStage("choose");
  $("#importVerificationWarnings").hidden = true;
  $("#importVerificationWarningList").innerHTML = "";
  $("#importDateGroups").hidden = true;
  $("#importGroupList").innerHTML = "";
  $("#importDateGroupsHelp").textContent = "";
  $("#backImportButton").hidden = true;
  $("#prepareImportButton").disabled = true;
  setButtonLabel("#prepareImportButton", "Review import");
  $("#importError").hidden = true;
  updateImportHistoryControls();
  renderImportModeOptions();
  setImportBusy(false);
  state.importLocation = preferredImportLocation();
  state.importLocationResults = [];
  $$('input[name="importMode"]').forEach((input) => { input.checked = true; });
  setImportStep(1);
  renderImportDrafts();
}

function renderImportModeOptions() {
  const backlog = state.importMode === "backlog";
  $("#importDialogTitle").textContent = backlog ? "ALM deployment backlog" : "ALM Workbook";
  $("#almDeployOptions").hidden = backlog;
  $("#almBacklogOptions").hidden = !backlog;
  $("#importDatePicker").hidden = backlog;
  $("#importDateSummary").hidden = !backlog;
  if (backlog) $("#importDateGroups").hidden = true;
  setButtonLabel("#prepareImportButton", backlog ? "Find undeployed devices" : "Review import");
  updateBacklogDaysLabel();
}

function updateBacklogDaysLabel() {
  const input = $("#almBacklogDaysInput");
  const label = $("#almBacklogDaysLabel");
  if (!input || !label) return;
  const rawDays = Number(input.value);
  const days = Number.isFinite(rawDays) && rawDays >= 1 ? Math.min(365, Math.round(rawDays)) : 1;
  if (input.value !== String(days)) input.value = String(days);
  label.textContent = `${days} day${days === 1 ? "" : "s"}`;
}

function setImportStep(step) {
  [1, 2, 3].forEach((number) => {
    const item = $(`#importStep${number}`);
    item.classList.toggle("active", number === step);
    item.classList.toggle("complete", number < step);
  });
}

function setImportStage(stage) {
  const stages = {
    choose: ["#importChoose"],
    mapping: ["#importChoose", "#importMapColumns"],
    options: ["#importConfigure"],
    preview: ["#importPreview"],
  };
  ["#importChoose", "#importMapColumns", "#importConfigure", "#importPreview"].forEach((selector) => {
    const node = $(selector);
    if (node) node.hidden = true;
  });
  (stages[stage] || []).forEach((selector) => {
    const node = $(selector);
    if (node) node.hidden = false;
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
  const previous = new Set(selectedImportDates());
  const today = new Date();
  const todayValue = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  const retained = dates.filter((entry) => previous.has(entry.value)).map((entry) => entry.value);
  const selected = retained.length
    ? retained
    : dates.some((entry) => entry.value === todayValue)
      ? [todayValue]
      : dates[0]?.value ? [dates[0].value] : [];
  state.importSelectedDates = selected;
  renderImportDateDialog();
  updateImportDateSummary();
  updateImportCounts();
}

function updateImportDateSummary() {
  const dates = workbookSheet($("#sheetInput").value)?.dates || [];
  const selected = selectedImportDateEntries();
  const trigger = $("#openImportDatesButton");
  const label = $("#importDateSelectionLabel");
  const detail = $("#importDateSelectionDetail");
  if (!trigger || !label || !detail) return;
  trigger.disabled = !dates.length;
  if (!selected.length) {
    label.textContent = dates.length ? "Choose dates" : "No dates available";
    detail.textContent = dates.length ? "Choose one or more dates" : "No deployment dates found";
    trigger.setAttribute("aria-label", "Choose deployment dates");
    return;
  }
  label.textContent = selected.length === 1
    ? relativeDateLabel(selected[0].value)
    : `${selected.length} dates selected`;
  detail.textContent = `${selected.length} date${selected.length === 1 ? "" : "s"} selected`;
  trigger.setAttribute(
    "aria-label",
    `Choose deployment dates. Selected: ${selected.map((entry) => entry.label).join(", ")}`,
  );
}

function updateImportDateDialogApplyButton() {
  const button = $("#applyImportDatesButton");
  if (!button) return;
  button.disabled = !$('[data-import-dialog-date]:checked');
}

function renderImportDateDialog() {
  const list = $("#importDateDialogList");
  if (!list) return;
  const dates = workbookSheet($("#sheetInput").value)?.dates || [];
  const selected = new Set(selectedImportDates());
  if (!dates.length) {
    list.innerHTML = '<div class="import-date-empty">No deployment dates were found in this sheet.</div>';
    updateImportDateDialogApplyButton();
    return;
  }
  list.innerHTML = dates.map((entry) => {
    const requests = Number(entry.deployment_count || 0)
      + Number(entry.returned_device_count || 0)
      + Number(entry.pending_return_count || 0);
    return `<label class="import-date-option">
      <input type="checkbox" data-import-dialog-date="${escapeHtml(entry.value)}" ${selected.has(entry.value) ? "checked" : ""}>
      <span><strong>${escapeHtml(entry.label)}</strong><small>${escapeHtml(relativeDateLabel(entry.value))} · ${requests} request${requests === 1 ? "" : "s"}</small></span>
    </label>`;
  }).join("");
  updateImportDateDialogApplyButton();
}

function selectedImportDates() {
  const available = new Set((workbookSheet($("#sheetInput").value)?.dates || []).map((entry) => entry.value));
  return [...new Set(state.importSelectedDates || [])].filter((value) => available.has(value));
}

function selectedImportDateEntries() {
  const selected = new Set(selectedImportDates());
  return (workbookSheet($("#sheetInput").value)?.dates || []).filter((entry) => selected.has(entry.value));
}

function setSelectedImportDates(values) {
  const available = new Set((workbookSheet($("#sheetInput").value)?.dates || []).map((entry) => entry.value));
  state.importSelectedDates = [...new Set((values || []).map((value) => String(value)))].filter((value) => available.has(value));
  renderImportDateDialog();
  updateImportDateSummary();
}

function openImportDateDialog() {
  renderImportDateDialog();
  const dialog = $("#importDatesDialog");
  if (!dialog?.open) {
    dialog.showModal();
    $("#openImportDatesButton").setAttribute("aria-expanded", "true");
  }
}

function applyImportDateSelection() {
  const selected = $$('[data-import-dialog-date]:checked')
    .map((input) => input.dataset.importDialogDate)
    .filter(Boolean);
  if (!selected.length) return;
  state.importSelectedDates = selected;
  updateImportDateSummary();
  updateImportCounts();
  refreshIcons($("#importDialog"));
  saveCurrentImportDraft();
  $("#importDatesDialog").close();
}

function selectedImportModes() {
  return $$('input[name="importMode"]:checked').map((input) => input.value);
}

function importModeValue() {
  const modes = selectedImportModes();
  return modes.length === 3 ? "all" : modes.join(",");
}

function selectedImportGroupSelections() {
  return Object.fromEntries(
    $$('[data-import-group-date]').map((select) => [select.dataset.importGroupDate, select.value || "all"]),
  );
}

function updateImportGroupChoices() {
  const selectedDates = selectedImportDateEntries();
  const wrapper = $("#importDateGroups");
  if (state.importMode === "backlog") {
    wrapper.hidden = true;
    $("#importGroupList").innerHTML = "";
    $("#importDateGroupsHelp").textContent = "";
    return;
  }
  const previous = selectedImportGroupSelections();
  const groupedDates = selectedDates.filter((entry) => (entry.groups || []).length > 1);
  if (!groupedDates.length) {
    wrapper.hidden = true;
    $("#importGroupList").innerHTML = "";
    $("#importDateGroupsHelp").textContent = "";
    return;
  }
  $("#importGroupList").innerHTML = groupedDates.map((entry) => {
    const groups = entry.groups || [];
    const selected = previous[entry.value];
    const validPrevious = selected === "all" || groups.some((group) => group.value === selected);
    return `<label class="import-date-group-row"><span>${escapeHtml(entry.label)}</span><select data-import-group-date="${escapeHtml(entry.value)}">
      <option value="">Choose a section</option>
      ${groups.map((group, index) => `<option value="${escapeHtml(group.value)}" ${validPrevious && selected === group.value ? "selected" : ""}>Section ${index + 1} · ${group.eligible_row_count ?? 0} valid-user rows</option>`).join("")}
      <option value="all" ${validPrevious && (selected === "all" || !selected) ? "selected" : ""}>All sections</option>
    </select></label>`;
  }).join("");
  wrapper.hidden = false;
  $("#importDateGroupsHelp").textContent = groupedDates.length === 1
    ? `This date appears grouped into ${groupedDates[0].groups.length} sections. Choose one section or all sections.`
    : "Some selected dates appear grouped into sections. Choose one section or all sections for each date.";
}

function updateImportCounts() {
  if (state.importMode === "backlog") {
    $("#importDateGroups").hidden = true;
    $("#importLocationFields").hidden = true;
    const prepareButton = $("#prepareImportButton");
    prepareButton.disabled = !state.workbook?.import_id;
    prepareButton.textContent = "Find undeployed devices";
    return;
  }
  updateImportGroupChoices();
  const selectedDates = selectedImportDateEntries();
  const groupSelections = selectedImportGroupSelections();
  const counts = selectedDates.map((entry) => {
    const groupValue = groupSelections[entry.value];
    const group = entry.groups?.find((item) => item.value === groupValue);
    return groupValue && groupValue !== "all" && group ? group : entry;
  });
  const deploymentCount = counts.reduce((total, item) => total + Number(item?.deployment_count || 0), 0);
  const missingUsernameDeploymentCount = counts.reduce((total, item) => total + Number(item?.missing_username_deployment_count || 0), 0);
  const returnedDeviceCount = counts.reduce((total, item) => total + Number(item?.returned_device_count || 0), 0);
  const pendingReturnCount = counts.reduce((total, item) => total + Number(item?.pending_return_count || 0), 0);
  $("#deploymentImportCount").textContent = `${deploymentCount} request${deploymentCount === 1 ? "" : "s"}${missingUsernameDeploymentCount ? ` · ${missingUsernameDeploymentCount} warning${missingUsernameDeploymentCount === 1 ? "" : "s"}` : ""}`;
  $("#returnedDeviceImportCount").textContent = `${returnedDeviceCount} request${returnedDeviceCount === 1 ? "" : "s"}`;
  $("#pendingReturnImportCount").textContent = `${pendingReturnCount} request${pendingReturnCount === 1 ? "" : "s"}`;
  const modes = selectedImportModes();
  const selectedCount = (modes.includes("deployments") ? deploymentCount + missingUsernameDeploymentCount : 0)
    + (modes.includes("returned_devices") ? returnedDeviceCount : 0)
    + (modes.includes("pending_returns") ? pendingReturnCount : 0);
  const groupRequired = selectedDates.some((entry) =>
    (entry.groups || []).length > 1 && !groupSelections[entry.value],
  );
  const needsLocation = modes.includes("returned_devices") && returnedDeviceCount > 0;
  const missingLocation = needsLocation && !hasCompleteLocation(state.importLocation);
  $("#importLocationFields").hidden = !needsLocation;
  if (needsLocation) renderImportLocationFields();
  const prepareButton = $("#prepareImportButton");
  prepareButton.disabled = !selectedDates.length || selectedCount === 0 || missingLocation || groupRequired;
  prepareButton.textContent = groupRequired
    ? "Choose a date section"
    : missingLocation ? "Choose a destination" : "Review import";
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
  $("#importStageContent").hidden = visible;
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
    setImportBusy(true, {
      percent: 28 + scanProgress * 70,
      title: status.message || "Reading ALM Workbook…",
      detail,
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
    saved.pending_return,
  ].filter(Boolean).every((heading) => headings.has(String(heading).trim().toLowerCase()));
}

function openImportColumnMapping() {
  const workbook = state.workbookInspection;
  if (!workbook) return;
  $("#importFileChooser").hidden = true;
  setImportStage("mapping");
  $("#importMapFilename").textContent = workbook.filename;
  const preferredSheet = $("#sheetInput").value || workbook.default_sheet;
  $("#importMapSheet").innerHTML = workbook.sheets.map((sheet) => (
    `<option value="${escapeHtml(sheet.name)}" ${sheet.name === preferredSheet ? "selected" : ""}>${escapeHtml(sheet.name)}</option>`
  )).join("");
  renderImportColumnMap();
  $("#backImportButton").hidden = true;
  setButtonLabel("#prepareImportButton", "Use columns");
  setImportStep(1);
}

async function showImportedWorkbook(workbook) {
  $("#importError").hidden = true;
  renderImportModeOptions();
  if (!state.importDraftId) state.importDraftId = newImportDraftId();
  if (workbook.needs_mapping) {
    state.workbook = workbook;
    state.workbookInspection = workbook;
    const saved = importColumns();
    if (workbookMappingMatches(workbook, saved)) {
      $("#importMapSheet").innerHTML = `<option value="${escapeHtml(workbook.default_sheet)}">${escapeHtml(workbook.default_sheet)}</option>`;
      renderImportColumnMap();
      try {
        await mapWorkbookColumns();
      } catch (error) {
        $("#importError").textContent = error.message;
        $("#importError").hidden = false;
      }
      return;
    }
    openImportColumnMapping();
    saveCurrentImportDraft();
    return;
  }
  state.workbook = workbook;
  if (workbook.inspection && Array.isArray(workbook.inspection.sheets)) {
    state.workbookInspection = workbook.inspection;
  }
  $("#importFileChooser").hidden = false;
  setImportStage("options");
  $("#backImportButton").hidden = true;
  setButtonLabel("#prepareImportButton", "Review import");
  renderImportModeOptions();
  setImportStep(2);
  $("#importFilename").textContent = workbook.filename;
  $("#importFileSummary").textContent = `${workbook.sheets.length} dated sheet${workbook.sheets.length === 1 ? "" : "s"}`;
  $("#sheetInput").innerHTML = workbook.sheets.map((sheet) => `<option value="${escapeHtml(sheet.name)}" ${sheet.name === workbook.default_sheet ? "selected" : ""}>${escapeHtml(sheet.name)}</option>`).join("");
  updateImportDates();
  saveCurrentImportDraft();
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
  select($("#importMapDeviceAllocation"), saved.device_allocation || "Device(s) Allocation");
  select($("#importMapNewAssetStatus"), saved.new_asset_status || "New Asset Status");
  select($("#importMapFirstName"), saved.first_name || "First Name");
  select($("#importMapLastName"), saved.last_name || "Last Name");
  updateImportColumnMapButton();
}

function selectedImportColumns() {
  return {
    username: $("#importMapUsername").value,
    deployment_serial: $("#importMapDeployment").value,
    returned_device: $("#importMapReturned").value,
    pending_return: $("#importMapPending").value,
    enabled: $("#importMapEnabled").value,
    device_allocation: $("#importMapDeviceAllocation").value,
    new_asset_status: $("#importMapNewAssetStatus").value,
    first_name: $("#importMapFirstName").value,
    last_name: $("#importMapLastName").value,
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
    validate_quick_import: validationEnabled("validate_quick_import"),
    validate_workbook_import: validationEnabled("validate_workbook_import"),
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
    throw new Error("Import the ALM Workbook again.");
  }
  $("#importError").hidden = true;
  setImportStage("choose");
  $("#importFileChooser").hidden = true;
  setImportBusy(true, { percent: 25, title: "Reading ALM Workbook…", detail: "Matching workbook columns" });
  try {
    const job = await api("/api/import/map", {
      method: "POST",
      body: JSON.stringify({
        import_id: inspection.import_id,
        columns,
      }),
    });
    const token = state.importUploadToken;
    const workbook = await waitForWorkbookImport(job.job_id, token);
    if (!workbook) return;
    if (persist) await saveImportColumnPreferences(columns);
    await showImportedWorkbook(workbook);
  } catch (error) {
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
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) {
    $("#importError").textContent = "Choose an .xlsx or .xlsm ALM Workbook.";
    $("#importError").hidden = false;
    setImportBusy(false);
    return;
  }
  if (file.size > MAX_WORKBOOK_BYTES) {
    $("#importError").textContent = "The ALM Workbook is larger than the 100 MB local limit.";
    $("#importError").hidden = false;
    setImportBusy(false);
    return;
  }
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
    if (workbook && token === state.importUploadToken) await showImportedWorkbook(workbook);
  } catch (error) {
    if (token === state.importUploadToken) {
      $("#importError").textContent = error.message;
      $("#importError").hidden = false;
    }
  } finally {
    if (token === state.importUploadToken) setImportBusy(false);
  }
}

function newImportDraftId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `draft-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function readImportDrafts() {
  return state.importDrafts.filter((draft) => draft && typeof draft === "object" && draft.id);
}

let importDraftSaveTimer = null;
let importDraftSavePending = null;
let importDraftSaveInFlight = Promise.resolve();
let importPreviewDraftSaveTimer = null;

async function loadImportDrafts() {
  const payload = await api("/api/import-drafts");
  state.importDrafts = Array.isArray(payload.drafts) ? payload.drafts : [];
  renderImportDrafts();
}

function importDraftPhase() {
  if (state.importPreview) return "review";
  if (state.workbook?.needs_mapping) return "mapping";
  return "options";
}

function importDraftPhaseLabel(phase) {
  return ({ mapping: "Choose columns", options: "Options", review: "Review" })[phase] || "Import";
}

function importDraftSettings() {
  return {
    mode: state.importMode,
    sheet: $("#sheetInput").value || "",
    dates: selectedImportDates(),
    groups: selectedImportGroupSelections(),
    modes: selectedImportModes(),
    location: state.importLocation ? JSON.parse(JSON.stringify(state.importLocation)) : null,
    columns: selectedImportColumns(),
    backlog_days: Math.max(1, Number($("#almBacklogDaysInput").value) || 30),
    backlog_include_today: $("#almBacklogIncludeToday").checked,
  };
}

function saveCurrentImportDraft({ immediate = false } = {}) {
  if (immediate && importPreviewDraftSaveTimer) {
    window.clearTimeout(importPreviewDraftSaveTimer);
    importPreviewDraftSaveTimer = null;
  }
  const workbook = state.workbook || state.workbookInspection;
  if (state.preferences.save_alm_import_drafts === false || !state.importDraftId || !workbook?.import_id) return Promise.resolve();
  const draft = {
    id: state.importDraftId,
    filename: workbook.filename || "ALM Workbook",
    import_id: workbook.import_id,
    workbook: JSON.parse(JSON.stringify(workbook)),
    phase: importDraftPhase(),
    settings: importDraftSettings(),
    preview: state.importPreview ? JSON.parse(JSON.stringify(state.importPreview)) : null,
    saved_at: new Date().toISOString(),
  };
  state.importDrafts = [draft, ...readImportDrafts().filter((item) => item.id !== draft.id)];
  renderImportDrafts();
  importDraftSavePending = draft;
  if (importDraftSaveTimer) window.clearTimeout(importDraftSaveTimer);
  if (immediate) return flushImportDraftSave({ keepalive: true });
  importDraftSaveTimer = window.setTimeout(() => {
    importDraftSaveTimer = null;
    flushImportDraftSave();
  }, 150);
  return importDraftSaveInFlight;
}

function scheduleImportDraftSave() {
  if (importPreviewDraftSaveTimer) return;
  importPreviewDraftSaveTimer = window.setTimeout(() => {
    importPreviewDraftSaveTimer = null;
    saveCurrentImportDraft();
  }, 180);
}

function flushImportDraftSave({ keepalive = false } = {}) {
  const draft = importDraftSavePending;
  importDraftSavePending = null;
  if (!draft) return importDraftSaveInFlight;
  importDraftSaveInFlight = importDraftSaveInFlight
    .catch(() => {})
    .then(async () => {
      const payload = await api("/api/import-drafts", {
        method: "POST",
        body: JSON.stringify(draft),
        keepalive,
      });
      state.importDrafts = Array.isArray(payload.drafts) ? payload.drafts : state.importDrafts;
      renderImportDrafts();
    })
    .catch((error) => {
      console.warn("Could not save ALM import draft.", error);
    });
  return importDraftSaveInFlight;
}

function deleteImportDraft(id) {
  if (!id) return Promise.resolve();
  const deletingCurrent = state.importDraftId === id;
  if (deletingCurrent) {
    if (importPreviewDraftSaveTimer) window.clearTimeout(importPreviewDraftSaveTimer);
    importPreviewDraftSaveTimer = null;
    if (importDraftSaveTimer) window.clearTimeout(importDraftSaveTimer);
    importDraftSaveTimer = null;
    // A late validation/render callback must not enqueue this draft again after
    // the requests have been moved into the queue.
    importDraftSavePending = null;
    state.importDraftId = null;
  }
  state.importDrafts = readImportDrafts().filter((draft) => draft.id !== id);
  renderImportDrafts();
  const pendingSave = deletingCurrent
    ? importDraftSaveInFlight.catch(() => {})
    : flushImportDraftSave();
  return pendingSave.then(() => api(`/api/import-drafts/${encodeURIComponent(id)}`, { method: "DELETE" }))
    .then((payload) => {
      state.importDrafts = Array.isArray(payload.drafts) ? payload.drafts : state.importDrafts;
      renderImportDrafts();
    })
    .catch((error) => toast(error.message, "error"));
}

function renderImportDrafts() {
  const wrapper = $("#importDrafts");
  const list = $("#importDraftList");
  if (!wrapper || !list) return;
  const drafts = readImportDrafts();
  wrapper.hidden = !drafts.length;
  list.innerHTML = drafts.map((draft) => {
    const timestamp = new Date(draft.saved_at || "");
    const saved = Number.isNaN(timestamp.getTime()) ? "Saved import" : `Saved ${timestamp.toLocaleString()}`;
    return `<div class="import-draft">
      <div><strong>${escapeHtml(draft.filename || "ALM Workbook")}</strong><small>${escapeHtml(importDraftPhaseLabel(draft.phase))} · ${escapeHtml(saved)}</small></div>
      <div class="import-draft-actions">
        <button class="button secondary compact" type="button" data-import-resume="${escapeHtml(draft.id)}">${iconMarkup("rotate-ccw")}<span>Resume</span></button>
        <button class="text-button" type="button" data-import-delete="${escapeHtml(draft.id)}">${iconMarkup("trash-2")}<span>Delete</span></button>
      </div>
    </div>`;
  }).join("");
  refreshIcons(list);
  list.querySelectorAll("[data-import-resume]").forEach((button) => button.addEventListener("click", () => resumeImportDraft(button.dataset.importResume)));
  list.querySelectorAll("[data-import-delete]").forEach((button) => button.addEventListener("click", () => {
    const draft = readImportDrafts().find((item) => item.id === button.dataset.importDelete);
    if (draft && confirm(`Delete the saved ALM import “${draft.filename || "ALM Workbook"}”?`)) void deleteImportDraft(draft.id);
  }));
  renderLatestImportDraft();
}

function renderLatestImportDraft() {
  const button = $("#resumeLatestImportButton");
  if (!button) return;
  const latest = readImportDrafts()[0];
  const visible = state.preferences.save_alm_import_drafts !== false && Boolean(latest);
  button.hidden = !visible;
  if (!visible) return;
  $("#resumeImportDraftTitle").textContent = `Resume ${latest.filename || "ALM import"}`;
  const timestamp = new Date(latest.saved_at || "");
  const saved = Number.isNaN(timestamp.getTime()) ? "saved import" : `saved ${timestamp.toLocaleString()}`;
  $("#resumeImportDraftDetail").textContent = `${importDraftPhaseLabel(latest.phase)} · ${saved}`;
  button.onclick = () => {
    $("#importDialog").showModal();
    resumeImportDraft(latest.id);
  };
}

function restoreImportDraftOptions(settings = {}) {
  state.importMode = settings.mode === "backlog" ? "backlog" : "deploy";
  renderImportModeOptions();
  if (settings.sheet && [...$("#sheetInput").options].some((option) => option.value === settings.sheet)) {
    $("#sheetInput").value = settings.sheet;
  }
  updateImportDates();
  const savedDates = Array.isArray(settings.dates)
    ? settings.dates
    : settings.date ? [settings.date] : [];
  if (savedDates.length) setSelectedImportDates(savedDates);
  if (Array.isArray(settings.modes) && settings.modes.length) {
    $$('input[name="importMode"]').forEach((input) => { input.checked = settings.modes.includes(input.value); });
  }
  if (settings.location) state.importLocation = JSON.parse(JSON.stringify(settings.location));
  if (settings.backlog_days != null) {
    const savedDays = Number(settings.backlog_days);
    $("#almBacklogDaysInput").value = String(Number.isFinite(savedDays) ? Math.max(1, Math.min(365, savedDays)) : 30);
  }
  $("#almBacklogIncludeToday").checked = Boolean(settings.backlog_include_today);
  updateBacklogDaysLabel();
  updateImportCounts();
  Object.entries(settings.groups || (settings.group ? { [savedDates[0] || ""]: settings.group } : {})).forEach(([date, group]) => {
    const select = $$('[data-import-group-date]').find((item) => item.dataset.importGroupDate === date);
    if (select && [...select.options].some((option) => option.value === group)) select.value = group;
  });
  updateImportCounts();
}

function restoreImportDraftMapping(settings = {}) {
  const selectors = {
    username: "#importMapUsername",
    deployment_serial: "#importMapDeployment",
    returned_device: "#importMapReturned",
    pending_return: "#importMapPending",
    enabled: "#importMapEnabled",
    device_allocation: "#importMapDeviceAllocation",
    new_asset_status: "#importMapNewAssetStatus",
    first_name: "#importMapFirstName",
    last_name: "#importMapLastName",
  };
  Object.entries(selectors).forEach(([key, selector]) => {
    const element = $(selector);
    const value = settings.columns?.[key] || "";
    if ([...element.options].some((option) => option.value === value)) element.value = value;
  });
  updateImportColumnMapButton();
}

function restoreImportPreviewState(rawPreview) {
  const preview = rawPreview && typeof rawPreview === "object"
    ? rawPreview
    : null;
  if (!preview) return null;
  if (!Array.isArray(preview.requests)) {
    const candidates = Array.isArray(preview.candidates) ? preview.candidates : [];
    preview.requests = candidates.map((candidate) => ({
      ...candidate,
      import_validation: candidate.included === false ? "idle" : "pending",
      import_error: "",
      user_info: null,
    }));
  }
  preview.requests.forEach((request) => {
    if (request.included === undefined) request.included = request.default_excluded !== true;
    if (request.backlog_ignored === undefined) request.backlog_ignored = false;
    if (request.included === false) {
      request.import_validation = "idle";
    } else if (request.import_validation === "checking"
      || request.cached_serial_verification
      || request.cached_user_verification) {
      // A cached result is usable immediately, but a spinner from a previous
      // browser session cannot be left running forever. Re-enter validation
      // so the cache can be used again and refreshed in the background.
      request.import_validation = "pending";
    }
    request.cached_serial_verification = false;
    request.cached_user_verification = false;
  });
  return preview;
}

function resumeImportDraft(id) {
  const draft = readImportDrafts().find((item) => item.id === id);
  if (!draft?.workbook?.import_id) return;
  resetImportDialog(draft.settings?.mode === "backlog" ? "backlog" : "deploy");
  state.importDraftId = draft.id;
  state.workbook = JSON.parse(JSON.stringify(draft.workbook));
  state.workbookInspection = state.workbook.needs_mapping
    ? state.workbook
    : (state.workbook.inspection || null);
  state.importLocation = draft.settings?.location ? JSON.parse(JSON.stringify(draft.settings.location)) : preferredImportLocation();
  if (draft.phase === "mapping" || draft.workbook.needs_mapping) {
    openImportColumnMapping();
    restoreImportDraftMapping(draft.settings);
    return;
  }
  showImportedWorkbook(state.workbook);
  restoreImportDraftOptions(draft.settings);
  if (!draft.preview) {
    saveCurrentImportDraft();
    return;
  }
  state.importPreview = restoreImportPreviewState(JSON.parse(JSON.stringify(draft.preview)));
  if (!state.importPreview) {
    saveCurrentImportDraft();
    return;
  }
  setImportStage("preview");
  $("#backImportButton").hidden = false;
  setImportStep(3);
  renderImportPreview();
  updateImportPrepareButton(state.importPreview);
  const pending = state.importPreview.requests.some((request) => request.included !== false && !["valid", "failed"].includes(request.import_validation));
  if (pending) {
    if (state.importPreview.mode === "backlog") void validateBacklogPreview(state.importPreview);
    else void validateImportPreview();
  }
}

function importFailedFields(request) {
  const userImport = request.kind === "user"
    || (!request.kind && String(request.username || "").trim());
  const defaults = ["serial", ...(userImport ? ["username"] : [])];
  const fields = Array.isArray(request.import_failed_fields) && request.import_failed_fields.length
    ? request.import_failed_fields
    : defaults;
  return fields.filter((field) => field === "serial" || (field === "username" && userImport));
}

function importValidationStatus(request) {
  const descriptor = importValidationDescriptor(request);
  if (!descriptor.state) return "";
  return verificationStatusMarkup(
    descriptor.state,
    descriptor.message,
    ` data-import-validation-id="${escapeHtml(request.id)}" data-validation-state="${descriptor.state}"`,
  );
}

function verificationStatusMarkup(stateName, message, attributes = "") {
  const indicator = verificationStatusIndicatorMarkup(stateName);
  return `<span class="import-validation-status import-check-${stateName}"${attributes}><span class="import-validation-indicator">${indicator}</span><span data-import-validation-copy>${escapeHtml(message)}</span></span>`;
}

function verificationStatusIndicatorMarkup(stateName) {
  return stateName === "checking"
    ? '<span class="import-status-spinner" aria-hidden="true"></span>'
    : iconMarkup(stateName === "valid" ? "circle-check" : "circle-alert");
}

function importValidationDescriptor(request) {
  const cachedFields = importCachedVerificationFields(request);
  if (request.import_validation === "checking") {
    return { state: "checking", message: "Verifying serial in Helix…" };
  }
  if (request.import_validation === "failed") {
    return { state: "failed", message: request.import_error || "Could not verify this row" };
  }
  if (cachedFields.includes("serial")) {
    return { state: "checking", message: cachedVerificationMessage("serial") };
  }
  return request.import_validation === "valid"
    ? { state: "valid", message: "Serial verified" }
    : { state: "idle", message: "" };
}

function patchImportValidationStatusNode(node, request) {
  const descriptor = importValidationDescriptor(request);
  const currentState = node.dataset.validationState || "idle";
  node.className = `import-validation-status import-check-${descriptor.state}`;
  node.dataset.validationState = descriptor.state;
  node.hidden = !descriptor.state || descriptor.state === "idle";
  if (!descriptor.state || descriptor.state === "idle") {
    node.replaceChildren();
    return;
  }
  const copy = node.querySelector("[data-import-validation-copy]");
  if (currentState === descriptor.state && copy) {
    if (copy.textContent !== descriptor.message) copy.textContent = descriptor.message;
    return;
  }
  node.innerHTML = `<span class="import-validation-indicator">${verificationStatusIndicatorMarkup(descriptor.state)}</span><span data-import-validation-copy>${escapeHtml(descriptor.message)}</span>`;
  refreshIcons(node);
}

function patchImportPreviewValidation(payload) {
  // Validation is a high-frequency update. Keep the review rows and their
  // animated indicators alive while only changing the status copy/icon.
  const list = $("#importPreviewList");
  if (!payload || !list) return;
  const rows = new Map([...list.querySelectorAll("[data-import-row-id]")].map((row) => [row.dataset.importRowId, row]));
  const statuses = new Map([...list.querySelectorAll("[data-import-validation-id]")].map((node) => [node.dataset.importValidationId, node]));
  const requestsById = new Map((payload.requests || []).map((request) => [String(request.id), request]));
  let needsFullRender = false;
  rows.forEach((_, id) => {
    const request = requestsById.get(id);
    if (!request) {
      needsFullRender = true;
      return;
    }
    if (request.included === false) return;
    const descriptor = importValidationDescriptor(request);
    const node = statuses.get(id);
    if (!node) {
      if (descriptor.state && descriptor.state !== "idle") needsFullRender = true;
      return;
    }
    patchImportValidationStatusNode(node, request);
  });
  if (needsFullRender) {
    renderImportPreview();
    return;
  }
  if (payload.mode !== "backlog") renderImportVerificationWarnings(payload);
  updateImportPrepareButton(payload);
  scheduleImportDraftSave();
}

function renderImportVerificationWarnings(payload = state.importPreview) {
  const warning = $("#importVerificationWarnings");
  if (!payload || !warning) return;
  const selected = payload.requests.filter((request) => request.included !== false);
  const checking = selected.some((request) => !["valid", "failed"].includes(request.import_validation));
  const failed = selected.filter((request) => request.import_validation === "failed");
  if (checking || !failed.length) {
    warning.hidden = true;
    $("#importVerificationWarningList").innerHTML = "";
    return;
  }
  $("#importVerificationWarningList").innerHTML = failed.map((request) => {
    const affected = importFailedFields(request).map((field) => field === "serial" ? "serial" : "username");
    const detail = affected.length ? ` (${affected.join(" and ")})` : "";
    const user = request.kind === "user" ? request.user || "No username" : "";
    const identity = user ? ` · ${escapeHtml(user)}` : "";
    return `<li><strong>${escapeHtml(request.serials[0] || "No serial")}</strong>${identity}${escapeHtml(detail)}: ${escapeHtml(request.import_error || "Could not verify in Helix.")}</li>`;
  }).join("");
  warning.hidden = false;
}

function updateImportPrepareButton(payload = state.importPreview) {
  if (!payload) return;
  if (payload.mode === "backlog") {
    const selected = payload.requests.filter((request) => request.included !== false);
    const checking = selected.some((request) => !["valid", "failed"].includes(request.import_validation));
    const verifiedCount = selected.filter((request) => ["valid", "failed"].includes(request.import_validation)).length;
    const missingStatus = selected.some((request) => !request.status);
    const invalid = selected.some((request) => request.import_validation !== "valid");
    const button = $("#prepareImportButton");
    button.disabled = !selected.length || checking || missingStatus || invalid;
    setButtonLabel(button, checking
      ? `Verifying ${verifiedCount}/${selected.length}…`
      : missingStatus
        ? "Choose deployment statuses"
        : invalid
          ? "Fix invalid rows or exclude them"
          : selected.length
            ? `Add ${selected.length} to queue`
            : "Select rows to add");
    return;
  }
  const selected = payload.requests.filter((request) => request.included !== false);
  const checking = selected.some((request) => request.import_validation === "checking" || !request.import_validation);
  const verifiedCount = selected.filter((request) => ["valid", "failed"].includes(request.import_validation)).length;
  const missingStatus = selected.some((request) => ALM_IMPORT_STATUS_OPTIONS[request.group] && !request.status);
  const missingLocation = selected.some((request) => request.group === "Returned devices")
    && !hasCompleteLocation(state.importLocation);
  const invalid = selected.some((request) => request.import_validation !== "valid");
  const valid = selected.filter((request) => request.import_validation === "valid").length;
  const button = $("#prepareImportButton");
  button.disabled = !selected.length || checking || missingStatus || missingLocation || invalid;
  setButtonLabel(button, checking
    ? `Verifying ${verifiedCount}/${selected.length}…`
    : missingStatus
      ? "Choose import statuses"
      : missingLocation
        ? "Choose a destination"
        : invalid
          ? "Fix invalid rows or exclude them"
          : selected.length
            ? `Add ${valid} to queue`
            : "Select rows to add");
}

function importPersonMarkup(request) {
  const username = String(request.username || request.user || "").trim();
  const workbookName = [request.first_name, request.last_name]
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .join(" ");
  const verifiedPerson = confirmedPersonLabel(username, request.user_info);
  const verifiedName = verifiedPerson.login ? verifiedPerson.fullName : "";
  const fullName = workbookName || verifiedName;
  return `<div><small class="import-field-title">User</small>${fullName ? `<strong class="import-person-name">${escapeHtml(fullName)}</strong>` : ""}${username ? `<small class="import-person-username">${escapeHtml(username)}</small>` : `<small class="import-person-username">No username</small>`}</div>`;
}

const IMPORT_HISTORY_FIELDS = [
  "status",
  "included",
  "default_excluded",
  "serial",
  "serials",
  "username",
  "user",
  "returning_user",
  "returning_user_info",
  "user_info",
  "import_validation",
  "import_error",
  "import_failed_fields",
  "import_validation_epoch",
  "backlog_validation_epoch",
  "serial_validation",
  "serial_validation_error",
  "user_validation",
  "user_validation_error",
  "cached_serial_verification",
  "cached_user_verification",
  "backlog_ignored",
];

function cloneImportHistoryValue(value) {
  return value === undefined ? undefined : structuredClone(value);
}

function importReviewSnapshot(payload = state.importPreview) {
  if (!payload) return null;
  return {
    requests: (payload.requests || []).map((request) => ({
      id: request.id,
      values: Object.fromEntries(
        IMPORT_HISTORY_FIELDS
          .filter((field) => Object.prototype.hasOwnProperty.call(request, field))
          .map((field) => [field, cloneImportHistoryValue(request[field])]),
      ),
    })),
  };
}

function updateImportHistoryControls() {
  const undo = $("#undoImportButton");
  const redo = $("#redoImportButton");
  const clear = $("#clearImportStatusesButton");
  if (undo) undo.disabled = !state.importUndoStack.length;
  if (redo) redo.disabled = !state.importRedoStack.length;
  if (clear) clear.disabled = !state.importPreview
    || !state.importPreview.requests?.some((request) => request.status && ALM_IMPORT_STATUS_OPTIONS[request.group]);
}

function recordImportEdit() {
  const snapshot = importReviewSnapshot();
  if (!snapshot) return;
  state.importUndoStack.push(snapshot);
  if (state.importUndoStack.length > 100) state.importUndoStack.shift();
  state.importRedoStack = [];
  updateImportHistoryControls();
}

function restoreImportReviewSnapshot(snapshot) {
  const payload = state.importPreview;
  if (!payload || !snapshot) return;
  const valuesById = new Map((snapshot.requests || []).map((item) => [item.id, item.values || {}]));
  (payload.requests || []).forEach((request) => {
    const values = valuesById.get(request.id);
    if (!values) return;
    const wasBacklogIgnored = request.backlog_ignored === true;
    IMPORT_HISTORY_FIELDS.forEach((field) => {
      if (Object.prototype.hasOwnProperty.call(values, field)) request[field] = cloneImportHistoryValue(values[field]);
      else delete request[field];
    });
    if (payload.mode === "backlog" && wasBacklogIgnored !== (request.backlog_ignored === true)) {
      persistBacklogIgnore(request, request.backlog_ignored === true);
    }
    request.import_validation_epoch = Number(request.import_validation_epoch || 0) + 1;
    request.backlog_validation_epoch = Number(request.backlog_validation_epoch || 0) + 1;
    if (request.import_validation === "checking") request.import_validation = "pending";
    if (request.included === false) request.import_validation = "idle";
  });
  state.backlogValidationIds.clear();
  renderImportPreview();
  updateImportPrepareButton(payload);
  updateImportHistoryControls();
  saveCurrentImportDraft();
  const pending = (payload.requests || []).filter((request) => request.included !== false
    && !["valid", "failed"].includes(request.import_validation));
  if (pending.length) {
    if (payload.mode === "backlog") void validateBacklogPreview(payload, pending);
    else void validateImportPreview(pending);
  }
}

function undoImportEdit() {
  if (!state.importPreview || !state.importUndoStack.length) return;
  state.importRedoStack.push(importReviewSnapshot());
  restoreImportReviewSnapshot(state.importUndoStack.pop());
}

function redoImportEdit() {
  if (!state.importPreview || !state.importRedoStack.length) return;
  state.importUndoStack.push(importReviewSnapshot());
  restoreImportReviewSnapshot(state.importRedoStack.pop());
}

function clearImportStatuses() {
  const payload = state.importPreview;
  if (!payload) return;
  const requests = (payload.requests || []).filter((request) =>
    request.status && ALM_IMPORT_STATUS_OPTIONS[request.group]);
  if (!requests.length) return;
  recordImportEdit();
  requests.forEach((request) => { request.status = ""; });
  renderImportPreview();
  updateImportPrepareButton(payload);
  saveCurrentImportDraft();
}

function persistBacklogIgnore(request, ignored) {
  const serial = String(request.serial || "").trim();
  const username = String(request.username || "").trim();
  if (!serial || !username) return;
  const requestOptions = ignored
    ? {
        method: "POST",
        body: JSON.stringify({ serial, username }),
      }
    : { method: "DELETE" };
  const path = ignored
    ? "/api/import/backlog/ignore"
    : `/api/import/backlog/ignore?serial=${encodeURIComponent(serial)}&username=${encodeURIComponent(username)}`;
  void api(path, requestOptions).catch((error) => toast(error.message, "error"));
}

function renderBacklogPreview(payload) {
  const requests = Array.isArray(payload.requests) ? payload.requests : (payload.requests = []);
  const included = requests.filter((request) => request.included !== false);
  $("#importVerificationWarnings").hidden = true;
  $("#importPreviewTitle").textContent = `${included.length} undeployed device${included.length === 1 ? "" : "s"}`;
  $("#importPreviewSubtitle").textContent = `${payload.sheet} · ${payload.start_date} to ${payload.end_date}${payload.include_today ? " · including today" : ""}`;
  $("#importPreviewCount").textContent = `${included.length} selected`;
  const rows = requests.map((request, index) => {
    const options = ALM_IMPORT_STATUS_OPTIONS.Deployments;
    const notAttending = request.attending === false;
    const usernameOccurrence = Number(request.username_occurrence || 0);
    const usernameOccurrenceTotal = Number(request.username_occurrence_total || 0);
    const occurrenceSuffix = usernameOccurrence % 100 >= 11 && usernameOccurrence % 100 <= 13
      ? "th"
      : ({ 1: "st", 2: "nd", 3: "rd" }[usernameOccurrence % 10] || "th");
    const occurrenceLabel = usernameOccurrence > 1
      ? `${usernameOccurrence}${occurrenceSuffix} occurrence in this sheet${usernameOccurrenceTotal > usernameOccurrence ? ` of ${usernameOccurrenceTotal}` : ""}`
      : "";
    const exclusionLabel = request.default_excluded === true
      ? "Did not attend"
      : "Ignored in future backlog checks";
    const statusControl = `<div class="import-status-control">
      <select data-backlog-status="${escapeHtml(request.id)}" aria-label="Deployment status for ${escapeHtml(request.serial)}">
        <option value="">Choose a deployment status</option>
        ${options.map((option) => `<option value="${escapeHtml(option.value)}" ${request.status === option.value ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}
      </select>
    </div>`;
    const validation = importValidationStatus(request);
    const includedRow = request.included !== false;
    return `<div class="import-preview-row ${includedRow ? "" : "excluded"}${notAttending ? " not-attending" : ""}" data-import-row-id="${escapeHtml(request.id)}">
      <label class="include-control" title="${includedRow ? "Included" : exclusionLabel}">
        <input type="checkbox" data-backlog-include="${escapeHtml(request.id)}" ${includedRow ? "checked" : ""}>
        <span>${index + 1}</span>
      </label>
      <div><small class="import-field-title">Deployment serial</small><strong>${escapeHtml(request.serial)}</strong><small class="import-device-allocation">${escapeHtml(request.date)}${request.device_allocation ? ` · ${escapeHtml(request.device_allocation)}` : ""}</small></div>
      ${importPersonMarkup(request).replace("</div>", `<small class="import-device-allocation">Current: ${escapeHtml(request.current_status)}</small>${occurrenceLabel ? `<small class="import-duplicate-warning">${escapeHtml(occurrenceLabel)}</small>` : ""}${notAttending ? '<small class="import-attendance-warning">Did not attend</small>' : ""}</div>`)}
      <div>${statusControl}${includedRow ? validation : `<small class="${notAttending ? "import-attendance-warning" : ""}">${escapeHtml(exclusionLabel)}</small>`}<div class="backlog-row-actions"><button class="text-button" type="button" data-backlog-ignore="${escapeHtml(request.id)}">${iconMarkup("eye-off")}<span>Ignore in future</span></button></div></div>
    </div>`;
  }).join("");
  const counts = payload.counts || {};
  const emptyMessage = Number(counts.already_deployed || 0) > 0
    ? "Every matching row is already marked Deployed."
    : Number(counts.today_excluded || 0) > 0 && !payload.include_today
      ? "No prior days matched. Include today to check today's rows."
      : "No undeployed devices were found in this range.";
  const allIncluded = requests.length > 0 && included.length === requests.length;
  $("#importPreviewList").innerHTML = rows
    ? `<section class="import-preview-section"><div class="import-group-heading"><div><strong>Undeployed devices</strong><small>${included.length} of ${requests.length} selected</small></div><div class="import-group-actions"><button class="text-button" type="button" data-backlog-toggle>${iconMarkup(allIncluded ? "square-minus" : "list-checks")}<span>${allIncluded ? "Deselect all deployments" : "Select all deployments"}</span></button></div></div>${rows}</section>`
    : `<div class="import-empty">${iconMarkup("search-x")}<strong>${escapeHtml(emptyMessage)}</strong><small>Try a wider date range or include today.</small></div>`;
  $("#importPreviewList").querySelectorAll("[data-backlog-status]").forEach((select) => select.addEventListener("change", () => {
    const request = requests.find((item) => item.id === select.dataset.backlogStatus);
    if (request && request.status !== select.value) {
      recordImportEdit();
      request.status = select.value;
    }
    updateImportPrepareButton(payload);
    saveCurrentImportDraft();
  }));
  $("#importPreviewList").querySelectorAll("[data-backlog-include]").forEach((checkbox) => checkbox.addEventListener("change", () => {
    const request = requests.find((item) => item.id === checkbox.dataset.backlogInclude);
    if (!request) return;
    if (request.included !== checkbox.checked) recordImportEdit();
    request.included = checkbox.checked;
    request.default_excluded = false;
    const wasIgnored = request.backlog_ignored === true;
    request.backlog_ignored = !checkbox.checked;
    request.backlog_validation_epoch = Number(request.backlog_validation_epoch || 0) + 1;
    const shouldValidate = checkbox.checked && request.import_validation !== "valid";
    if (!checkbox.checked) {
      state.backlogValidationIds.delete(request.id);
      request.import_validation = "idle";
      request.import_error = "";
      request.import_failed_fields = [];
      request.cached_serial_verification = false;
      request.cached_user_verification = false;
    }
    if (!checkbox.checked) {
      persistBacklogIgnore(request, true);
    } else if (wasIgnored) {
      persistBacklogIgnore(request, false);
    }
    renderImportPreview();
    updateImportPrepareButton(payload);
    saveCurrentImportDraft();
    if (shouldValidate) void validateBacklogPreview(payload, [request]);
  }));
  $("#importPreviewList").querySelectorAll("[data-backlog-ignore]").forEach((button) => button.addEventListener("click", () => {
    const request = requests.find((item) => item.id === button.dataset.backlogIgnore);
    if (!request) return;
    if (request.included !== false) recordImportEdit();
    request.included = false;
    request.default_excluded = false;
    request.backlog_ignored = true;
    request.backlog_validation_epoch = Number(request.backlog_validation_epoch || 0) + 1;
    state.backlogValidationIds.delete(request.id);
    request.import_validation = "idle";
    request.import_error = "";
    request.import_failed_fields = [];
    request.cached_serial_verification = false;
    request.cached_user_verification = false;
    persistBacklogIgnore(request, true);
    renderImportPreview();
    updateImportPrepareButton(payload);
    saveCurrentImportDraft();
  }));
  $("#importPreviewList").querySelectorAll("[data-backlog-toggle]").forEach((button) => button.addEventListener("click", () => {
    const include = requests.some((request) => request.included === false);
    const toValidate = [];
    if (!requests.length) return;
    recordImportEdit();
    requests.forEach((request) => {
      const wasIgnored = request.backlog_ignored === true;
      request.included = include;
      request.default_excluded = false;
      request.backlog_ignored = !include;
      request.backlog_validation_epoch = Number(request.backlog_validation_epoch || 0) + 1;
      state.backlogValidationIds.delete(request.id);
      if (include) {
        if (request.import_validation !== "valid") toValidate.push(request);
      } else {
        request.import_validation = "idle";
        request.import_error = "";
        request.import_failed_fields = [];
        request.cached_serial_verification = false;
        request.cached_user_verification = false;
      }
      if (!include || wasIgnored) persistBacklogIgnore(request, !include);
    });
    renderImportPreview();
    updateImportPrepareButton(payload);
    saveCurrentImportDraft();
    if (toValidate.length) void validateBacklogPreview(payload, toValidate);
  }));
  refreshIcons($("#importPreviewList"));
  saveCurrentImportDraft();
}

function renderImportPreview() {
  const payload = state.importPreview;
  if (!payload) return;
  updateImportHistoryControls();
  if (payload.mode === "backlog") {
    renderBacklogPreview(payload);
    return;
  }
  renderImportVerificationWarnings(payload);
  const included = payload.requests.filter((request) => request.included !== false);
  const deploymentCount = included.filter((request) => request.group === "Deployments").length;
  const returnedDeviceCount = included.filter((request) => request.group === "Returned devices").length;
  const pendingReturnCount = included.filter((request) => request.group === "Pending returns").length;
  $("#importPreviewTitle").textContent = `${deploymentCount} deployments · ${returnedDeviceCount} returned devices · ${pendingReturnCount} pending returns`;
  const selectedDateLabels = selectedImportDateEntries().map((entry) => entry.label);
  $("#importPreviewSubtitle").textContent = `${$("#sheetInput").value} · ${selectedDateLabels.join(", ")}`;
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
    const missingUsernameWarnings = group.key === "Deployments"
      ? (Array.isArray(payload.warnings?.missing_username_deployments) ? payload.warnings.missing_username_deployments : [])
      : [];
    if (!requests.length && !missingUsernameWarnings.length) return "";
    const selectedCount = requests.filter((request) => request.included !== false).length;
    const expanded = state.importExpandedGroups.has(group.key);
    const visibleRequests = expanded ? requests : requests.slice(0, IMPORT_PREVIEW_ROW_LIMIT);
    const rows = visibleRequests.map((request, index) => {
      const isDeployment = request.group === "Deployments";
      const isReturnedDevice = request.group === "Returned devices";
      const isIncluded = request.included !== false;
      const statusOptions = ALM_IMPORT_STATUS_OPTIONS[request.group] || [];
      const statusControl = statusOptions.length
        ? `<div class="import-status-control">
            <select data-import-status="${escapeHtml(request.id)}" aria-label="Status for ${escapeHtml(request.serials[0])}">
              <option value="" ${!request.status ? "selected" : ""}>Choose a ${isDeployment ? "deployment" : "returned-device"} status</option>
              ${statusOptions.map((option) => `<option value="${escapeHtml(option.value)}" ${request.status === option.value ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}
            </select>
            </div>`
        : `<span class="fixed-status">Deployed - Pending Return</span>`;
      const validation = importValidationStatus(request);
      const failedFields = importFailedFields(request);
      const editable = request.import_validation === "failed" && failedFields.length ? `<div class="import-inline-edit">
        ${failedFields.includes("serial") ? `<input data-import-serial="${escapeHtml(request.id)}" value="${escapeHtml(request.serials[0])}" aria-label="Serial number" placeholder="Correct serial">` : ""}
        ${failedFields.includes("username") ? `<input data-import-user="${escapeHtml(request.id)}" value="${escapeHtml(request.user)}" aria-label="Username" placeholder="Correct username">` : ""}
        <button class="text-button" data-import-retry="${escapeHtml(request.id)}" type="button">${iconMarkup("rotate-ccw")}<span>Retry</span></button>
      </div>` : "";
      const destination = locationDisplay(state.importLocation) || "Location stock";
      const missingReturnWarning = isDeployment
        && !request.has_returned_device_serial
        && !request.has_pending_return_serial
        ? request.new_joiner
          ? '<small class="import-new-joiner">New joiner</small>'
          : '<small class="import-return-warning">User has no return or pending return</small>'
        : "";
      const personColumn = isReturnedDevice
        ? `<div><small class="import-field-title">Destination</small><strong>${escapeHtml(destination)}</strong></div>`
        : `${importPersonMarkup(request).replace("</div>", `${missingReturnWarning}</div>`)}`;
      return `<div class="import-preview-row ${isIncluded ? "" : "excluded"}" data-import-row-id="${escapeHtml(request.id)}">
        <label class="include-control" title="${isIncluded ? "Included" : "Do not deploy"}">
          <input type="checkbox" data-import-include="${escapeHtml(request.id)}" ${isIncluded ? "checked" : ""}>
          <span>${index + 1}</span>
        </label>
        <div><small class="import-field-title">${isDeployment ? "Deployment serial" : isReturnedDevice ? "Returned device" : "Pending return"}</small><strong>${escapeHtml(request.serials[0])}</strong>${request.device_allocation ? `<small class="import-device-allocation">${escapeHtml(request.device_allocation)}</small>` : ""}${isDeployment && request.new_asset_status ? `<small class="import-device-status">New asset status: ${escapeHtml(request.new_asset_status)}</small>` : ""}</div>
        ${personColumn}
        <div>${statusControl}${isIncluded ? validation : "<small>Do not deploy</small>"}${editable}</div>
      </div>`;
    }).join("");
    const missingUsernameWarning = missingUsernameWarnings.length
      ? `<div class="import-data-warning" role="status"><div class="import-data-warning-heading">${iconMarkup("user-round-x")}<strong>Deployment serial${missingUsernameWarnings.length === 1 ? "" : "s"} without a username</strong></div><small>These rows have no username in the Username column.</small><ul>${missingUsernameWarnings.map((warning) => `<li>${escapeHtml(warning.serial)} · row ${escapeHtml(warning.row_number)} · ${escapeHtml(warning.date)}</li>`).join("")}</ul></div>`
      : "";
    const bulkStatusOptions = ALM_IMPORT_STATUS_OPTIONS[group.key] || [];
    const bulkStatusControl = bulkStatusOptions.length
      ? `<select data-import-status-all="${escapeHtml(group.key)}" aria-label="Set status for all ${escapeHtml(group.key.toLowerCase())}">
          <option value="">Set all unselected statuses</option>
          ${bulkStatusOptions.map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`).join("")}
        </select>`
      : "";
    const groupActions = requests.length
      ? `${bulkStatusControl}<button class="text-button" type="button" data-import-group-toggle="${escapeHtml(group.key)}">${iconMarkup(selectedCount === requests.length ? "square-minus" : "list-checks")}<span>${selectedCount === requests.length ? "Deselect" : "Select"} all ${group.key === "Deployments" ? "deployments" : group.key === "Returned devices" ? "returned devices" : "pending returns"}</span></button>`
      : "";
    return `<section class="import-preview-section">
      <div class="import-group-heading">
        <div><strong>${group.title}</strong><small>${group.detail} · ${selectedCount} of ${requests.length} selected</small></div>
        ${groupActions ? `<div class="import-group-actions">${groupActions}</div>` : ""}
      </div>
      ${missingUsernameWarning}
      ${rows}
      ${visibleRequests.length < requests.length ? `<button class="import-show-more" type="button" data-import-expand="${escapeHtml(group.key)}">${iconMarkup("chevron-down")}<span>Show ${requests.length - visibleRequests.length} more</span></button>` : ""}
    </section>`;
  }).join("");
  $("#importPreviewList").querySelectorAll("[data-import-status]").forEach((select) => {
    select.addEventListener("change", () => {
      const request = payload.requests.find((item) => item.id === select.dataset.importStatus);
      if (request && request.status !== select.value) {
        recordImportEdit();
        request.status = select.value;
      }
      updateImportPrepareButton(payload);
      saveCurrentImportDraft();
    });
  });
  $("#importPreviewList").querySelectorAll("[data-import-status-all]").forEach((select) => select.addEventListener("change", () => {
    const status = select.value;
    if (!status) return;
    const requests = payload.requests.filter((request) =>
      request.group === select.dataset.importStatusAll
      && request.included !== false
      && !request.status
    );
    if (!requests.length) return;
    recordImportEdit();
    requests.forEach((request) => { request.status = status; });
    renderImportPreview();
    updateImportPrepareButton(payload);
    saveCurrentImportDraft();
  }));
  $("#importPreviewList").querySelectorAll("[data-import-include]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const request = payload.requests.find((item) => item.id === checkbox.dataset.importInclude);
      if (!request) return;
      if (request.included !== checkbox.checked) recordImportEdit();
      request.included = checkbox.checked;
      request.import_validation_epoch = Number(request.import_validation_epoch || 0) + 1;
      if (!checkbox.checked) {
        request.import_validation = "idle";
        request.import_error = "";
        request.import_failed_fields = [];
        request.cached_serial_verification = false;
        request.cached_user_verification = false;
        request.user_info = null;
        request.returning_user_info = null;
      }
      renderImportPreview();
      updateImportPrepareButton(payload);
      saveCurrentImportDraft();
      if (checkbox.checked && request.import_validation !== "valid") void validateImportPreview([request]);
    });
  });
  $("#importPreviewList").querySelectorAll("[data-import-group-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const groupRequests = payload.requests.filter((request) => request.group === button.dataset.importGroupToggle);
      const included = groupRequests.some((request) => request.included === false);
      if (!groupRequests.length) return;
      recordImportEdit();
      const toValidate = [];
      payload.requests
        .filter((request) => request.group === button.dataset.importGroupToggle)
        .forEach((request) => {
          request.included = included;
          request.import_validation_epoch = Number(request.import_validation_epoch || 0) + 1;
          if (included) {
            if (request.import_validation !== "valid") toValidate.push(request);
          } else {
            request.import_validation = "idle";
            request.import_error = "";
            request.import_failed_fields = [];
            request.cached_serial_verification = false;
            request.cached_user_verification = false;
            request.user_info = null;
            request.returning_user_info = null;
          }
        });
      renderImportPreview();
      updateImportPrepareButton(payload);
      saveCurrentImportDraft();
      if (toValidate.length) void validateImportPreview(toValidate);
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
    recordImportEdit();
    const serial = $(`[data-import-serial="${button.dataset.importRetry}"]`);
    const user = $(`[data-import-user="${button.dataset.importRetry}"]`);
    if (serial) request.serials = [serial.value.trim()];
    if (user) {
      if (request.kind === "location") {
        request.returning_user = user.value.trim();
        request.returning_user_info = null;
      } else request.user = user.value.trim();
    }
    request.cached_serial_verification = false;
    request.cached_user_verification = false;
    request.user_info = null;
    request.returning_user_info = null;
    void validateImportPreview([request]);
  }));
  refreshIcons($("#importPreviewList"));
  saveCurrentImportDraft();
}

function scheduleImportPreviewUpdate(payload = state.importPreview, { full = false } = {}) {
  // Coalesce result bursts into one frame without rebuilding the whole review
  // list for every serial that finishes.
  if (!payload || state.importPreview !== payload) return;
  state.importPreviewNeedsFullRender ||= full;
  if (state.importPreviewRenderFrame) return;
  state.importPreviewRenderFrame = window.requestAnimationFrame(() => {
    state.importPreviewRenderFrame = null;
    if (!payload || state.importPreview !== payload) {
      state.importPreviewNeedsFullRender = false;
      return;
    }
    const renderFull = state.importPreviewNeedsFullRender;
    state.importPreviewNeedsFullRender = false;
    if (renderFull) renderImportPreview();
    else patchImportPreviewValidation(payload);
  });
}

function importValidationIsCurrent(payload, request, epoch) {
  return state.importPreview === payload
    && request.included !== false
    && request.import_validation_epoch === epoch;
}

function addImportFailedField(request, field) {
  request.import_failed_fields = [...new Set([...(request.import_failed_fields || []), field])];
}

function resumePendingImportValidation() {
  const payload = state.importPreview;
  if (!payload || payload.mode === "backlog" || !connectionIsReady()) return;
  const requests = (payload.requests || []).filter((request) => request.included !== false
    && (!['valid', 'failed'].includes(request.import_validation)
      || request.cached_serial_verification
      || request.cached_user_verification));
  if (requests.length) void validateImportPreview(requests);
}

async function validateImportPreview(retryRequests = null) {
  const payload = state.importPreview;
  if (!payload) return;
  const requests = (retryRequests || payload.requests || []).filter((request) => request.included !== false);
  if (!requests.length) {
    renderImportPreview();
    updateImportPrepareButton(payload);
    return;
  }
  if (!connectionIsReady()) {
    requests.forEach((request) => {
      if (request.import_validation === "checking") request.import_validation = "pending";
      request.cached_serial_verification = false;
      request.cached_user_verification = false;
    });
    renderImportPreview();
    updateImportPrepareButton(payload);
    return;
  }
  const importValidationEnabled = validationEnabled("validate_workbook_import");
  if (!importValidationEnabled) {
    requests.forEach((request) => {
      request.import_validation = "valid";
      request.import_error = "";
      request.import_failed_fields = [];
      request.returning_user_loading = false;
      request.cached_serial_verification = false;
      request.cached_user_verification = false;
    });
    renderImportPreview();
    updateImportPrepareButton(payload);
    return;
  }
  requests.forEach((request) => {
    request.import_validation_epoch = Number(request.import_validation_epoch || 0) + 1;
    request.import_validation = "checking";
    request.import_error = "";
    request.import_failed_fields = [];
    request.cached_serial_verification = false;
    request.cached_user_verification = false;
    request.serial_validation = "checking";
    if (request.kind === "user") {
      request.user_info = null;
      request.user_validation = request.user ? "checking" : "empty";
    }
  });
  renderImportPreview();
  $("#prepareImportButton").disabled = true;
  await forEachWithConcurrency(requests, VALIDATION_CONCURRENCY, async (request) => {
    const epoch = request.import_validation_epoch;
    const serial = String(request.serials?.[0] || "").trim();
    const username = String(request.user || "").trim();
    const needsUserVerification = request.kind === "user";
    try {
      const [assets, users] = await Promise.all([
        api("/api/search/assets", {
          method: "POST",
          body: JSON.stringify({
            query: serial,
            fresh: true,
          }),
        }),
        needsUserVerification
          ? api("/api/search/users", { method: "POST", body: JSON.stringify({ query: username, fresh: true }) })
          : Promise.resolve({ results: [] }),
      ]);
      if (!importValidationIsCurrent(payload, request, epoch)) return;
      const asset = (assets.results || []).find((item) => serialResultMatches(item, serial));
      const user = needsUserVerification
        ? (users.results || []).find((item) => userResultMatches(item, username))
        : null;
      const userInfo = needsUserVerification ? verifiedUserInfo(user, username) : null;
      const missingFields = [];
      if (!asset) missingFields.push("serial");
      if (needsUserVerification && !userInfo) missingFields.push("username");
      if (missingFields.length) {
        const missingLabels = missingFields.map((field) => field === "serial" ? "Serial number" : "Username");
        const validationError = new Error(`${missingLabels.join(" and ")} ${missingLabels.length === 1 ? "was" : "were"} not found in Helix.`);
        validationError.failedFields = missingFields;
        throw validationError;
      }
      if (needsUserVerification) {
        request.user = userInfo.login;
        request.user_info = userInfo;
        request.user_validation = "valid";
      }
      request.serial_validation = "valid";
      request.import_validation = "valid";
      if (assets.cached) {
        request.cached_serial_verification = true;
        scheduleImportPreviewUpdate(payload);
        verifyCachedValueInBackground("serial", serial, false, (freshPayload) => {
          if (!importValidationIsCurrent(payload, request, epoch) || request.serials?.[0] !== serial) return;
          const freshAsset = (freshPayload.results || []).find((item) => serialResultMatches(item, serial));
          request.cached_serial_verification = false;
          if (freshAsset) {
          } else {
            request.import_validation = "failed";
            request.serial_validation = "failed";
            addImportFailedField(request, "serial");
            request.import_error = "Serial number was not found in Helix.";
          }
          scheduleImportPreviewUpdate(payload, { full: !freshAsset });
        }, () => {
          if (!importValidationIsCurrent(payload, request, epoch)) return;
          request.cached_serial_verification = false;
          scheduleImportPreviewUpdate(payload);
        });
      }
      if (needsUserVerification && users.cached) {
        request.cached_user_verification = true;
        scheduleImportPreviewUpdate(payload);
        verifyCachedValueInBackground("username", username, false, (freshPayload) => {
          if (!importValidationIsCurrent(payload, request, epoch)) return;
          const freshUser = (freshPayload.results || []).find((item) => userResultMatches(item, username));
          const freshUserInfo = verifiedUserInfo(freshUser, username);
          request.cached_user_verification = false;
          if (freshUserInfo) {
            request.user_info = freshUserInfo;
          } else {
            request.import_validation = "failed";
            request.user_validation = "failed";
            addImportFailedField(request, "username");
            request.import_error = "Username was not found in Helix.";
            request.user_info = null;
          }
          scheduleImportPreviewUpdate(payload, { full: !freshUserInfo });
        }, () => {
          if (!importValidationIsCurrent(payload, request, epoch)) return;
          request.cached_user_verification = false;
          scheduleImportPreviewUpdate(payload);
        });
      }
    } catch (error) {
      if (!importValidationIsCurrent(payload, request, epoch)) return;
      request.import_validation = "failed";
      request.import_error = error.message || "Could not validate this request.";
      request.import_failed_fields = error.failedFields || ["serial", ...(request.kind === "user" ? ["username"] : [])];
      request.user_info = null;
    } finally {
      if (importValidationIsCurrent(payload, request, epoch)) {
        scheduleImportPreviewUpdate(payload);
      }
    }
  });
  renderImportPreview();
  updateImportPrepareButton(payload);
}

function backlogValidationIsCurrent(payload, request, epoch) {
  return state.importPreview === payload
    && request.included !== false
    && request.backlog_validation_epoch === epoch;
}

function resumePendingBacklogValidation() {
  const payload = state.importPreview;
  if (!payload || payload.mode !== "backlog" || !connectionIsReady()) return;
  const requests = (payload.requests || []).filter((request) => request.included !== false
    && (!['valid', 'failed'].includes(request.import_validation)
      || request.cached_serial_verification
      || request.cached_user_verification)
    && !state.backlogValidationIds.has(request.id));
  if (requests.length) void validateBacklogPreview(payload, requests);
}

async function validateBacklogPreview(payload = state.importPreview, retryRequests = null) {
  if (!payload || payload.mode !== "backlog") return;
  const source = Array.isArray(retryRequests) ? retryRequests : payload.requests || [];
  const requests = source.filter((request) => request.included !== false);
  if (!requests.length) {
    renderImportPreview();
    updateImportPrepareButton(payload);
    return;
  }
  if (!connectionIsReady()) {
    requests.forEach((request) => {
      if (request.import_validation === "checking") request.import_validation = "pending";
      request.cached_serial_verification = false;
      request.cached_user_verification = false;
    });
    renderImportPreview();
    updateImportPrepareButton(payload);
    return;
  }
  const requestsToValidate = requests.filter((request) => !state.backlogValidationIds.has(request.id));
  if (!requestsToValidate.length) return;
  if (!validationEnabled("validate_workbook_import")) {
    requestsToValidate.forEach((request) => {
      request.import_validation = "valid";
      request.import_error = "";
      request.import_failed_fields = [];
      request.serial_validation = "valid";
      request.user_validation = "valid";
      request.cached_serial_verification = false;
      request.cached_user_verification = false;
    });
    renderImportPreview();
    updateImportPrepareButton(payload);
    return;
  }
  requestsToValidate.forEach((request) => {
    state.backlogValidationIds.add(request.id);
    request.backlog_validation_epoch = Number(request.backlog_validation_epoch || 0) + 1;
    request.import_validation = "checking";
    request.import_error = "";
    request.import_failed_fields = [];
    request.serial_validation = "checking";
    request.user_validation = "checking";
    request.cached_serial_verification = false;
    request.cached_user_verification = false;
    request.user_info = null;
  });
  renderImportPreview();
  updateImportPrepareButton(payload);
  await forEachWithConcurrency(requestsToValidate, VALIDATION_CONCURRENCY, async (request) => {
    const epoch = request.backlog_validation_epoch;
    const serial = String(request.serial || "").trim();
    const username = String(request.username || "").trim();
    try {
      const [assets, users] = await Promise.all([
        api("/api/search/assets", { method: "POST", body: JSON.stringify({ query: serial, fresh: true }) }),
        api("/api/search/users", { method: "POST", body: JSON.stringify({ query: username, fresh: true }) }),
      ]);
      if (!backlogValidationIsCurrent(payload, request, epoch)) return;
      const asset = (assets.results || []).find((item) => serialResultMatches(item, serial));
      const user = (users.results || []).find((item) => userResultMatches(item, username));
      const userInfo = verifiedUserInfo(user, username);
      if (!asset || !userInfo) {
        request.import_validation = "failed";
        request.serial_validation = asset ? "valid" : "failed";
        request.user_validation = userInfo ? "valid" : "failed";
        request.import_failed_fields = [
          ...(!asset ? ["serial"] : []),
          ...(!userInfo ? ["username"] : []),
        ];
        request.import_error = !asset && !userInfo
          ? "Serial and user were not found in Helix."
          : !asset
            ? "Serial number was not found in Helix."
            : "User was not found in Helix.";
        request.user_info = null;
        return;
      }
      request.user_info = userInfo;
      request.serial_validation = "valid";
      request.user_validation = "valid";
      request.import_validation = "valid";
      if (assets.cached) {
        request.cached_serial_verification = true;
        scheduleImportPreviewUpdate(payload);
        verifyCachedValueInBackground("serial", serial, false, (freshPayload) => {
          if (!backlogValidationIsCurrent(payload, request, epoch)) return;
          const freshAsset = (freshPayload.results || []).find((item) => serialResultMatches(item, serial));
          request.cached_serial_verification = false;
          if (freshAsset) {
          } else {
            request.import_validation = "failed";
            request.serial_validation = "failed";
            addImportFailedField(request, "serial");
            request.import_error = "Serial number was not found in Helix.";
          }
          scheduleImportPreviewUpdate(payload, { full: !freshAsset });
        }, () => {
          if (!backlogValidationIsCurrent(payload, request, epoch)) return;
          request.cached_serial_verification = false;
          scheduleImportPreviewUpdate(payload);
        });
      }
      if (users.cached) {
        request.cached_user_verification = true;
        scheduleImportPreviewUpdate(payload);
        verifyCachedValueInBackground("username", username, false, (freshPayload) => {
          if (!backlogValidationIsCurrent(payload, request, epoch)) return;
          const freshUser = (freshPayload.results || []).find((item) => userResultMatches(item, username));
          const freshUserInfo = verifiedUserInfo(freshUser, username);
          request.cached_user_verification = false;
          if (freshUserInfo) {
            request.user_info = freshUserInfo;
          } else {
            request.import_validation = "failed";
            request.user_validation = "failed";
            addImportFailedField(request, "username");
            request.import_error = "User was not found in Helix.";
            request.user_info = null;
          }
          scheduleImportPreviewUpdate(payload, { full: !freshUserInfo });
        }, () => {
          if (!backlogValidationIsCurrent(payload, request, epoch)) return;
          request.cached_user_verification = false;
          scheduleImportPreviewUpdate(payload);
        });
      }
    } catch (error) {
      if (!backlogValidationIsCurrent(payload, request, epoch)) return;
      request.import_validation = "failed";
      request.serial_validation = "failed";
      request.user_validation = "failed";
      request.import_failed_fields = ["serial", "username"];
      request.import_error = error.message || "Could not verify this row.";
      request.user_info = null;
    } finally {
      if (request.backlog_validation_epoch === epoch) state.backlogValidationIds.delete(request.id);
      scheduleImportPreviewUpdate(payload);
    }
  });
  renderImportPreview();
  updateImportPrepareButton(payload);
}

function backToImportSelection() {
  state.importPreview = null;
  state.importUndoStack = [];
  state.importRedoStack = [];
  updateImportHistoryControls();
  setImportStage("options");
  $("#backImportButton").hidden = true;
  setButtonLabel("#prepareImportButton", "Review import");
  $("#prepareImportButton").disabled = false;
  $("#importError").hidden = true;
  renderImportModeOptions();
  setImportStep(2);
  saveCurrentImportDraft();
}

async function prepareImport() {
  const button = $("#prepareImportButton");
  if (state.importPreview) {
    if (state.importPreview.mode === "backlog") {
      const selected = state.importPreview.requests.filter((request) => request.included !== false);
      if (!selected.length) {
        toast("Select at least one undeployed device to add.", "error");
        updateImportPrepareButton(state.importPreview);
        return;
      }
      if (selected.some((request) => request.import_validation === "checking" || !request.import_validation)) {
        toast("Wait for verification to finish.", "error");
        updateImportPrepareButton(state.importPreview);
        return;
      }
      if (selected.some((request) => !request.status)) {
        toast("Choose a deployment status for every included device.", "error");
        updateImportPrepareButton(state.importPreview);
        return;
      }
      if (selected.some((request) => request.import_validation !== "valid")) {
        toast("Verify every included device, or exclude invalid rows.", "error");
        updateImportPrepareButton(state.importPreview);
        return;
      }
      const requests = selected.map((request) => ({
        id: request.id,
        kind: "user",
        serials: [request.serial],
        status: request.status,
        user: request.username,
        returning: false,
        returning_user: "",
        returning_user_info: null,
        location: null,
        group: "Deployments",
        source: `${state.workbook?.filename || "ALM Workbook"} · backlog · ${request.date}`,
        device_allocation: request.device_allocation || "",
        first_name: request.first_name || "",
        last_name: request.last_name || "",
        serial_validation: "valid",
        user_validation: "valid",
        user_info: request.user_info || null,
      }));
      recordAppEdit();
      state.queue.push(...requests);
      state.selectedId = requests[0]?.id || state.selectedId;
      await deleteImportDraft(state.importDraftId);
      state.importDraftId = null;
      $("#importDialog").close();
      renderAll();
      toast(`${requests.length} backlog request${requests.length === 1 ? "" : "s"} added.`, "success");
      return;
    }
    const selected = state.importPreview.requests.filter((request) => request.included !== false);
    if (selected.some((request) => request.import_validation === "checking" || !request.import_validation)) {
      toast("Wait for verification to finish.", "error");
      updateImportPrepareButton(state.importPreview);
      return;
    }
    if (selected.some((request) => ALM_IMPORT_STATUS_OPTIONS[request.group] && !request.status)) {
      toast("Choose a status for every deployment and returned device.", "error");
      updateImportPrepareButton(state.importPreview);
      return;
    }
    if (selected.some((request) => request.group === "Returned devices") && !hasCompleteLocation(state.importLocation)) {
      toast("Choose a complete destination for returned devices.", "error");
      updateImportPrepareButton(state.importPreview);
      return;
    }
    if (selected.some((request) => request.import_validation !== "valid")) {
      toast("Verify every included row, or exclude invalid rows.", "error");
      updateImportPrepareButton(state.importPreview);
      return;
    }
    const requests = selected.map((request) => {
      const cleanRequest = { ...request };
      cleanRequest.serial_validation = "valid";
      if (cleanRequest.kind === "user") cleanRequest.user_validation = "valid";
      if (cleanRequest.kind === "location") {
        delete cleanRequest.returning;
        delete cleanRequest.returning_user;
        delete cleanRequest.returning_user_info;
        delete cleanRequest.returning_user_validation;
        delete cleanRequest.returning_user_validation_error;
      }
      delete cleanRequest.included;
      return cleanRequest;
    });
    if (!requests.length) {
      $("#importError").textContent = "Select at least one deployment to add.";
      $("#importError").hidden = false;
      return;
    }
    recordAppEdit();
    state.queue.push(...requests);
    state.selectedId = requests[0]?.id || state.selectedId;
    await deleteImportDraft(state.importDraftId);
    state.importDraftId = null;
    $("#importDialog").close();
    renderAll();
    toast(`${requests.length} request${requests.length === 1 ? "" : "s"} added.`, "success");
    return;
  }
  button.disabled = true;
  try {
    if (!$("#importMapColumns").hidden) {
      await mapWorkbookColumns();
      return;
    }
    const mode = importModeValue();
    if (state.importMode === "backlog") {
      if (!state.workbook?.import_id) throw new Error("Import the ALM Workbook first.");
      const payload = await api("/api/import/backlog", {
        method: "POST",
        body: JSON.stringify({
          import_id: state.workbook.import_id,
          sheet: $("#sheetInput").value,
          days_back: Math.max(1, Number($("#almBacklogDaysInput").value) || 30),
          include_today: $("#almBacklogIncludeToday").checked,
        }),
      });
      payload.requests = (payload.candidates || []).map((candidate) => ({
        ...candidate,
        import_validation: candidate.included === false ? "idle" : "checking",
        import_error: "",
        user_info: null,
      }));
      state.importPreview = payload;
      setImportStage("preview");
      $("#backImportButton").hidden = false;
      $("#prepareImportButton").disabled = true;
      setImportStep(3);
      renderImportPreview();
      saveCurrentImportDraft();
      validateBacklogPreview(payload);
      return;
    }
    if (!mode) throw new Error("Select at least one type of deployment to import.");
    const selectedDates = selectedImportDates();
    if (!selectedDates.length) throw new Error("Choose at least one deployment date.");
    const groupSelections = selectedImportGroupSelections();
    const selectedDateEntries = selectedImportDateEntries();
    if (selectedDateEntries.some((entry) => (entry.groups || []).length > 1 && !groupSelections[entry.value])) {
      throw new Error("Choose a date section for each grouped date, or choose all sections.");
    }
    const selectedReturnedCount = selectedDateEntries.reduce((total, entry) => {
      const groupValue = groupSelections[entry.value];
      const group = entry.groups?.find((item) => item.value === groupValue);
      return total + Number((groupValue && groupValue !== "all" ? group : entry)?.returned_device_count || 0);
    }, 0);
    if (selectedReturnedCount > 0 && !hasCompleteLocation(state.importLocation)) {
      throw new Error("Choose a complete destination for returned devices.");
    }
    const payload = await api("/api/import/prepare", {
      method: "POST",
      body: JSON.stringify({
        import_id: state.workbook.import_id,
        sheet: $("#sheetInput").value,
        dates: selectedDates,
        mode,
        location: state.importLocation,
        group_selections: groupSelections,
      }),
    });
    payload.requests.forEach((request) => {
      request.included = true;
      request.import_validation = "checking";
      request.import_error = "";
      request.import_failed_fields = [];
    });
    state.importPreview = payload;
    state.importExpandedGroups.clear();
    setImportStage("preview");
    $("#backImportButton").hidden = false;
    setButtonLabel(button, "Verifying selections…");
    button.disabled = true;
    setImportStep(3);
    renderImportPreview();
    saveCurrentImportDraft();
    validateImportPreview();
  } catch (error) {
    $("#importError").textContent = error.message;
    $("#importError").hidden = false;
  } finally {
    if (state.importPreview) {
      updateImportPrepareButton(state.importPreview);
    } else {
      updateImportCounts();
    }
  }
}

async function openReview() {
  if (!state.queue.length || elements.reviewButton.disabled) return;
  const validations = queueValidation();
  const invalid = [...validations.values()].filter((errors) => errors.length);
  $("#reviewList").innerHTML = `
    <div class="review-row review-heading" aria-hidden="true">
      <span></span><span>Device</span><span>Status</span><span>Destination</span>
    </div>
    ${state.queue.map((request) => {
      const errors = validations.get(request.id) || [];
      const errorText = validationErrorTexts(request, errors);
      const secondary = request.source
        || (request.group && request.group !== kindLabel(request.kind) ? request.group : "");
      return `<div class="review-row">
        <span class="review-state ${errors.length ? "invalid-mark" : "ready-mark"}" aria-label="${errors.length ? "Needs attention" : "Ready"}">${errors.length ? iconMarkup("circle-alert") : iconMarkup("check")}</span>
        <div class="review-field review-request">
          <small class="review-label">Device</small>
          <strong>${escapeHtml(request.serials.join(", ") || "No serial")}</strong>
          <small class="review-meta">${escapeHtml(kindLabel(request.kind))}${request.kind === "bulk_location" ? ` · ${request.serials.length} devices` : ""}</small>
          ${request.device_allocation ? `<small class="review-meta">${escapeHtml(request.device_allocation)}</small>` : ""}
        </div>
        <div class="review-field review-status">
          <small class="review-label">Status</small>
          <strong>${escapeHtml(statusLabel(request))}</strong>
          ${secondary ? `<small class="review-meta">${escapeHtml(secondary)}</small>` : ""}
        </div>
        <div class="review-field review-destination">
          <small class="review-label">Destination</small>
          <strong>${escapeHtml(destinationLabel(request))}</strong>
          ${errors.length
            ? `<small class="review-error">${escapeHtml(errorText[0])}</small>`
            : request.returning_user
              ? `<small class="review-meta">Returned by ${escapeHtml(request.returning_user)}</small>`
              : ""}
        </div>
      </div>`;
  }).join("")}`;
  $("#submitQueueButton").disabled = invalid.length > 0;
  $("#reviewDialog").showModal();
  refreshIcons($("#reviewDialog"));
}

function progressStateSymbol(entry) {
  if (entry.state === "succeeded") return iconMarkup("check");
  if (entry.state === "failed") return iconMarkup("circle-alert");
  if (entry.state === "running") return '<span class="activity-spinner" aria-hidden="true"></span>';
  return iconMarkup("circle-dashed");
}

function progressStateLabel(entry) {
  if (entry.state === "succeeded") return "Submitted";
  if (entry.state === "failed") return "Failed";
  if (entry.state === "running") return "Submitting";
  return "Pending";
}

function progressDestinationLine(entry) {
  const destination = entry.destination || "No destination";
  const returner = entry.returning_user ? ` · returned by ${entry.returning_user}` : "";
  return `${entry.status || "Status not selected"} · ${destination}${returner}`;
}

function formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  if (minutes < 60) return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function jobCompletedSuccessfully(job) {
  const counts = job?.counts || {};
  return job?.state === "finished"
    && Number(counts.total || 0) > 0
    && Number(counts.failed || 0) === 0
    && Number(counts.succeeded || 0) === Number(counts.total || 0);
}

function celebrateSubmission(job) {
  if (!jobCompletedSuccessfully(job) || state.celebratedJobs.has(job.job_id)) return;
  state.celebratedJobs.add(job.job_id);
  const celebration = $("#progressCelebration");
  if (celebration) {
    celebration.classList.remove("is-celebrating");
    // Force a fresh animation when a completed run is rendered more than once.
    void celebration.offsetWidth;
    celebration.classList.add("is-celebrating");
    window.setTimeout(() => celebration.classList.remove("is-celebrating"), 1800);
  }
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const burst = window.confetti;
  if (typeof burst !== "function") return;
  const colors = ["#f48234", "#24724a", "#f9c56d", "#ffffff"];
  try {
    burst({ particleCount: 54, spread: 62, startVelocity: 28, gravity: .9, origin: { x: .18, y: .58 }, colors });
    window.setTimeout(() => {
      try {
        burst({ particleCount: 38, spread: 54, startVelocity: 24, gravity: .95, origin: { x: .82, y: .56 }, colors });
      } catch (_) {
        // The in-dialog completion animation remains as the visual fallback.
      }
    }, 150);
  } catch (_) {
    // The in-dialog completion animation remains as the visual fallback.
  }
}

function renderSubmissionNotice(job = state.currentJob) {
  if (!elements.submissionNotice) return;
  const visible = state.submissionStarting || Boolean(job);
  elements.submissionNotice.hidden = !visible;
  if (!visible) return;
  const finished = job?.state === "finished";
  const counts = job?.counts || {};
  const done = Number(counts.succeeded || 0) + Number(counts.failed || 0);
  const total = Number(counts.total || state.queue.length || 0);
  const failures = Number(counts.failed || 0);
  elements.submissionNotice.classList.toggle("finished", finished);
  elements.submissionNotice.classList.toggle("has-failures", finished && failures > 0);
  elements.submissionNoticeState.innerHTML = finished
    ? iconMarkup(failures ? "circle-alert" : "circle-check")
    : '<span class="activity-spinner" aria-hidden="true"></span>';
  refreshIcons(elements.submissionNoticeState);
  elements.submissionNoticeTitle.textContent = state.submissionStarting
    ? "Starting submission"
    : finished && !failures ? "All requests submitted" : finished ? "Submission finished" : "Submitting requests";
  elements.submissionNoticeDetail.textContent = state.pollStatusMessage
    || (state.submissionStarting
      ? "Preparing the request run…"
      : finished
        ? failures
          ? `${counts.succeeded || 0} submitted · ${failures} failed`
          : `${counts.succeeded || 0} submitted successfully`
        : `${done} of ${total} complete${counts.running ? ` · ${counts.running} active` : ""}`);
  const viewButton = $("#viewSubmissionButton");
  const viewLabel = viewButton?.querySelector("span");
  if (viewLabel) viewLabel.textContent = finished ? "View results" : "View progress";
}

function resetProgressView() {
  const bar = $("#progressBar");
  bar.style.transition = "none";
  bar.style.width = "0%";
  $("#progressCounts").textContent = "Starting…";
  $("#progressList").replaceChildren();
  $("#progressActions").hidden = true;
  $("#progressCelebration").hidden = true;
  $("#progressHeading").textContent = "Starting submission";
  $("#closeProgressButton").title = "Continue in the background";
  requestAnimationFrame(() => { bar.style.transition = ""; });
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
  let queueChanged = false;
  state.queue.forEach((request) => {
    const entry = entriesById.get(request.id);
    if (!entry) return;
    const updates = {
      request_id: entry.request_id || request.request_id || "",
      order_id: entry.order_id || request.order_id || "",
      result_state: entry.state,
      result_message: entry.message || "",
    };
    Object.entries(updates).forEach(([key, value]) => {
      if (request[key] === value) return;
      request[key] = value;
      queueChanged = true;
    });
  });
  if (queueChanged) renderQueue();
  else renderSubmissionNotice(job);
  recordSuccessfulLocations(job);
  const done = job.counts.succeeded + job.counts.failed;
  const percentage = job.counts.total
    ? job.entries.reduce(
      (sum, entry) => sum + (["succeeded", "failed"].includes(entry.state) ? 100 : Number(entry.progress_percent || 0)),
      0,
    ) / job.counts.total
    : 0;
  $("#progressBar").style.width = `${percentage}%`;
  $("#progressCounts").textContent = state.pollStatusMessage
    || `${done} of ${job.counts.total} complete${job.counts.running ? ` · ${job.counts.running} active` : ""}`;
  const spinnerDelay = -(performance.now() % 720);
  const progressList = $("#progressList");
  const previousScrollTop = progressList.scrollTop;
  $("#progressList").innerHTML = job.entries.map((entry) => {
    const elapsed = entry.elapsed_seconds == null ? "" : formatElapsed(entry.elapsed_seconds);
    const progressMessage = entry.state === "queued"
      ? entry.message
      : `Step ${entry.step || 1} of ${entry.step_count || 1} · ${entry.message}`;
    return `
    <div class="progress-row ${entry.state}">
      <span class="progress-state" aria-label="${progressStateLabel(entry)}">${entry.state === "running" ? `<i class="activity-spinner" style="animation-delay:${spinnerDelay}ms"></i>` : progressStateSymbol(entry)}</span>
      <div class="progress-device">
        <small class="progress-field-label">Device</small>
        <strong>${escapeHtml(entry.serials.join(", "))}</strong>
        <small class="progress-destination">${escapeHtml(progressDestinationLine(entry))}</small>
      </div>
      <div class="progress-message">
        <small class="progress-field-label">Progress</small>
        <strong>${escapeHtml(progressMessage)}${elapsed ? ` · ${escapeHtml(elapsed)}` : ""}</strong>
      </div>
      <div class="progress-request-cell">${entry.request_id && entry.state !== "running"
        ? requestIdDisplay(entry.request_id, "progress-request-id")
        : `<span class="progress-pending-id">${entry.state === "failed" ? "No request ID" : "Request ID pending"}</span>`}</div>
    </div>`;
  }).join("");
  progressList.scrollTop = previousScrollTop;
  const finished = job.state === "finished";
  const fullySuccessful = jobCompletedSuccessfully(job);
  const celebration = $("#progressCelebration");
  celebration.hidden = !fullySuccessful;
  if (fullySuccessful) {
    $("#progressCelebrationDetail").textContent = `All ${job.counts.total} request${job.counts.total === 1 ? "" : "s"} were submitted successfully.`;
  }
  $("#progressHeading").textContent = finished
    ? `${job.counts.succeeded} submitted, ${job.counts.failed} failed`
    : "Submitting requests";
  $("#progressActions").hidden = !finished;
  $("#closeProgressButton").title = finished ? "Close results" : "Continue in the background";
  $("#downloadResultsLink").href = `/api/jobs/${job.job_id}/results.txt`;
  renderSubmissionNotice(job);
  refreshIcons($("#progressDialog"));
}

function formatHistoryDate(value) {
  if (!value) return "Unknown time";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function historyPerson(entry) {
  const login = String(entry.user || entry.returning_user || "").trim();
  const info = entry.user_info || entry.returning_user_info;
  const label = confirmedPersonLabel(login, info);
  return {
    role: entry.user ? "Deployed to" : entry.returning_user ? "Returning user" : "Destination",
    name: label.fullName || entry.destination || "No destination",
    login: label.login,
  };
}

function historyEntryMatches(entry, run, query, filter) {
  if (filter === "submitted" && entry.state !== "succeeded") return false;
  if (filter === "failed" && entry.state !== "failed") return false;
  if (filter === "active" && !["queued", "running"].includes(entry.state)) return false;
  if (!query) return true;
  const person = historyPerson(entry);
  const values = [
    ...(entry.serials || []),
    entry.status,
    kindLabel(entry.kind),
    entry.destination,
    entry.request_id,
    entry.order_id,
    entry.message,
    entry.user,
    entry.returning_user,
    person.name,
    person.login,
    ...(entry.user_info?.columns || []),
    ...(entry.returning_user_info?.columns || []),
    run.request_for,
  ];
  return values.some((value) => String(value || "").toLocaleLowerCase().includes(query));
}

function renderHistory(runs) {
  const query = String($("#historySearchInput")?.value || "").trim().toLocaleLowerCase();
  const filter = $("#historyStateFilter")?.value || "all";
  // Active submission jobs are kept in the history response so startup can
  // restore progress, but they are not historical requests yet.
  const completedRuns = runs.filter((run) => run.state === "finished");
  const matchingRuns = completedRuns.map((run) => ({
    ...run,
    entries: (run.entries || []).filter((entry) => historyEntryMatches(entry, run, query, filter)),
  })).filter((run) => run.entries.length);
  const matchedEntries = matchingRuns.reduce((total, run) => total + run.entries.length, 0);
  const summary = $("#historyResultsSummary");
  if (summary) summary.textContent = completedRuns.length
    ? `${matchedEntries} request${matchedEntries === 1 ? "" : "s"} in ${matchingRuns.length} run${matchingRuns.length === 1 ? "" : "s"}`
    : "";
  if (!matchingRuns.length) {
    elements.historyList.innerHTML = `<div class="history-empty">${completedRuns.length ? "No requests match these filters." : "No request runs yet."}</div>`;
    return;
  }
  elements.historyList.innerHTML = matchingRuns.map((run) => {
    const succeeded = run.counts?.succeeded || 0;
    const failed = run.counts?.failed || 0;
    const stateLabel = run.state === "finished"
      ? `${succeeded} submitted · ${failed} failed`
      : run.state;
    const entries = (run.entries || []).map((entry) => {
      const person = historyPerson(entry);
      const requestLink = entry.request_id
        ? requestIdDisplay(entry.request_id, "history-request-id")
        : '<strong class="history-request-id">No request ID</strong>';
      const serials = (entry.serials || []).filter(Boolean);
      const serialMarkup = serials.length
        ? serials.map((serial) => `<button class="history-filter-link" type="button" data-history-filter="${escapeHtml(serial)}" title="Show requests containing ${escapeHtml(serial)}">${escapeHtml(serial)}</button>`).join("")
        : "No serial";
      return `<div class="history-entry ${entry.state === "failed" ? "failed" : ""}">
        <div class="history-device"><div class="history-device-serials">${serialMarkup}</div><span>${escapeHtml(entry.status || kindLabel(entry.kind))}</span></div>
        <div class="history-person"><small>${escapeHtml(person.role)}</small><strong>${escapeHtml(person.name)}</strong>${person.login ? `<button class="history-filter-link" type="button" data-history-filter="${escapeHtml(person.login)}" title="Show requests for ${escapeHtml(person.login)}">${escapeHtml(person.login)}</button>` : ""}</div>
        <div class="history-result"><span class="history-result-state ${entry.state === "failed" ? "failed" : ""}">${escapeHtml(entry.state === "succeeded" ? "Submitted" : entry.state)}</span>${requestLink}<small>${escapeHtml(entry.message || "")}</small></div>
        <div class="history-entry-actions"><button class="button secondary compact" type="button" data-history-readd="${escapeHtml(entry.id)}">${iconMarkup("rotate-ccw")}<span>Re-add to queue</span></button></div>
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
  refreshIcons(elements.historyList);
  elements.historyList.querySelectorAll("[data-history-readd]").forEach((button) => {
    button.addEventListener("click", () => {
      const run = runs.find((item) => (item.entries || []).some((entry) => entry.id === button.dataset.historyReadd));
      const entry = run?.entries?.find((item) => item.id === button.dataset.historyReadd);
      if (entry) reAddHistoryEntry(entry, run);
    });
  });
  elements.historyList.querySelectorAll("[data-history-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      $("#historySearchInput").value = button.dataset.historyFilter || "";
      $("#historyStateFilter").value = "all";
      renderHistory(state.historyRuns);
    });
  });
}

function reAddHistoryEntry(entry, run) {
  const request = structuredClone(entry);
  request.id = uid();
  request.source = request.source
    ? `${request.source} · re-added`
    : `Re-added from history · ${formatHistoryDate(run?.created_at)}`;
  request.serial_validation = request.serials?.length ? "valid" : "empty";
  request.serial_validation_error = "";
  request.user_validation = request.user ? "valid" : "empty";
  request.user_validation_error = "";
  request.returning_user_validation = request.returning_user ? "valid" : "empty";
  request.returning_user_validation_error = "";
  request.returning_user_loading = false;
  request.bulk_validation = request.kind === "bulk_location" && request.serials?.length ? "valid" : "empty";
  request.bulk_validation_error = "";
  request.bulk_validation_missing = [];
  delete request.request_id;
  delete request.order_id;
  delete request.result_state;
  delete request.result_message;
  delete request.state;
  delete request.message;
  delete request.step;
  delete request.step_count;
  delete request.progress_percent;
  delete request.elapsed_seconds;
  recordAppEdit();
  state.queue.push(request);
  state.selectedId = request.id;
  renderAll();
  toast("Request re-added to the queue.", "success");
}

async function openHistory() {
  elements.historyButton.disabled = true;
  try {
    const payload = await api("/api/history");
    state.historyRuns = payload.runs || [];
    $("#historySearchInput").value = "";
    $("#historyStateFilter").value = "all";
    renderHistory(state.historyRuns);
    $("#historyDialog").showModal();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    elements.historyButton.disabled = false;
  }
}

function stopJobPolling() {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function scheduleJobPoll(jobId, delay) {
  stopJobPolling();
  state.pollTimer = window.setTimeout(() => { void pollJob(jobId); }, delay);
}

function showProgressDialog() {
  const dialog = $("#progressDialog");
  if (!state.submissionStarting && !state.currentJob) return;
  if (state.currentJob) renderProgress(state.currentJob);
  if (!dialog.open) dialog.showModal();
}

function finalizeCurrentSubmission() {
  const job = state.currentJob;
  if (!job || job.state !== "finished") return;
  const succeeded = new Set(
    (job.entries || []).filter((entry) => entry.state === "succeeded").map((entry) => entry.id),
  );
  const failed = new Set(
    (job.entries || []).filter((entry) => entry.state === "failed").map((entry) => entry.id),
  );
  state.queue = state.queue.filter((request) => {
    if (succeeded.has(request.id)) return false;
    if (failed.has(request.id)) {
      const previousId = request.id;
      request.id = uid();
      if (state.selectedId === previousId) state.selectedId = request.id;
    }
    return true;
  });
  const failedRequest = state.queue.find((request) => request.result_state === "failed");
  if (!state.queue.some((request) => request.id === state.selectedId)) {
    state.selectedId = failedRequest?.id || state.queue[0]?.id || null;
  }
  state.currentJob = null;
  state.pollFailures = 0;
  state.pollStatusMessage = "";
  stopJobPolling();
  renderAll();
}

async function restoreSubmissionFromHistory() {
  if (!state.queue.length) return;
  try {
    const payload = await api("/api/history");
    const queueIds = new Set(state.queue.map((request) => request.id));
    const matchingRun = (payload.runs || []).find((run) =>
      (run.entries || []).some((entry) => queueIds.has(entry.id)),
    );
    if (!matchingRun) {
      let changed = false;
      state.queue.forEach((request) => {
        if (!["queued", "running"].includes(request.result_state)) return;
        delete request.result_state;
        delete request.result_message;
        changed = true;
      });
      if (changed) renderQueue();
      return;
    }
    renderProgress(matchingRun);
    if (matchingRun.state !== "finished") scheduleJobPoll(matchingRun.job_id, 250);
  } catch (error) {
    console.warn("Could not restore the latest submission status.", error);
  }
}

async function pollJob(jobId) {
  if (state.pollInFlight) return;
  if (state.currentJob && state.currentJob.job_id !== jobId) return;
  state.pollInFlight = true;
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (state.currentJob && state.currentJob.job_id !== jobId) return;
    state.pollFailures = 0;
    state.pollStatusMessage = "";
    renderProgress(job);
    if (job.state !== "finished") {
      scheduleJobPoll(jobId, document.hidden ? 1800 : 750);
      return;
    }
    stopJobPolling();
    void refreshConnection();
    celebrateSubmission(job);
    if (!state.notifiedJobs.has(job.job_id)) {
      state.notifiedJobs.add(job.job_id);
      const type = job.counts.failed ? "error" : "success";
      toast(
        jobCompletedSuccessfully(job)
          ? `All ${job.counts.succeeded} request${job.counts.succeeded === 1 ? "" : "s"} submitted successfully.`
          : `${job.counts.succeeded} request${job.counts.succeeded === 1 ? "" : "s"} submitted; ${job.counts.failed} failed.`,
        type,
      );
    }
  } catch (error) {
    if (state.currentJob && state.currentJob.job_id !== jobId) return;
    state.pollFailures += 1;
    state.pollStatusMessage = "Connection interrupted — submission is still running and status will retry.";
    renderSubmissionNotice();
    if ($("#progressDialog").open) $("#progressCounts").textContent = "Reconnecting to submission status…";
    if (state.pollFailures === 1) toast("Submission is still running. Reconnecting to its status…", "error");
    scheduleJobPoll(jobId, Math.min(10_000, 1200 * (2 ** Math.min(state.pollFailures - 1, 3))));
  } finally {
    state.pollInFlight = false;
  }
}

async function submitQueue() {
  if (submissionBusy()) {
    showProgressDialog();
    return;
  }
  const button = $("#submitQueueButton");
  button.disabled = true;
  state.queue.forEach(clearRequestSubmissionMetadata);
  state.submissionStarting = true;
  state.pollFailures = 0;
  state.pollStatusMessage = "";
  resetProgressView();
  renderQueue();
  $("#reviewDialog").close();
  $("#progressDialog").showModal();
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
    state.submissionStarting = false;
    renderQueue();
    const progressWasOpen = $("#progressDialog").open;
    if (progressWasOpen) $("#progressDialog").close();
    if (error.payload?.validation) {
      const queueErrors = error.payload.validation._queue || [];
      toast(
        queueErrors.length
          ? queueErrors.join(" ")
          : "Some requests need attention. Return to the queue to correct them.",
        "error",
      );
    } else {
      toast(error.message, "error");
    }
    button.disabled = false;
    if (progressWasOpen && !$("#reviewDialog").open) $("#reviewDialog").showModal();
    return;
  }
  state.submissionStarting = false;
  try {
    renderProgress(job);
  } catch (error) {
    console.error("Could not render initial submission status", error);
    $("#progressHeading").textContent = "Submitting requests";
    $("#progressCounts").textContent = "Request accepted. Loading status…";
  }
  void pollJob(job.job_id);
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
  $("#commandPaletteButton").addEventListener("click", openCommandPalette);
  $("#closeCommandPaletteButton").addEventListener("click", () => $("#commandPaletteDialog").close());
  $("#commandPaletteDialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  $("#commandPaletteInput").addEventListener("input", () => {
    state.commandPaletteIndex = 0;
    renderCommandPalette();
  });
  $("#commandPaletteInput").addEventListener("keydown", (event) => {
    const actions = filteredCommandPaletteActions();
    if (event.key === "ArrowDown" && actions.length) {
      event.preventDefault();
      state.commandPaletteIndex = (state.commandPaletteIndex + 1) % actions.length;
      renderCommandPalette();
    } else if (event.key === "ArrowUp" && actions.length) {
      event.preventDefault();
      state.commandPaletteIndex = (state.commandPaletteIndex - 1 + actions.length) % actions.length;
      renderCommandPalette();
    } else if (event.key === "Enter") {
      event.preventDefault();
      runCommandPaletteItem(state.commandPaletteIndex);
    }
  });
  elements.queueSearch.addEventListener("input", () => {
    state.queueSearch = elements.queueSearch.value;
    renderQueue();
  });
  $("#clearQueueSearchButton").addEventListener("click", () => {
    state.queueSearch = "";
    elements.queueSearch.value = "";
    renderQueue();
    focusQueueSearch();
  });
  $("#newRequestButton").addEventListener("click", startNewRequest);
  $("#emptyNewRequestButton").addEventListener("click", startNewRequest);
  $("#emptyAlmImportButton").addEventListener("click", openAlmWorkbookImport);
  $("#saveNewRequestButton").addEventListener("click", saveNewRequest);
  $("#discardNewRequestButton").addEventListener("click", discardNewRequest);
  elements.inspectorContent.addEventListener("keydown", handleInspectorDefaultKey);
  ["input", "change"].forEach((eventName) => {
    $("#requestEditorFields").addEventListener(eventName, () => {
      const request = selectedRequest();
      if (request?.result_state === "failed") clearRequestSubmissionMetadata(request);
    }, true);
  });
  $("#pastePairsButton").addEventListener("click", openPasteDialog);
  $("#addPairsButton").addEventListener("click", addPairs);
  $("#pairsBulkKind").addEventListener("change", applyQuickImportKind);
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
    recordAppEdit();
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
      recordAppEdit();
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
    recordAppEdit();
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
  $$('[data-request-status-list]').forEach((list) => list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-request-status-move]");
    if (button) moveRequestStatus(button);
  }));
  $("#settingsDialog form").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.defaultPrevented || event.isComposing
      || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target;
    if (!(target instanceof HTMLElement)
      || !["INPUT", "SELECT"].includes(target.tagName)
      || target.matches("input[type='checkbox'], input[type='radio']")) return;
    event.preventDefault();
    $("#saveSettingsButton").click();
  });
  $("#saveSettingsButton").addEventListener("click", async () => {
    const button = $("#saveSettingsButton");
    const columns = {
      username: $("#spreadsheetUsernameColumnInput").value.trim(),
      deployment_serial: $("#spreadsheetDeploymentColumnInput").value.trim(),
      returned_device: $("#spreadsheetReturnedColumnInput").value.trim(),
      pending_return: $("#spreadsheetPendingColumnInput").value.trim(),
      enabled: $("#spreadsheetEnabledColumnInput").value.trim(),
      device_allocation: $("#spreadsheetDeviceAllocationColumnInput").value.trim(),
      new_asset_status: $("#spreadsheetNewAssetStatusColumnInput").value.trim(),
      first_name: $("#spreadsheetFirstNameColumnInput").value.trim(),
      last_name: $("#spreadsheetLastNameColumnInput").value.trim(),
    };
    if (!columns.username || !columns.deployment_serial || !columns.pending_return) {
      toast("Set the username, deployment serial, and pending return columns.", "error");
      return;
    }
    const requestStatuses = readRequestStatusSettings();
    const requestStatusError = validateRequestStatusSettings(requestStatuses);
    if (requestStatusError) {
      toast(requestStatusError, "error");
      return;
    }
    const preferences = {
      concurrency: Number(elements.concurrency.value),
      validate_quick_import: $("#validateQuickImportInput").checked,
      validate_workbook_import: $("#validateWorkbookImportInput").checked,
      save_alm_import_drafts: $("#saveAlmImportDraftsInput").checked,
      request_statuses: requestStatuses,
      import_columns: columns,
    };
    button.disabled = true;
    try {
      state.preferences = await api("/api/preferences", {
        method: "POST",
        body: JSON.stringify(preferences),
      });
      if (state.preferences.save_alm_import_drafts === false) {
        state.importDrafts = [];
      } else {
        await loadImportDrafts();
      }
      renderAll();
      localStorage.setItem(IMPORT_COLUMNS_STORAGE_KEY, JSON.stringify(columns));
      localStorage.setItem(CONCURRENCY_STORAGE_KEY, elements.concurrency.value);
      $("#settingsDialog").close();
      toast("Settings saved.", "success");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });
  $("#clearAlmBacklogIgnoredButton").addEventListener("click", async () => {
    if (!confirm("Clear all remembered ALM backlog exclusions?")) return;
    try {
      await api("/api/import/backlog/ignored", { method: "DELETE" });
      toast("Remembered backlog exclusions cleared.", "success");
    } catch (error) {
      toast(error.message, "error");
    }
  });
  $("#importSheetButton").addEventListener("click", openAlmWorkbookImport);
  $("#almBacklogButton").addEventListener("click", openAlmBacklogImport);
  $("#importDialog").addEventListener("close", () => {
    saveCurrentImportDraft({ immediate: true });
    state.importUploadToken += 1;
    setImportBusy(false);
  });
  window.addEventListener("beforeunload", () => saveCurrentImportDraft({ immediate: true }));
  const importForm = $("#importDialog form");
  const importDialog = $("#importDialog");
  importForm.addEventListener("submit", (event) => {
    // The dialog form uses method="dialog" for the intentional Cancel action.
    // Prevent implicit Enter submission from closing the importer and losing
    // an in-progress workbook review.
    if (event.submitter?.value !== "cancel") event.preventDefault();
  });
  importDialog.addEventListener("keydown", (event) => {
    const key = String(event.key || "").toLowerCase();
    const target = event.target;
    const inputType = target instanceof HTMLInputElement ? String(target.type || "text").toLowerCase() : "";
    const editingText = target instanceof HTMLTextAreaElement
      || (target instanceof HTMLInputElement && !["checkbox", "radio", "range", "file", "button", "submit", "reset", "color"].includes(inputType))
      || target?.isContentEditable;
    if (state.importPreview && (event.metaKey || event.ctrlKey) && !event.altKey
      && !editingText && (key === "z" || key === "y")) {
      event.preventDefault();
      if (key === "y" || (key === "z" && event.shiftKey)) redoImportEdit();
      else undoImportEdit();
      return;
    }
  });
  importForm.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    const target = event.target;
    const inputType = target instanceof HTMLInputElement ? String(target.type || "text").toLowerCase() : "";
    const allowsNativeEnter = target instanceof HTMLButtonElement
      || target instanceof HTMLSelectElement
      || target instanceof HTMLTextAreaElement
      || (target instanceof HTMLInputElement && ["button", "submit", "reset", "file"].includes(inputType));
    if (allowsNativeEnter) return;
    event.preventDefault();
    const retry = target instanceof Element
      ? target.closest(".import-inline-edit")?.querySelector("[data-import-retry]")
      : null;
    if (retry) retry.click();
  });
  let importDropDepth = 0;
  importDialog.addEventListener("dragenter", (event) => {
    if (!state.config?.spreadsheet_import_enabled || !fileDrag(event)) return;
    event.preventDefault();
    importDropDepth += 1;
    setImportDialogDropActive(true);
  });
  importDialog.addEventListener("dragover", (event) => {
    if (!state.config?.spreadsheet_import_enabled || !fileDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  importDialog.addEventListener("dragleave", (event) => {
    if (!fileDrag(event)) return;
    importDropDepth = Math.max(0, importDropDepth - 1);
    if (!importDropDepth) setImportDialogDropActive(false);
  });
  importDialog.addEventListener("drop", (event) => {
    if (!state.config?.spreadsheet_import_enabled || !fileDrag(event)) return;
    event.preventDefault();
    importDropDepth = 0;
    setImportDialogDropActive(false);
    const files = Array.from(event.dataTransfer?.files || []);
    if (files.length !== 1) {
      toast("Drop one ALM Workbook at a time.", "error");
      return;
    }
    importDroppedIntoDialog(files[0]);
  });
  $("#workbookInput").addEventListener("change", (event) => {
    if (event.target.files[0]) uploadWorkbook(event.target.files[0]);
  });
  const queuePanel = $(".queue-panel");
  const fileDrag = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
  queuePanel.addEventListener("dragenter", (event) => {
    if (!state.config?.spreadsheet_import_enabled || !fileDrag(event)) return;
    event.preventDefault();
    state.queueDropDepth += 1;
    setQueueWorkbookDropActive(true);
  });
  queuePanel.addEventListener("dragover", (event) => {
    if (!state.config?.spreadsheet_import_enabled || !fileDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  queuePanel.addEventListener("dragleave", (event) => {
    if (!fileDrag(event)) return;
    state.queueDropDepth = Math.max(0, state.queueDropDepth - 1);
    if (!state.queueDropDepth) setQueueWorkbookDropActive(false);
  });
  queuePanel.addEventListener("drop", (event) => {
    if (!state.config?.spreadsheet_import_enabled || !fileDrag(event)) return;
    event.preventDefault();
    state.queueDropDepth = 0;
    setQueueWorkbookDropActive(false);
    const files = Array.from(event.dataTransfer?.files || []);
    if (files.length !== 1) {
      toast("Drop one ALM Workbook at a time.", "error");
      return;
    }
    importDroppedWorkbook(files[0]);
  });
  $("#changeFileButton").addEventListener("click", () => resetImportDialog());
  $("#changeMappedFileButton").addEventListener("click", () => resetImportDialog());
  $("#changeColumnsButton").addEventListener("click", openImportColumnMapping);
  $("#importMapSheet").addEventListener("change", renderImportColumnMap);
  [
    "#importMapUsername",
    "#importMapDeployment",
    "#importMapReturned",
    "#importMapPending",
    "#importMapEnabled",
    "#importMapDeviceAllocation",
    "#importMapNewAssetStatus",
    "#importMapFirstName",
    "#importMapLastName",
  ].forEach((selector) => $(selector).addEventListener("change", updateImportColumnMapButton));
  $("#sheetInput").addEventListener("change", () => {
    updateImportDates();
  });
  $("#openImportDatesButton").addEventListener("click", openImportDateDialog);
  $("#importDateDialogList").addEventListener("change", updateImportDateDialogApplyButton);
  $("#applyImportDatesButton").addEventListener("click", applyImportDateSelection);
  $("#importDatesDialog").addEventListener("close", () => {
    $("#openImportDatesButton").setAttribute("aria-expanded", "false");
  });
  const importDateForm = $("#importDatesDialog form");
  importDateForm.addEventListener("submit", (event) => {
    if (event.submitter?.value !== "cancel") event.preventDefault();
  });
  $("#importDateGroups").addEventListener("change", updateImportCounts);
  $("#almBacklogDaysInput").addEventListener("input", () => {
    updateBacklogDaysLabel();
    saveCurrentImportDraft();
  });
  $("#almBacklogIncludeToday").addEventListener("change", () => saveCurrentImportDraft());
  $$('input[name="importMode"]').forEach((radio) => radio.addEventListener("change", updateImportCounts));
  $("#importCityInput").addEventListener("change", () => {
    state.importLocation = { city: $("#importCityInput").value, building: "", floor: "", room: "", cabinet: "" };
    state.importLocationResults = [];
    updateImportCounts();
  });
  $("#importLocationInput").addEventListener("change", () => {
    const result = state.importLocationResults[Number($("#importLocationInput").value)];
    if (!result) return;
    const [building = "", floor = "", room = "", cabinet = ""] = result.columns;
    state.importLocation = { city: $("#importCityInput").value, building, floor, room, cabinet };
    rememberImportLocation(state.importLocation);
    updateImportCounts();
  });
  $("#requestSizeInput").addEventListener("change", () => changeRequestSize($("#requestSizeInput").value));
  $("#bulkSerialEntryModeButton").addEventListener("click", () => setBulkSerialMode("individual"));
  $("#bulkSerialTextModeButton").addEventListener("click", () => setBulkSerialMode("text"));
  $("#addBulkSerialButton").addEventListener("click", addBulkSerial);
  $("#bulkSerialAddInput").addEventListener("input", () => setBulkSerialEntryError());
  $("#bulkSerialAddInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      addBulkSerial();
    }
  });
  $("#bulkSerialList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-bulk-serial-remove]");
    if (button) removeBulkSerial(Number(button.dataset.bulkSerialRemove));
  });
  $("#removeBulkSerialPrefixesButton").addEventListener("click", removeBulkSerialPrefixes);
  $("#prepareImportButton").addEventListener("click", prepareImport);
  $("#backImportButton").addEventListener("click", backToImportSelection);
  $("#undoImportButton").addEventListener("click", undoImportEdit);
  $("#redoImportButton").addEventListener("click", redoImportEdit);
  $("#clearImportStatusesButton").addEventListener("click", clearImportStatuses);
  elements.reviewButton.addEventListener("click", openReview);
  $("#reviewDialog form").addEventListener("submit", (event) => {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
    if (!$("#submitQueueButton").disabled) submitQueue();
  });
  elements.clearQueueButton.addEventListener("click", () => {
    if (!state.queue.length || confirm(`Remove all ${state.queue.length} prepared requests?`)) {
      recordAppEdit();
      state.queue = [];
      state.selectedId = null;
      renderAll();
    }
  });
  bindConnectionSheetEvents();
  elements.connectionStatus.addEventListener("click", () => {
    // Keep the successful state compact, but make it the reconnect control the
    // moment the user needs to refresh a stale Helix session.
    void connect();
  });
  elements.historyButton.addEventListener("click", openHistory);
  $("#historySearchInput").addEventListener("input", () => renderHistory(state.historyRuns));
  $("#historyStateFilter").addEventListener("change", () => renderHistory(state.historyRuns));
  $("#duplicateButton").addEventListener("click", duplicateSelected);
  $("#closeInspectorButton").addEventListener("click", () => {
    if (state.newRequest) {
      discardNewRequest();
      return;
    }
    state.selectedId = null;
    renderAll();
  });
  $("#requestCreateDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    discardNewRequest();
  });
  $("#removeButton").addEventListener("click", () => removeRequest(state.selectedId));
  $("#searchSerialButton").addEventListener("click", searchAssets);
  $("#resetSerialSelection").addEventListener("click", () => resetLookupSelection("serial"));
  $("#validateBulkSerialButton").addEventListener("click", async () => {
    const request = selectedRequest();
    if (!request || request.kind !== "bulk_location" || !request.serials.length) return;
    await validateBulkSerials({ force: true, requests: [request], render: false });
    refreshSelectedValidation();
    renderQueue();
  });
  $("#searchUserButton").addEventListener("click", () => searchUsers(false));
  $("#searchReturningButton").addEventListener("click", () => searchUsers(true));
  $("#resetUserSelection").addEventListener("click", () => resetLookupSelection("user"));
  $("#resetReturningSelection").addEventListener("click", () => resetLookupSelection("returning_user"));
  $("#loadLocationsButton").addEventListener("click", loadLocations);
  elements.serialInput.addEventListener("input", () => {
    hideSearchResults();
    setLookupStatus("serial", "");
    const request = selectedRequest();
    if (!request) return;
    recordAppInputEdit(`${request.id}:serial`);
    const value = elements.serialInput.value.trim();
    request.serial_validation_epoch = Number(request.serial_validation_epoch || 0) + 1;
    request.serial_selected = false;
    request.serials = value ? [value] : [];
    request.serial_validation = request.serials.length ? "pending" : "empty";
    request.serial_validation_error = "";
    request.cached_serial_verification = false;
    if (value) setLookupInputStatus("serial", value);
    if (request.serials.length) scheduleValidation(request, "serial", () => loadSerialSuggestions(request, value, { requireSelection: true }));
    refreshSelectedValidation();
    updateLookupControlStates(request);
    renderQueue();
  });
  elements.serialsInput.addEventListener("input", () => {
    const request = selectedRequest();
    if (!request) return;
    recordAppInputEdit(`${request.id}:serials`);
    request.serials = parseSerials(elements.serialsInput.value);
    request.bulk_validation_epoch = Number(request.bulk_validation_epoch || 0) + 1;
    request.bulk_validation = request.serials.length
      ? "pending"
      : "empty";
    request.bulk_validation_error = "";
    request.bulk_validation_missing = [];
    request.bulk_serial_states = {};
    request.bulk_serial_errors = {};
    elements.serialHint.textContent = request.serials.length + " serial" + (request.serials.length === 1 ? "" : "s");
    renderBulkSerialEditor(request);
    refreshBulkValidationButton(request);
    refreshSelectedValidation();
    renderQueue();
  });
  elements.statusInput.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request) return;
    recordAppEdit();
    applyInferredKind(request, elements.statusInput.value);
    renderAll();
  });
  elements.userInput.addEventListener("input", () => {
    hideSearchResults();
    setLookupStatus("user", "");
    const request = selectedRequest();
    if (!request) return;
    recordAppInputEdit(`${request.id}:user`);
    request.user = elements.userInput.value.trim();
    request.user_validation_epoch = Number(request.user_validation_epoch || 0) + 1;
    request.user_selected = false;
    request.user_validation = request.user ? "pending" : "empty";
    request.user_validation_error = "";
    request.cached_user_verification = false;
    if (request.user) setLookupInputStatus("user", request.user);
    if (request.user) scheduleValidation(request, "user", () => validateUserAfterPause(request, false));
    refreshSelectedValidation();
    updateLookupControlStates(request);
    renderQueue();
  });
  elements.cityInput.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request) return;
    recordAppEdit();
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
    recordAppEdit();
    const [building = "", floor = "", room = "", cabinet = ""] = result.columns;
    request.location = { city: elements.cityInput.value, building, floor, room, cabinet };
    elements.locationDetail.textContent = "";
    renderQueue();
  });
  elements.returningToggle.addEventListener("change", () => {
    const request = selectedRequest();
    if (!request) return;
    recordAppEdit();
    request.returning = elements.returningToggle.checked;
    if (!request.returning) {
      request.returning_user = "";
      request.returning_user_selected = false;
      request.returning_user_info = null;
      request.returning_user_validation = "empty";
      request.returning_user_validation_error = "";
      request.returning_user_loading = false;
    }
    renderInspector();
    renderQueue();
  });
  elements.returningUserInput.addEventListener("input", () => {
    hideSearchResults();
    setLookupStatus("returning", "");
    const request = selectedRequest();
    if (!request) return;
    recordAppInputEdit(`${request.id}:returning-user`);
    request.returning_user = elements.returningUserInput.value.trim();
    request.returning_user_info = null;
    request.returning_user_validation_epoch = Number(request.returning_user_validation_epoch || 0) + 1;
    request.returning_user_selected = false;
    request.returning_user_validation = request.returning_user ? "pending" : "empty";
    request.returning_user_validation_error = "";
    request.cached_user_verification = false;
    request.returning_user_loading = Boolean(request.returning_user);
    if (request.returning_user) setLookupInputStatus("returning", request.returning_user);
    if (request.returning_user) scheduleValidation(request, "returning_user", () => validateUserAfterPause(request, true));
    renderReturningUserInfo(request);
    refreshSelectedValidation();
    updateLookupControlStates(request);
    renderQueue();
  });
  elements.serialInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !$("#searchSerialButton").disabled) { event.preventDefault(); searchAssets(); }
  });
  elements.userInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !$("#searchUserButton").disabled) { event.preventDefault(); searchUsers(false); }
  });
  elements.returningUserInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !$("#searchReturningButton").disabled) { event.preventDefault(); searchUsers(true); }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && [elements.serialResults, elements.userResults, elements.returningResults].some((node) => !node.hidden)) {
      hideSearchResults();
    }
  });
  document.addEventListener("keydown", (event) => {
    const key = String(event.key || "").toLowerCase();
    const mac = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
    const modifier = mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
    const editable = event.target instanceof Element
      && Boolean(event.target.closest("input, textarea, select, [contenteditable='true']"));
    const openDialog = $("dialog[open]");
    if (modifier && key === "k" && !event.altKey && !event.shiftKey
      && (!openDialog || openDialog.id === "commandPaletteDialog")) {
      event.preventDefault();
      if (openDialog?.id === "commandPaletteDialog") $("#commandPaletteInput")?.focus();
      else openCommandPalette();
      return;
    }
    if (!editable && !openDialog && !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey) {
      if (key === "n") {
        event.preventDefault();
        startNewRequest();
        return;
      }
      if (key === "b") {
        event.preventDefault();
        startNewBulkRequest();
        return;
      }
      if (key === "a" && state.config?.spreadsheet_import_enabled) {
        event.preventDefault();
        openAlmWorkbookImport();
        return;
      }
    }
    if (!modifier || event.altKey || editable || $("#importDialog").open
      || (openDialog && !["requestCreateDialog", "pasteDialog"].includes(openDialog.id))) return;
    const redo = key === "y" || (key === "z" && event.shiftKey);
    if (key !== "z" && key !== "y") return;
    event.preventDefault();
    if (redo) redoAppEdit();
    else undoAppEdit();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest(".search-control, .search-results")) hideSearchResults();
  });
  $("#doneButton").addEventListener("click", () => $("#progressDialog").close());
  $("#closeProgressButton").addEventListener("click", () => $("#progressDialog").close());
  $("#viewSubmissionButton").addEventListener("click", showProgressDialog);
  $("#progressDialog").addEventListener("close", () => {
    if (state.currentJob?.state === "finished") finalizeCurrentSubmission();
    else renderSubmissionNotice();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.currentJob && state.currentJob.state !== "finished") {
      scheduleJobPoll(state.currentJob.job_id, 0);
    }
  });
  document.addEventListener("keydown", (event) => {
    const mac = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
    const shortcut = mac
      ? (event.altKey && !event.metaKey && !event.ctrlKey)
      : (event.ctrlKey && !event.metaKey && !event.altKey);
    const editable = event.target instanceof Element
      && Boolean(event.target.closest("input, textarea, select, [contenteditable='true']"));
    const dialogOpen = Boolean($("dialog[open]"));

    if (!shortcut && !event.altKey && !editable && !dialogOpen && event.key === "?") {
      event.preventDefault();
      openShortcuts();
      return;
    }
    if (!shortcut || dialogOpen) return;

    if (event.code === "Enter" && !elements.reviewButton.disabled) {
      event.preventDefault();
      openReview();
      return;
    }
    if (editable) return;

    const code = event.code;
    if (event.shiftKey && code === "KeyH") {
      event.preventDefault();
      openHistory();
      return;
    }
    if (event.shiftKey) return;

    if (code === "KeyN") {
      event.preventDefault();
      startNewRequest();
    } else if (code === "KeyI") {
      event.preventDefault();
      openPasteDialog();
    } else if (code === "KeyO" && state.config?.spreadsheet_import_enabled) {
      event.preventDefault();
      openAlmWorkbookImport();
    } else if (code === "Comma") {
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
  bindConnectionSheetEvents();
  renderConnectionSheet();
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
    await loadPersistedQueue();
    if (state.preferences.save_alm_import_drafts !== false) await loadImportDrafts();
    const spreadsheetEnabled = Boolean(state.config.spreadsheet_import_enabled);
    $("#importSheetButton").hidden = !spreadsheetEnabled;
    $("#emptyAlmImportButton").hidden = !spreadsheetEnabled;
    const spreadsheetSettings = $('[data-settings-tab="spreadsheet"]');
    if (spreadsheetSettings) spreadsheetSettings.hidden = !spreadsheetEnabled;
    configureConcurrency(state.config.concurrency);
    bindEvents();
    await window.autoAnimateReady;
    setupOptionalListAnimation();
    renderAll();
    await restoreSubmissionFromHistory();
    renderConnectionSheet();
    await refreshConnection({ verify: true });
    state.connectionHeartbeatTimer = window.setInterval(checkConnection, 30_000);
    renderAll();
  } catch (error) {
    document.body.innerHTML = `<main class="empty-state"><h1>AutoEUDM could not start</h1><p>${escapeHtml(error.message)}</p></main>`;
  }
}

init();
