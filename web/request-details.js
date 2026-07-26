const params = new URLSearchParams(location.search);
const requestId = params.get("request_id") || "";

function text(id, value) {
  document.getElementById(id).textContent = value || "—";
}

function addDetail(label, value) {
  const grid = document.getElementById("detailsGrid");
  const term = document.createElement("dt");
  const detail = document.createElement("dd");
  term.textContent = label;
  detail.textContent = value || "—";
  grid.append(term, detail);
}

function findPrimaryRequest(details) {
  return Array.isArray(details?.requests) ? details.requests[0] || {} : {};
}

async function loadDetails(attempt = 0) {
  if (!requestId) {
    text("detailsHeading", "Request ID missing");
    text("detailsStatus", "Open this page from a completed AutoEUDM run.");
    return;
  }
  text("detailsHeading", `Request ${requestId}`);
  try {
    const response = await fetch(`/api/requests/${encodeURIComponent(requestId)}/details`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Request details could not be loaded.");
    const details = payload.details || {};
    const request = findPrimaryRequest(details);
    text("detailsHeading", request.displayId ? `Request ${request.displayId}` : `Request ${requestId}`);
    text("detailsStatus", [details.title, details.subtitle].filter(Boolean).join(" · "));
    const grid = document.getElementById("detailsGrid");
    grid.hidden = false;
    addDetail("AutoEUDM request ID", requestId);
    addDetail("EUDM display ID", request.displayId);
    addDetail("Status", request.status || details.state);
    addDetail("Order ID", details.orderId);
    addDetail("Updated", details.updateTime ? new Date(details.updateTime).toLocaleString() : "");
    addDetail("Requested for", request.requestedFor?.displayName || request.requestedFor?.loginId);
    addDetail("Requested by", request.requestedBy?.displayName || request.requestedBy?.loginId);
    document.getElementById("activityLink").href = payload.eudm_activity_url;
    document.getElementById("rawDetails").hidden = false;
    text("rawDetailsText", JSON.stringify(details, null, 2));
  } catch (error) {
    if (attempt < 5) {
      text("detailsStatus", "EUDM is still publishing this request. Retrying…");
      await new Promise((resolve) => setTimeout(resolve, 900 + attempt * 300));
      return loadDetails(attempt + 1);
    }
    const node = document.getElementById("detailsError");
    node.hidden = false;
    node.textContent = error.message;
    text("detailsStatus", "Reconnect to EUDM in the main AutoEUDM window, then reload this page.");
  }
}

loadDetails();
