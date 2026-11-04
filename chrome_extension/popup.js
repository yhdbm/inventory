const $ = (s) => document.querySelector(s);

let rows = [];

// Show the build version (with hour) so it's easy to confirm the right load.
(function () {
  const m = chrome.runtime.getManifest();
  const b = document.getElementById("build");
  if (b) b.textContent = m.version_name || ("v" + m.version);
})();

// --- Show the version (import date/time) + counts --------------------------
function refreshVersion(importedAt) {
  const ver = $("#ver");
  if (rows.length) {
    const assets = new Set(
      rows.map((r) => (r.asset_tag || "").trim()).filter(Boolean)
    ).size;
    const changes = rows.filter((r) => r.field && (r.current || "") !== (r.expected || "")).length;
    const when = importedAt ? new Date(importedAt).toLocaleString() : "unknown";
    ver.innerHTML =
      `<b>Imported:</b> ${when}<br>` +
      `<b>${rows.length}</b> rows · <b>${assets}</b> assets · <b>${changes}</b> changes`;
    $("#apply").disabled = changes === 0;
    $("#open-sn").disabled = uniqueTags().length === 0;
  } else {
    ver.textContent = "No file imported yet — choose a corrections CSV below.";
    $("#apply").disabled = true;
    $("#open-sn").disabled = true;
  }
}

// --- ServiceNow list link from the imported asset tags (same as the viewer) --
const SN_BASE = "https://your-instance.service-now.com/now/nav/ui/classic/params/target/";

function uniqueTags() {
  const seen = new Set();
  const out = [];
  for (const r of rows) {
    const t = (r.asset_tag || "").trim();
    if (t && !seen.has(t)) { seen.add(t); out.push(t); }
  }
  return out;
}

function buildServiceNowUrl(tags) {
  const inner = tags.map((t) => "asset_tag=" + t).join("^OR");
  const target = "alm_asset_list.do?sysparm_query=" + encodeURIComponent(inner) +
                 "&sysparm_first_row=1&sysparm_view=";
  return SN_BASE + encodeURIComponent(target);
}

chrome.storage.local.get(["corrections", "importedAt"], function (data) {
  rows = (data && data.corrections) || [];
  refreshVersion(data && data.importedAt);
});

// --- Per-field apply toggles (slow reference fields the user may bypass) -----
// Maps a checkbox to the lowercased field label it controls. Unchecked = bypass.
const FIELD_TOGGLES = [
  { id: "opt-assigned-to", label: "assigned to" },
  { id: "opt-managed-by", label: "managed by" },
];

function saveToggles() {
  const out = {};
  FIELD_TOGGLES.forEach((t) => {
    const el = document.getElementById(t.id);
    if (el) out[t.label] = el.checked;
  });
  chrome.storage.local.set({ fieldToggles: out });
}

chrome.storage.local.get(["fieldToggles"], function (data) {
  const saved = (data && data.fieldToggles) || {};
  FIELD_TOGGLES.forEach((t) => {
    const el = document.getElementById(t.id);
    if (!el) return;
    if (t.label in saved) el.checked = !!saved[t.label];   // restore last choice
    el.addEventListener("change", saveToggles);
  });
});

// Labels the user has chosen to bypass on this run (checkbox unchecked).
function bypassedLabels() {
  return new Set(
    FIELD_TOGGLES
      .filter((t) => { const el = document.getElementById(t.id); return el && !el.checked; })
      .map((t) => t.label)
  );
}

// Import a CSV straight from the popup (replaces any previous data).
$("#import").addEventListener("change", function (e) {
  const file = e.target.files[0];
  if (!file) return;
  parseCorrectionsCSV(file, function (parsed, err) {
    if (err) { $("#status").innerHTML = "<span style='color:#dc2626'>" + err + "</span>"; return; }
    storeCorrections(parsed, function () {
      rows = parsed;
      refreshVersion(new Date().toISOString());
      $("#status").innerHTML = "<span style='color:#16a34a'>Imported " + parsed.length + " rows (old data removed).</span>";
    });
  });
});

$("#open-sn").addEventListener("click", function () {
  const tags = uniqueTags();
  if (!tags.length) { $("#status").textContent = "No asset tags to open."; return; }
  chrome.tabs.create({ url: buildServiceNowUrl(tags) });
  window.close();
});

$("#open").addEventListener("click", function () {
  chrome.tabs.create({ url: chrome.runtime.getURL("viewer.html") });
  window.close();
});

$("#strategies").addEventListener("click", function () {
  chrome.tabs.create({ url: chrome.runtime.getURL("strategies.html") });
  window.close();
});

// --- Apply expected values onto the active ServiceNow tab ------------------
$("#apply").addEventListener("click", async function () {
  const status = $("#status");

  // Build plan: { assetTag: { columnLabelLower: expectedValue } }
  const bypass = bypassedLabels();
  let bypassed = 0;
  const plan = {};
  for (const r of rows) {
    if (!r.field) continue;                                   // skip no-change rows
    if ((r.current || "") === (r.expected || "")) continue;   // only real changes
    const tag = (r.asset_tag || "").trim();
    const label = (r.label || r.field || "").trim().toLowerCase();
    if (!tag || !label) continue;
    if (bypass.has(label)) { bypassed += 1; continue; }       // user chose to skip this field
    (plan[tag] = plan[tag] || {})[label] = r.expected || "";
  }
  if (!Object.keys(plan).length) {
    status.textContent = bypassed
      ? `Nothing to apply (${bypassed} change(s) bypassed).`
      : "Nothing to apply.";
    return;
  }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) { status.textContent = "No active tab."; return; }
  if (!/https:\/\/acme\.service-now\.com\//.test(tab.url || "")) {
    status.innerHTML = "<span style='color:#dc2626'>Open the ServiceNow asset list tab first.</span>";
    return;
  }

  // Field apply order (labels, lowercased). State precedes Substate, etc.
  const fieldOrder = STRATEGY_FIELDS.map((f) => f.label.trim().toLowerCase());

  status.innerHTML = "Applying… (please wait)";
  $("#apply").disabled = true;
  try {
    // Inject the editing engine (covers already-open tabs).
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      files: ["content.js"],
    });
    // Kick off the run via a message so it keeps going after the popup closes,
    // then close the popup so it doesn't cover the ServiceNow list. Progress and
    // results are shown by the on-page overlay (with its Stop button).
    chrome.tabs.sendMessage(tab.id, { type: "INV_APPLY", plan, fieldOrder }).catch(() => {});
    window.close();
  } catch (e) {
    status.innerHTML = "<span style='color:#dc2626'>" + (e.message || "Injection failed") + "</span>";
    $("#apply").disabled = false;
  }
});
