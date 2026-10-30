/* PnL Diary & Institutional Edge Suite — Slice 1 Controller (diary.js)
 *
 * Strictly segregates Paper vs Live books (?mode=paper|live), enforces IST
 * calendar date math, and renders exact 2dp Gross − Commission = Net figures
 * with a single total Commission sum (zero fee-component breakdown).
 */
(function (root) {
  'use strict';

  var MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'
  ];
  var DOW_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  function istTodayIso() {
    try {
      var parts = new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Kolkata',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
      }).formatToParts(new Date());
      var y = '2026', m = '01', d = '01';
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === 'year') y = parts[i].value;
        if (parts[i].type === 'month') m = parts[i].value;
        if (parts[i].type === 'day') d = parts[i].value;
      }
      return y + '-' + m + '-' + d;
    } catch (_e) {
      return new Date().toISOString().slice(0, 10);
    }
  }

  function parseIsoDate(iso) {
    var parts = String(iso || '').split('-');
    return {
      year: parseInt(parts[0], 10) || 2026,
      month: parseInt(parts[1], 10) || 1,
      day: parseInt(parts[2], 10) || 1
    };
  }

  function pad2(n) {
    return n < 10 ? '0' + n : String(n);
  }

  function daysInMonth(year, month) {
    return new Date(Date.UTC(year, month, 0)).getUTCDate();
  }

  function monthBounds(year, month) {
    var last = daysInMonth(year, month);
    var ym = year + '-' + pad2(month);
    return { from: ym + '-01', to: ym + '-' + pad2(last) };
  }

  function shiftDaysIso(iso, deltaDays) {
    var p = parseIsoDate(iso);
    var dt = new Date(Date.UTC(p.year, p.month - 1, p.day + deltaDays));
    return (
      dt.getUTCFullYear() + '-' +
      pad2(dt.getUTCMonth() + 1) + '-' +
      pad2(dt.getUTCDate())
    );
  }

  function mondayOfWeekIso(iso) {
    var p = parseIsoDate(iso);
    var dt = new Date(Date.UTC(p.year, p.month - 1, p.day));
    var dow = (dt.getUTCDay() + 6) % 7; // Mon=0 .. Sun=6
    return shiftDaysIso(iso, -dow);
  }

  function fmtInrAbs(val) {
    var n = Number(val || 0);
    if (!Number.isFinite(n)) n = 0;
    var abs = Math.abs(n);
    var formatted = abs.toLocaleString('en-IN', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
    return '₹' + formatted;
  }

  function fmtInrSigned(val) {
    var n = Number(val || 0);
    if (!Number.isFinite(n)) n = 0;
    if (Math.abs(n) < 0.005) return '₹0.00';
    var sign = n > 0 ? '+' : '−';
    return sign + fmtInrAbs(Math.abs(n));
  }

  function pnlToneClass(val) {
    var n = Number(val || 0);
    if (!Number.isFinite(n) || Math.abs(n) < 0.005) return '';
    return n > 0 ? 'diary-pos' : 'diary-neg';
  }

  function computeHeatClass(netPnl, maxAbsNet, tradeCount) {
    var n = Number(netPnl || 0);
    if (!tradeCount || Math.abs(n) < 0.005 || maxAbsNet <= 0) return '';
    var ratio = Math.min(1, Math.abs(n) / maxAbsNet);
    var tier = ratio <= 0.25 ? 1 : ratio <= 0.5 ? 2 : ratio <= 0.75 ? 3 : 4;
    return (n > 0 ? 'level-pos-' : 'level-neg-') + tier;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatPrettyDate(iso) {
    var p = parseIsoDate(iso);
    var shortMonths = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return pad2(p.day) + '-' + (shortMonths[p.month - 1] || 'Jan') + '-' + p.year;
  }

  var state = {
    mode: 'paper',
    strategy: '',
    todayIst: istTodayIso(),
    calYear: 2026,
    calMonth: 10,
    selFrom: '',
    selTo: '',
    anchorDate: '',
    activePreset: 'mtd',
    calendarDays: [],
    calendarWeeks: []
  };

  function resolveInitialMode() {
    try {
      var params = new URLSearchParams(window.location.search || '');
      var qMode = (params.get('mode') || '').trim().toLowerCase();
      if (qMode === 'paper' || qMode === 'live') return qMode;
    } catch (_e) { /* ignore */ }

    var rootEl = document.getElementById('diary-root');
    var attrMode = rootEl ? (rootEl.getAttribute('data-initial-mode') || '').toLowerCase() : '';
    try {
      var stored = (localStorage.getItem('trading-diary.mode') || '').toLowerCase();
      var paramsCheck = new URLSearchParams(window.location.search || '');
      if (!paramsCheck.has('mode') && (stored === 'paper' || stored === 'live')) {
        return stored;
      }
    } catch (_e2) { /* storage blocked */ }
    return (attrMode === 'live' || attrMode === 'paper') ? attrMode : 'paper';
  }

  function syncModeChrome(mode) {
    var selectEl = document.getElementById('diary-mode');
    if (selectEl) {
      selectEl.value = mode;
      selectEl.setAttribute('data-mode', mode);
    }
    var pillEl = document.getElementById('diary-book-pill');
    if (pillEl) {
      pillEl.setAttribute('data-mode', mode);
      pillEl.textContent = mode === 'live' ? 'LIVE BOOK' : 'PAPER BOOK';
    }
    var openBookEl = document.getElementById('diary-open-book');
    if (openBookEl) {
      openBookEl.setAttribute('href', '/portfolio/' + mode);
    }
    var openBookLabel = document.getElementById('diary-open-book-label');
    if (openBookLabel) {
      openBookLabel.textContent = mode === 'live' ? 'Open Live book' : 'Open Paper book';
    }
    try {
      localStorage.setItem('trading-diary.mode', mode);
    } catch (_e) { /* ignore */ }
    try {
      var url = new URL(window.location.href);
      url.searchParams.set('mode', mode);
      window.history.replaceState({}, '', url.pathname + url.search);
    } catch (_e2) { /* ignore */ }
  }

  function setPresetPills(preset) {
    state.activePreset = preset || '';
    var pills = document.querySelectorAll('.diary-range-pill');
    for (var i = 0; i < pills.length; i++) {
      var isMatch = pills[i].getAttribute('data-preset') === state.activePreset;
      pills[i].classList.toggle('is-active', isMatch);
    }
  }

  function syncDateInputs() {
    var fromEl = document.getElementById('diary-from');
    var toEl = document.getElementById('diary-to');
    if (fromEl) fromEl.value = state.selFrom;
    if (toEl) toEl.value = state.selTo;
  }

  function applyPreset(preset) {
    var today = state.todayIst || istTodayIso();
    var tp = parseIsoDate(today);
    if (preset === 'today') {
      state.selFrom = today;
      state.selTo = today;
      state.calYear = tp.year;
      state.calMonth = tp.month;
    } else if (preset === '7d') {
      state.selFrom = shiftDaysIso(today, -6);
      state.selTo = today;
      state.calYear = tp.year;
      state.calMonth = tp.month;
    } else if (preset === '30d') {
      state.selFrom = shiftDaysIso(today, -29);
      state.selTo = today;
      state.calYear = tp.year;
      state.calMonth = tp.month;
    } else {
      // Default MTD
      var mb = monthBounds(tp.year, tp.month);
      state.selFrom = mb.from;
      state.selTo = today < mb.to ? today : mb.to;
      state.calYear = tp.year;
      state.calMonth = tp.month;
      preset = 'mtd';
    }
    state.anchorDate = state.selFrom;
    setPresetPills(preset);
    syncDateInputs();
  }

  function populateStrategies(strategies) {
    var sel = document.getElementById('diary-strategy');
    if (!sel || !Array.isArray(strategies)) return;
    var current = state.strategy || '';
    var html = '<option value="">All strategies</option>';
    var found = false;
    for (var i = 0; i < strategies.length; i++) {
      var s = String(strategies[i] || '');
      if (!s) continue;
      if (s === current) found = true;
      html += '<option value="' + escapeHtml(s) + '"' +
        (s === current ? ' selected' : '') + '>' + escapeHtml(s) + '</option>';
    }
    sel.innerHTML = html;
    if (current && !found) {
      state.strategy = '';
      sel.value = '';
    }
  }

  function setSignedValue(elId, val, textOverride) {
    var el = document.getElementById(elId);
    if (!el) return;
    el.classList.remove('diary-pos', 'diary-neg', 'diary-warn');
    var cls = pnlToneClass(val);
    if (cls) el.classList.add(cls);
    el.textContent = textOverride != null ? textOverride : fmtInrSigned(val);
  }

  function renderSummary(data) {
    if (!data) return;
    var scopeLabel = document.getElementById('diary-scope-label');
    if (scopeLabel) {
      if (data.from_date === data.to_date) {
        var prefix = data.from_date === data.today_ist ? 'Today · ' : 'Session · ';
        scopeLabel.textContent = prefix + formatPrettyDate(data.from_date) + ' IST';
      } else {
        scopeLabel.textContent =
          'Window · ' + formatPrettyDate(data.from_date) + ' → ' + formatPrettyDate(data.to_date) + ' IST';
      }
    }

    var scopeCounts = document.getElementById('diary-scope-counts');
    if (scopeCounts) {
      scopeCounts.textContent =
        '(' + (data.trade_count || 0) + ' closed trades across ' +
        (data.active_days || 0) + ' active IST session' + ((data.active_days === 1) ? '' : 's') +
        (state.strategy ? ' · Strategy: ' + state.strategy : '') + ')';
    }

    var reconPill = document.getElementById('diary-recon-pill');
    if (reconPill) {
      var anoms = Number(data.reconcile_anomalies || 0);
      if (anoms === 0) {
        reconPill.className = 'diary-recon-pill is-ok';
        reconPill.textContent = 'Gross − Commission = Net verified';
      } else {
        reconPill.className = 'diary-recon-pill is-warn';
        reconPill.textContent = anoms + ' row(s) flagged Gross − Cost ≠ Net';
      }
    }

    // 1. Net Realized P&L
    setSignedValue('dkpi-net-pnl', data.net_pnl);
    var netSub = document.getElementById('dkpi-net-sub');
    if (netSub) {
      netSub.textContent =
        'Gross ' + fmtInrSigned(data.gross_pnl) + ' − Cost ' + fmtInrAbs(data.commission);
    }

    // 2. Gross Realized P&L
    setSignedValue('dkpi-gross-pnl', data.gross_pnl);
    var grossSub = document.getElementById('dkpi-gross-sub');
    if (grossSub) {
      grossSub.textContent = (data.trade_count || 0) + ' closed trade' + (data.trade_count === 1 ? '' : 's');
    }

    // 3. Commission (Single Sum) + Drag %
    var commEl = document.getElementById('dkpi-commission');
    if (commEl) {
      commEl.textContent = fmtInrAbs(data.commission);
      commEl.className = 'diary-kpi-value' + (Number(data.commission || 0) > 0 ? ' diary-warn' : '');
    }
    var commSub = document.getElementById('dkpi-commission-sub');
    if (commSub) {
      if (data.commission_drag_pct != null) {
        commSub.textContent = 'Drag: ' + Number(data.commission_drag_pct).toFixed(1) + '% of gross profit';
      } else if (Number(data.commission || 0) > 0) {
        commSub.textContent = 'Gross ≤ ₹0 · Cost compounds drawdown';
      } else {
        commSub.textContent = 'Brokerage + statutory taxes (1 sum)';
      }
    }

    // 4. Live Running MTM (IST Today only)
    var mtmBadge = document.getElementById('dkpi-mtm-badge');
    var mtmSub = document.getElementById('dkpi-running-sub');
    if (data.includes_today) {
      if (mtmBadge) mtmBadge.textContent = (data.open_count || 0) + ' Open';
      setSignedValue('dkpi-running-mtm', data.running_mtm || 0);
      if (mtmSub) {
        mtmSub.textContent =
          'Window Net + MTM: ' + fmtInrSigned(data.total_pnl_with_mtm != null ? data.total_pnl_with_mtm : data.net_pnl);
      }
    } else {
      if (mtmBadge) mtmBadge.textContent = 'Historical';
      var mtmEl = document.getElementById('dkpi-running-mtm');
      if (mtmEl) {
        mtmEl.className = 'diary-kpi-value';
        mtmEl.textContent = 'Closed';
      }
      if (mtmSub) {
        mtmSub.textContent = 'Historical IST window (no live MTM)';
      }
    }

    // 5. Net Win Rate
    var wrEl = document.getElementById('dkpi-win-rate');
    if (wrEl) {
      var wr = Number(data.win_rate_pct || 0);
      wrEl.textContent = wr.toFixed(1) + '%';
      wrEl.className = 'diary-kpi-value' + (data.trade_count > 0 ? (wr >= 50 ? ' diary-pos' : ' diary-neg') : '');
    }
    var wrSub = document.getElementById('dkpi-win-sub');
    if (wrSub) {
      wrSub.textContent =
        (data.win_count || 0) + 'W · ' + (data.loss_count || 0) + 'L · ' + (data.flat_count || 0) + 'F';
    }

    // 6. Net Profit Factor
    var pfEl = document.getElementById('dkpi-profit-factor');
    if (pfEl) {
      if (data.profit_factor != null) {
        var pf = Number(data.profit_factor);
        pfEl.textContent = pf.toFixed(2) + '×';
        pfEl.className = 'diary-kpi-value ' + (pf >= 1.0 ? 'diary-pos' : 'diary-neg');
      } else if ((data.win_count || 0) > 0 && (data.loss_count || 0) === 0) {
        pfEl.textContent = '∞';
        pfEl.className = 'diary-kpi-value diary-pos';
      } else {
        pfEl.textContent = '—';
        pfEl.className = 'diary-kpi-value';
      }
    }
    var pfSub = document.getElementById('dkpi-pf-sub');
    if (pfSub) {
      pfSub.textContent = 'Net wins ÷ |Net losses| (BE 1.00×)';
    }

    // 7. Net Expectancy
    setSignedValue('dkpi-expectancy', data.expectancy_net || 0);
    var expSub = document.getElementById('dkpi-exp-sub');
    if (expSub) {
      expSub.textContent =
        'Avg W ' + fmtInrSigned(data.avg_win_net || 0) + ' · Avg L ' + fmtInrSigned(data.avg_loss_net || 0);
    }

    // 8. Extremes & Streak
    var extEl = document.getElementById('dkpi-extremes');
    if (extEl) {
      if ((data.trade_count || 0) > 0) {
        extEl.textContent =
          fmtInrSigned(data.best_trade_net || 0) + ' / ' + fmtInrSigned(data.worst_trade_net || 0);
      } else {
        extEl.textContent = '— / —';
      }
    }
    var streakSub = document.getElementById('dkpi-streak-sub');
    if (streakSub) {
      var st = data.streak || { type: 'flat', count: 0 };
      var stLabel = st.count > 0
        ? (st.count + '-day ' + (st.type === 'win' ? 'Win' : 'Loss') + ' streak')
        : 'No active streak';
      streakSub.textContent = stLabel;
    }
  }

  function renderCalendarGrid() {
    var gridEl = document.getElementById('diary-cal-grid');
    var labelEl = document.getElementById('diary-month-label');
    if (labelEl) {
      labelEl.textContent = (MONTH_NAMES[state.calMonth - 1] || '') + ' ' + state.calYear + ' (IST)';
    }
    if (!gridEl) return;

    var days = state.calendarDays || [];
    var dayMap = {};
    var maxAbsNet = 0;
    for (var i = 0; i < days.length; i++) {
      dayMap[days[i].date] = days[i];
      var absNet = Math.abs(Number(days[i].net_pnl || 0));
      if (days[i].trade_count > 0 && absNet > maxAbsNet) {
        maxAbsNet = absNet;
      }
    }

    var weekMap = {};
    var weeks = state.calendarWeeks || [];
    for (var w = 0; w < weeks.length; w++) {
      var wkKey = weeks[w].week_monday || weeks[w].week_start;
      weekMap[wkKey] = weeks[w];
    }

    var mb = monthBounds(state.calYear, state.calMonth);
    var gridStart = mondayOfWeekIso(mb.from);
    var endMonday = mondayOfWeekIso(mb.to);
    var gridEnd = shiftDaysIso(endMonday, 6);

    var html = '';
    for (var h = 0; h < DOW_LABELS.length; h++) {
      html += '<div class="diary-cal-dow" role="columnheader">' + DOW_LABELS[h] + '</div>';
    }
    html += '<div class="diary-cal-dow diary-cal-dow-week" role="columnheader">Week Net</div>';

    var cur = gridStart;
    while (cur <= gridEnd) {
      var weekStart = cur;
      for (var col = 0; col < 7; col++) {
        var dInfo = dayMap[cur];
        var cp = parseIsoDate(cur);
        var inMonth = (cp.year === state.calYear && cp.month === state.calMonth);
        if (!inMonth || !dInfo) {
          html += '<div class="diary-day-cell is-outside" aria-disabled="true">' +
            '<div class="diary-day-top"><span class="diary-day-num">' + pad2(cp.day) + '</span></div>' +
            '</div>';
        } else {
          var isSelected = (state.selFrom && state.selTo && cur >= state.selFrom && cur <= state.selTo);
          var heatCls = computeHeatClass(dInfo.net_pnl, maxAbsNet, dInfo.trade_count);
          var classes = ['diary-day-cell'];
          if (dInfo.is_weekend) classes.push('is-weekend');
          if (dInfo.is_holiday) classes.push('is-holiday');
          if (dInfo.is_today) classes.push('is-today');
          if (isSelected) classes.push('is-selected');
          if (heatCls) classes.push(heatCls);

          var tagHtml = '';
          if ((dInfo.reconcile_anomalies || 0) > 0) {
            tagHtml = '<span class="diary-day-tag tag-anom" title="Gross − Commission ≠ Net anomaly">!</span>';
          } else if (dInfo.is_today) {
            tagHtml = '<span class="diary-day-tag tag-today">Today</span>';
          } else if (dInfo.is_holiday) {
            tagHtml = '<span class="diary-day-tag tag-holiday">Holiday</span>';
          }

          var bodyHtml = '';
          if (dInfo.trade_count > 0) {
            var tone = pnlToneClass(dInfo.net_pnl);
            bodyHtml += '<div class="diary-day-pnl ' + tone + '">' + fmtInrSigned(dInfo.net_pnl) + '</div>';
            bodyHtml += '<div class="diary-day-meta">' +
              '<span>' + dInfo.trade_count + ' tr · ' + Number(dInfo.win_rate_pct || 0).toFixed(0) + '% W</span>' +
              '<span>Cost ' + fmtInrAbs(dInfo.commission) + '</span>' +
              '</div>';
          } else if (dInfo.is_holiday) {
            bodyHtml += '<div class="diary-day-meta"><span>' +
              escapeHtml(dInfo.holiday_name || 'NSE Holiday') + '</span></div>';
          } else if (dInfo.is_weekend) {
            bodyHtml += '<div class="diary-day-meta"><span>Weekend</span></div>';
          } else {
            bodyHtml += '<div class="diary-day-meta"><span>Flat</span></div>';
          }

          if (dInfo.is_today && (dInfo.open_count || 0) > 0) {
            bodyHtml += '<div class="diary-day-mtm">MTM ' + fmtInrSigned(dInfo.running_mtm || 0) +
              ' (' + dInfo.open_count + ')</div>';
          }

          html += '<button type="button" class="' + classes.join(' ') + '" data-date="' + cur + '" ' +
            'aria-pressed="' + (isSelected ? 'true' : 'false') + '">' +
            '<div class="diary-day-top"><span class="diary-day-num">' + pad2(cp.day) + '</span>' + tagHtml + '</div>' +
            bodyHtml +
            '</button>';
        }
        cur = shiftDaysIso(cur, 1);
      }

      // Weekly rollup cell (8th column)
      var wk = weekMap[weekStart];
      if (wk) {
        var wkTone = pnlToneClass(wk.net_pnl);
        html += '<button type="button" class="diary-week-cell" ' +
          'data-week-from="' + wk.start_date + '" data-week-to="' + wk.end_date + '" ' +
          'title="Select week ' + wk.start_date + ' to ' + wk.end_date + '">' +
          '<div class="diary-day-top" style="width:100%;justify-content:flex-end;">' +
          '<span class="muted" style="font-size:10px;">' + wk.start_date.slice(5) + '→' + wk.end_date.slice(5) + '</span>' +
          '</div>' +
          '<div class="diary-day-pnl ' + wkTone + '">' +
          (wk.trade_count > 0 ? fmtInrSigned(wk.net_pnl) : '₹0.00') + '</div>' +
          '<div class="diary-day-meta" style="text-align:right;">' +
          '<span>' + wk.trade_count + ' tr · ' + wk.active_days + 'd</span>' +
          '<span>Cost ' + fmtInrAbs(wk.commission) + '</span>' +
          '</div>' +
          '</button>';
      } else {
        html += '<div class="diary-week-cell" aria-disabled="true"></div>';
      }
    }

    gridEl.innerHTML = html;
  }

  function renderSessionBreakdownTable(daysInWindow) {
    var wrap = document.getElementById('diary-sessions-table-wrap');
    if (!wrap) return;

    var activeRows = [];
    for (var i = 0; i < daysInWindow.length; i++) {
      var d = daysInWindow[i];
      if (d.trade_count > 0 || (d.is_today && (d.open_count || 0) > 0) || d.date === state.selFrom && state.selFrom === state.selTo) {
        activeRows.push(d);
      }
    }
    // Sort reverse chronological (most recent IST session on top)
    activeRows.sort(function (a, b) {
      return a.date < b.date ? 1 : a.date > b.date ? -1 : 0;
    });

    if (activeRows.length === 0) {
      wrap.innerHTML = '<div class="diary-empty">No closed trades or active positions in this IST window for the ' +
        (state.mode === 'live' ? 'Live' : 'Paper') + ' book.</div>';
      return;
    }

    var html = '<table class="diary-table" id="diary-sessions-table">' +
      '<thead><tr>' +
      '<th>IST Session</th>' +
      '<th>Status</th>' +
      '<th class="num">Trades</th>' +
      '<th class="num">Win %</th>' +
      '<th class="num">Gross P&amp;L</th>' +
      '<th class="num">Commission</th>' +
      '<th class="num">Net P&amp;L</th>' +
      '<th class="num">Running MTM</th>' +
      '</tr></thead><tbody>';

    for (var r = 0; r < activeRows.length; r++) {
      var row = activeRows[r];
      var statusBadge = row.is_today
        ? 'Today (IST)'
        : row.is_holiday
          ? ('Holiday · ' + escapeHtml(row.holiday_name || 'NSE'))
          : row.is_weekend
            ? 'Weekend'
            : 'Regular Session';
      var mtmText = row.is_today
        ? fmtInrSigned(row.running_mtm || 0) + (row.open_count ? ' (' + row.open_count + ')' : '')
        : '—';

      html += '<tr class="is-clickable" data-session-date="' + row.date + '">' +
        '<td class="tabular-nums"><strong>' + row.date + '</strong></td>' +
        '<td>' + statusBadge + '</td>' +
        '<td class="num">' + row.trade_count + ' (' + row.win_count + 'W/' + row.loss_count + 'L)</td>' +
        '<td class="num">' + Number(row.win_rate_pct || 0).toFixed(1) + '%</td>' +
        '<td class="num ' + pnlToneClass(row.gross_pnl) + '">' + fmtInrSigned(row.gross_pnl) + '</td>' +
        '<td class="num">' + fmtInrAbs(row.commission) + '</td>' +
        '<td class="num ' + pnlToneClass(row.net_pnl) + '"><strong>' + fmtInrSigned(row.net_pnl) + '</strong></td>' +
        '<td class="num ' + (row.is_today ? pnlToneClass(row.running_mtm) : '') + '">' + mtmText + '</td>' +
        '</tr>';
    }
    html += '</tbody></table>';
    wrap.innerHTML = html;
  }

  function buildQuery(paramsObj) {
    var usp = new URLSearchParams();
    for (var k in paramsObj) {
      if (Object.prototype.hasOwnProperty.call(paramsObj, k) && paramsObj[k] != null && paramsObj[k] !== '') {
        usp.set(k, paramsObj[k]);
      }
    }
    return usp.toString();
  }

  function fetchJson(url) {
    return fetch(url, { headers: { 'Accept': 'application/json' } }).then(function (resp) {
      return resp.json().then(function (body) {
        if (!resp.ok || (body && body.success === false)) {
          throw new Error((body && body.error) || ('HTTP ' + resp.status));
        }
        return body;
      });
    });
  }

  function loadAll() {
    var mb = monthBounds(state.calYear, state.calMonth);
    var calQs = buildQuery({
      mode: state.mode,
      from: mb.from,
      to: mb.to,
      strategy: state.strategy
    });
    var sumQs = buildQuery({
      mode: state.mode,
      from: state.selFrom,
      to: state.selTo,
      strategy: state.strategy
    });
    // Also fetch calendar days for selected window if it spans outside visible month
    var winCalPromise = (state.selFrom >= mb.from && state.selTo <= mb.to)
      ? null
      : fetchJson('/api/diary/calendar?' + sumQs);

    return Promise.all([
      fetchJson('/api/diary/calendar?' + calQs),
      fetchJson('/api/diary/summary?' + sumQs),
      winCalPromise
    ]).then(function (res) {
      var calData = res[0];
      var sumData = res[1];
      var winCal = res[2];

      if (calData.today_ist) {
        state.todayIst = calData.today_ist;
      }
      state.calendarDays = calData.days || [];
      state.calendarWeeks = calData.weeks || [];

      populateStrategies(sumData.available_strategies || calData.available_strategies || []);
      renderSummary(sumData);
      renderCalendarGrid();

      var windowDays = [];
      var sourceDays = winCal ? (winCal.days || []) : state.calendarDays;
      for (var i = 0; i < sourceDays.length; i++) {
        var d = sourceDays[i];
        if (d.date >= state.selFrom && d.date <= state.selTo) {
          windowDays.push(d);
        }
      }
      renderSessionBreakdownTable(windowDays);
    }).catch(function (err) {
      var scopeLabel = document.getElementById('diary-scope-label');
      if (scopeLabel) {
        scopeLabel.textContent = 'Diary load error: ' + (err && err.message ? err.message : String(err));
      }
    });
  }

  function selectDateCell(clickedDate, shiftKey) {
    if (!clickedDate) return;
    if (shiftKey && state.anchorDate) {
      state.selFrom = clickedDate < state.anchorDate ? clickedDate : state.anchorDate;
      state.selTo = clickedDate > state.anchorDate ? clickedDate : state.anchorDate;
    } else {
      state.anchorDate = clickedDate;
      state.selFrom = clickedDate;
      state.selTo = clickedDate;
    }
    setPresetPills(state.selFrom === state.todayIst && state.selTo === state.todayIst ? 'today' : '');
    syncDateInputs();
    loadAll();
  }

  function stepMonth(delta) {
    var m = state.calMonth + delta;
    var y = state.calYear;
    if (m < 1) { m = 12; y -= 1; }
    if (m > 12) { m = 1; y += 1; }
    state.calYear = y;
    state.calMonth = m;
    var mb = monthBounds(y, m);
    state.selFrom = mb.from;
    state.selTo = (y === parseIsoDate(state.todayIst).year && m === parseIsoDate(state.todayIst).month && state.todayIst < mb.to)
      ? state.todayIst
      : mb.to;
    state.anchorDate = state.selFrom;
    setPresetPills('');
    syncDateInputs();
    loadAll();
  }

  function bindEvents() {
    var modeEl = document.getElementById('diary-mode');
    if (modeEl) {
      modeEl.addEventListener('change', function () {
        var next = (modeEl.value || '').toLowerCase() === 'live' ? 'live' : 'paper';
        state.mode = next;
        state.strategy = '';
        syncModeChrome(next);
        loadAll();
      });
    }

    var stratEl = document.getElementById('diary-strategy');
    if (stratEl) {
      stratEl.addEventListener('change', function () {
        state.strategy = stratEl.value || '';
        loadAll();
      });
    }

    var pills = document.querySelectorAll('.diary-range-pill');
    for (var i = 0; i < pills.length; i++) {
      pills[i].addEventListener('click', function (ev) {
        var preset = ev.currentTarget.getAttribute('data-preset');
        applyPreset(preset);
        loadAll();
      });
    }

    var applyBtn = document.getElementById('diary-apply');
    if (applyBtn) {
      applyBtn.addEventListener('click', function () {
        var fromVal = (document.getElementById('diary-from') || {}).value || '';
        var toVal = (document.getElementById('diary-to') || {}).value || '';
        if (!fromVal || !toVal) return;
        if (fromVal > toVal) {
          var tmp = fromVal; fromVal = toVal; toVal = tmp;
        }
        state.selFrom = fromVal;
        state.selTo = toVal;
        state.anchorDate = fromVal;
        var fp = parseIsoDate(fromVal);
        state.calYear = fp.year;
        state.calMonth = fp.month;
        setPresetPills('');
        syncDateInputs();
        loadAll();
      });
    }

    var prevBtn = document.getElementById('diary-month-prev');
    if (prevBtn) prevBtn.addEventListener('click', function () { stepMonth(-1); });
    var nextBtn = document.getElementById('diary-month-next');
    if (nextBtn) nextBtn.addEventListener('click', function () { stepMonth(1); });

    var refreshBtn = document.getElementById('diary-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', function () { loadAll(); });

    var gridEl = document.getElementById('diary-cal-grid');
    if (gridEl) {
      gridEl.addEventListener('click', function (ev) {
        var dayBtn = ev.target.closest ? ev.target.closest('[data-date]') : null;
        if (dayBtn) {
          selectDateCell(dayBtn.getAttribute('data-date'), Boolean(ev.shiftKey));
          return;
        }
        var wkBtn = ev.target.closest ? ev.target.closest('[data-week-from]') : null;
        if (wkBtn) {
          state.selFrom = wkBtn.getAttribute('data-week-from');
          state.selTo = wkBtn.getAttribute('data-week-to');
          state.anchorDate = state.selFrom;
          setPresetPills('');
          syncDateInputs();
          loadAll();
        }
      });
    }

    var tableWrap = document.getElementById('diary-sessions-table-wrap');
    if (tableWrap) {
      tableWrap.addEventListener('click', function (ev) {
        var tr = ev.target.closest ? ev.target.closest('[data-session-date]') : null;
        if (tr) {
          selectDateCell(tr.getAttribute('data-session-date'), false);
        }
      });
    }
  }

  function init() {
    state.mode = resolveInitialMode();
    syncModeChrome(state.mode);
    applyPreset('mtd');
    bindEvents();
    return loadAll();
  }

  var PnLDiary = {
    init: init,
    loadAll: loadAll,
    fmtInrAbs: fmtInrAbs,
    fmtInrSigned: fmtInrSigned,
    computeHeatClass: computeHeatClass,
    monthBounds: monthBounds,
    mondayOfWeekIso: mondayOfWeekIso,
    _state: state
  };

  root.PnLDiary = PnLDiary;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = PnLDiary;
  }
})(typeof globalThis !== 'undefined' ? globalThis : window);
