(function () {
  const STORAGE_KEYS = {
    bulkProjects: 'bulk_projects',
    bulkSelectedProjectId: 'bulk_selected_project_id',
    surgicalProjects: 'surgical_projects',
    surgicalSelectedProjectId: 'surgical_selected_project_id',
    activeTaskType: 'active_task_type'
  };

  const HEADER_OPTIONS = [
    'Acquisition method',
    'Asset tag',
    'Assigned',
    'Assigned to',
    'Class',
    'Comments',
    'Company',
    'Cost',
    'Cost center',
    'Department',
    'Depreciation effective date',
    'Display name',
    'Expenditure type',
    'GL account',
    'Installed',
    'Invoice number',
    'Location',
    'Managed by',
    'Model',
    'Model category',
    'Name',
    'Opened',
    'Order received',
    'Owned by',
    'PO number',
    'Parent',
    'Request line',
    'Serial number',
    'State',
    'Station ID',
    'Substate',
    'Vendor'
  ].sort((a, b) => a.localeCompare(b));

  const BEHAVIOR_OPTIONS = [
    { value: 'autocomplete_legacy_first', label: 'Autocomplete first option' },
    { value: 'choice_select', label: 'Choice / select' },
    { value: 'direct_replace', label: 'Direct replace' }
  ];

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
        },
        {
          id: 'surgical-rule-po-number',
          headerText: 'PO number',
          value: '',
          behavior: 'direct_replace',
          enabled: false
        }
      ]
    }
  ];

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function init() {
    const bulkTabBtn = document.getElementById('bulkTabBtn');
    const surgicalTabBtn = document.getElementById('surgicalTabBtn');
    const newProjectNameInput = document.getElementById('newProjectName');
    const projectNameInput = document.getElementById('projectName');
    const addProjectBtn = document.getElementById('addProjectBtn');
    const saveProjectBtn = document.getElementById('saveProjectBtn');
    const addRuleBtn = document.getElementById('addRuleBtn');
    const deleteProjectBtn = document.getElementById('deleteProjectBtn');
    const exportBtn = document.getElementById('exportBtn');
    const importFile = document.getElementById('importFile');
    const projectList = document.getElementById('projectList');
    const rulesBody = document.getElementById('rulesBody');
    const statusEl = document.getElementById('status');
    const hintBox = document.getElementById('hintBox');

    if (
      !bulkTabBtn || !surgicalTabBtn || !newProjectNameInput || !projectNameInput ||
      !addProjectBtn || !saveProjectBtn || !addRuleBtn || !deleteProjectBtn ||
      !exportBtn || !importFile || !projectList || !rulesBody || !statusEl || !hintBox
    ) {
      return;
    }

    let state = {
      bulkProjects: [],
      bulkSelectedProjectId: '',
      surgicalProjects: [],
      surgicalSelectedProjectId: '',
      activeTaskType: 'bulk'
    };

    function setStatus(message, isError = false) {
      statusEl.textContent = message;
      statusEl.style.color = isError ? '#b91c1c' : '#1f2937';
    }

    async function readState() {
      const result = await chrome.storage.local.get(Object.values(STORAGE_KEYS));

      state.bulkProjects =
        Array.isArray(result[STORAGE_KEYS.bulkProjects]) && result[STORAGE_KEYS.bulkProjects].length
          ? result[STORAGE_KEYS.bulkProjects]
          : clone(DEFAULT_BULK_PROJECTS);

      state.bulkSelectedProjectId =
        result[STORAGE_KEYS.bulkSelectedProjectId] || state.bulkProjects[0]?.id || '';

      state.surgicalProjects =
        Array.isArray(result[STORAGE_KEYS.surgicalProjects]) && result[STORAGE_KEYS.surgicalProjects].length
          ? result[STORAGE_KEYS.surgicalProjects]
          : clone(DEFAULT_SURGICAL_PROJECTS);

      state.surgicalSelectedProjectId =
        result[STORAGE_KEYS.surgicalSelectedProjectId] || state.surgicalProjects[0]?.id || '';

      state.activeTaskType = result[STORAGE_KEYS.activeTaskType] || 'bulk';

      await persistState(false);
    }

    async function persistState(showMessage = true) {
      await chrome.storage.local.set({
        [STORAGE_KEYS.bulkProjects]: state.bulkProjects,
        [STORAGE_KEYS.bulkSelectedProjectId]: state.bulkSelectedProjectId,
        [STORAGE_KEYS.surgicalProjects]: state.surgicalProjects,
        [STORAGE_KEYS.surgicalSelectedProjectId]: state.surgicalSelectedProjectId,
        [STORAGE_KEYS.activeTaskType]: state.activeTaskType
      });

      if (showMessage) setStatus('Project settings saved.');
    }

    function getProjectsForActiveTask() {
      return state.activeTaskType === 'surgical' ? state.surgicalProjects : state.bulkProjects;
    }

    function getSelectedProjectIdForActiveTask() {
      return state.activeTaskType === 'surgical'
        ? state.surgicalSelectedProjectId
        : state.bulkSelectedProjectId;
    }

    function setSelectedProjectIdForActiveTask(projectId) {
      if (state.activeTaskType === 'surgical') state.surgicalSelectedProjectId = projectId;
      else state.bulkSelectedProjectId = projectId;
    }

    function getSelectedProject() {
      return (
        getProjectsForActiveTask().find(
          (project) => project.id === getSelectedProjectIdForActiveTask()
        ) || null
      );
    }

    function setActiveTabStyles() {
      bulkTabBtn.classList.toggle('active', state.activeTaskType === 'bulk');
      surgicalTabBtn.classList.toggle('active', state.activeTaskType === 'surgical');

      hintBox.textContent =
        state.activeTaskType === 'surgical'
          ? 'Surgical Tasks targets the current single-record form. It includes Financial-tab fields like PO number.'
          : 'Bulk Tasks updates the current list page using the list editor.';
    }

    function renderProjectList() {
      projectList.innerHTML = '';

      getProjectsForActiveTask().forEach((project) => {
        const item = document.createElement('button');
        item.className = `project-item${
          project.id === getSelectedProjectIdForActiveTask() ? ' active' : ''
        }`;
        item.textContent = project.name;

        item.addEventListener('click', async () => {
          setSelectedProjectIdForActiveTask(project.id);
          render();
          await persistState(false);
          setStatus(
            `Selected ${state.activeTaskType === 'surgical' ? 'Surgical Tasks' : 'Bulk Tasks'} project: ${project.name}`
          );
        });

        projectList.appendChild(item);
      });
    }

    function buildHeaderEditor(rule) {
      const wrap = document.createElement('div');
      const select = document.createElement('select');
      const knownValue = HEADER_OPTIONS.includes(rule.headerText) ? rule.headerText : '__custom__';

      HEADER_OPTIONS.forEach((header) => {
        const option = document.createElement('option');
        option.value = header;
        option.textContent = header;
        select.appendChild(option);
      });

      const customOption = document.createElement('option');
      customOption.value = '__custom__';
      customOption.textContent = 'Custom...';
      select.appendChild(customOption);

      select.value = knownValue;

      const customWrap = document.createElement('div');
      customWrap.className = 'custom-header-wrap';

      const customInput = document.createElement('input');
      customInput.type = 'text';
      customInput.placeholder = 'Custom field name';
      customInput.value = knownValue === '__custom__' ? (rule.headerText || '') : '';
      customWrap.appendChild(customInput);
      customWrap.style.display = knownValue === '__custom__' ? 'block' : 'none';

      select.addEventListener('change', () => {
        if (select.value === '__custom__') {
          customWrap.style.display = 'block';
          rule.headerText = customInput.value.trim();
        } else {
          customWrap.style.display = 'none';
          rule.headerText = select.value;
        }
      });

      customInput.addEventListener('input', () => {
        if (select.value === '__custom__') rule.headerText = customInput.value.trim();
      });

      if (knownValue !== '__custom__') rule.headerText = knownValue;

      wrap.appendChild(select);
      wrap.appendChild(customWrap);
      return wrap;
    }

    function buildBehaviorEditor(rule) {
      const select = document.createElement('select');
      BEHAVIOR_OPTIONS.forEach((behavior) => {
        const option = document.createElement('option');
        option.value = behavior.value;
        option.textContent = behavior.label;
        select.appendChild(option);
      });

      select.value = rule.behavior || 'direct_replace';
      select.addEventListener('change', () => {
        rule.behavior = select.value;
      });
      return select;
    }

    function renderRules() {
      rulesBody.innerHTML = '';
      const project = getSelectedProject();
      projectNameInput.value = project?.name || '';
      if (!project) return;

      project.rules.forEach((rule) => {
        const tr = document.createElement('tr');

        const enabledTd = document.createElement('td');
        enabledTd.className = 'checkbox-wrap';
        const enabled = document.createElement('input');
        enabled.type = 'checkbox';
        enabled.checked = rule.enabled !== false;
        enabled.addEventListener('change', () => {
          rule.enabled = enabled.checked;
        });
        enabledTd.appendChild(enabled);

        const headerTd = document.createElement('td');
        headerTd.appendChild(buildHeaderEditor(rule));

        const behaviorTd = document.createElement('td');
        behaviorTd.appendChild(buildBehaviorEditor(rule));

        const valueTd = document.createElement('td');
        const valueInput = document.createElement('input');
        valueInput.type = 'text';
        valueInput.value = rule.value || '';
        valueInput.placeholder = 'Replacement value';
        valueInput.addEventListener('input', () => {
          rule.value = valueInput.value;
        });
        valueTd.appendChild(valueInput);

        const actionTd = document.createElement('td');
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.textContent = 'Remove';
        removeBtn.className = 'secondary';
        removeBtn.addEventListener('click', () => {
          project.rules = project.rules.filter((item) => item.id !== rule.id);
          render();
        });
        actionTd.appendChild(removeBtn);

        tr.append(enabledTd, headerTd, behaviorTd, valueTd, actionTd);
        rulesBody.appendChild(tr);
      });

      if (!project.rules.length) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 5;
        td.textContent = 'No rules yet. Click Add Rule.';
        tr.appendChild(td);
        rulesBody.appendChild(tr);
      }
    }

    function render() {
      setActiveTabStyles();
      renderProjectList();
      renderRules();
    }

    function downloadJsonFile(filename, data) {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    }

    bulkTabBtn.addEventListener('click', async () => {
      state.activeTaskType = 'bulk';
      render();
      await persistState(false);
      setStatus('Switched to Bulk Tasks.');
    });

    surgicalTabBtn.addEventListener('click', async () => {
      state.activeTaskType = 'surgical';
      render();
      await persistState(false);
      setStatus('Switched to Surgical Tasks.');
    });

    addProjectBtn.addEventListener('click', async () => {
      const name = newProjectNameInput.value.trim();
      if (!name) {
        setStatus('Enter a project name first.', true);
        return;
      }

      const project = { id: crypto.randomUUID(), name, rules: [] };
      if (state.activeTaskType === 'surgical') {
        state.surgicalProjects.push(project);
        state.surgicalSelectedProjectId = project.id;
      } else {
        state.bulkProjects.push(project);
        state.bulkSelectedProjectId = project.id;
      }

      newProjectNameInput.value = '';
      render();
      await persistState();
    });

    saveProjectBtn.addEventListener('click', async () => {
      const project = getSelectedProject();
      if (!project) {
        setStatus('No project selected.', true);
        return;
      }

      const name = projectNameInput.value.trim();
      if (!name) {
        setStatus('Project name cannot be empty.', true);
        return;
      }

      project.name = name;
      project.rules = project.rules
        .map((rule) => ({
          id: rule.id || crypto.randomUUID(),
          headerText: (rule.headerText || '').trim(),
          value: rule.value || '',
          behavior: rule.behavior || 'direct_replace',
          enabled: rule.enabled !== false
        }))
        .filter((rule) => rule.headerText);

      render();
      await persistState();
    });

    addRuleBtn.addEventListener('click', () => {
      const project = getSelectedProject();
      if (!project) {
        setStatus('No project selected.', true);
        return;
      }

      project.rules.push({
        id: crypto.randomUUID(),
        headerText: state.activeTaskType === 'surgical' ? 'Display name' : 'GL account',
        value: '',
        behavior: 'direct_replace',
        enabled: true
      });

      render();
      setStatus('Rule added.');
    });

    deleteProjectBtn.addEventListener('click', async () => {
      const project = getSelectedProject();
      if (!project) {
        setStatus('No project selected.', true);
        return;
      }

      if (state.activeTaskType === 'surgical') {
        state.surgicalProjects = state.surgicalProjects.filter((item) => item.id !== project.id);
        state.surgicalSelectedProjectId = state.surgicalProjects[0]?.id || '';
      } else {
        state.bulkProjects = state.bulkProjects.filter((item) => item.id !== project.id);
        state.bulkSelectedProjectId = state.bulkProjects[0]?.id || '';
      }

      render();
      await persistState();
    });

    exportBtn.addEventListener('click', () => {
      downloadJsonFile('servicenow-autopilot-projects.json', {
        activeTaskType: state.activeTaskType,
        bulkProjects: state.bulkProjects,
        bulkSelectedProjectId: state.bulkSelectedProjectId,
        surgicalProjects: state.surgicalProjects,
        surgicalSelectedProjectId: state.surgicalSelectedProjectId
      });
      setStatus('Projects exported.');
    });

    importFile.addEventListener('change', async (event) => {
      const file = event.target.files?.[0];
      if (!file) return;

      try {
        const text = await file.text();
        const parsed = JSON.parse(text);

        if (!Array.isArray(parsed.bulkProjects) || !parsed.bulkProjects.length) {
          throw new Error('Imported file does not contain Bulk Tasks projects.');
        }
        if (!Array.isArray(parsed.surgicalProjects) || !parsed.surgicalProjects.length) {
          throw new Error('Imported file does not contain Surgical Tasks projects.');
        }

        state.bulkProjects = parsed.bulkProjects;
        state.bulkSelectedProjectId = parsed.bulkSelectedProjectId || parsed.bulkProjects[0]?.id || '';
        state.surgicalProjects = parsed.surgicalProjects;
        state.surgicalSelectedProjectId = parsed.surgicalSelectedProjectId || parsed.surgicalProjects[0]?.id || '';
        state.activeTaskType = parsed.activeTaskType === 'surgical' ? 'surgical' : 'bulk';

        render();
        await persistState();
        setStatus('Projects imported.');
      } catch (error) {
        setStatus(error.message || 'Import failed.', true);
      } finally {
        importFile.value = '';
      }
    });

    readState().then(() => {
      render();
      setStatus('Project settings loaded.');
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
