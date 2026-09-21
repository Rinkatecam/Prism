// ═══════════════════════════════════════════════════════════════
// Scheduled Restarts
// ═══════════════════════════════════════════════════════════════

function updateFlaskRestartDayVisibility() {
  const sched = document.getElementById('flask-restart-schedule').value;
  document.getElementById('flask-restart-day-wrap').classList.toggle('hidden', sched !== 'weekly');
  document.getElementById('flask-restart-monthday-wrap').classList.toggle('hidden', sched !== 'monthly');
}
document.getElementById('flask-restart-schedule').addEventListener('change', updateFlaskRestartDayVisibility);

function updateServerRestartDayVisibility() {
  const sched = document.getElementById('server-restart-schedule').value;
  document.getElementById('server-restart-day-wrap').classList.toggle('hidden', sched !== 'weekly');
  document.getElementById('server-restart-monthday-wrap').classList.toggle('hidden', sched !== 'monthly');
}
document.getElementById('server-restart-schedule').addEventListener('change', updateServerRestartDayVisibility);

function loadRestartSchedules() {
  fetch('/api/scheduled-restarts')
    .then(r => r.json())
    .then(data => {
      if (!data.ok) return;
      // Flask restart
      const fr = data.flask_restart || {};
      document.getElementById('flask-restart-enabled').checked = fr.enabled || false;
      document.getElementById('flask-restart-schedule').value = fr.schedule || 'daily';
      document.getElementById('flask-restart-time').value = fr.time || '03:00';
      if (fr.day) document.getElementById('flask-restart-day').value = fr.day;
      if (fr.month_day) document.getElementById('flask-restart-monthday').value = fr.month_day;
      updateFlaskRestartDayVisibility();
      // Server restart schedule (global)
      const srs = data.server_restart_schedule || {};
      document.getElementById('server-restart-enabled').checked = srs.enabled || false;
      document.getElementById('server-restart-schedule').value = srs.schedule || 'weekly';
      document.getElementById('server-restart-time').value = srs.time || '03:00';
      if (srs.day) document.getElementById('server-restart-day').value = srs.day;
      if (srs.month_day) document.getElementById('server-restart-monthday').value = srs.month_day;
      updateServerRestartDayVisibility();
      // Delay
      if (data.delay_between_seconds) {
        document.getElementById('restart-delay').value = data.delay_between_seconds;
      }
      // Server restarts (with conditions)
      const list = data.server_restarts || [];
      const container = document.getElementById('server-restart-list');
      container.innerHTML = '';
      list.forEach((sr, i) => addServerRestartRow(sr));
      document.getElementById('no-server-restarts').classList.toggle('hidden', list.length > 0);
    })
    .catch(() => {});
}

function addServerRestartRow(data) {
  const container = document.getElementById('server-restart-list');
  document.getElementById('no-server-restarts').classList.add('hidden');
  const idx = container.children.length;
  const d = data || {};
  const conditions = d.conditions || [];
  const row = document.createElement('div');
  row.className = 'rounded-lg border border-line bg-page dark:bg-page/50 overflow-hidden';
  const serverOpts = _serverNames.map(n => `<option value="${n}" ${d.server === n ? 'selected' : ''}>${n}</option>`).join('');
  row.innerHTML = `
    <div class="flex flex-wrap items-center gap-2 p-2.5">
      <div class="flex items-center gap-1 bg-info/10 rounded px-1.5 py-0.5">
        <span class="text-[10px] font-bold text-info">#</span>
        <input type="number" class="sr-order w-8 px-0 py-0 text-xs font-bold text-info bg-transparent border-0 text-center outline-none focus-visible:ring-2 focus-visible:ring-brand" value="${d.order || (idx + 1)}" min="1" max="99">
      </div>
      <select class="sr-server px-2 py-1 text-xs font-medium rounded border border-line bg-field">
        <option value="">-- Server --</option>
        ${serverOpts}
      </select>
      <label class="flex items-center gap-1.5 px-2 py-1 rounded border border-warning/30 dark:border-[#F59E0B]/20 bg-warning/5 dark:bg-[#F59E0B]/5 cursor-pointer" title="` + {{ t.install_updates_desc | tojson }} + `">
        <input type="checkbox" class="sr-install-updates w-3.5 h-3.5 rounded border-line dark:border-faint accent-warning" ${d.install_updates ? 'checked' : ''}>
        <i data-lucide="download" class="w-3 h-3 text-warning"></i>
        <span class="text-[10px] font-medium text-warning-strong">` + {{ t.install_updates | tojson }} + `</span>
      </label>
      <div class="flex items-center gap-1 ml-auto">
        <button data-action="toggleConditionsEl" class="px-2 py-1 text-[10px] font-medium rounded border border-brand text-brand hover:bg-brand/10 flex items-center gap-1" title="` + {{ t.restart_conditions | tojson }} + `">
          <i data-lucide="filter" class="w-3 h-3"></i>
          <span class="sr-cond-count">${conditions.length || 0}</span>
        </button>
        <button data-action="removeRestartRowEl" title="` + {{ t.delete | default('Delete') | tojson }} + `"
                class="inline-flex items-center justify-center w-7 h-7 rounded border border-critical text-critical hover:bg-critical/10">
          <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
        </button>
      </div>
    </div>
    <div class="sr-conditions hidden border-t border-line bg-card/50 dark:bg-card/50 p-2.5">
      <div class="flex items-center justify-between mb-2">
        <span class="text-[10px] font-semibold uppercase tracking-wider text-brand">` + {{ t.restart_conditions | tojson }} + `</span>
        <button data-action="addConditionToRowEl" class="px-2 py-0.5 text-[10px] font-medium rounded border border-brand text-brand hover:bg-brand/10 flex items-center gap-1">
          <i data-lucide="plus" class="w-3 h-3"></i>
          ` + {{ t.add_condition | tojson }} + `
        </button>
      </div>
      <div class="sr-cond-list space-y-1.5"></div>
      <div class="sr-no-cond text-[10px] text-faint italic ${conditions.length > 0 ? 'hidden' : ''}">` + {{ t.no_conditions | tojson }} + `</div>
    </div>`;
  container.appendChild(row);
  // Load existing conditions
  conditions.forEach(c => addConditionToRow(row.querySelector('.sr-conditions button'), c));
  lucide.createIcons();
}

// data-action wrappers: bridge `this` (the clicked element) to the element-arg APIs.
function clickConfigUpload() { document.getElementById('config-upload-input').click(); }
function uploadConfigEl() { uploadConfig(this); }
function toggleConditionsEl() { toggleConditions(this); }
function removeRestartRowEl() { removeRestartRow(this); }
function addConditionToRowEl() { addConditionToRow(this); }
function updateConditionFieldsEl() { updateConditionFields(this); }
function openServicePickerEl() { openServicePicker(this); }
function removeConditionEl() { removeCondition(this); }

function toggleConditions(btn) {
  const row = btn.closest('.rounded-lg');
  const panel = row.querySelector('.sr-conditions');
  panel.classList.toggle('hidden');
}

function removeRestartRow(btn) {
  btn.closest('.rounded-lg').remove();
  const container = document.getElementById('server-restart-list');
  document.getElementById('no-server-restarts').classList.toggle('hidden', container.children.length > 0);
}

function addConditionToRow(btn, data) {
  const condPanel = btn.closest('.sr-conditions');
  const condList = condPanel.querySelector('.sr-cond-list');
  condPanel.querySelector('.sr-no-cond').classList.add('hidden');
  const d = data || {};
  const condRow = document.createElement('div');
  condRow.className = 'flex flex-wrap items-center gap-1.5 p-1.5 rounded bg-page dark:bg-page/50 border border-line';
  condRow.innerHTML = `
    <select class="cond-type px-1.5 py-0.5 text-[11px] rounded border border-line bg-field" data-change="updateConditionFieldsEl">
      <option value="wait_online" ${d.type==='wait_online'?'selected':''}>` + {{ t.wait_online | tojson }} + `</option>
      <option value="wait_service" ${d.type==='wait_service'?'selected':''}>` + {{ t.wait_service | tojson }} + `</option>
      <option value="wait_process" ${d.type==='wait_process'?'selected':''}>` + {{ t.wait_process | tojson }} + `</option>
    </select>
    <input type="text" class="cond-value px-1.5 py-0.5 text-[11px] rounded border border-line bg-field w-36 ${d.type && d.type !== 'wait_online' ? '' : 'hidden'}"
           placeholder="${d.type === 'wait_process' ? '` + {{ t.process_name | tojson }} + `' : '` + {{ t.service_name | tojson }} + `'}" value="${d.value || ''}">
    <button type="button" data-action="openServicePickerEl" class="cond-browse-btn px-1.5 py-0.5 text-[11px] rounded border border-line bg-field hover:bg-page dark:hover:bg-line text-muted ${d.type && d.type !== 'wait_online' ? '' : 'hidden'}" title="Browse">
      <i data-lucide="list" class="w-3 h-3"></i>
    </button>
    <div class="flex items-center gap-1">
      <span class="text-[10px] text-muted">` + {{ t.condition_timeout | tojson }} + `:</span>
      <input type="number" class="cond-timeout w-14 px-1 py-0.5 text-[11px] rounded border border-line bg-field text-center" value="${d.timeout || 300}" min="30" max="3600">
      <span class="text-[10px] text-muted">s</span>
    </div>
    <button data-action="removeConditionEl" class="p-0.5 text-critical hover:bg-critical/10 rounded ml-auto" title="` + {{ t.remove_condition | tojson }} + `">
      <i data-lucide="x" class="w-3 h-3"></i>
    </button>`;
  condList.appendChild(condRow);
  updateCondCountBadge(condPanel);
  lucide.createIcons();
}

function updateConditionFields(select) {
  const row = select.closest('.flex');
  const valueInput = row.querySelector('.cond-value');
  const browseBtn = row.querySelector('.cond-browse-btn');
  if (select.value === 'wait_online') {
    valueInput.classList.add('hidden');
    valueInput.value = '';
    if (browseBtn) browseBtn.classList.add('hidden');
  } else {
    valueInput.classList.remove('hidden');
    valueInput.placeholder = select.value === 'wait_process' ? {{ t.process_name | tojson }} : {{ t.service_name | tojson }};
    if (browseBtn) browseBtn.classList.remove('hidden');
  }
}

function removeCondition(btn) {
  const condPanel = btn.closest('.sr-conditions');
  btn.closest('.flex').remove();
  const condList = condPanel.querySelector('.sr-cond-list');
  if (condList.children.length === 0) {
    condPanel.querySelector('.sr-no-cond').classList.remove('hidden');
  }
  updateCondCountBadge(condPanel);
}

function updateCondCountBadge(condPanel) {
  const row = condPanel.closest('.rounded-lg');
  const count = condPanel.querySelector('.sr-cond-list').children.length;
  row.querySelector('.sr-cond-count').textContent = count;
}

function saveRestartSchedules() {
  const btn = document.getElementById('save-restart-btn');
  const result = document.getElementById('restart-save-result');
  btn.disabled = true;
  result.textContent = '';

  const flaskRestart = {
    enabled: document.getElementById('flask-restart-enabled').checked,
    schedule: document.getElementById('flask-restart-schedule').value,
    time: document.getElementById('flask-restart-time').value,
    day: document.getElementById('flask-restart-day').value,
    month_day: parseInt(document.getElementById('flask-restart-monthday').value) || 1,
  };

  // Global server restart schedule
  const serverRestartSchedule = {
    enabled: document.getElementById('server-restart-enabled').checked,
    schedule: document.getElementById('server-restart-schedule').value,
    time: document.getElementById('server-restart-time').value,
    day: document.getElementById('server-restart-day').value,
    month_day: parseInt(document.getElementById('server-restart-monthday').value) || 1,
  };

  const serverRestarts = [];
  document.querySelectorAll('#server-restart-list > .rounded-lg').forEach(row => {
    const server = row.querySelector('.sr-server').value;
    if (!server) return;
    // Gather conditions
    const conditions = [];
    row.querySelectorAll('.sr-cond-list > div').forEach(condRow => {
      const cond = {
        type: condRow.querySelector('.cond-type').value,
        timeout: parseInt(condRow.querySelector('.cond-timeout').value) || 300,
      };
      const val = condRow.querySelector('.cond-value').value.trim();
      if (val) cond.value = val;
      conditions.push(cond);
    });
    serverRestarts.push({
      server: server,
      order: parseInt(row.querySelector('.sr-order').value) || 1,
      install_updates: row.querySelector('.sr-install-updates').checked,
      conditions: conditions,
    });
  });
  // Sort by order
  serverRestarts.sort((a, b) => a.order - b.order);

  const payload = {
    flask_restart: flaskRestart,
    server_restart_schedule: serverRestartSchedule,
    server_restarts: serverRestarts,
    delay_between_seconds: parseInt(document.getElementById('restart-delay').value) || 60,
  };

  fetch('/api/scheduled-restarts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    body: JSON.stringify(payload),
  })
  .then(r => r.json())
  .then(data => {
    if (data.ok) {
      result.textContent = {{ t.schedule_saved | tojson }};
      result.className = 'text-xs text-healthy';
    } else {
      result.textContent = data.error || 'Error';
      result.className = 'text-xs text-critical';
    }
  })
  .catch(err => {
    result.textContent = 'Error: ' + err.message;
    result.className = 'text-xs text-critical';
  })
  .finally(() => { btn.disabled = false; });
}

