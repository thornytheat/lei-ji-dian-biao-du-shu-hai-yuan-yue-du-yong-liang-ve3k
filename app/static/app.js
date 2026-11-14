// 原生 JS（无框架/无打包器）：JSON POST 封装、错误展示、分段标签切换、列表筛选与 CSV 导出。
(function () {
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // 分段控件切换（.segmented 或旧版 .tabs，作用域限定在同一个 .panel）
  qsa('[data-tab-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = btn.closest('.segmented, .tabs');
      if (group) qsa('button', group).forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      var target = document.getElementById(btn.getAttribute('data-tab-target'));
      if (!target) return;
      var host = btn.closest('.panel') || target.parentElement;
      qsa('.tab-panel', host).forEach(function (panel) { panel.hidden = (panel !== target); });
    });
  });

  function describe(data, status) {
    if (!data) return '请求失败：HTTP ' + status;
    if (typeof data === 'string') return data;
    if (data.error) return data.error;
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map(function (item) {
        return (item.loc ? item.loc.join('.') + '：' : '') + (item.msg || JSON.stringify(item));
      }).join('；');
    }
    if (data.detail) return JSON.stringify(data.detail);
    return '请求失败：HTTP ' + status;
  }

  window.postJSON = async function (url, payload) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    var data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new Error(describe(data, res.status));
    return data;
  };

  window.showError = function (host, err) {
    if (!host) return;
    host.hidden = false;
    host.textContent = '执行失败：' + (err && err.message ? err.message : String(err));
  };

  // 列表页：把可见行导出为 CSV（纯前端，不新增接口）
  function toCsv(tableEl) {
    var lines = [];
    qsa('tr', tableEl).forEach(function (tr) {
      if (tr.hidden) return;
      var cells = qsa('th, td', tr).map(function (cell) {
        return '"' + cell.innerText.replace(/\s+/g, ' ').replace(/"/g, '""').trim() + '"';
      });
      if (cells.length) lines.push(cells.join(','));
    });
    return lines.join('\n');
  }

  qsa('[data-export-csv]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var table = document.getElementById(btn.getAttribute('data-export-csv'));
      if (!table) return;
      var name = btn.getAttribute('data-export-name') || 'export.csv';
      var blob = new Blob(['\ufeff' + toCsv(table)], { type: 'text/csv;charset=utf-8' });
      var link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = name;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(link.href);
    });
  });

  // 复核页：月份 / 表号 / 关键字筛选台账行（纯前端，不动数据）
  var ledger = qs('#ledger-table');
  if (ledger) {
    var monthSel = qs('#review-month');
    var meterSel = qs('#review-meter');
    var search = qs('#review-search');
    var stat = qs('#review-stat');
    var rows = qsa('tbody tr', ledger).filter(function (tr) { return !tr.hasAttribute('data-placeholder'); });

    function applyFilter() {
      var term = ((search && search.value) || '').trim().toLowerCase();
      var month = monthSel && monthSel.value;
      var meter = meterSel && meterSel.value;
      var shown = 0;
      rows.forEach(function (tr) {
        var text = tr.innerText.toLowerCase();
        var ok = (!month || tr.getAttribute('data-month') === month)
          && (!meter || tr.getAttribute('data-meter') === meter)
          && (!term || text.indexOf(term) !== -1);
        tr.hidden = !ok;
        if (ok) shown += 1;
      });
      if (stat) stat.textContent = '显示 ' + shown + ' / ' + rows.length + ' 行台账';
    }

    [monthSel, meterSel, search].forEach(function (el) {
      if (!el) return;
      el.addEventListener(el.tagName === 'INPUT' ? 'input' : 'change', applyFilter);
    });
    applyFilter();
  }
})();
