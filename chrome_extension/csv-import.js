// Shared CSV import helpers used by both the popup and the viewer.

window.CORRECTIONS_HEADER = ["asset_tag", "field", "label", "current", "expected", "status"];

// Minimal RFC-4180 CSV parser (handles quotes, commas, newlines).
window.parseCSV = function (text) {
  const rows = [];
  let row = [], field = "", inQuotes = false;
  text = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += ch;
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(field); field = "";
    } else if (ch === "\n") {
      row.push(field); rows.push(row); row = []; field = "";
    } else field += ch;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((c) => c !== ""));
};

window.rowsToObjects = function (matrix) {
  if (!matrix.length) return [];
  const header = matrix[0].map((h) => h.trim().toLowerCase());
  return matrix.slice(1).map((cols) => {
    const obj = {};
    header.forEach((h, i) => { obj[h] = cols[i] !== undefined ? cols[i] : ""; });
    return obj;
  });
};

// Read + parse + validate a corrections CSV file. cb(rows, errorMessage).
window.parseCorrectionsCSV = function (file, cb) {
  const reader = new FileReader();
  reader.onload = function () {
    const matrix = window.parseCSV(String(reader.result));
    const header = (matrix[0] || []).map((h) => h.trim().toLowerCase());
    if (!window.CORRECTIONS_HEADER.every((h) => header.includes(h))) {
      cb(null, "This doesn't look like a corrections export.\nExpected columns: " +
               window.CORRECTIONS_HEADER.join(", "));
      return;
    }
    cb(window.rowsToObjects(matrix), null);
  };
  reader.onerror = function () { cb(null, "Could not read the file."); };
  reader.readAsText(file);
};

// Replace stored corrections with a fresh import — removes the OLD data first,
// then writes the new rows + import timestamp. cb() when done.
window.storeCorrections = function (rows, cb) {
  chrome.storage.local.remove(["corrections", "importedAt"], function () {
    chrome.storage.local.set(
      { corrections: rows, importedAt: new Date().toISOString() },
      cb || function () {}
    );
  });
};
