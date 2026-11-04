(function () {
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

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function behaviorLabel(behavior) {
    if (behavior === 'autocomplete_legacy_first') return 'Autocomplete first option';
    if (behavior === 'choice_select') return 'Choice / select';
    return 'Direct replace';
  }

  async function getState() {
    const state = await chrome.storage.local.get(Object.values(STORAGE_KEYS));
    const updates = {};

    let bulkProjects = state[STORAGE_KEYS.bulkProjects];
    let bulkSelectedProjectId = state[STORAGE_KEYS.bulkSelectedProjectId] || '';
    let surgicalProjects = state[STORAGE_KEYS.surgicalProjects];
    let surgicalSelectedProjectId = state[STORAGE_KEYS.surgicalSelectedProjectId] || '';
    let activeTaskType = state[STORAGE_KEYS.activeTaskType] || 'bulk';

    if (!Array.isArray(bulkProjects) || !bulkProjects.length) {
      bulkProjects = clone(DEFAULT_BULK_PROJECTS);
      bulkSelectedProjectId = bulkProjects[0].id;
      updates[STORAGE_KEYS.bulkProjects] = bulkProjects;
      updates[STORAGE_KEYS.bulkSelectedProjectId] = bulkSelectedProjectId;
    } else if (!bulkSelectedProjectId) {
      bulkSelectedProjectId = bulkProjects[0].id;
      updates[STORAGE_KEYS.bulkSelectedProjectId] = bulkSelectedProjectId;
    }

    if (!Array.isArray(surgicalProjects) || !surgicalProjects.length) {
      surgicalProjects = clone(DEFAULT_SURGICAL_PROJECTS);
      surgicalSelectedProjectId = surgicalProjects[0].id;
      updates[STORAGE_KEYS.surgicalProjects] = surgicalProjects;
      updates[STORAGE_KEYS.surgicalSelectedProjectId] = surgicalSelectedProjectId;
    } else if (!surgicalSelectedProjectId) {
      surgicalSelectedProjectId = surgicalProjects[0].id;
      updates[STORAGE_KEYS.surgicalSelectedProjectId] = surgicalSelectedProjectId;
    }

    if (!state[STORAGE_KEYS.activeTaskType]) {
      updates[STORAGE_KEYS.activeTaskType] = activeTaskType;
    }

    if (Object.keys(updates).length) {
      await chrome.storage.local.set(updates);
    }

    return {
      bulkProjects,
      bulkSelectedProjectId,
      surgicalProjects,
      surgicalSelectedProjectId,
      activeTaskType
    };
  }

  function init() {
    const taskTypeSelect = document.getElementById('taskTypeSelect');
    const projectSelect = document.getElementById('projectSelect');
    const runBtn = document.getElementById('runBtn');
    const stopBtn = document.getElementById('stopBtn');
    const settingsBtn = document.getElementById('settingsBtn');
    const statusEl = document.getElementById('status');
    const rulesList = document.getElementById('rulesList');

    if (!taskTypeSelect || !projectSelect || !runBtn || !stopBtn || !settingsBtn || !statusEl || !rulesList) {
      return;
    }

    function setStatus(message, isError = false) {
      statusEl.textContent = message;
      statusEl.style.color = isError ? '#b91c1c' : '#1f2937';
    }

    function getProjectsForType(state, taskType) {
      return taskType === 'surgical' ? state.surgicalProjects : state.bulkProjects;
    }

    function getSelectedProjectIdForType(state, taskType) {
      return taskType === 'surgical' ? state.surgicalSelectedProjectId : state.bulkSelectedProjectId;
    }

    async function setSelectedProjectId(taskType, projectId) {
      await chrome.storage.local.set({
        [taskType === 'surgical' ? STORAGE_KEYS.surgicalSelectedProjectId : STORAGE_KEYS.bulkSelectedProjectId]: projectId
      });
    }

    function renderRules(project) {
      rulesList.innerHTML = '';

      if (!project) {
        rulesList.innerHTML = '<li>No project selected.</li>';
        return;
      }

      const enabledRules = (project.rules || []).filter((rule) => rule.enabled !== false);
      if (!enabledRules.length) {
        rulesList.innerHTML = '<li>This project has no enabled rules.</li>';
        return;
      }

      enabledRules.forEach((rule) => {
        const li = document.createElement('li');
        li.textContent = `${rule.headerText} → ${rule.value || '(blank)'} (${behaviorLabel(rule.behavior)})`;
        rulesList.appendChild(li);
      });
    }

    async function refreshRunUi() {
      try {
        const state = await chrome.runtime.sendMessage({ type: 'VTBE_GET_RUN_STATE' });
        stopBtn.disabled = !state?.running;
      } catch (error) {
        stopBtn.disabled = true;
      }
    }

    async function loadProjects() {
      const state = await getState();
      const taskType = state.activeTaskType || 'bulk';
      const projects = getProjectsForType(state, taskType) || [];
      const selectedProjectId = getSelectedProjectIdForType(state, taskType) || projects[0]?.id || '';

      taskTypeSelect.value = taskType;
      projectSelect.innerHTML = '';

      projects.forEach((project) => {
        const option = document.createElement('option');
        option.value = project.id;
        option.textContent = project.name;
        projectSelect.appendChild(option);
      });

      projectSelect.value = selectedProjectId || projects[0]?.id || '';
      const selectedProject = projects.find((project) => project.id === projectSelect.value) || projects[0] || null;
      renderRules(selectedProject);
      setStatus(selectedProject ? `Ready to run ${taskType === 'surgical' ? 'Surgical Tasks' : 'Bulk Tasks'}: ${selectedProject.name}` : 'No project available.', !selectedProject);
      await refreshRunUi();
    }

    async function handleTaskTypeChange() {
      const taskType = taskTypeSelect.value === 'surgical' ? 'surgical' : 'bulk';
      await chrome.storage.local.set({ [STORAGE_KEYS.activeTaskType]: taskType });
      await loadProjects();
    }

    async function handleProjectChange() {
      const state = await getState();
      const taskType = taskTypeSelect.value === 'surgical' ? 'surgical' : 'bulk';
      const projects = getProjectsForType(state, taskType) || [];
      const project = projects.find((item) => item.id === projectSelect.value) || null;
      await setSelectedProjectId(taskType, projectSelect.value);
      renderRules(project);
      setStatus(project ? `Selected ${taskType === 'surgical' ? 'Surgical Tasks' : 'Bulk Tasks'} project: ${project.name}` : 'No project selected.', !project);
    }

    async function runProject() {
      try {
        const state = await getState();
        const taskType = taskTypeSelect.value === 'surgical' ? 'surgical' : 'bulk';
        const projects = getProjectsForType(state, taskType) || [];
        const project = projects.find((item) => item.id === projectSelect.value);

        if (!project) {
          setStatus('Pick a valid project first.', true);
          return;
        }

        const enabledRules = (project.rules || []).filter(
          (rule) => rule.enabled !== false && (rule.headerText || '').trim()
        );

        if (!enabledRules.length) {
          setStatus('This project has no enabled rules.', true);
          return;
        }

        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab?.id) {
          setStatus('No active tab found.', true);
          return;
        }

        setStatus(`Starting ${taskType === 'surgical' ? 'Surgical Tasks' : 'Bulk Tasks'}: ${project.name}`);

        const response = await chrome.runtime.sendMessage({
          type: 'VTBE_START_RUN',
          tabId: tab.id,
          taskType,
          projectName: project.name,
          rules: enabledRules
        });

        if (!response?.ok) {
          setStatus(response?.error || 'Could not start the run.', true);
          await refreshRunUi();
          return;
        }

        stopBtn.disabled = false;
        setTimeout(() => window.close(), 120);
      } catch (error) {
        setStatus(error.message || 'Unexpected error.', true);
        await refreshRunUi();
      }
    }

    async function stopRun() {
      try {
        const response = await chrome.runtime.sendMessage({ type: 'VTBE_STOP_RUN' });
        if (response?.ok) {
          setStatus('Stop requested. The current field will finish, then the run will stop.');
          stopBtn.disabled = true;
        } else {
          setStatus(response?.error || 'Could not stop the run.', true);
        }
      } catch (error) {
        setStatus(error.message || 'Unable to stop the run.', true);
      }
    }

    taskTypeSelect.addEventListener('change', handleTaskTypeChange);
    projectSelect.addEventListener('change', handleProjectChange);
    runBtn.addEventListener('click', runProject);
    stopBtn.addEventListener('click', stopRun);
    settingsBtn.addEventListener('click', () => chrome.runtime.openOptionsPage());

    loadProjects();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
