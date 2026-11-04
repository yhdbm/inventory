const STORAGE_KEYS = {
  bulkProjects: 'bulk_projects',
  bulkSelectedProjectId: 'bulk_selected_project_id',
  surgicalProjects: 'surgical_projects',
  surgicalSelectedProjectId: 'surgical_selected_project_id',
  activeTaskType: 'active_task_type'
};

const DEFAULT_BULK_PROJECTS = [
  {
    id: 'bulk-default-project',
    name: 'GL Update Project',
    rules: [
      {
        id: 'bulk-rule-gl-account',
        headerText: 'GL account',
        value: '2513',
        behavior: 'direct_replace',
        enabled: true
      }
    ]
  }
];

const DEFAULT_SURGICAL_PROJECTS = [
  {
    id: 'surgical-default-project',
    name: 'New Device Setup',
    rules: [
      {
        id: 'surgical-rule-display-name',
        headerText: 'Display name',
        value: '',
        behavior: 'direct_replace',
        enabled: false
      },
      {
        id: 'surgical-rule-location',
        headerText: 'Location',
        value: '',
        behavior: 'autocomplete_legacy_first',
        enabled: false
      }
    ]
  }
];

const RUN_STATE = {
  running: false,
  tabId: null,
  taskType: 'bulk',
  status: 'idle',
  lastError: ''
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

async function seedDefaults() {
  const state = await chrome.storage.local.get(Object.values(STORAGE_KEYS));
  const updates = {};

  if (!Array.isArray(state[STORAGE_KEYS.bulkProjects]) || !state[STORAGE_KEYS.bulkProjects].length) {
    updates[STORAGE_KEYS.bulkProjects] = clone(DEFAULT_BULK_PROJECTS);
    updates[STORAGE_KEYS.bulkSelectedProjectId] = DEFAULT_BULK_PROJECTS[0].id;
  } else if (!state[STORAGE_KEYS.bulkSelectedProjectId]) {
    updates[STORAGE_KEYS.bulkSelectedProjectId] = state[STORAGE_KEYS.bulkProjects][0].id;
  }

  if (!Array.isArray(state[STORAGE_KEYS.surgicalProjects]) || !state[STORAGE_KEYS.surgicalProjects].length) {
    updates[STORAGE_KEYS.surgicalProjects] = clone(DEFAULT_SURGICAL_PROJECTS);
    updates[STORAGE_KEYS.surgicalSelectedProjectId] = DEFAULT_SURGICAL_PROJECTS[0].id;
  } else if (!state[STORAGE_KEYS.surgicalSelectedProjectId]) {
    updates[STORAGE_KEYS.surgicalSelectedProjectId] = state[STORAGE_KEYS.surgicalProjects][0].id;
  }

  if (!state[STORAGE_KEYS.activeTaskType]) {
    updates[STORAGE_KEYS.activeTaskType] = 'bulk';
  }

  if (Object.keys(updates).length) {
    await chrome.storage.local.set(updates);
  }
}

async function setIconState(state) {
  const suffix = state === 'running' ? 'running' : state === 'error' ? 'error' : 'idle';
  await chrome.action.setIcon({
    path: {
      16: `icons/icon_${suffix}_16.png`,
      32: `icons/icon_${suffix}_32.png`,
      48: `icons/icon_${suffix}_48.png`,
      128: `icons/icon_${suffix}_128.png`
    }
  });
}

async function ensureContentScript(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!tab?.url || tab.url.startsWith('chrome://') || tab.url.startsWith('edge://') || tab.url.startsWith('about:')) {
    throw new Error('This page does not allow extensions to run.');
  }

  try {
    const ping = await chrome.tabs.sendMessage(tabId, { type: 'VTBE_PING' });
    if (ping?.ok) return;
  } catch (error) {
    await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      files: ['content.js']
    });
  }

  const ping = await chrome.tabs.sendMessage(tabId, { type: 'VTBE_PING' });
  if (!ping?.ok) {
    throw new Error('Content script not connected. Refresh the page and try again.');
  }
}

async function finalizeRun(result) {
  RUN_STATE.running = false;
  RUN_STATE.tabId = null;
  RUN_STATE.status = result?.completionStatus || 'idle';
  RUN_STATE.lastError = result?.error || result?.stopReason || '';

  if (['error', 'stopped_with_pending_cells', 'completed_with_failures'].includes(RUN_STATE.status)) {
    await setIconState('error');
  } else {
    await setIconState('idle');
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await seedDefaults();
  await setIconState('idle');
});

chrome.runtime.onStartup.addListener(async () => {
  await seedDefaults();
  await setIconState('idle');
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'VTBE_GET_RUN_STATE') {
    sendResponse({ ...RUN_STATE });
    return false;
  }

  if (message?.type === 'VTBE_STOP_RUN') {
    if (!RUN_STATE.running || !RUN_STATE.tabId) {
      sendResponse({ ok: false, error: 'No run is currently active.' });
      return false;
    }

    chrome.tabs.sendMessage(RUN_STATE.tabId, { type: 'VTBE_STOP_RUN' })
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message || 'Could not stop the run.' }));
    return true;
  }

  if (message?.type === 'VTBE_START_RUN') {
    (async () => {
      if (RUN_STATE.running) {
        sendResponse({ ok: false, error: 'A run is already in progress.' });
        return;
      }

      try {
        await ensureContentScript(message.tabId);
        RUN_STATE.running = true;
        RUN_STATE.tabId = message.tabId;
        RUN_STATE.taskType = message.taskType || 'bulk';
        RUN_STATE.status = 'running';
        RUN_STATE.lastError = '';
        await setIconState('running');
        sendResponse({ ok: true });

        const response = await chrome.tabs.sendMessage(message.tabId, {
          type: RUN_STATE.taskType === 'surgical' ? 'RUN_SURGICAL_PROJECT' : 'RUN_VISIBLE_TABLE_PROJECT',
          payload: {
            projectName: message.projectName,
            rules: message.rules
          }
        });

        await finalizeRun(response || { completionStatus: 'error', stopReason: 'No response from page script.' });
      } catch (error) {
        await finalizeRun({ completionStatus: 'error', stopReason: error.message || 'Could not start the run.' });
        sendResponse({ ok: false, error: error.message || 'Could not start the run.' });
      }
    })();
    return true;
  }

  return false;
});
