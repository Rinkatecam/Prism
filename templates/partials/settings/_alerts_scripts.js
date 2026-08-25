// ═══════════════════════════════════════════════════════════════
// Maintenance Windows
// ═══════════════════════════════════════════════════════════════

let maintWindows = [];

function loadMaintenanceWindows() {
  fetch('/api/maintenance-windows')
    .then(r => r.json())
    .then(data => {
      if (!data.ok) return;
      maintWindows = data.windows || [];
      renderMaintWindows();
    })
    .catch(() => {});
}

function renderMaintWindows() {
  const container = document.getElementById('maint-list');

  if (maintWindows.length === 0) {
    const _noMaintMsg = {{ t.no_maintenance_windows | tojson }};
    const _noMaintHint = {{ t.get('no_maintenance_hint', 'Define windows when servers are expected to be under load') | tojson }};
    container.innerHTML =
      '<div class="text-sm text-faint text-center py-8">' +
        '<i data-lucide="clock" class="w-8 h-8 mx-auto mb-2 opacity-30"></i>' +
        '<p>' + _escHtml(_noMaintMsg) + '</p>' +
        '<p class="text-[10px] mt-1 opacity-60">' + _escHtml(_noMaintHint) + '</p>' +
      '</div>';
    if (typeof lucide !== 'undefined') lucide.createIcons();
    return;
  }

  const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  container.innerHTML = '<div class="space-y-2">' + maintWindows.map((w, i) => {
    const days = (w.days || []).map(d => dayNames[d] || '?').join(', ');
    const servers = (w.servers || []).join(', ');
    const timeRange = (w.start_time || '00:00') + ' – ' + (w.end_time || '23:59');
    return `<div class="flex items-center gap-3 px-3 py-2 rounded-lg border border-line bg-page dark:bg-page/50">
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2">
          <span class="text-xs font-semibold text-ink dark:text-[#F9FAFB]">${_escHtml(w.name || 'Unnamed')}</span>
          <span class="px-1.5 py-0.5 text-[9px] font-bold rounded bg-info/10 text-info dark:bg-info/20">${_escHtml(timeRange)}</span>
        </div>
        <div class="text-[10px] text-muted mt-0.5 font-mono truncate">${_escHtml(days)} → ${_escHtml(servers || 'No servers')}</div>
      </div>
      <button data-action="editMaintWindow" data-args="[${i}]" class="p-1.5 text-muted hover:text-info rounded hover:bg-info/10" title="Edit">
        <i data-lucide="pencil" class="w-3.5 h-3.5"></i>
      </button>
      <button data-action="deleteMaintWindow" data-args="[${i}]" class="p-1.5 text-muted hover:text-critical rounded hover:bg-critical/10" title="Delete">
        <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
      </button>
    </div>`;
  }).join('') + '</div>';
  lucide.createIcons();
}

function showAddMaintenance() {
  document.getElementById('maint-modal-title').textContent = {{ t.add_maintenance | tojson }};
  document.getElementById('maint-edit-index').value = -1;
  document.getElementById('maint-name').value = '';
  document.querySelectorAll('.maint-server-cb').forEach(cb => cb.checked = false);
  document.querySelectorAll('.maint-day-cb').forEach(cb => cb.checked = false);
  document.getElementById('maint-start').value = '02:00';
  document.getElementById('maint-end').value = '06:00';
  document.getElementById('maint-cpu-warn').value = 90;
  document.getElementById('maint-cpu-crit').value = 98;
  document.getElementById('maint-ram-warn').value = 95;
  document.getElementById('maint-ram-crit').value = 99;
  document.getElementById('maint-disk-warn').value = 90;
  document.getElementById('maint-disk-crit').value = 98;
  document.getElementById('maint-suppress-alerts').checked = false;
  document.getElementById('maint-modal').classList.remove('hidden');
  lucide.createIcons();
}

function editMaintWindow(index) {
  const w = maintWindows[index];
  if (!w) return;
  document.getElementById('maint-modal-title').textContent = {{ t.get('edit_maintenance', 'Edit Maintenance Window') | tojson }};
  document.getElementById('maint-edit-index').value = index;
  document.getElementById('maint-name').value = w.name || '';
  document.querySelectorAll('.maint-server-cb').forEach(cb => {
    cb.checked = (w.servers || []).includes(cb.value);
  });
  document.querySelectorAll('.maint-day-cb').forEach(cb => {
    cb.checked = (w.days || []).includes(parseInt(cb.value));
  });
  document.getElementById('maint-start').value = w.start_time || '02:00';
  document.getElementById('maint-end').value = w.end_time || '06:00';
  const th = w.thresholds || {};
  document.getElementById('maint-cpu-warn').value = th.cpu_warning || 90;
  document.getElementById('maint-cpu-crit').value = th.cpu_critical || 98;
  document.getElementById('maint-ram-warn').value = th.ram_warning || 95;
  document.getElementById('maint-ram-crit').value = th.ram_critical || 99;
  document.getElementById('maint-disk-warn').value = th.disk_warning || 90;
  document.getElementById('maint-disk-crit').value = th.disk_critical || 98;
  document.getElementById('maint-suppress-alerts').checked = !!w.suppress_alerts;
  document.getElementById('maint-modal').classList.remove('hidden');
  lucide.createIcons();
}

function closeMaintModal() {
  document.getElementById('maint-modal').classList.add('hidden');
}

function saveMaintWindow() {
  const name = document.getElementById('maint-name').value.trim();
  if (!name) { pAlert('Window name is required'); return; }

  const servers = [];
  document.querySelectorAll('.maint-server-cb:checked').forEach(cb => servers.push(cb.value));
  const days = [];
  document.querySelectorAll('.maint-day-cb:checked').forEach(cb => days.push(parseInt(cb.value)));

  const window_data = {
    name: name,
    servers: servers,
    days: days,
    start_time: document.getElementById('maint-start').value || '02:00',
    end_time: document.getElementById('maint-end').value || '06:00',
    suppress_alerts: document.getElementById('maint-suppress-alerts').checked,
    thresholds: {
      cpu_warning: parseInt(document.getElementById('maint-cpu-warn').value) || 90,
      cpu_critical: parseInt(document.getElementById('maint-cpu-crit').value) || 98,
      ram_warning: parseInt(document.getElementById('maint-ram-warn').value) || 95,
      ram_critical: parseInt(document.getElementById('maint-ram-crit').value) || 99,
      disk_warning: parseInt(document.getElementById('maint-disk-warn').value) || 90,
      disk_critical: parseInt(document.getElementById('maint-disk-crit').value) || 98,
    },
  };

  const index = parseInt(document.getElementById('maint-edit-index').value);

  fetch('/api/maintenance-windows', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    body: JSON.stringify({ window: window_data, index: index }),
  })
  .then(r => r.json())
  .then(data => {
    if (data.ok) {
      maintWindows = data.windows || [];
      renderMaintWindows();
      closeMaintModal();
    } else {
      pAlert('Error: ' + (data.error || 'Save failed'));
    }
  })
  .catch(err => pAlert('Save failed: ' + err.message));
}

async function deleteMaintWindow(index) {
  if (!await pConfirm('Delete this maintenance window?')) return;
  fetch('/api/maintenance-windows/' + index, {
    method: 'DELETE',
    headers: { 'X-CSRFToken': csrfToken },
  })
  .then(r => r.json())
  .then(data => {
    if (data.ok) {
      maintWindows = data.windows || [];
      renderMaintWindows();
    }
  })
  .catch(err => pAlert('Delete failed: ' + err.message));
}

// ═══════════════════════════════════════════════════════════════
// TLS Monitoring
// ═══════════════════════════════════════════════════════════════

function loadTlsConfig() {
  if (!configData) return;
  const tls = (configData.settings || {}).tls_monitoring || {};
  const enabled = document.getElementById('tls-monitoring-enabled');
  if (enabled) enabled.checked = !!tls.enabled;
  const warnDays = document.getElementById('tls-warning-days');
  if (warnDays && tls.warning_days) warnDays.value = tls.warning_days;
  const critDays = document.getElementById('tls-critical-days');
  if (critDays && tls.critical_days) critDays.value = tls.critical_days;

  const certs = tls.certificates || [];
  const list = document.getElementById('tls-cert-list');
  const noCerts = document.getElementById('no-tls-certs');
  list.innerHTML = '';
  if (certs.length === 0) {
    noCerts.classList.remove('hidden');
  } else {
    noCerts.classList.add('hidden');
    certs.forEach(c => addTlsCertRow(c));
  }
}

function addTlsCertRow(data) {
  const list = document.getElementById('tls-cert-list');
  document.getElementById('no-tls-certs').classList.add('hidden');

  const row = document.createElement('div');
  row.className = 'flex items-center gap-2 px-3 py-2 rounded-lg border border-line bg-page dark:bg-page/50 tls-cert-row';

  const serverOpts = _serverNames.map(n => `<option value="${n}" ${(data && data.server === n) ? 'selected' : ''}>${n}</option>`).join('');

  row.innerHTML = `
    <select class="tls-server px-2 py-1 text-xs rounded border border-line bg-field">
      <option value="">-- Server --</option>
      ${serverOpts}
    </select>
    <input type="text" class="tls-host px-2 py-1 text-xs rounded border border-line bg-field font-mono flex-1" placeholder="hostname or IP" value="${_escHtml((data && data.host) || '')}">
    <input type="number" class="tls-port w-16 px-2 py-1 text-xs rounded border border-line bg-field text-right" placeholder="443" min="1" max="65535" value="${(data && data.port) || 443}">
    <button data-action="testTlsCertRow" class="p-1.5 text-muted hover:text-healthy rounded hover:bg-healthy/10" title="Test">
      <i data-lucide="zap" class="w-3.5 h-3.5"></i>
    </button>
    <button data-action="removeTlsCertRow" class="p-1.5 text-muted hover:text-critical rounded hover:bg-critical/10" title="Delete">
      <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
    </button>
    <div class="tls-test-result text-xs ml-1 hidden"></div>
  `;
  list.appendChild(row);
  lucide.createIcons({ nodes: [row] });
}

function updateTlsNoItemsMsg() {
  const list = document.getElementById('tls-cert-list');
  const noCerts = document.getElementById('no-tls-certs');
  if (list.children.length === 0) noCerts.classList.remove('hidden');
  else noCerts.classList.add('hidden');
}

// Row-scoped helpers for the delegated data-action fallback (`this` = the
// clicked button, bound by the delegation dispatcher).
function testTlsCertRow() { testTlsCert(this.closest('.tls-cert-row')); }
function removeTlsCertRow() { const r = this.closest('.tls-cert-row'); if (r) r.remove(); updateTlsNoItemsMsg(); }

function testTlsCert(row) {
  const host = row.querySelector('.tls-host').value.trim();
  const port = parseInt(row.querySelector('.tls-port').value) || 443;
  const resultEl = row.querySelector('.tls-test-result');
  if (!host) { resultEl.textContent = 'Enter a host'; resultEl.classList.remove('hidden'); return; }

  resultEl.textContent = 'Checking...';
  resultEl.className = 'tls-test-result text-xs ml-1 text-muted';

  fetch('/api/tls-certificates/check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    body: JSON.stringify({ host: host, port: port }),
  })
    .then(r => r.json())
    .then(result => {
      if (result.ok) {
        const days = result.days_remaining || 0;
        const color = days <= 7 ? '#DC2626' : days <= 30 ? '#F59E0B' : '#10B981';
        resultEl.innerHTML = `<span style="color:${color}">${days}d remaining</span>`;
      } else {
        resultEl.innerHTML = `<span class="text-critical">${_escHtml(result.error || 'Check failed')}</span>`;
      }
      resultEl.classList.remove('hidden');
    })
    .catch(err => {
      resultEl.innerHTML = `<span class="text-critical">${_escHtml(err.message)}</span>`;
      resultEl.classList.remove('hidden');
    });
}

function getTlsConfigPayload() {
  const rows = document.querySelectorAll('.tls-cert-row');
  const certificates = [];
  rows.forEach(row => {
    const server = row.querySelector('.tls-server').value;
    const host = row.querySelector('.tls-host').value.trim();
    const port = parseInt(row.querySelector('.tls-port').value) || 443;
    // NOTE: backend reads `server_name` (collector.py _check_tls_certificates).
    // Send both `server_name` (canonical) and `server` (legacy compat for older configs).
    if (host) certificates.push({ server_name: server, server: server, host: host, port: port });
  });
  return {
    enabled: document.getElementById('tls-monitoring-enabled').checked,
    warning_days: parseInt(document.getElementById('tls-warning-days').value) || 30,
    critical_days: parseInt(document.getElementById('tls-critical-days').value) || 7,
    certificates: certificates,
  };
}

// ═══════════════════════════════════════════════════════════════
// Init on page load
// ═══════════════════════════════════════════════════════════════
