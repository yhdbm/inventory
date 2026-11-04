(function () {
  if (window.__VTBE_LOADED__) return;
  window.__VTBE_LOADED__ = true;

  const VTBE_TIMING = {
    betweenRowsPause: 120,
    betweenRulesPause: 60,
    maxRetriesPerCell: 3,
    pauseEveryNRows: 8,
    longPauseEveryNRows: 350,
    pollInterval: 50,
    editorOpenTimeout: 3000,
    editorReadyTimeout: 3000,
    autocompleteTimeout: 3000,
    saveTimeout: 4500,
    shortSaveTimeout: 1200,
    legacyAutocompletePause: 450,
    retryBackoff: 150,
    surgicalBetweenFieldsPause: 100,
    tabSwitchPause: 250
  };

  const VTBE_STATE = {
    stopRequested: false,
    running: false,
    mode: null
  };

  const SURGICAL_TAB_MAP = {
    'acquisition method': 'financial',
    'cost': 'financial',
    'cost center': 'financial',
    'expenditure type': 'financial',
    'gl account': 'financial',
    'invoice number': 'financial',
    'opened': 'financial',
    'po number': 'financial',
    'request line': 'financial',
    'vendor': 'financial'
  };

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function waitForCondition(checkFn, { timeout = VTBE_TIMING.saveTimeout, interval = VTBE_TIMING.pollInterval } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      if (VTBE_STATE.stopRequested) return null;
      try {
        const result = checkFn();
        if (result) return result;
      } catch (error) {}
      await sleep(interval);
    }
    return null;
  }

  function normalizeText(value) {
    return (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  }

  function normalizeFieldToken(value) {
    return normalizeText(value).replace(/[^a-z0-9]+/g, ' ').trim();
  }

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function dispatchMouseSequence(target, type) {
    const options = { bubbles: true, cancelable: true, view: window };
    target.dispatchEvent(new MouseEvent('pointerdown', options));
    target.dispatchEvent(new MouseEvent('mousedown', options));
    target.dispatchEvent(new MouseEvent('mouseup', options));
    target.dispatchEvent(new MouseEvent('click', options));
    if (type === 'dblclick') {
      target.dispatchEvent(new MouseEvent('pointerdown', options));
      target.dispatchEvent(new MouseEvent('mousedown', options));
      target.dispatchEvent(new MouseEvent('mouseup', options));
      target.dispatchEvent(new MouseEvent('click', options));
      target.dispatchEvent(new MouseEvent('dblclick', options));
    }
  }

  function clickElement(target) {
    if (!target) return;
    dispatchMouseSequence(target, 'click');
    if (typeof target.click === 'function') target.click();
  }

  function dispatchKeyboard(target, key, extra = {}) {
    target.dispatchEvent(new KeyboardEvent('keydown', { key, code: key, bubbles: true, ...extra }));
    target.dispatchEvent(new KeyboardEvent('keypress', { key, code: key, bubbles: true, ...extra }));
    target.dispatchEvent(new KeyboardEvent('keyup', { key, code: key, bubbles: true, ...extra }));
  }

  function selectAllText(control) {
    control.focus();
    const isMac = /Mac|iPhone|iPad|iPod/.test(navigator.platform);
    dispatchKeyboard(control, 'a', isMac ? { metaKey: true } : { ctrlKey: true });
    if (typeof control.select === 'function') control.select();
    if (typeof control.setSelectionRange === 'function') {
      const currentValue = control.value || '';
      control.setSelectionRange(0, currentValue.length);
    }
    if (control.isContentEditable) {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(control);
      selection.removeAllRanges();
      selection.addRange(range);
    }
  }

  function clearEditorValue(control) {
    if (control.isContentEditable) {
      selectAllText(control);
      document.execCommand('delete', false);
      control.textContent = '';
      return;
    }
    selectAllText(control);
    setNativeValue(control, '');
    control.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function typeTextLikeUser(control, value) {
    if (control.isContentEditable) {
      control.focus();
      document.execCommand('insertText', false, value);
      if (normalizeText(control.textContent) !== normalizeText(value)) control.textContent = value;
      control.dispatchEvent(new Event('input', { bubbles: true }));
      control.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }
    control.focus();
    setNativeValue(control, value);
    control.dispatchEvent(new Event('input', { bubbles: true }));
    control.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function setNativeValue(element, value) {
    const prototype = Object.getPrototypeOf(element);
    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
    if (descriptor && descriptor.set) descriptor.set.call(element, value);
    else element.value = value;
  }

  function replaceEditorValue(control, value) {
    const tag = control.tagName.toLowerCase();
    if (tag === 'select') {
      const option = Array.from(control.options).find(
        (opt) => normalizeText(opt.textContent) === normalizeText(value) || normalizeText(opt.value) === normalizeText(value)
      );
      control.focus();
      control.value = option ? option.value : value;
      control.dispatchEvent(new Event('input', { bubbles: true }));
      control.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    clearEditorValue(control);
    typeTextLikeUser(control, value);
    return true;
  }

  function findAutocompleteOptions() {
    const selectors = [
      '#AC\\.cell_edit_value li',
      '#AC li',
      '#ac_reference_input_list li',
      'ul.ui-autocomplete li',
      '.ui-autocomplete li',
      '.ac_results li',
      '.ac_cell',
      '.autocomplete li'
    ];
    const options = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)));
    return Array.from(new Set(options)).filter((element) => isVisible(element) && normalizeText(element.textContent));
  }

  function waitForAutocompleteOption() {
    return waitForCondition(() => {
      const options = findAutocompleteOptions();
      return options[0] || null;
    }, { timeout: VTBE_TIMING.autocompleteTimeout });
  }

  function findCandidateTables() {
    const selectors = ['table', '[role="grid"]', '[role="table"]', '[aria-rowcount]', '.table', '.list_table', '.list2_table', 'table.list_table'];
    const candidates = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)));
    const unique = Array.from(new Set(candidates)).filter(isVisible);
    return unique.filter((table) => getHeaderCells(table).length > 0);
  }

  function getHeaderCells(table) {
    const selectors = ['thead th', 'thead [role="columnheader"]', '[role="columnheader"]', 'th', 'tr.list_header th', 'tr.column_head th', '.list_hdr', '.column_head'];
    for (const selector of selectors) {
      const cells = Array.from(table.querySelectorAll(selector)).filter(isVisible);
      if (cells.length) return cells;
    }
    return [];
  }

  function getColumnTarget(table, wantedHeaderText) {
    const wanted = normalizeText(wantedHeaderText);
    const headers = getHeaderCells(table);
    for (let index = 0; index < headers.length; index += 1) {
      const header = headers[index];
      const text = normalizeText(header.innerText || header.textContent);
      const ariaIndex = parseInt(header.getAttribute('aria-colindex') || '', 10);
      const glideField = normalizeText(header.getAttribute('glide_field') || header.dataset?.glideField || '');
      const nameAttr = normalizeText(header.getAttribute('name') || '');
      if (!text && !glideField && !nameAttr) continue;
      if (
        text === wanted || text.includes(wanted) || wanted.includes(text) ||
        glideField === wanted || glideField.includes(wanted) ||
        nameAttr === wanted || nameAttr.includes(wanted)
      ) {
        return { header, positionIndex: index, ariaIndex: Number.isFinite(ariaIndex) ? ariaIndex : null };
      }
    }
    return null;
  }

  function getAllRows(table) {
    let rows = Array.from(table.querySelectorAll('tbody tr')).filter(isVisible);
    if (!rows.length) rows = Array.from(table.querySelectorAll('tr.list_row')).filter(isVisible);
    if (!rows.length) rows = Array.from(table.querySelectorAll('tr')).filter(isVisible);
    if (!rows.length) rows = Array.from(table.querySelectorAll('[role="row"]')).filter(isVisible);
    return rows.filter((row) => !row.closest('thead') && getRowCells(row).length > 0);
  }

  function getRowCells(row) {
    let cells = Array.from(row.querySelectorAll('td')).filter(isVisible);
    if (!cells.length) {
      cells = Array.from(row.querySelectorAll('[role="gridcell"], [role="cell"]')).filter(isVisible);
    }
    return cells;
  }

  function getCellForRow(row, columnTarget) {
    const cells = getRowCells(row);
    if (columnTarget.ariaIndex) {
      const ariaMatch = cells.find((cell) => parseInt(cell.getAttribute('aria-colindex') || '', 10) === columnTarget.ariaIndex);
      if (ariaMatch) return ariaMatch;
    }
    return cells[columnTarget.positionIndex] || null;
  }

  function getServiceNowEditorCell(originalCell) {
    return document.querySelector('td.vt.list_edit_cursor_cell') || document.querySelector('td.list_edit_cursor_cell') || originalCell;
  }

  function findEditableTarget(cell) {
    if (!cell) return null;
    const selectors = [
      '#cell_edit_value',
      '#cell_edit_window input',
      '#cell_edit_window textarea',
      '#cell_editor_value',
      '.list_edit_input',
      '.cell_edit_value',
      'input[id*="cell_edit"]',
      'textarea[id*="cell_edit"]',
      'input[id*="sys_display"]'
    ];
    for (const selector of selectors) {
      const match = document.querySelector(selector);
      if (match && isVisible(match)) return match;
    }
    if (cell.matches('input, textarea, select, [contenteditable="true"]')) return cell;
    const inside = cell.querySelector('input, textarea, select, [contenteditable="true"]');
    if (inside) return inside;
    return document.querySelector('input:focus, textarea:focus, select:focus, [contenteditable="true"]:focus');
  }

  function waitForEditorCell(originalCell) {
    return waitForCondition(() => {
      const active = getServiceNowEditorCell(originalCell);
      return active && isVisible(active) ? active : null;
    }, { timeout: VTBE_TIMING.editorOpenTimeout });
  }

  function waitForEditableTarget(cell) {
    return waitForCondition(() => {
      const target = findEditableTarget(cell);
      return target && isVisible(target) ? target : null;
    }, { timeout: VTBE_TIMING.editorReadyTimeout });
  }

  function getNearbyActionRoot(control, cell) {
    return control.closest('tr, td, .input-group, .form-group, .editable, .input_controls, .form-control, #cell_edit_window') ||
      cell.closest('tr, td, .input-group, .form-group, .editable, .input_controls, .form-control') ||
      cell.parentElement ||
      document;
  }

  function scoreConfirmCandidate(element) {
    if (!element) return 0;
    if (element.id === 'cell_edit_ok') return 100;

    const text = normalizeText([
      element.getAttribute('aria-label'),
      element.getAttribute('title'),
      element.getAttribute('data-original-title'),
      element.getAttribute('data-action-name'),
      element.getAttribute('class'),
      element.textContent
    ].filter(Boolean).join(' '));

    let score = 0;
    if (/save|update|confirm|apply|submit|done|ok/.test(text)) score += 8;
    if (/check|accept|approve|complete/.test(text)) score += 6;
    if (/success|primary|icon-check|fa-check|checkmark|check-circle/.test(text)) score += 4;
    return score;
  }

  function findConfirmButton(control, cell) {
    const explicit = document.getElementById('cell_edit_ok');
    if (explicit && isVisible(explicit)) return explicit;

    const root = getNearbyActionRoot(control, cell);
    const selectors = ['button', '[role="button"]', 'a', 'span[role="button"]', 'i', 'svg'].join(',');
    const candidates = Array.from(root.querySelectorAll(selectors))
      .filter(isVisible)
      .map((element) => element.closest('button, [role="button"], a, span[role="button"]') || element);

    const unique = Array.from(new Set(candidates));
    const scored = unique
      .map((element) => ({ element, score: scoreConfirmCandidate(element) }))
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score);

    return scored[0]?.element || null;
  }

  function waitForConfirmButton(control, cell) {
    return waitForCondition(() => {
      const button = findConfirmButton(control, cell);
      return button && isVisible(button) ? button : null;
    }, { timeout: VTBE_TIMING.editorReadyTimeout });
  }

  async function waitForCellValue(cell, expectedValue, timeout = VTBE_TIMING.saveTimeout) {
    const expected = normalizeText(expectedValue);
    const matched = await waitForCondition(() => {
      const latestText = normalizeText(cell?.textContent || '');
      return latestText === expected ? cell : null;
    }, { timeout });
    return !!matched;
  }

  async function waitForAppliedValue(control, expectedValue, timeout = VTBE_TIMING.editorReadyTimeout) {
    const expected = normalizeText(expectedValue);
    const applied = await waitForCondition(() => {
      const current = normalizeText(control.value || control.textContent || '');
      return current === expected ? true : null;
    }, { timeout });
    return !!applied;
  }

  async function commitListEdit(control, cell, expectedValue, behavior) {
    if (behavior === 'autocomplete_legacy_first') {
      const confirmButton = await waitForConfirmButton(control, cell);
      if (!confirmButton) return false;
      clickElement(confirmButton);
      return waitForCellValue(cell, expectedValue);
    }

    dispatchKeyboard(control, 'Enter');
    let saved = await waitForCellValue(cell, expectedValue, VTBE_TIMING.shortSaveTimeout);
    if (saved) return true;

    const confirmButton = await waitForConfirmButton(control, cell);
    if (confirmButton) {
      clickElement(confirmButton);
      saved = await waitForCellValue(cell, expectedValue);
      if (saved) return true;
    }

    dispatchKeyboard(control, 'Tab');
    if (typeof control.blur === 'function') control.blur();
    cell.dispatchEvent(new Event('blur', { bubbles: true }));
    cell.click();
    return waitForCellValue(cell, expectedValue);
  }

  async function applyListBehavior(control, value, behavior) {
    if (behavior === 'autocomplete_legacy_first') {
      const typedValue = value.length > 1 ? value.slice(0, -1) : value;
      clearEditorValue(control);
      typeTextLikeUser(control, typedValue);
      await sleep(VTBE_TIMING.legacyAutocompletePause);
      const firstOption = await waitForAutocompleteOption();
      if (!firstOption) return false;
      clickElement(firstOption);
      return true;
    }

    replaceEditorValue(control, value);
    return waitForAppliedValue(control, value);
  }

  async function editListCellOnce(cell, value, behavior) {
    if (VTBE_STATE.stopRequested) return false;
    cell.scrollIntoView({ block: 'center', inline: 'nearest' });
    clickElement(cell);
    dispatchMouseSequence(cell, 'dblclick');
    const activeCell = await waitForEditorCell(cell);
    if (!activeCell) return false;
    let target = await waitForEditableTarget(activeCell);
    if (!target && activeCell.isContentEditable) target = activeCell;
    if (!target) return false;
    const valueApplied = await applyListBehavior(target, value, behavior);
    if (!valueApplied) return false;
    return commitListEdit(target, activeCell, value, behavior);
  }

  async function editListCell(cell, value, behavior) {
    for (let attempt = 1; attempt <= VTBE_TIMING.maxRetriesPerCell; attempt += 1) {
      if (VTBE_STATE.stopRequested) return false;
      const ok = await editListCellOnce(cell, value, behavior);
      if (ok) return true;
      await sleep(VTBE_TIMING.retryBackoff * attempt);
    }
    return false;
  }

  function buildBulkTargets(rules) {
    const tables = findCandidateTables();
    for (const table of tables) {
      const targets = [];
      for (const rule of rules) {
        const columnTarget = getColumnTarget(table, rule.headerText);
        if (!columnTarget) return { table, targets: [], missingRule: rule.headerText };
        targets.push({ rule, columnTarget });
      }
      if (targets.length === rules.length) return { table, targets, missingRule: null };
    }
    return { table: null, targets: [], missingRule: rules[0]?.headerText || '' };
  }

  function listCellMatchesRule(cell, rule) {
    return normalizeText(cell?.textContent || '') === normalizeText(rule.value || '');
  }

  function countPendingCells(rows, targets) {
    let pending = 0;
    rows.forEach((row) => {
      targets.forEach((target) => {
        const cell = getCellForRow(row, target.columnTarget);
        if (!cell) return;
        if (!listCellMatchesRule(cell, target.rule)) pending += 1;
      });
    });
    return pending;
  }

  async function runBulkProject(payload) {
    VTBE_STATE.stopRequested = false;
    VTBE_STATE.running = true;
    VTBE_STATE.mode = 'bulk';

    try {
      const rules = (payload.rules || []).filter((rule) => rule.enabled !== false && normalizeText(rule.headerText));
      if (!rules.length) return { error: 'No enabled rules were provided.' };

      const targetBuild = buildBulkTargets(rules);
      if (!targetBuild.table || targetBuild.missingRule) {
        return {
          completionStatus: 'error',
          stopReason: `Header not found: ${targetBuild.missingRule}`,
          totalRows: 0,
          processedRows: 0,
          totalEdited: 0,
          totalFailed: 0,
          pendingCount: 0,
          details: [`${targetBuild.missingRule}: Header not found`]
        };
      }

      const rows = getAllRows(targetBuild.table);
      if (!rows.length) {
        return {
          completionStatus: 'error',
          stopReason: 'No data rows found on the current page.',
          totalRows: 0,
          processedRows: 0,
          totalEdited: 0,
          totalFailed: 0,
          pendingCount: 0,
          details: ['No data rows found on the current page.']
        };
      }

      const perRuleStats = Object.fromEntries(rules.map((rule) => [rule.headerText, { edited: 0, failed: 0, skipped: 0 }]));
      let processedRows = 0;
      let totalEdited = 0;
      let totalFailed = 0;
      let stopReason = 'All target cells on the current page were updated.';

      for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
        if (VTBE_STATE.stopRequested) {
          stopReason = 'Stopped by user.';
          break;
        }

        const row = rows[rowIndex];
        for (const target of targetBuild.targets) {
          if (VTBE_STATE.stopRequested) {
            stopReason = 'Stopped by user.';
            break;
          }

          const cell = getCellForRow(row, target.columnTarget);
          if (!cell) {
            totalFailed += 1;
            perRuleStats[target.rule.headerText].failed += 1;
            continue;
          }

          if (listCellMatchesRule(cell, target.rule)) {
            perRuleStats[target.rule.headerText].skipped += 1;
            continue;
          }

          try {
            const ok = await editListCell(cell, target.rule.value || '', target.rule.behavior || 'direct_replace');
            if (ok) {
              totalEdited += 1;
              perRuleStats[target.rule.headerText].edited += 1;
            } else {
              totalFailed += 1;
              perRuleStats[target.rule.headerText].failed += 1;
            }
          } catch (error) {
            totalFailed += 1;
            perRuleStats[target.rule.headerText].failed += 1;
            stopReason = error.message || 'A row-level edit error occurred.';
          }

          await sleep(VTBE_TIMING.betweenRulesPause);
        }

        processedRows += 1;
        await sleep(VTBE_TIMING.betweenRowsPause);
        if ((rowIndex + 1) % VTBE_TIMING.pauseEveryNRows === 0) {
          await sleep(VTBE_TIMING.longPauseEveryNRows);
        }
      }

      const pendingCount = countPendingCells(rows, targetBuild.targets);
      let completionStatus = 'completed';
      if (VTBE_STATE.stopRequested) {
        completionStatus = 'stopped_by_user';
      } else if (pendingCount > 0) {
        completionStatus = 'stopped_with_pending_cells';
        stopReason = 'Some target cells still do not match their expected values after retries.';
      } else if (totalFailed > 0) {
        completionStatus = 'completed_with_failures';
        stopReason = 'The run finished, but some cells failed.';
      }

      const details = Object.entries(perRuleStats).map(([headerText, stats]) => {
        const parts = [`${headerText}: edited ${stats.edited}`];
        if (stats.skipped) parts.push(`skipped ${stats.skipped}`);
        if (stats.failed) parts.push(`failed ${stats.failed}`);
        return parts.join(', ');
      });

      return {
        projectName: payload.projectName || 'Unnamed project',
        completionStatus,
        stopReason,
        totalRows: rows.length,
        processedRows,
        ruleCount: rules.length,
        totalEdited,
        totalFailed,
        pendingCount,
        details
      };
    } finally {
      VTBE_STATE.running = false;
      VTBE_STATE.mode = null;
    }
  }

  function getFieldNameCandidates(headerText) {
    const normalized = normalizeFieldToken(headerText);
    const noSpaces = normalized.replace(/\s+/g, '');
    const underscores = normalized.replace(/\s+/g, '_');
    const dots = normalized.replace(/\s+/g, '.');
    return Array.from(new Set([normalized, noSpaces, underscores, dots])).filter(Boolean);
  }

  function getControlDisplayValue(control) {
    if (!control) return '';
    if (control.tagName && control.tagName.toLowerCase() === 'select') {
      const selected = control.options[control.selectedIndex];
      return selected ? selected.textContent : control.value;
    }
    return control.value || control.textContent || '';
  }

  function getNearbyLabelText(control) {
    if (!control) return '';
    const pieces = [];
    const id = control.id || '';

    if (id) {
      const labels = Array.from(document.querySelectorAll(`label[for="${CSS.escape(id)}"]`));
      labels.forEach((label) => pieces.push(label.innerText || label.textContent || ''));
    }

    const row = control.closest('tr');
    if (row) {
      const cells = Array.from(row.children).slice(0, 2);
      cells.forEach((cell) => pieces.push(cell.innerText || cell.textContent || ''));
    }

    const container = control.closest('td, .form-group, .sn-form-field, .form-control') || control.parentElement;
    if (container) {
      const labels = Array.from(container.querySelectorAll('label, .label-text, .sn-form-field-label, .control-label'));
      labels.forEach((label) => pieces.push(label.innerText || label.textContent || ''));
    }

    return normalizeFieldToken(pieces.join(' '));
  }

  function scoreSurgicalControl(control, headerText) {
    if (!control || !isVisible(control)) return -1;
    const wanted = normalizeFieldToken(headerText);
    if (!wanted) return -1;

    const attrs = [
      control.id,
      control.name,
      control.getAttribute('aria-label'),
      control.getAttribute('placeholder'),
      control.getAttribute('data-name'),
      control.getAttribute('data-label'),
      control.getAttribute('data-fieldname')
    ].filter(Boolean).map(normalizeFieldToken).join(' ');

    const nearby = getNearbyLabelText(control);
    let score = 0;
    if (nearby === wanted) score += 120;
    else if (nearby.includes(wanted) || wanted.includes(nearby)) score += 80;
    if (attrs.includes(wanted)) score += 90;

    const candidates = getFieldNameCandidates(headerText);
    candidates.forEach((candidate) => {
      if (!candidate) return;
      if ((control.id || '').toLowerCase().includes(candidate)) score += 25;
      if ((control.name || '').toLowerCase().includes(candidate)) score += 25;
    });

    if (control.tagName && ['INPUT', 'TEXTAREA', 'SELECT'].includes(control.tagName.toUpperCase())) score += 10;
    return score;
  }

  function findBestSurgicalControl(headerText) {
    const controls = Array.from(document.querySelectorAll('input, textarea, select, [contenteditable="true"]')).filter((control) => {
      const tag = (control.tagName || '').toLowerCase();
      if (tag === 'input') {
        const type = (control.type || 'text').toLowerCase();
        if (['hidden', 'button', 'submit', 'reset', 'checkbox', 'radio', 'file'].includes(type)) return false;
      }
      return isVisible(control);
    });

    const scored = controls
      .map((control) => ({ control, score: scoreSurgicalControl(control, headerText) }))
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score);

    return scored[0]?.control || null;
  }

  function waitForSurgicalControl(headerText) {
    return waitForCondition(() => findBestSurgicalControl(headerText), { timeout: VTBE_TIMING.editorReadyTimeout });
  }

  function tabIsActive(element) {
    if (!element) return false;
    const cls = (element.className || '').toString().toLowerCase();
    const ariaSelected = (element.getAttribute('aria-selected') || '').toLowerCase();
    const ariaExpanded = (element.getAttribute('aria-expanded') || '').toLowerCase();
    return cls.includes('active') || cls.includes('selected') || ariaSelected === 'true' || ariaExpanded === 'true';
  }

  function findTabElementByName(tabName) {
    const wanted = normalizeFieldToken(tabName);
    const candidates = Array.from(document.querySelectorAll('[role="tab"], button, a, span, div')).filter(isVisible);
    const scored = candidates
      .map((element) => {
        const text = normalizeFieldToken([
          element.innerText,
          element.textContent,
          element.getAttribute('aria-label'),
          element.getAttribute('title'),
          element.getAttribute('data-title')
        ].filter(Boolean).join(' '));

        let score = 0;
        if (text === wanted) score = 100;
        else if (text.includes(wanted) || wanted.includes(text)) score = 60;
        if (score && tabIsActive(element)) score += 20;
        return { element, score };
      })
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score);
    return scored[0]?.element || null;
  }

  async function switchToTab(tabName) {
    const tab = await waitForCondition(() => findTabElementByName(tabName), { timeout: VTBE_TIMING.editorReadyTimeout });
    if (!tab) return false;
    if (!tabIsActive(tab)) clickElement(tab);
    await sleep(VTBE_TIMING.tabSwitchPause);
    return true;
  }

  async function ensureSurgicalTabForField(headerText) {
    const normalized = normalizeFieldToken(headerText);
    const desiredTab = SURGICAL_TAB_MAP[normalized] || 'general';
    return switchToTab(desiredTab);
  }

  async function returnToGeneralTabIfNeeded(headerText) {
    const normalized = normalizeFieldToken(headerText);
    const desiredTab = SURGICAL_TAB_MAP[normalized] || 'general';
    if (desiredTab !== 'general') await switchToTab('general');
  }

  async function applySurgicalBehavior(control, rule) {
    const behavior = rule.behavior || 'direct_replace';
    const value = rule.value || '';
    const tag = (control.tagName || '').toLowerCase();

    if (behavior === 'choice_select' || tag === 'select') {
      if (tag !== 'select') return false;
      const option = Array.from(control.options).find(
        (opt) => normalizeText(opt.textContent) === normalizeText(value) || normalizeText(opt.value) === normalizeText(value)
      );
      control.focus();
      control.value = option ? option.value : value;
      control.dispatchEvent(new Event('input', { bubbles: true }));
      control.dispatchEvent(new Event('change', { bubbles: true }));
      if (typeof control.blur === 'function') control.blur();
      return true;
    }

    if (behavior === 'autocomplete_legacy_first') {
      const typedValue = value.length > 1 ? value.slice(0, -1) : value;
      clearEditorValue(control);
      typeTextLikeUser(control, typedValue);
      await sleep(VTBE_TIMING.legacyAutocompletePause);
      const firstOption = await waitForAutocompleteOption();
      if (!firstOption) return false;
      clickElement(firstOption);
      await sleep(120);
      if (typeof control.blur === 'function') control.blur();
      return true;
    }

    replaceEditorValue(control, value);
    if (typeof control.blur === 'function') control.blur();
    return true;
  }

  async function runSurgicalProject(payload) {
    VTBE_STATE.stopRequested = false;
    VTBE_STATE.running = true;
    VTBE_STATE.mode = 'surgical';

    try {
      const rules = (payload.rules || []).filter((rule) => rule.enabled !== false && normalizeText(rule.headerText));
      if (!rules.length) return { error: 'No enabled rules were provided.' };

      const perRuleStats = Object.fromEntries(rules.map((rule) => [rule.headerText, { edited: 0, failed: 0, skipped: 0 }]));
      let totalEdited = 0;
      let totalFailed = 0;
      let stopReason = 'All enabled form fields were filled on the current record.';

      for (const rule of rules) {
        if (VTBE_STATE.stopRequested) {
          stopReason = 'Stopped by user.';
          break;
        }

        const tabReady = await ensureSurgicalTabForField(rule.headerText);
        if (!tabReady) {
          totalFailed += 1;
          perRuleStats[rule.headerText].failed += 1;
          continue;
        }

        const control = await waitForSurgicalControl(rule.headerText);
        if (!control) {
          totalFailed += 1;
          perRuleStats[rule.headerText].failed += 1;
          await returnToGeneralTabIfNeeded(rule.headerText);
          continue;
        }

        const currentValue = normalizeText(getControlDisplayValue(control));
        const wantedValue = normalizeText(rule.value || '');
        if (currentValue === wantedValue) {
          perRuleStats[rule.headerText].skipped += 1;
          await returnToGeneralTabIfNeeded(rule.headerText);
          continue;
        }

        let success = false;
        for (let attempt = 1; attempt <= VTBE_TIMING.maxRetriesPerCell; attempt += 1) {
          if (VTBE_STATE.stopRequested) break;
          try {
            success = await applySurgicalBehavior(control, rule);
            if (success) break;
          } catch (error) {}
          await sleep(VTBE_TIMING.retryBackoff * attempt);
        }

        if (success) {
          totalEdited += 1;
          perRuleStats[rule.headerText].edited += 1;
        } else {
          totalFailed += 1;
          perRuleStats[rule.headerText].failed += 1;
        }

        await returnToGeneralTabIfNeeded(rule.headerText);
        await sleep(VTBE_TIMING.surgicalBetweenFieldsPause);
      }

      let completionStatus = 'completed';
      if (VTBE_STATE.stopRequested) {
        completionStatus = 'stopped_by_user';
      } else if (totalFailed > 0) {
        completionStatus = 'completed_with_failures';
        stopReason = 'The run finished, but some form fields could not be filled.';
      }

      const details = Object.entries(perRuleStats).map(([headerText, stats]) => {
        const parts = [`${headerText}: edited ${stats.edited}`];
        if (stats.skipped) parts.push(`skipped ${stats.skipped}`);
        if (stats.failed) parts.push(`failed ${stats.failed}`);
        return parts.join(', ');
      });

      return {
        projectName: payload.projectName || 'Unnamed project',
        completionStatus,
        stopReason,
        totalRows: 1,
        processedRows: 1,
        ruleCount: rules.length,
        totalEdited,
        totalFailed,
        pendingCount: totalFailed,
        details
      };
    } finally {
      VTBE_STATE.running = false;
      VTBE_STATE.mode = null;
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'VTBE_PING') {
      sendResponse({ ok: true });
      return false;
    }

    if (message?.type === 'VTBE_GET_STATUS') {
      sendResponse({ running: VTBE_STATE.running, mode: VTBE_STATE.mode });
      return false;
    }

    if (message?.type === 'VTBE_STOP_RUN') {
      VTBE_STATE.stopRequested = true;
      sendResponse({ ok: true });
      return false;
    }

    if (message?.type === 'RUN_VISIBLE_TABLE_PROJECT') {
      runBulkProject(message.payload || {})
        .then(sendResponse)
        .catch((error) => sendResponse({
          error: error.message || 'Unexpected content script error.',
          completionStatus: 'error',
          stopReason: error.message || 'Unexpected content script error.'
        }));
      return true;
    }

    if (message?.type === 'RUN_SURGICAL_PROJECT') {
      runSurgicalProject(message.payload || {})
        .then(sendResponse)
        .catch((error) => sendResponse({
          error: error.message || 'Unexpected content script error.',
          completionStatus: 'error',
          stopReason: error.message || 'Unexpected content script error.'
        }));
      return true;
    }

    return false;
  });
})();
