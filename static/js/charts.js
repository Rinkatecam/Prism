/* Prism - Chart.js trend chart for server detail page */

let trendChart = null;
let _restartEvents = [];  // restart events for chart annotations

/* ── Custom plugin: draw vertical dashed lines for restart events ── */
const restartAnnotationPlugin = {
  id: 'restartAnnotations',
  afterDraw(chart) {
    if (!_restartEvents || _restartEvents.length === 0) return;

    const ctx = chart.ctx;
    const xScale = chart.scales.x;
    const yScale = chart.scales.y;
    const labels = chart.data.labels;
    const dataTimestamps = chart.data._rawTimestamps || [];

    if (!labels || labels.length === 0) return;

    ctx.save();

    _restartEvents.forEach(evt => {
      // Find the closest data point index for this restart event timestamp
      const evtTime = new Date(evt.timestamp).getTime();
      let closestIdx = 0;
      let closestDist = Infinity;

      for (let i = 0; i < dataTimestamps.length; i++) {
        const dist = Math.abs(dataTimestamps[i] - evtTime);
        if (dist < closestDist) {
          closestDist = dist;
          closestIdx = i;
        }
      }

      // Get pixel position for this label index
      const xPos = xScale.getPixelForValue(closestIdx);
      const yTop = yScale.top;
      const yBottom = yScale.bottom;

      // Determine color based on status
      let color;
      if (evt.status === 'success' || evt.status === 'completed') {
        color = '#22C55E';  // green
      } else if (evt.status === 'skipped' || evt.status === 'conditions_not_met') {
        color = '#F59E0B';  // amber
      } else if (evt.status === 'failed' || evt.status === 'error') {
        color = '#EF4444';  // red
      } else {
        color = '#3B82F6';  // blue default
      }

      // Draw vertical dashed line
      ctx.beginPath();
      ctx.setLineDash([6, 4]);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = color;
      ctx.moveTo(xPos, yTop);
      ctx.lineTo(xPos, yBottom);
      ctx.stroke();
      ctx.setLineDash([]);

      // Draw label at top
      const isDark = document.documentElement.classList.contains('dark');
      const labelText = '\u21BB Restart';  // ↻ symbol

      ctx.font = 'bold 10px system-ui, sans-serif';
      const textWidth = ctx.measureText(labelText).width;
      const padding = 4;
      const boxWidth = textWidth + padding * 2;
      const boxHeight = 16;
      const boxX = xPos - boxWidth / 2;
      const boxY = yTop - boxHeight - 4;

      // Background pill
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.9;
      ctx.beginPath();
      if (ctx.roundRect) {
        ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 3);
      } else {
        // Fallback for browsers without roundRect
        ctx.rect(boxX, boxY, boxWidth, boxHeight);
      }
      ctx.fill();
      ctx.globalAlpha = 1.0;

      // Label text
      ctx.fillStyle = '#FFFFFF';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(labelText, xPos, boxY + boxHeight / 2);
    });

    ctx.restore();
  }
};

// Register the plugin globally
Chart.register(restartAnnotationPlugin);

function loadChart(hours, clickedBtn) {
  const loading = document.getElementById('chart-loading');
  const canvas = document.getElementById('trend-chart');
  if (!canvas) return;

  // Update button styles — highlight the active range button
  document.querySelectorAll('.chart-range-btn').forEach(btn => {
    btn.classList.remove('bg-[#2563EB]', 'dark:bg-[#3B82F6]', 'text-white');
  });
  // Find the clicked button, or the one matching the hours value
  const activeBtn = clickedBtn || document.querySelector(`.chart-range-btn[data-hours="${hours}"]`);
  if (activeBtn) activeBtn.classList.add('bg-[#2563EB]', 'dark:bg-[#3B82F6]', 'text-white');

  if (loading) loading.style.display = 'block';
  canvas.style.display = 'none';

  fetch(`/api/servers/${encodeURIComponent(SERVER_NAME)}/history?hours=${hours}`)
    .then(r => r.json())
    .then(data => {
      if (loading) loading.style.display = 'none';
      canvas.style.display = 'block';
      _restartEvents = data.restart_events || [];
      renderChart(canvas, data.data, hours);
    })
    .catch(err => {
      if (loading) {
        loading.style.display = 'block';
        loading.textContent = 'Failed to load chart data.';
      }
      console.error('Chart load error:', err);
    });
}

function renderChart(canvas, dataPoints, hours) {
  const ctx = canvas.getContext('2d');
  const isDark = document.documentElement.classList.contains('dark');

  // Parse data — keep raw timestamps for restart event matching
  const rawTimestamps = dataPoints.map(d => {
    if (!d.timestamp) return 0;
    return new Date(d.timestamp).getTime();
  });

  const labels = dataPoints.map(d => {
    if (!d.timestamp) return '';
    // formatTs is defined globally in base.html with timezone support
    if (typeof formatTs === 'function') {
      return hours <= 24 ? formatTs(d.timestamp, true) : formatTs(d.timestamp);
    }
    const t = d.timestamp;
    return hours <= 24 ? t.substring(11, 16) : t.substring(5, 10) + ' ' + t.substring(11, 16);
  });

  const cpu = dataPoints.map(d => d.cpu_percent);
  const ram = dataPoints.map(d => d.ram_percent);
  const diskC = dataPoints.map(d => d.disk_c_percent >= 0 ? d.disk_c_percent : null);
  const diskD = dataPoints.map(d => d.disk_d_percent !== null && d.disk_d_percent >= 0 ? d.disk_d_percent : null);

  // Check if Disk D has any data
  const hasDiskD = diskD.some(v => v !== null);

  const gridColor = isDark ? 'rgba(51, 65, 85, 0.5)' : 'rgba(229, 231, 235, 0.8)';
  const textColor = isDark ? '#CBD5E1' : '#6B7280';

  if (trendChart) trendChart.destroy();

  const datasets = [
    {
      label: 'CPU',
      data: cpu,
      borderColor: '#3B82F6',
      backgroundColor: 'rgba(59, 130, 246, 0.1)',
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.3,
      fill: false,
    },
    {
      label: 'RAM',
      data: ram,
      borderColor: '#8B5CF6',
      backgroundColor: 'rgba(139, 92, 246, 0.1)',
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.3,
      fill: false,
    },
    {
      label: 'Disk C:',
      data: diskC,
      borderColor: '#F59E0B',
      backgroundColor: 'rgba(245, 158, 11, 0.1)',
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.3,
      fill: false,
    },
  ];

  if (hasDiskD) {
    datasets.push({
      label: 'Disk D:',
      data: diskD,
      borderColor: '#14B8A6',
      backgroundColor: 'rgba(20, 184, 166, 0.1)',
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.3,
      fill: false,
    });
  }

  // Downsample labels for readability
  const maxLabels = 24;
  const step = Math.max(1, Math.floor(labels.length / maxLabels));

  trendChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets,
      _rawTimestamps: rawTimestamps,  // stash for the restart annotation plugin
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: 'index',
        intersect: false,
      },
      plugins: {
        title: {
          display: true,
          text: 'Server Resource Usage',
          color: textColor,
          font: { size: 14, weight: 'bold' },
          padding: { bottom: 10 },
        },
        legend: {
          display: true,
          position: 'top',
          labels: {
            color: textColor,
            usePointStyle: true,
            pointStyle: 'line',
            padding: 16,
            font: { size: 12 },
          },
        },
        tooltip: {
          backgroundColor: isDark ? '#1E293B' : '#FFFFFF',
          titleColor: isDark ? '#F8FAFC' : '#1F2937',
          bodyColor: isDark ? '#CBD5E1' : '#6B7280',
          borderColor: isDark ? '#334155' : '#E5E7EB',
          borderWidth: 1,
          padding: 10,
          callbacks: {
            afterBody: function(tooltipItems) {
              // Show restart event info in tooltip if one falls near this data point
              if (!_restartEvents || _restartEvents.length === 0) return '';
              const idx = tooltipItems[0]?.dataIndex;
              if (idx === undefined) return '';
              const rawTs = rawTimestamps[idx];
              if (!rawTs) return '';

              // Check if any restart event is within one data-point interval of this index
              const interval = rawTimestamps.length > 1
                ? Math.abs(rawTimestamps[1] - rawTimestamps[0])
                : 300000;  // default 5min

              for (const evt of _restartEvents) {
                const evtTime = new Date(evt.timestamp).getTime();
                if (Math.abs(evtTime - rawTs) <= interval) {
                  const statusLabel = evt.status === 'success' || evt.status === 'completed'
                    ? 'OK' : evt.status === 'failed' || evt.status === 'error'
                    ? 'FAILED' : evt.status.toUpperCase();
                  let line = `\u21BB Restart [${statusLabel}]`;
                  if (evt.details) line += ': ' + evt.details.substring(0, 60);
                  return '\n' + line;
                }
              }
              return '';
            },
            label: function(context) {
              const val = context.parsed.y;
              return val !== null ? `${context.dataset.label}: ${val.toFixed(1)}%` : null;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: textColor,
            font: { size: 10 },
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: maxLabels,
          },
          grid: { color: gridColor },
        },
        y: {
          min: 0,
          max: 100,
          title: {
            display: true,
            text: '%',
            color: textColor,
            font: { size: 12 },
          },
          ticks: {
            color: textColor,
            font: { size: 10 },
            callback: v => v + '%',
            stepSize: 25,
          },
          grid: { color: gridColor },
        },
      },
      animation: { duration: 400 },
    },
  });
}
