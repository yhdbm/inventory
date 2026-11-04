// Shared field/strategy definitions used by both the popup and the strategies
// settings page. Loaded as a plain script (defines globals on window).

// The two ways the extension can set a value in ServiceNow's inline list edit.
window.STRATEGY_OPTIONS = [
  { value: "dropdown", label: "Dropdown (select option)" },
  { value: "input", label: "Text input (type value)" },
];

// All auditable fields, by their ServiceNow column label (matches the CSV).
window.STRATEGY_FIELDS = [
  { key: "state", label: "State" },
  { key: "substate", label: "Substate" },
  { key: "disposal_reason", label: "Disposal reason" },
  { key: "location", label: "Location" },
  { key: "department", label: "Department" },
  { key: "gl_account", label: "GL account" },
  { key: "assigned_to", label: "Assigned to" },
  { key: "managed_by", label: "Managed by" },
  { key: "stockroom", label: "Stockroom" },
  { key: "station_id", label: "Station ID" },
  { key: "name", label: "Name" },
  { key: "display_name", label: "Display name" },
  { key: "model_category", label: "Model category" },
  { key: "manufacturer", label: "Manufacturer" },
  { key: "model", label: "Model" },
  { key: "comments", label: "Comments" },
];

// Hardcoded defaults (can be changed by the user, and rolled back to these).
// Choice fields default to dropdown; string/reference fields to text input.
window.STRATEGY_DEFAULTS = {
  state: "dropdown",
  substate: "dropdown",
  disposal_reason: "input",
  location: "dropdown",
  department: "dropdown",
  gl_account: "input",
  assigned_to: "dropdown",
  managed_by: "dropdown",
  stockroom: "input",
  station_id: "input",
  name: "input",
  display_name: "input",
  model_category: "input",
  manufacturer: "input",
  model: "input",
  comments: "input",
};

// Load the effective config (saved overrides merged onto the defaults).
window.loadStrategies = function (cb) {
  chrome.storage.local.get("strategies", function (data) {
    const saved = (data && data.strategies) || {};
    const merged = {};
    window.STRATEGY_FIELDS.forEach(function (f) {
      merged[f.key] = saved[f.key] || window.STRATEGY_DEFAULTS[f.key];
    });
    cb(merged);
  });
};
