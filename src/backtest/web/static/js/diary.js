/* PnL Diary & Institutional Edge Suite — Slice 1 + Slice 2 Controller (diary.js)
 *
 * Strictly segregates Paper vs Live books (?mode=paper|live), enforces IST
 * calendar date math, renders exact 2dp Gross − Commission = Net figures
 * with a single total Commission sum (zero fee-component breakdown), and
 * mounts the 3-level Strategy → Spawned Instance → Trade/Structure tree.
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

  function pfRankValue(node) {
    if (node.unbounded_pf) return 1e9;
    if (node.profit_factor == null) return -1;
    return Number(node.profit_factor);
  }

  function makeComparator(sortKey) {
    return function (a, b) {
      if (sortKey === 'net_desc') {
        return Number(b.net_pnl || 0) - Number(a.net_pnl || 0);
      }
      if (sortKey === 'comm_desc') {
        return Number(b.commission || 0) - Number(a.commission || 0);
      }
      if (sortKey === 'trades_desc') {
        return Number(b.trade_count || 0) - Number(a.trade_count || 0);
      }
      if (sortKey === 'wr_desc') {
        return Number(b.win_rate_pct || 0) - Number(a.win_rate_pct || 0);
      }
      if (sortKey === 'pf_desc') {
        return pfRankValue(b) - pfRankValue(a);
      }
      // Default: net_asc (worst bleeders surfaced first)
      return Number(a.net_pnl || 0) - Number(b.net_pnl || 0);
    };
  }

  function sortAttributionStrategies(strategies, sortKey) {
    if (!Array.isArray(strategies)) return [];
    var cmp = makeComparator(sortKey || 'net_asc');
    var copy = strategies.map(function (s) {
      var instCopy = Array.isArray(s.instances) ? s.instances.slice().sort(cmp) : [];
      var cloned = {};
      for (var k in s) {
        if (Object.prototype.hasOwnProperty.call(s, k)) cloned[k] = s[k];
      }
      cloned.instances = instCopy;
      return cloned;
    });
    copy.sort(cmp);
    return copy;
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
    activeTab: 'attribution',
    attrSort: 'net_asc',
    regimeClock: 'entry',
    regimeGroupBy: 'strategy',
    payoffGroupBy: 'instance',
    selectedPayoffKey: '',
    calendarDays: [],
    calendarWeeks: [],
    attributionData: null,
    regimesData: null,
    payoffData: null,
    graduationData: null,
    notesData: null,
    selectedJournalTags: {},
    expandedStrategies: {},
    expandedInstances: {},
    hasAutoExpanded: false
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
    var journalBadgeEl = document.getElementById('diary-journal-book-badge');
    if (journalBadgeEl) {
      journalBadgeEl.setAttribute('data-mode', mode);
      journalBadgeEl.textContent = mode === 'live' ? 'LIVE JOURNAL' : 'PAPER JOURNAL';
    }
    var gradTitleEl = document.getElementById('diary-graduation-title');
    if (gradTitleEl) {
      gradTitleEl.innerHTML = mode === 'live'
        ? 'Live Runner Health &amp; Continuation Scorecard'
        : 'Paper &rarr; Live Graduation Scorecard';
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

  function switchTab(tabName) {
    var allowed = {
      attribution: true,
      regimes: true,
      payoff: true,
      graduation: true,
      sessions: true
    };
    state.activeTab = allowed[tabName] ? tabName : 'attribution';
    var tabs = document.querySelectorAll('.diary-tab-btn');
    for (var i = 0; i < tabs.length; i++) {
      var isSel = tabs[i].getAttribute('data-tab') === state.activeTab;
      tabs[i].classList.toggle('is-active', isSel);
      tabs[i].setAttribute('aria-selected', isSel ? 'true' : 'false');
    }
    var pAttr = document.getElementById('diary-panel-attribution');
    var pReg = document.getElementById('diary-panel-regimes');
    var pPay = document.getElementById('diary-panel-payoff');
    var pGrad = document.getElementById('diary-panel-graduation');
    var pSess = document.getElementById('diary-panel-sessions');
    if (pAttr) pAttr.hidden = state.activeTab !== 'attribution';
    if (pReg) pReg.hidden = state.activeTab !== 'regimes';
    if (pPay) pPay.hidden = state.activeTab !== 'payoff';
    if (pGrad) pGrad.hidden = state.activeTab !== 'graduation';
    if (pSess) pSess.hidden = state.activeTab !== 'sessions';
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

  function fmtPfText(node) {
    if (node.profit_factor != null) {
      return Number(node.profit_factor).toFixed(2) + '×';
    }
    if (node.unbounded_pf) return '∞';
    return '—';
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

    setSignedValue('dkpi-net-pnl', data.net_pnl);
    var netSub = document.getElementById('dkpi-net-sub');
    if (netSub) {
      netSub.textContent =
        'Gross ' + fmtInrSigned(data.gross_pnl) + ' − Cost ' + fmtInrAbs(data.commission);
    }

    setSignedValue('dkpi-gross-pnl', data.gross_pnl);
    var grossSub = document.getElementById('dkpi-gross-sub');
    if (grossSub) {
      grossSub.textContent = (data.trade_count || 0) + ' closed trade' + (data.trade_count === 1 ? '' : 's');
    }

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

    setSignedValue('dkpi-expectancy', data.expectancy_net || 0);
    var expSub = document.getElementById('dkpi-exp-sub');
    if (expSub) {
      expSub.textContent =
        'Avg W ' + fmtInrSigned(data.avg_win_net || 0) + ' · Avg L ' + fmtInrSigned(data.avg_loss_net || 0);
    }

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

  function renderInstanceDrawerHtml(inst, includesToday) {
    var html = '';
    var openRows = inst.open_positions || [];
    if (includesToday && openRows.length > 0) {
      html += '<div class="diary-drawer-section">' +
        '<div class="diary-drawer-title">Live Open Positions / Option Structures (' + openRows.length + ' · Gross MTM)</div>' +
        '<table class="diary-subtable">' +
        '<thead><tr>' +
        '<th>Instrument / Structure</th>' +
        '<th>Kind</th>' +
        '<th>Side</th>' +
        '<th class="num">Qty</th>' +
        '<th class="num">Entry</th>' +
        '<th class="num">Last Mark</th>' +
        '<th class="num">Unrealized MTM</th>' +
        '</tr></thead><tbody>';

      for (var o = 0; o < openRows.length; o++) {
        var op = openRows[o];
        html += '<tr>' +
          '<td><strong>' + escapeHtml(op.display_symbol || op.symbol) + '</strong>' +
          (op.mark_stale ? ' <span class="diary-recon-flag is-bad">STALE</span>' : '') + '</td>' +
          '<td>' + escapeHtml(op.kind || 'equity') + '</td>' +
          '<td>' + escapeHtml(op.side || 'LONG') + '</td>' +
          '<td class="num">' + Number(op.qty || 0) + '</td>' +
          '<td class="num">' + fmtInrAbs(op.entry_price) + '</td>' +
          '<td class="num">' + fmtInrAbs(op.current_price) + '</td>' +
          '<td class="num ' + pnlToneClass(op.unrealized_pnl) + '"><strong>' +
          fmtInrSigned(op.unrealized_pnl) + '</strong></td>' +
          '</tr>';
      }
      html += '</tbody></table></div>';
    }

    var trades = inst.trades || [];
    html += '<div class="diary-drawer-section">' +
      '<div class="diary-drawer-title">Closed Trade &amp; Structure Ledger (' +
      inst.trade_count + ' round-trip' + (inst.trade_count === 1 ? '' : 's') +
      ' · Single Commission Sum · 2dp Reconciled)</div>';

    if (trades.length === 0) {
      html += '<div class="muted" style="font-size:12px;padding:6px 0;">No closed round-trips in this IST window for this instance.</div>';
    } else {
      html += '<table class="diary-subtable">' +
        '<thead><tr>' +
        '<th>Exit (IST)</th>' +
        '<th>Entry (IST)</th>' +
        '<th>Hold</th>' +
        '<th>Instrument / Structure</th>' +
        '<th>Side</th>' +
        '<th class="num">Qty</th>' +
        '<th class="num">Entry &rarr; Exit</th>' +
        '<th>Exit Reason</th>' +
        '<th class="num">Gross P&amp;L</th>' +
        '<th class="num">Commission</th>' +
        '<th class="num">Net P&amp;L</th>' +
        '<th>Recon</th>' +
        '</tr></thead><tbody>';

      for (var t = 0; t < trades.length; t++) {
        var tr = trades[t];
        var qtyLabel = tr.kind === 'option' && tr.units
          ? (Number(tr.qty) + ' (' + tr.units + 'u)')
          : String(Number(tr.qty || 0));
        var reconCell = tr.reconcile_ok
          ? '<span class="diary-recon-flag is-ok" title="Gross − Commission = Net verified">&#10003;</span>'
          : '<span class="diary-recon-flag is-bad" title="Recorded Net differs by ' +
            fmtInrSigned(tr.reconcile_diff) + '">&Delta; ' + fmtInrAbs(tr.reconcile_diff) + '</span>';

        html += '<tr>' +
          '<td class="tabular-nums">' + escapeHtml(tr.exit_ist_label || tr.session_date || '—') + '</td>' +
          '<td class="tabular-nums muted">' + escapeHtml(tr.entry_ist_label || '—') + '</td>' +
          '<td class="tabular-nums">' + escapeHtml(tr.holding_display || '—') + '</td>' +
          '<td><strong>' + escapeHtml(tr.display_symbol || tr.symbol) + '</strong></td>' +
          '<td>' + escapeHtml(tr.side || 'LONG') + '</td>' +
          '<td class="num">' + escapeHtml(qtyLabel) + '</td>' +
          '<td class="num">' + fmtInrAbs(tr.entry_price) + ' &rarr; ' + fmtInrAbs(tr.exit_price) + '</td>' +
          '<td><span class="diary-cfg-pill">' + escapeHtml(tr.exit_reason || 'unspecified') + '</span></td>' +
          '<td class="num ' + pnlToneClass(tr.gross_pnl) + '">' + fmtInrSigned(tr.gross_pnl) + '</td>' +
          '<td class="num">' + fmtInrAbs(tr.commission) + '</td>' +
          '<td class="num ' + pnlToneClass(tr.net_pnl) + '"><strong>' + fmtInrSigned(tr.net_pnl) + '</strong></td>' +
          '<td>' + reconCell + '</td>' +
          '</tr>';
      }
      html += '</tbody></table>';

      if (inst.trades_truncated) {
        html += '<div style="margin-top:6px;">' +
          '<button type="button" class="btn btn-ghost btn-small" data-load-full-instance="' +
          escapeHtml(inst.instance_key) + '">Load all ' + inst.trade_count + ' trades for this instance</button>' +
          '</div>';
      }
    }
    html += '</div>';
    return html;
  }

  function renderAttributionTree() {
    var wrap = document.getElementById('diary-attr-tree-wrap');
    var badgeEl = document.getElementById('diary-attr-summary-badge');
    var data = state.attributionData;
    if (!wrap) return;
    if (!data) {
      wrap.innerHTML = '<div class="diary-empty">No attribution data loaded.</div>';
      return;
    }

    var totals = data.totals || {};
    if (badgeEl) {
      badgeEl.textContent =
        (totals.strategy_count || 0) + ' strateg' + (totals.strategy_count === 1 ? 'y' : 'ies') +
        ' · ' + (totals.instance_count || 0) + ' spawned instance' + (totals.instance_count === 1 ? '' : 's') +
        ' (' + (totals.active_instance_count || 0) + ' active · ' +
        (totals.retired_instance_count || 0) + ' retired)';
    }

    var rawStrategies = data.strategies || [];
    if (rawStrategies.length === 0) {
      wrap.innerHTML = '<div class="diary-empty">No closed trades or active positions in this IST window for the ' +
        (state.mode === 'live' ? 'Live' : 'Paper') + ' book.</div>';
      return;
    }

    if (!state.hasAutoExpanded && rawStrategies.length <= 5) {
      state.expandedStrategies[rawStrategies[0].strategy_name] = true;
      state.hasAutoExpanded = true;
    }

    var sorted = sortAttributionStrategies(rawStrategies, state.attrSort);
    var includesToday = Boolean(data.includes_today);
    var maxAbs = Number(data.max_abs_strategy_net || 0);

    var html = '<table class="diary-table diary-tree-table">' +
      '<thead><tr>' +
      '<th>Strategy / Spawned Instance</th>' +
      '<th class="num">Trades</th>' +
      '<th class="num">Win %</th>' +
      '<th class="num">PF</th>' +
      '<th class="num">Expectancy</th>' +
      '<th class="num">Gross P&amp;L</th>' +
      '<th class="num">Commission</th>' +
      '<th class="num">Net P&amp;L</th>' +
      '<th class="num">' + (includesToday ? 'Running MTM' : 'Total Net') + '</th>' +
      '</tr></thead><tbody>';

    for (var sIdx = 0; sIdx < sorted.length; sIdx++) {
      var st = sorted[sIdx];
      var sName = st.strategy_name;
      var sOpen = Boolean(state.expandedStrategies[sName]);
      var barPct = maxAbs > 0 ? Math.min(100, Math.round((Math.abs(Number(st.net_pnl || 0)) / maxAbs) * 100)) : 0;
      var barTone = Number(st.net_pnl || 0) >= 0 ? 'is-pos' : 'is-neg';
      var isFiltered = state.strategy === sName;

      html += '<tr class="diary-row-strategy">' +
        '<td>' +
        '<button type="button" class="diary-expand-btn" data-toggle-strategy="' + escapeHtml(sName) + '" ' +
        'aria-expanded="' + (sOpen ? 'true' : 'false') + '">' +
        '<span class="diary-chevron">&#9654;</span>' +
        '<span>' + escapeHtml(sName) + '</span>' +
        '</button>' +
        '<span class="diary-cfg-pill" style="margin-left:6px;">' +
        st.instance_count + ' inst (' + st.active_instance_count + ' live · ' + st.retired_instance_count + ' ret)' +
        '</span>' +
        '<button type="button" class="diary-strat-filter-btn' + (isFiltered ? ' is-filtered' : '') + '" ' +
        'data-filter-strategy="' + escapeHtml(sName) + '" title="Filter calendar &amp; KPIs to this strategy">' +
        (isFiltered ? 'Filtered ✓' : 'Filter') +
        '</button>' +
        '</td>' +
        '<td class="num">' + st.trade_count + ' (' + st.win_count + 'W/' + st.loss_count + 'L)</td>' +
        '<td class="num">' + Number(st.win_rate_pct || 0).toFixed(1) + '%</td>' +
        '<td class="num">' + fmtPfText(st) + '</td>' +
        '<td class="num ' + pnlToneClass(st.expectancy_net) + '">' + fmtInrSigned(st.expectancy_net) + '</td>' +
        '<td class="num ' + pnlToneClass(st.gross_pnl) + '">' + fmtInrSigned(st.gross_pnl) + '</td>' +
        '<td class="num">' + fmtInrAbs(st.commission) +
        (st.commission_drag_pct != null ? ' <span class="muted" style="font-size:10px;">(' + Number(st.commission_drag_pct).toFixed(0) + '%)</span>' : '') +
        '</td>' +
        '<td class="num">' +
        '<div class="diary-contrib-wrap">' +
        '<span class="' + pnlToneClass(st.net_pnl) + '"><strong>' + fmtInrSigned(st.net_pnl) + '</strong></span>' +
        '<span class="diary-contrib-track"><span class="diary-contrib-fill ' + barTone + '" style="width:' + barPct + '%"></span></span>' +
        '</div>' +
        '</td>' +
        '<td class="num ' + pnlToneClass(includesToday ? st.running_mtm : st.net_pnl) + '">' +
        (includesToday ? fmtInrSigned(st.running_mtm || 0) : fmtInrSigned(st.net_pnl)) +
        '</td>' +
        '</tr>';

      if (sOpen) {
        var instances = st.instances || [];
        for (var iIdx = 0; iIdx < instances.length; iIdx++) {
          var inst = instances[iIdx];
          var iKey = inst.instance_key;
          var iOpen = Boolean(state.expandedInstances[iKey]);

          var pillsHtml = '<span class="diary-status-pill" data-status="' +
            escapeHtml(inst.lifecycle_status) + '">' + escapeHtml(inst.lifecycle_status) + '</span>';

          if (inst.symbols && inst.symbols.length > 0) {
            var symText = inst.symbols.slice(0, 3).join(', ') +
              (inst.symbol_count > 3 ? ' +' + (inst.symbol_count - 3) : '');
            pillsHtml += '<span class="diary-cfg-pill">' + escapeHtml(symText) + '</span>';
          }
          if (inst.allocated_capital != null && Number(inst.allocated_capital) > 0) {
            pillsHtml += '<span class="diary-cfg-pill">Cap ' + fmtInrAbs(inst.allocated_capital) + '</span>';
          }
          var cfgBadges = inst.config_badges || [];
          for (var b = 0; b < cfgBadges.length; b++) {
            pillsHtml += '<span class="diary-cfg-pill">' + escapeHtml(cfgBadges[b]) + '</span>';
          }
          if (inst.reconcile_anomalies > 0) {
            pillsHtml += '<span class="diary-recon-flag is-bad">' + inst.reconcile_anomalies + ' recon !</span>';
          }
          if (inst.is_active && inst.instance_id) {
            pillsHtml += '<a class="diary-inst-link" href="/portfolio/' + state.mode +
              '?instance=' + encodeURIComponent(inst.instance_id) + '">Inspect runner &nearr;</a>';
          }

          html += '<tr class="diary-row-instance">' +
            '<td>' +
            '<button type="button" class="diary-expand-btn" style="font-weight:600;font-size:12.5px;" ' +
            'data-toggle-instance="' + escapeHtml(iKey) + '" aria-expanded="' + (iOpen ? 'true' : 'false') + '">' +
            '<span class="diary-chevron">&#9654;</span>' +
            '<span>' + escapeHtml(inst.display_name) + '</span>' +
            '</button>' +
            '<div class="diary-inst-meta">' + pillsHtml + '</div>' +
            '</td>' +
            '<td class="num">' + inst.trade_count + ' (' + inst.win_count + 'W/' + inst.loss_count + 'L)</td>' +
            '<td class="num">' + Number(inst.win_rate_pct || 0).toFixed(1) + '%</td>' +
            '<td class="num">' + fmtPfText(inst) + '</td>' +
            '<td class="num ' + pnlToneClass(inst.expectancy_net) + '">' + fmtInrSigned(inst.expectancy_net) + '</td>' +
            '<td class="num ' + pnlToneClass(inst.gross_pnl) + '">' + fmtInrSigned(inst.gross_pnl) + '</td>' +
            '<td class="num">' + fmtInrAbs(inst.commission) + '</td>' +
            '<td class="num ' + pnlToneClass(inst.net_pnl) + '"><strong>' + fmtInrSigned(inst.net_pnl) + '</strong></td>' +
            '<td class="num ' + pnlToneClass(includesToday ? inst.running_mtm : inst.net_pnl) + '">' +
            (includesToday
              ? (inst.is_active ? fmtInrSigned(inst.running_mtm || 0) : '<span class="muted">Retired</span>')
              : fmtInrSigned(inst.net_pnl)) +
            '</td>' +
            '</tr>';

          if (iOpen) {
            html += '<tr class="diary-row-drawer"><td colspan="9">' +
              renderInstanceDrawerHtml(inst, includesToday) +
              '</td></tr>';
          }
        }
      }
    }

    // Book Grand Total Footer Row (verifies Sum(Strategies) == Summary Tile)
    html += '<tr class="diary-row-strategy" style="border-top:2px solid var(--border);">' +
      '<td><strong>Book Window Total (' + (state.mode === 'live' ? 'Live' : 'Paper') + ')</strong></td>' +
      '<td class="num"><strong>' + (totals.trade_count || 0) + '</strong></td>' +
      '<td class="num"><strong>' + Number(totals.win_rate_pct || 0).toFixed(1) + '%</strong></td>' +
      '<td class="num"><strong>' + fmtPfText(totals) + '</strong></td>' +
      '<td class="num ' + pnlToneClass(totals.expectancy_net) + '"><strong>' + fmtInrSigned(totals.expectancy_net || 0) + '</strong></td>' +
      '<td class="num ' + pnlToneClass(totals.gross_pnl) + '"><strong>' + fmtInrSigned(totals.gross_pnl || 0) + '</strong></td>' +
      '<td class="num"><strong>' + fmtInrAbs(totals.commission || 0) + '</strong></td>' +
      '<td class="num ' + pnlToneClass(totals.net_pnl) + '"><strong>' + fmtInrSigned(totals.net_pnl || 0) + '</strong></td>' +
      '<td class="num ' + pnlToneClass(includesToday ? totals.running_mtm : totals.net_pnl) + '"><strong>' +
      (includesToday ? fmtInrSigned(totals.running_mtm || 0) : fmtInrSigned(totals.net_pnl || 0)) +
      '</strong></td>' +
      '</tr>';

    html += '</tbody></table>';
    wrap.innerHTML = html;
  }

  function computeMatrixCellHeat(netPnl, maxAbsNet, tradeCount) {
    var n = Number(netPnl || 0);
    if (!tradeCount || Math.abs(n) < 0.005 || maxAbsNet <= 0) return '';
    var ratio = Math.min(1, Math.abs(n) / maxAbsNet);
    var tier = ratio <= 0.25 ? 1 : ratio <= 0.5 ? 2 : ratio <= 0.75 ? 3 : 4;
    return (n > 0 ? 'mheat-pos-' : 'mheat-neg-') + tier;
  }

  function renderMatrixCellHtml(cell, maxAbsNet, isTotalCol) {
    var c = cell || { trade_count: 0, net_pnl: 0, win_rate_pct: 0, commission: 0 };
    var heat = isTotalCol ? '' : computeMatrixCellHeat(c.net_pnl, maxAbsNet, c.trade_count);
    var cls = 'diary-mcell' + (isTotalCol ? ' diary-matrix-tot' : '') + (heat ? ' ' + heat : '');
    if (!c.trade_count) {
      return '<td class="' + cls + '"><span class="muted">—</span></td>';
    }
    var tone = pnlToneClass(c.net_pnl);
    return '<td class="' + cls + '">' +
      '<div class="diary-mcell-net ' + tone + '">' + fmtInrSigned(c.net_pnl) + '</div>' +
      '<div class="diary-mcell-sub">' + c.trade_count + 'tr · ' +
      Number(c.win_rate_pct || 0).toFixed(0) + '%W · Cost ' + fmtInrAbs(c.commission) + '</div>' +
      '</td>';
  }

  function buildMatrixHtml(matrix, rowHeaderLabel, modeLabel) {
    if (!matrix || !Array.isArray(matrix.rows) || matrix.rows.length === 0) {
      return '<div class="diary-empty">No closed trades in this IST window for the ' +
        escapeHtml(modeLabel || 'selected') + ' book.</div>';
    }
    var cols = matrix.columns || [];
    var rows = matrix.rows || [];
    var colTots = matrix.column_totals || {};
    var grand = matrix.grand_totals || {};
    var maxAbs = Number(matrix.max_abs_cell_net || 0);

    var html = '<table class="diary-table diary-matrix-table">' +
      '<thead><tr><th>' + escapeHtml(rowHeaderLabel) + '</th>';
    for (var c = 0; c < cols.length; c++) {
      html += '<th class="num">' + escapeHtml(cols[c].label) +
        (cols[c].subtitle ? '<span class="diary-matrix-col-sub">' + escapeHtml(cols[c].subtitle) + '</span>' : '') +
        '</th>';
    }
    html += '<th class="num diary-matrix-tot">Row Total</th></tr></thead><tbody>';

    for (var r = 0; r < rows.length; r++) {
      var row = rows[r];
      html += '<tr><td><strong>' + escapeHtml(row.row_label) + '</strong></td>';
      for (var k = 0; k < cols.length; k++) {
        var ck = cols[k].key;
        html += renderMatrixCellHtml((row.cells || {})[ck], maxAbs, false);
      }
      html += renderMatrixCellHtml(row.totals, maxAbs, true) + '</tr>';
    }

    html += '<tr class="diary-row-strategy" style="border-top:2px solid var(--border);">' +
      '<td><strong>Column Total (' + escapeHtml(modeLabel || '') + ')</strong></td>';
    for (var t = 0; t < cols.length; t++) {
      html += renderMatrixCellHtml(colTots[cols[t].key], maxAbs, true);
    }
    html += renderMatrixCellHtml(grand, maxAbs, true) + '</tr></tbody></table>';
    return html;
  }

  function renderRegimesPanel() {
    var calloutsEl = document.getElementById('diary-regime-callouts');
    var exitWrap = document.getElementById('diary-exit-matrix-wrap');
    var sessWrap = document.getElementById('diary-session-matrix-wrap');
    var clockNote = document.getElementById('diary-session-clock-note');
    var data = state.regimesData;

    if (clockNote) {
      clockNote.textContent = 'Bucketed by ' +
        (state.regimeClock === 'exit' ? 'Exit Time (IST)' : 'Entry Time (IST)');
    }
    if (!data) {
      if (exitWrap) exitWrap.innerHTML = '<div class="diary-empty">No regime diagnostics loaded.</div>';
      if (sessWrap) sessWrap.innerHTML = '';
      if (calloutsEl) calloutsEl.innerHTML = '';
      return;
    }

    if (calloutsEl) {
      var callouts = data.callouts || [];
      var cHtml = '';
      for (var i = 0; i < callouts.length; i++) {
        var co = callouts[i];
        cHtml += '<article class="diary-callout-card" data-severity="' + escapeHtml(co.severity || 'warning') + '">' +
          '<div class="diary-callout-title">' + escapeHtml(co.title) + '</div>' +
          '<div class="diary-callout-detail">' + escapeHtml(co.detail) + '</div>' +
          '</article>';
      }
      calloutsEl.innerHTML = cHtml;
    }

    var rowLabel = data.group_by === 'instance' ? 'Spawned Instance' : 'Strategy';
    var modeLabel = state.mode === 'live' ? 'Live' : 'Paper';
    if (exitWrap) {
      exitWrap.innerHTML = buildMatrixHtml(data.exit_matrix, rowLabel, modeLabel);
    }
    if (sessWrap) {
      sessWrap.innerHTML = buildMatrixHtml(data.session_matrix, rowLabel, modeLabel);
    }
  }

  function loadRegimesOnly() {
    var regQs = buildQuery({
      mode: state.mode,
      from: state.selFrom,
      to: state.selTo,
      strategy: state.strategy,
      clock: state.regimeClock,
      group_by: state.regimeGroupBy
    });
    return fetchJson('/api/diary/edge/regimes?' + regQs).then(function (regData) {
      state.regimesData = regData;
      renderRegimesPanel();
    });
  }

  function renderDragMeterHtml(row) {
    if (!row || !row.trade_count || Number(row.commission || 0) <= 0.005) {
      return '<span class="muted">—</span>';
    }
    var band = row.drag_band || 'none';
    var pctVal = row.profit_drag_pct != null
      ? Number(row.profit_drag_pct)
      : (row.commission_drag_pct != null ? Number(row.commission_drag_pct) : 100);
    var fillW = row.gross_pnl <= 0.005 ? 100 : Math.min(100, Math.max(4, Math.round(pctVal)));
    var textLbl = row.gross_pnl > 0.005
      ? pctVal.toFixed(1) + '%'
      : 'Gross ≤ ₹0';
    var textCls = band === 'critical' ? 'diary-neg' : (band === 'warning' ? 'diary-warn' : 'diary-pos');
    return '<div class="diary-drag-cell">' +
      '<span class="' + textCls + '">' + textLbl + '</span>' +
      '<span class="diary-drag-bar"><span class="diary-drag-fill" data-band="' +
      escapeHtml(band) + '" style="width:' + fillW + '%"></span></span>' +
      '</div>';
  }

  function buildHoldDurationTableHtml(holdBuckets, holdTotals, modeLabel) {
    var rows = Array.isArray(holdBuckets) ? holdBuckets : [];
    var tot = holdTotals || {};
    if (!tot.trade_count) {
      return '<div class="diary-empty">No closed trades in this IST window for the ' +
        escapeHtml(modeLabel || 'selected') + ' book.</div>';
    }
    var html = '<table class="diary-table">' +
      '<thead><tr>' +
      '<th>Hold Window</th>' +
      '<th class="num">Trades (Share)</th>' +
      '<th class="num">Net Win %</th>' +
      '<th class="num">Gross P&amp;L</th>' +
      '<th class="num">Commission</th>' +
      '<th class="num">Fee Hurdle / Tr</th>' +
      '<th class="num">Commission Drag</th>' +
      '<th class="num">Net P&amp;L</th>' +
      '<th class="num">Expectancy</th>' +
      '</tr></thead><tbody>';

    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      html += '<tr>' +
        '<td><strong>' + escapeHtml(r.label) + '</strong> ' +
        '<span class="muted" style="font-size:11px;">' + escapeHtml(r.subtitle || '') + '</span></td>' +
        '<td class="num">' + r.trade_count + ' <span class="muted">(' + Number(r.trade_share_pct || 0).toFixed(0) + '%)</span></td>' +
        '<td class="num">' + (r.trade_count ? Number(r.win_rate_pct || 0).toFixed(1) + '%' : '—') + '</td>' +
        '<td class="num ' + pnlToneClass(r.gross_pnl) + '">' + (r.trade_count ? fmtInrSigned(r.gross_pnl) : '—') + '</td>' +
        '<td class="num">' + (r.trade_count ? fmtInrAbs(r.commission) : '—') + '</td>' +
        '<td class="num">' + (r.trade_count ? fmtInrAbs(r.avg_commission_per_trade) : '—') + '</td>' +
        '<td class="num">' + renderDragMeterHtml(r) + '</td>' +
        '<td class="num ' + pnlToneClass(r.net_pnl) + '"><strong>' + (r.trade_count ? fmtInrSigned(r.net_pnl) : '—') + '</strong></td>' +
        '<td class="num ' + pnlToneClass(r.expectancy_net) + '">' + (r.trade_count ? fmtInrSigned(r.expectancy_net) : '—') + '</td>' +
        '</tr>';
    }

    html += '<tr class="diary-row-strategy" style="border-top:2px solid var(--border);">' +
      '<td><strong>Book Window Total (' + escapeHtml(modeLabel || '') + ')</strong></td>' +
      '<td class="num"><strong>' + tot.trade_count + ' (100%)</strong></td>' +
      '<td class="num"><strong>' + Number(tot.win_rate_pct || 0).toFixed(1) + '%</strong></td>' +
      '<td class="num ' + pnlToneClass(tot.gross_pnl) + '"><strong>' + fmtInrSigned(tot.gross_pnl) + '</strong></td>' +
      '<td class="num"><strong>' + fmtInrAbs(tot.commission) + '</strong></td>' +
      '<td class="num"><strong>' + fmtInrAbs(tot.avg_commission_per_trade) + '</strong></td>' +
      '<td class="num">' + renderDragMeterHtml(tot) + '</td>' +
      '<td class="num ' + pnlToneClass(tot.net_pnl) + '"><strong>' + fmtInrSigned(tot.net_pnl) + '</strong></td>' +
      '<td class="num ' + pnlToneClass(tot.expectancy_net) + '"><strong>' + fmtInrSigned(tot.expectancy_net) + '</strong></td>' +
      '</tr></tbody></table>';
    return html;
  }

  function buildPayoffQuadrantSvg(entities, curvePoints, selectedKey, maxY) {
    var list = Array.isArray(entities) ? entities : [];
    var capY = Number(maxY || 4.0);
    var W = 560, H = 270;
    var padL = 48, padR = 18, padT = 16, padB = 34;
    var plotW = W - padL - padR;
    var plotH = H - padT - padB;

    function xOf(winPct) {
      var clamped = Math.max(0, Math.min(100, Number(winPct || 0)));
      return +(padL + (clamped / 100) * plotW).toFixed(1);
    }
    function yOf(rVal) {
      var clamped = Math.max(0, Math.min(capY, Number(rVal || 0)));
      return +(padT + plotH - (clamped / capY) * plotH).toFixed(1);
    }

    var pts = Array.isArray(curvePoints) && curvePoints.length > 0 ? curvePoints : [];
    var pathD = '';
    var areaD = '';
    if (pts.length > 0) {
      for (var p = 0; p < pts.length; p++) {
        var px = xOf(pts[p].win_rate_pct);
        var py = yOf(pts[p].payoff_ratio);
        pathD += (p === 0 ? 'M ' : ' L ') + px + ' ' + py;
      }
      areaD = pathD + ' L ' + xOf(100) + ' ' + yOf(capY) + ' L ' + xOf(pts[0].win_rate_pct) + ' ' + yOf(capY) + ' Z';
    }

    var svg = '<svg class="diary-quadrant-svg" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
      'aria-label="Net Win % vs Payoff Ratio breakeven scatter">' +
      '<rect x="' + padL + '" y="' + padT + '" width="' + plotW + '" height="' + plotH + '" ' +
      'fill="rgba(241,132,145,0.04)" stroke="var(--border)" stroke-width="1"/>';

    if (areaD) {
      svg += '<path d="' + areaD + '" fill="rgba(101,208,165,0.09)"/>';
    }

    // Reference crosshairs: W = 50% and R = 1.0x
    var x50 = xOf(50);
    var y1 = yOf(1.0);
    svg += '<line x1="' + x50 + '" y1="' + padT + '" x2="' + x50 + '" y2="' + (padT + plotH) + '" ' +
      'stroke="var(--border)" stroke-dasharray="3,3" stroke-width="1"/>';
    svg += '<line x1="' + padL + '" y1="' + y1 + '" x2="' + (padL + plotW) + '" y2="' + y1 + '" ' +
      'stroke="var(--border)" stroke-dasharray="3,3" stroke-width="1"/>';

    if (pathD) {
      svg += '<path class="diary-be-curve" d="' + pathD + '" fill="none" ' +
        'stroke="var(--warning)" stroke-width="1.8" stroke-dasharray="5,3"/>';
    }

    // Axis ticks & labels
    var xTicks = [0, 25, 50, 75, 100];
    for (var xt = 0; xt < xTicks.length; xt++) {
      var tx = xOf(xTicks[xt]);
      svg += '<text x="' + tx + '" y="' + (H - 16) + '" fill="var(--muted)" font-size="10" text-anchor="middle">' +
        xTicks[xt] + '%</text>';
    }
    var yTicks = [0, 1, 2, 3, 4];
    for (var yt = 0; yt < yTicks.length; yt++) {
      if (yTicks[yt] > capY) continue;
      var ty = yOf(yTicks[yt]);
      svg += '<text x="' + (padL - 6) + '" y="' + (ty + 3) + '" fill="var(--muted)" font-size="10" text-anchor="end">' +
        (yTicks[yt] === capY ? capY + '×+' : yTicks[yt] + '×') + '</text>';
    }

    svg += '<text x="' + (padL + plotW / 2) + '" y="' + (H - 3) + '" fill="var(--muted)" font-size="10.5" text-anchor="middle">' +
      'Net Win Rate (W)</text>';
    svg += '<text x="13" y="' + (padT + plotH / 2) + '" fill="var(--muted)" font-size="10.5" text-anchor="middle" ' +
      'transform="rotate(-90 13 ' + (padT + plotH / 2) + ')">Payoff Ratio (AvgW / |AvgL|)</text>';
    svg += '<text x="' + (padL + plotW - 6) + '" y="' + (padT + 14) + '" fill="var(--success-text)" font-size="10" text-anchor="end">' +
      'Positive Expectancy Zone (E &gt; 0)</text>';

    // Plot entity bubbles
    for (var e = 0; e < list.length; e++) {
      var ent = list[e];
      var cx = xOf(ent.win_rate_pct);
      var cy = yOf(ent.plot_payoff_ratio);
      var rad = Math.min(16, Math.max(6, Math.round(4 + Math.sqrt(Number(ent.trade_count || 1)) * 2.1)));
      var isSel = ent.entity_key === selectedKey;
      var fillCol = ent.prescription_severity === 'positive'
        ? 'rgba(101,208,165,0.72)'
        : ent.prescription_severity === 'warning'
          ? 'rgba(230,185,112,0.72)'
          : ent.prescription_severity === 'critical'
            ? 'rgba(241,132,145,0.74)'
            : 'rgba(145,161,175,0.55)';
      var strokeCol = ent.prescription_code === 'CHURN_FRICTION_DRAG'
        ? 'var(--warning)'
        : 'var(--bg)';

      svg += '<g class="diary-qbubble' + (isSel ? ' is-selected' : '') + '" data-entity-key="' +
        escapeHtml(ent.entity_key) + '">' +
        '<title>' + escapeHtml(ent.entity_label) + ' · W=' + Number(ent.win_rate_pct).toFixed(1) +
        '% · R=' + (ent.unbounded_payoff ? '∞' : Number(ent.payoff_ratio || 0).toFixed(2) + '×') +
        ' · Net ' + fmtInrSigned(ent.net_pnl) + '</title>' +
        '<circle cx="' + cx + '" cy="' + cy + '" r="' + rad + '" fill="' + fillCol + '" ' +
        'stroke="' + strokeCol + '" stroke-width="1.6"/>' +
        '</g>';
    }

    svg += '</svg>';
    return svg;
  }

  function renderPayoffInspector(ent) {
    var box = document.getElementById('diary-quadrant-inspector');
    if (!box) return;
    if (!ent) {
      box.innerHTML = '<div class="muted" style="font-size:12px;">No closed trades in this window to evaluate payoff coordinates.</div>';
      return;
    }
    var rStr = ent.unbounded_payoff ? '∞ (0 losses)' : Number(ent.payoff_ratio || 0).toFixed(2) + '×';
    var beStr = ent.breakeven_payoff_ratio == null
      ? '∞ (0% W)'
      : Number(ent.breakeven_payoff_ratio).toFixed(2) + '×';

    box.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">' +
      '<strong style="font-size:13.5px;color:var(--text-strong);">' + escapeHtml(ent.entity_label) + '</strong>' +
      '<span class="diary-rx-pill" data-severity="' + escapeHtml(ent.prescription_severity) + '">' +
      escapeHtml(ent.prescription_label) + '</span>' +
      '</div>' +
      '<div class="diary-inspector-stats">' +
      '<div class="diary-inspector-stat"><span>Net Win Rate (W)</span><strong>' +
      Number(ent.win_rate_pct || 0).toFixed(1) + '% (' + ent.win_count + 'W/' + ent.loss_count + 'L)</strong></div>' +
      '<div class="diary-inspector-stat"><span>Payoff vs Breakeven</span><strong>' +
      rStr + ' vs ' + beStr + '</strong></div>' +
      '<div class="diary-inspector-stat"><span>Net Expectancy / Tr</span><strong class="' +
      pnlToneClass(ent.expectancy_net) + '">' + fmtInrSigned(ent.expectancy_net) + '</strong></div>' +
      '<div class="diary-inspector-stat"><span>Fee Hurdle · Med Hold</span><strong>' +
      fmtInrAbs(ent.avg_commission_per_trade) + ' · ' + escapeHtml(ent.median_hold_display || '—') + '</strong></div>' +
      '</div>' +
      '<div class="diary-callout-detail">' + escapeHtml(ent.prescription_detail) + '</div>';
  }

  function renderPayoffPanel() {
    var holdWrap = document.getElementById('diary-hold-table-wrap');
    var svgWrap = document.getElementById('diary-quadrant-svg-wrap');
    var tableWrap = document.getElementById('diary-payoff-table-wrap');
    var data = state.payoffData;
    var modeLabel = state.mode === 'live' ? 'Live' : 'Paper';

    if (!data) {
      if (holdWrap) holdWrap.innerHTML = '<div class="diary-empty">No payoff diagnostics loaded.</div>';
      if (svgWrap) svgWrap.innerHTML = '';
      if (tableWrap) tableWrap.innerHTML = '';
      renderPayoffInspector(null);
      return;
    }

    if (holdWrap) {
      holdWrap.innerHTML = buildHoldDurationTableHtml(data.hold_buckets, data.hold_totals, modeLabel);
    }

    var entities = data.entities || [];
    if (entities.length === 0) {
      if (svgWrap) {
        svgWrap.innerHTML = '<div class="diary-empty">No closed trades in this IST window for the ' +
          modeLabel + ' book.</div>';
      }
      if (tableWrap) tableWrap.innerHTML = '';
      renderPayoffInspector(null);
      return;
    }

    var selected = null;
    for (var s = 0; s < entities.length; s++) {
      if (entities[s].entity_key === state.selectedPayoffKey) {
        selected = entities[s];
        break;
      }
    }
    if (!selected) {
      selected = entities[0];
      state.selectedPayoffKey = selected.entity_key;
    }

    if (svgWrap) {
      svgWrap.innerHTML = buildPayoffQuadrantSvg(
        entities,
        data.breakeven_curve,
        state.selectedPayoffKey,
        data.plot_payoff_cap || 4.0
      );
    }
    renderPayoffInspector(selected);

    if (tableWrap) {
      var colHdr = data.group_by === 'strategy' ? 'Strategy' : 'Spawned Instance';
      var tHtml = '<table class="diary-table">' +
        '<thead><tr>' +
        '<th>' + colHdr + '</th>' +
        '<th class="num">Trades</th>' +
        '<th class="num">Med Hold</th>' +
        '<th class="num">Net Win %</th>' +
        '<th class="num">Payoff R (BE)</th>' +
        '<th class="num">Fee Hurdle</th>' +
        '<th class="num">Expectancy</th>' +
        '<th class="num">Net P&amp;L</th>' +
        '<th>Edge Prescription</th>' +
        '</tr></thead><tbody>';

      for (var i = 0; i < entities.length; i++) {
        var ent = entities[i];
        var isRowSel = ent.entity_key === state.selectedPayoffKey;
        var rTxt = ent.unbounded_payoff ? '∞' : Number(ent.payoff_ratio || 0).toFixed(2) + '×';
        var beTxt = ent.breakeven_payoff_ratio == null
          ? '∞'
          : Number(ent.breakeven_payoff_ratio).toFixed(2) + '×';

        tHtml += '<tr class="is-clickable diary-payoff-row' + (isRowSel ? ' is-selected' : '') + '" ' +
          'data-entity-key="' + escapeHtml(ent.entity_key) + '">' +
          '<td><strong>' + escapeHtml(ent.entity_label) + '</strong></td>' +
          '<td class="num">' + ent.trade_count + ' (' + ent.win_count + 'W/' + ent.loss_count + 'L)</td>' +
          '<td class="num">' + escapeHtml(ent.median_hold_display || '—') + '</td>' +
          '<td class="num">' + Number(ent.win_rate_pct || 0).toFixed(1) + '%</td>' +
          '<td class="num">' + rTxt + ' <span class="muted">(' + beTxt + ')</span></td>' +
          '<td class="num">' + fmtInrAbs(ent.avg_commission_per_trade) + '</td>' +
          '<td class="num ' + pnlToneClass(ent.expectancy_net) + '">' + fmtInrSigned(ent.expectancy_net) + '</td>' +
          '<td class="num ' + pnlToneClass(ent.net_pnl) + '"><strong>' + fmtInrSigned(ent.net_pnl) + '</strong></td>' +
          '<td><span class="diary-rx-pill" data-severity="' + escapeHtml(ent.prescription_severity) + '">' +
          escapeHtml(ent.prescription_label) + '</span></td>' +
          '</tr>';
      }
      tHtml += '</tbody></table>';
      tableWrap.innerHTML = tHtml;
    }
  }

  function loadPayoffOnly() {
    var payQs = buildQuery({
      mode: state.mode,
      from: state.selFrom,
      to: state.selTo,
      strategy: state.strategy,
      group_by: state.payoffGroupBy
    });
    return fetchJson('/api/diary/edge/payoff?' + payQs).then(function (payData) {
      state.payoffData = payData;
      state.selectedPayoffKey = '';
      renderPayoffPanel();
    });
  }

  function renderSessionBreakdownTable(daysInWindow) {
    var wrap = document.getElementById('diary-sessions-table-wrap');
    if (!wrap) return;

    var activeRows = [];
    for (var i = 0; i < daysInWindow.length; i++) {
      var d = daysInWindow[i];
      if (
        d.trade_count > 0 ||
        (d.is_today && (d.open_count || 0) > 0) ||
        (d.date === state.selFrom && state.selFrom === state.selTo)
      ) {
        activeRows.push(d);
      }
    }
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

  /* ------------------------------------------------------------------
   * Slice 5 — 6-Gate Graduation Scorecard & Persistent Operator Journal
   * ------------------------------------------------------------------ */

  function renderGateChip(shortLabel, gateObj) {
    var passed = Boolean(gateObj && gateObj.passed);
    var cls = passed ? 'diary-gate-chip is-pass' : 'diary-gate-chip is-fail';
    var mark = passed ? '✓' : '✕';
    var tip = gateObj && gateObj.target ? ('Target ' + gateObj.target) : '';
    return '<span class="' + cls + '" title="' + escapeHtml(tip) + '">' +
      mark + ' ' + escapeHtml(shortLabel) + '</span>';
  }

  function buildGraduationTableHtml(instances, bookLabel) {
    var rows = Array.isArray(instances) ? instances : [];
    if (rows.length === 0) {
      return '<div class="diary-empty">No spawned instances evaluated in this IST window for the ' +
        escapeHtml(bookLabel || 'Paper') + ' book.</div>';
    }

    var html = '<table class="diary-table" id="diary-graduation-table">' +
      '<thead><tr>' +
      '<th>Spawned Instance / Strategy</th>' +
      '<th>6-Gate Check</th>' +
      '<th class="num">Trades</th>' +
      '<th class="num">Expectancy</th>' +
      '<th class="num">PF</th>' +
      '<th class="num">Fee Drag</th>' +
      '<th class="num">Max DD</th>' +
      '<th class="num">Commission</th>' +
      '<th class="num">Net P&amp;L</th>' +
      '<th>Verdict &amp; Advisory</th>' +
      '</tr></thead><tbody>';

    for (var i = 0; i < rows.length; i++) {
      var inst = rows[i];
      var g = inst.gates || {};
      var gatesHtml = '<div class="diary-gate-strip">' +
        renderGateChip('n', g.sample_size) +
        renderGateChip('E', g.net_expectancy) +
        renderGateChip('PF', g.profit_factor) +
        renderGateChip('Fee', g.commission_drag) +
        renderGateChip('DD', g.max_drawdown) +
        renderGateChip('Led', g.ledger_integrity) +
        '</div>' +
        '<div class="muted tabular-nums" style="font-size:10px;margin-top:2px;">' +
        Number(inst.gates_passed || 0) + '/' + Number(inst.gates_total || 6) + ' passed</div>';

      var pfDisp = inst.unbounded_pf
        ? '∞'
        : (inst.profit_factor != null ? Number(inst.profit_factor).toFixed(2) + '×' : '—');
      var dragDisp = inst.commission_drag_pct != null
        ? Number(inst.commission_drag_pct).toFixed(1) + '%'
        : '—';
      var ddDisp = inst.max_drawdown_pct != null
        ? (Number(inst.max_drawdown_pct).toFixed(2) + '% (' + fmtInrAbs(inst.max_drawdown_inr || 0) + ')')
        : fmtInrAbs(inst.max_drawdown_inr || 0);

      var badgesHtml = '';
      var cfgBadges = inst.config_badges || [];
      for (var b = 0; b < cfgBadges.length; b++) {
        badgesHtml += '<span class="diary-cfg-pill">' + escapeHtml(cfgBadges[b]) + '</span> ';
      }

      var linkHtml = inst.portfolio_deep_link
        ? ' <a class="diary-cfg-pill" href="' + escapeHtml(inst.portfolio_deep_link) + '">Open Runner &rarr;</a>'
        : '';

      var adv = inst.entitlement_advisory || {};
      var advHtml = adv.requires_live_entitlement_review && adv.message
        ? '<div class="diary-advisory-note">' + escapeHtml(adv.message) + '</div>'
        : '';

      html += '<tr>' +
        '<td>' +
          '<div><strong>' + escapeHtml(inst.instance_label || inst.display_name || '') + '</strong>' + linkHtml + '</div>' +
          '<div class="muted" style="font-size:11px;">' +
            escapeHtml(inst.strategy || '') + ' &middot; ' + escapeHtml(inst.lifecycle_status || '') +
          '</div>' +
          (badgesHtml ? '<div style="margin-top:3px;">' + badgesHtml + '</div>' : '') +
        '</td>' +
        '<td>' + gatesHtml + '</td>' +
        '<td class="num tabular-nums">' + Number(inst.trade_count || 0) + '</td>' +
        '<td class="num tabular-nums ' + pnlToneClass(inst.expectancy_net) + '">' + fmtInrSigned(inst.expectancy_net || 0) + '</td>' +
        '<td class="num tabular-nums">' + pfDisp + '</td>' +
        '<td class="num tabular-nums">' + dragDisp + '</td>' +
        '<td class="num tabular-nums">' + ddDisp + '</td>' +
        '<td class="num tabular-nums">' + fmtInrAbs(inst.commission || 0) + '</td>' +
        '<td class="num tabular-nums ' + pnlToneClass(inst.net_pnl) + '"><strong>' + fmtInrSigned(inst.net_pnl || 0) + '</strong></td>' +
        '<td>' +
          '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">' +
            '<span class="diary-verdict-badge" data-tone="' + escapeHtml(inst.verdict_tone || 'caution') + '">' +
              escapeHtml(inst.verdict_label || inst.verdict || '') +
            '</span>' +
            '<button type="button" class="diary-strat-filter-btn" ' +
              'data-journal-prefill-strategy="' + escapeHtml(inst.strategy || '') + '" ' +
              'data-journal-prefill-instance="' + escapeHtml(inst.instance_label || '') + '" ' +
              'data-journal-prefill-pid="' + escapeHtml(inst.portfolio_id || '') + '">' +
              '+ Journal' +
            '</button>' +
          '</div>' +
          '<div class="muted" style="font-size:10.5px;margin-top:3px;max-width:300px;white-space:normal;">' +
            escapeHtml(inst.rationale || '') +
          '</div>' +
          advHtml +
        '</td>' +
        '</tr>';
    }
    html += '</tbody></table>';
    return html;
  }

  function buildJournalFeedHtml(notes, bookLabel) {
    var list = Array.isArray(notes) ? notes : [];
    if (list.length === 0) {
      return '<div class="diary-empty">No operator journal notes recorded for the ' +
        escapeHtml(bookLabel || 'Paper') + ' book in this IST window yet.</div>';
    }

    var html = '';
    for (var i = 0; i < list.length; i++) {
      var n = list[i];
      var tagsHtml = '';
      var tags = Array.isArray(n.regime_tags) ? n.regime_tags : [];
      for (var t = 0; t < tags.length; t++) {
        tagsHtml += '<span class="diary-cfg-pill">' + escapeHtml(tags[t]) + '</span>';
      }
      var scopePills = '';
      if (n.strategy_name) {
        scopePills += '<span class="diary-cfg-pill">' + escapeHtml(n.strategy_name) + '</span>';
      }
      if (n.instance_label) {
        scopePills += '<span class="diary-cfg-pill">' + escapeHtml(n.instance_label) + '</span>';
      }

      html += '<article class="diary-journal-card" data-note-card="' + escapeHtml(n.note_id || '') + '">' +
        '<div class="diary-journal-card-head">' +
          '<div class="diary-journal-card-meta">' +
            '<strong class="tabular-nums">' + escapeHtml(n.session_date || '') + ' IST</strong>' +
            scopePills +
            tagsHtml +
          '</div>' +
          '<div class="diary-journal-card-meta">' +
            (n.updated_ist_label
              ? '<span class="muted tabular-nums" style="font-size:10.5px;">' + escapeHtml(n.updated_ist_label) + '</span>'
              : '') +
            '<button type="button" class="diary-strat-filter-btn" data-edit-note="' + escapeHtml(n.note_id || '') + '">Edit</button>' +
            '<button type="button" class="diary-strat-filter-btn" data-delete-note="' + escapeHtml(n.note_id || '') + '">Delete</button>' +
          '</div>' +
        '</div>' +
        (n.note_text
          ? '<div class="diary-journal-card-body">' + escapeHtml(n.note_text) + '</div>'
          : '<div class="muted" style="font-size:11.5px;">(Tag-only session marker)</div>') +
        '</article>';
    }
    return html;
  }

  function renderGraduationPanel() {
    var data = state.graduationData;
    var tableWrap = document.getElementById('diary-graduation-table-wrap');
    var pillsWrap = document.getElementById('diary-grad-summary-pills');
    var bookLabel = state.mode === 'live' ? 'Live' : 'Paper';
    if (!tableWrap) return;
    if (!data) {
      tableWrap.innerHTML = '<div class="diary-empty">No graduation scorecard loaded.</div>';
      return;
    }

    var sum = data.summary || {};
    if (pillsWrap) {
      var pText = state.mode === 'live' ? 'Healthy' : 'Promote';
      pillsWrap.innerHTML =
        '<span class="diary-verdict-badge" data-tone="positive">' + pText + ': ' + Number(sum.promote_count || 0) + '</span>' +
        '<span class="diary-verdict-badge" data-tone="caution">Tune: ' + Number(sum.tune_count || 0) + '</span>' +
        '<span class="diary-verdict-badge" data-tone="negative">Retire/Kill: ' + Number(sum.retire_count || 0) + '</span>' +
        (Number(sum.entitlement_advisory_count || 0) > 0
          ? '<span class="diary-cfg-pill">Option Entitlement Review: ' + Number(sum.entitlement_advisory_count) + '</span>'
          : '');
    }

    tableWrap.innerHTML = buildGraduationTableHtml(data.instances || [], bookLabel);
  }

  function syncJournalTagButtons() {
    var btns = document.querySelectorAll('[data-journal-tag]');
    for (var i = 0; i < btns.length; i++) {
      var tag = btns[i].getAttribute('data-journal-tag') || '';
      var active = Boolean(state.selectedJournalTags[tag]);
      btns[i].classList.toggle('is-selected', active);
      btns[i].setAttribute('aria-pressed', active ? 'true' : 'false');
    }
  }

  function resetJournalComposer(keepStatus) {
    state.selectedJournalTags = {};
    syncJournalTagButtons();
    var idEl = document.getElementById('diary-note-id');
    var pidEl = document.getElementById('diary-note-portfolio-id');
    var dateEl = document.getElementById('diary-note-date');
    var stratEl = document.getElementById('diary-note-strategy');
    var instEl = document.getElementById('diary-note-instance');
    var textEl = document.getElementById('diary-note-text');
    var cancelBtn = document.getElementById('diary-note-cancel');
    var saveBtn = document.getElementById('diary-note-save');
    var statusEl = document.getElementById('diary-note-status');
    if (idEl) idEl.value = '';
    if (pidEl) pidEl.value = '';
    if (dateEl && !dateEl.value) dateEl.value = state.selTo || state.todayIst;
    if (stratEl) stratEl.value = state.strategy || '';
    if (instEl) instEl.value = '';
    if (textEl) textEl.value = '';
    if (cancelBtn) cancelBtn.hidden = true;
    if (saveBtn) saveBtn.textContent = 'Save journal entry';
    if (statusEl && !keepStatus) statusEl.textContent = '';
  }

  function renderJournalPanel() {
    var data = state.notesData;
    var feedWrap = document.getElementById('diary-journal-feed-wrap');
    var countEl = document.getElementById('diary-journal-count-badge');
    var dateEl = document.getElementById('diary-note-date');
    var bookLabel = state.mode === 'live' ? 'Live' : 'Paper';
    if (dateEl && !dateEl.value) {
      dateEl.value = state.selTo || state.todayIst;
    }
    if (!feedWrap) return;
    if (!data) {
      feedWrap.innerHTML = '<div class="diary-empty">Loading operator journal…</div>';
      return;
    }
    var notes = data.notes || [];
    if (countEl) {
      countEl.textContent = notes.length + (notes.length === 1 ? ' entry' : ' entries') +
        ' in ' + bookLabel + ' window';
    }
    feedWrap.innerHTML = buildJournalFeedHtml(notes, bookLabel);
  }

  function loadNotesOnly() {
    var qs = buildQuery({
      mode: state.mode,
      from: state.selFrom,
      to: state.selTo,
      strategy: state.strategy
    });
    return fetchJson('/api/diary/notes?' + qs).then(function (payload) {
      state.notesData = payload;
      renderJournalPanel();
    });
  }

  function saveJournalNote(ev) {
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    var idVal = ((document.getElementById('diary-note-id') || {}).value || '').trim();
    var pidVal = ((document.getElementById('diary-note-portfolio-id') || {}).value || '').trim();
    var dateVal = ((document.getElementById('diary-note-date') || {}).value || '').trim() || state.selTo || state.todayIst;
    var stratVal = ((document.getElementById('diary-note-strategy') || {}).value || '').trim();
    var instVal = ((document.getElementById('diary-note-instance') || {}).value || '').trim();
    var textVal = ((document.getElementById('diary-note-text') || {}).value || '').trim();
    var statusEl = document.getElementById('diary-note-status');

    var chosenTags = [];
    for (var k in state.selectedJournalTags) {
      if (Object.prototype.hasOwnProperty.call(state.selectedJournalTags, k) && state.selectedJournalTags[k]) {
        chosenTags.push(k);
      }
    }

    var body = {
      mode: state.mode,
      session_date: dateVal,
      strategy_name: stratVal || null,
      portfolio_id: pidVal || null,
      instance_label: instVal || null,
      regime_tags: chosenTags,
      note_text: textVal
    };
    if (idVal) body.note_id = idVal;

    return fetch('/api/diary/notes', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      body: JSON.stringify(body)
    }).then(function (resp) {
      return resp.json().then(function (resBody) {
        if (!resp.ok || (resBody && resBody.success === false)) {
          throw new Error((resBody && resBody.error) || ('HTTP ' + resp.status));
        }
        return resBody;
      });
    }).then(function () {
      if (statusEl) {
        statusEl.textContent = 'Saved to ' + (state.mode === 'live' ? 'Live' : 'Paper') + ' journal.';
      }
      resetJournalComposer(true);
      return loadNotesOnly();
    }).catch(function (err) {
      if (statusEl) {
        statusEl.textContent = 'Save error: ' + (err && err.message ? err.message : String(err));
      }
    });
  }

  function populateComposerForEdit(noteId) {
    if (!state.notesData || !Array.isArray(state.notesData.notes)) return;
    var found = null;
    for (var i = 0; i < state.notesData.notes.length; i++) {
      if (state.notesData.notes[i].note_id === noteId) {
        found = state.notesData.notes[i];
        break;
      }
    }
    if (!found) return;
    state.selectedJournalTags = {};
    var tags = Array.isArray(found.regime_tags) ? found.regime_tags : [];
    for (var t = 0; t < tags.length; t++) {
      state.selectedJournalTags[tags[t]] = true;
    }
    syncJournalTagButtons();
    var idEl = document.getElementById('diary-note-id');
    var pidEl = document.getElementById('diary-note-portfolio-id');
    var dateEl = document.getElementById('diary-note-date');
    var stratEl = document.getElementById('diary-note-strategy');
    var instEl = document.getElementById('diary-note-instance');
    var textEl = document.getElementById('diary-note-text');
    var cancelBtn = document.getElementById('diary-note-cancel');
    var saveBtn = document.getElementById('diary-note-save');
    var statusEl = document.getElementById('diary-note-status');
    if (idEl) idEl.value = found.note_id || '';
    if (pidEl) pidEl.value = found.portfolio_id || '';
    if (dateEl) dateEl.value = found.session_date || state.todayIst;
    if (stratEl) stratEl.value = found.strategy_name || '';
    if (instEl) instEl.value = found.instance_label || '';
    if (textEl) {
      textEl.value = found.note_text || '';
      if (typeof textEl.focus === 'function') textEl.focus();
    }
    if (cancelBtn) cancelBtn.hidden = false;
    if (saveBtn) saveBtn.textContent = 'Update entry';
    if (statusEl) statusEl.textContent = 'Editing ' + (found.session_date || '') + ' entry…';
  }

  function deleteJournalNote(noteId) {
    if (!noteId) return Promise.resolve();
    var statusEl = document.getElementById('diary-note-status');
    var qs = buildQuery({ mode: state.mode });
    return fetch('/api/diary/notes/' + encodeURIComponent(noteId) + '?' + qs, {
      method: 'DELETE',
      headers: { 'Accept': 'application/json' }
    }).then(function (resp) {
      return resp.json().then(function (resBody) {
        if (!resp.ok || (resBody && resBody.success === false)) {
          throw new Error((resBody && resBody.error) || ('HTTP ' + resp.status));
        }
        return resBody;
      });
    }).then(function () {
      if (statusEl) statusEl.textContent = 'Deleted journal entry.';
      return loadNotesOnly();
    }).catch(function (err) {
      if (statusEl) {
        statusEl.textContent = 'Delete error: ' + (err && err.message ? err.message : String(err));
      }
    });
  }

  function prefillJournalFromInstance(strategyName, instanceLabel, portfolioId) {
    resetJournalComposer(false);
    var dateEl = document.getElementById('diary-note-date');
    var stratEl = document.getElementById('diary-note-strategy');
    var instEl = document.getElementById('diary-note-instance');
    var pidEl = document.getElementById('diary-note-portfolio-id');
    var textEl = document.getElementById('diary-note-text');
    if (dateEl) dateEl.value = state.selTo || state.todayIst;
    if (stratEl) stratEl.value = strategyName || '';
    if (instEl) instEl.value = instanceLabel || '';
    if (pidEl) pidEl.value = portfolioId || '';
    if (textEl && typeof textEl.focus === 'function') textEl.focus();
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

  function loadFullInstanceTrades(instanceKey) {
    if (!instanceKey || !state.attributionData) return Promise.resolve();
    var qs = buildQuery({
      mode: state.mode,
      from: state.selFrom,
      to: state.selTo
    });
    return fetchJson(
      '/api/diary/instance/' + encodeURIComponent(instanceKey) + '/trades?' + qs
    ).then(function (payload) {
      if (!payload || !payload.instance || !state.attributionData) return;
      var strategies = state.attributionData.strategies || [];
      for (var s = 0; s < strategies.length; s++) {
        var insts = strategies[s].instances || [];
        for (var i = 0; i < insts.length; i++) {
          if (insts[i].instance_key === instanceKey) {
            insts[i].trades = payload.instance.trades || [];
            insts[i].trades_truncated = false;
          }
        }
      }
      renderAttributionTree();
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
    var winQs = buildQuery({
      mode: state.mode,
      from: state.selFrom,
      to: state.selTo,
      strategy: state.strategy
    });
    var regQs = buildQuery({
      mode: state.mode,
      from: state.selFrom,
      to: state.selTo,
      strategy: state.strategy,
      clock: state.regimeClock,
      group_by: state.regimeGroupBy
    });
    var payQs = buildQuery({
      mode: state.mode,
      from: state.selFrom,
      to: state.selTo,
      strategy: state.strategy,
      group_by: state.payoffGroupBy
    });
    var winCalPromise = (state.selFrom >= mb.from && state.selTo <= mb.to)
      ? null
      : fetchJson('/api/diary/calendar?' + winQs);

    return Promise.all([
      fetchJson('/api/diary/calendar?' + calQs),
      fetchJson('/api/diary/summary?' + winQs),
      fetchJson('/api/diary/attribution?' + winQs),
      fetchJson('/api/diary/edge/regimes?' + regQs),
      fetchJson('/api/diary/edge/payoff?' + payQs),
      fetchJson('/api/diary/graduation?' + winQs),
      fetchJson('/api/diary/notes?' + winQs),
      winCalPromise
    ]).then(function (res) {
      var calData = res[0];
      var sumData = res[1];
      var attrData = res[2];
      var regData = res[3];
      var payData = res[4];
      var gradData = res[5];
      var notesData = res[6];
      var winCal = res[7];

      if (calData.today_ist) {
        state.todayIst = calData.today_ist;
      }
      state.calendarDays = calData.days || [];
      state.calendarWeeks = calData.weeks || [];
      state.attributionData = attrData;
      state.regimesData = regData;
      state.payoffData = payData;
      state.graduationData = gradData;
      state.notesData = notesData;

      populateStrategies(sumData.available_strategies || calData.available_strategies || []);
      renderSummary(sumData);
      renderCalendarGrid();
      renderAttributionTree();
      renderRegimesPanel();
      renderPayoffPanel();
      renderGraduationPanel();
      renderJournalPanel();

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

  function expandOrCollapseAll(expand) {
    state.expandedStrategies = {};
    state.expandedInstances = {};
    if (expand && state.attributionData && Array.isArray(state.attributionData.strategies)) {
      var strats = state.attributionData.strategies;
      for (var s = 0; s < strats.length; s++) {
        state.expandedStrategies[strats[s].strategy_name] = true;
        var insts = strats[s].instances || [];
        for (var i = 0; i < insts.length; i++) {
          state.expandedInstances[insts[i].instance_key] = true;
        }
      }
    }
    renderAttributionTree();
  }

  function bindEvents() {
    var modeEl = document.getElementById('diary-mode');
    if (modeEl) {
      modeEl.addEventListener('change', function () {
        var next = (modeEl.value || '').toLowerCase() === 'live' ? 'live' : 'paper';
        state.mode = next;
        state.strategy = '';
        state.expandedStrategies = {};
        state.expandedInstances = {};
        state.hasAutoExpanded = false;
        resetJournalComposer(false);
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

    var tabs = document.querySelectorAll('.diary-tab-btn');
    for (var t = 0; t < tabs.length; t++) {
      tabs[t].addEventListener('click', function (ev) {
        switchTab(ev.currentTarget.getAttribute('data-tab'));
      });
    }

    var sortEl = document.getElementById('diary-attr-sort');
    if (sortEl) {
      sortEl.addEventListener('change', function () {
        state.attrSort = sortEl.value || 'net_asc';
        renderAttributionTree();
      });
    }

    var regGroupEl = document.getElementById('diary-regime-groupby');
    if (regGroupEl) {
      regGroupEl.addEventListener('change', function () {
        state.regimeGroupBy = regGroupEl.value === 'instance' ? 'instance' : 'strategy';
        loadRegimesOnly();
      });
    }

    var regClockEl = document.getElementById('diary-regime-clock');
    if (regClockEl) {
      regClockEl.addEventListener('change', function () {
        state.regimeClock = regClockEl.value === 'exit' ? 'exit' : 'entry';
        loadRegimesOnly();
      });
    }

    var payGroupEl = document.getElementById('diary-payoff-groupby');
    if (payGroupEl) {
      payGroupEl.addEventListener('change', function () {
        state.payoffGroupBy = payGroupEl.value === 'strategy' ? 'strategy' : 'instance';
        loadPayoffOnly();
      });
    }

    var svgWrapEl = document.getElementById('diary-quadrant-svg-wrap');
    if (svgWrapEl) {
      svgWrapEl.addEventListener('click', function (ev) {
        var g = ev.target && ev.target.closest ? ev.target.closest('[data-entity-key]') : null;
        if (g) {
          state.selectedPayoffKey = g.getAttribute('data-entity-key') || '';
          renderPayoffPanel();
        }
      });
    }

    var payTableWrapEl = document.getElementById('diary-payoff-table-wrap');
    if (payTableWrapEl) {
      payTableWrapEl.addEventListener('click', function (ev) {
        var tr = ev.target && ev.target.closest ? ev.target.closest('[data-entity-key]') : null;
        if (tr) {
          state.selectedPayoffKey = tr.getAttribute('data-entity-key') || '';
          renderPayoffPanel();
        }
      });
    }

    var expBtn = document.getElementById('diary-expand-all');
    if (expBtn) expBtn.addEventListener('click', function () { expandOrCollapseAll(true); });
    var colBtn = document.getElementById('diary-collapse-all');
    if (colBtn) colBtn.addEventListener('click', function () { expandOrCollapseAll(false); });

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

    var treeWrap = document.getElementById('diary-attr-tree-wrap');
    if (treeWrap) {
      treeWrap.addEventListener('click', function (ev) {
        if (!ev.target || !ev.target.closest) return;

        var stratBtn = ev.target.closest('[data-toggle-strategy]');
        if (stratBtn) {
          var sKey = stratBtn.getAttribute('data-toggle-strategy');
          state.expandedStrategies[sKey] = !state.expandedStrategies[sKey];
          renderAttributionTree();
          return;
        }

        var filterBtn = ev.target.closest('[data-filter-strategy]');
        if (filterBtn) {
          var targetStrat = filterBtn.getAttribute('data-filter-strategy') || '';
          state.strategy = state.strategy === targetStrat ? '' : targetStrat;
          var selEl = document.getElementById('diary-strategy');
          if (selEl) selEl.value = state.strategy;
          loadAll();
          return;
        }

        var instBtn = ev.target.closest('[data-toggle-instance]');
        if (instBtn) {
          var iKey = instBtn.getAttribute('data-toggle-instance');
          state.expandedInstances[iKey] = !state.expandedInstances[iKey];
          renderAttributionTree();
          return;
        }

        var fullBtn = ev.target.closest('[data-load-full-instance]');
        if (fullBtn) {
          loadFullInstanceTrades(fullBtn.getAttribute('data-load-full-instance'));
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

    var gradWrap = document.getElementById('diary-graduation-table-wrap');
    if (gradWrap) {
      gradWrap.addEventListener('click', function (ev) {
        var btn = ev.target && ev.target.closest
          ? ev.target.closest('[data-journal-prefill-instance]')
          : null;
        if (btn) {
          prefillJournalFromInstance(
            btn.getAttribute('data-journal-prefill-strategy') || '',
            btn.getAttribute('data-journal-prefill-instance') || '',
            btn.getAttribute('data-journal-prefill-pid') || ''
          );
        }
      });
    }

    var tagPickerEl = document.getElementById('diary-note-tags');
    if (tagPickerEl) {
      tagPickerEl.addEventListener('click', function (ev) {
        var chip = ev.target && ev.target.closest
          ? ev.target.closest('[data-journal-tag]')
          : null;
        if (chip) {
          var tag = chip.getAttribute('data-journal-tag') || '';
          if (tag) {
            state.selectedJournalTags[tag] = !state.selectedJournalTags[tag];
            syncJournalTagButtons();
          }
        }
      });
    }

    var journalFormEl = document.getElementById('diary-journal-form');
    if (journalFormEl) {
      journalFormEl.addEventListener('submit', saveJournalNote);
    }

    var cancelEditBtn = document.getElementById('diary-note-cancel');
    if (cancelEditBtn) {
      cancelEditBtn.addEventListener('click', function () {
        resetJournalComposer(false);
      });
    }

    var feedWrapEl = document.getElementById('diary-journal-feed-wrap');
    if (feedWrapEl) {
      feedWrapEl.addEventListener('click', function (ev) {
        if (!ev.target || !ev.target.closest) return;
        var editBtn = ev.target.closest('[data-edit-note]');
        if (editBtn) {
          populateComposerForEdit(editBtn.getAttribute('data-edit-note') || '');
          return;
        }
        var delBtn = ev.target.closest('[data-delete-note]');
        if (delBtn) {
          deleteJournalNote(delBtn.getAttribute('data-delete-note') || '');
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
    computeMatrixCellHeat: computeMatrixCellHeat,
    buildMatrixHtml: buildMatrixHtml,
    buildHoldDurationTableHtml: buildHoldDurationTableHtml,
    buildPayoffQuadrantSvg: buildPayoffQuadrantSvg,
    buildGraduationTableHtml: buildGraduationTableHtml,
    buildJournalFeedHtml: buildJournalFeedHtml,
    monthBounds: monthBounds,
    mondayOfWeekIso: mondayOfWeekIso,
    sortAttributionStrategies: sortAttributionStrategies,
    _state: state
  };

  root.PnLDiary = PnLDiary;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = PnLDiary;
  }
})(typeof globalThis !== 'undefined' ? globalThis : window);
