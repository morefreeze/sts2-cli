'use strict';

// The run table for the batch selected in the left tree. Rows are always
// addressed through their `ref` object -- real data has run_id: null for
// every run, so building a request from a bare run_id would 404 against
// 100% of it. `ref.kind` picks id= vs source= for the /api/run* query
// string; runs-table.js itself only needs `ref` to build the route hash.

window.RunsTable = (() => {
  let currentRows = [];
  let sortKey = null; // null | 'global_floor' | 'status'
  let sortDir = 'desc';

  function missingCell(value) {
    return value === null || value === undefined || value === '' ? '—' : String(value);
  }

  function sortedRows(rows) {
    if (!sortKey) return rows;
    const withIndex = rows.map((row, index) => ({ row, index }));
    withIndex.sort((a, b) => {
      let result;
      if (sortKey === 'global_floor') {
        // A run with no recorded floor carries no ranking information, so it
        // sinks to the bottom in both directions instead of crowding the top
        // of the ascending view. Returning here deliberately bypasses sortDir.
        const aMissing = !Number.isFinite(a.row.global_floor);
        const bMissing = !Number.isFinite(b.row.global_floor);
        if (aMissing !== bMissing) return aMissing ? 1 : -1;
        if (aMissing && bMissing) return a.index - b.index;
        result = a.row.global_floor - b.row.global_floor;
      } else {
        const av = STATUS_LABELS[a.row.status] || a.row.status || '';
        const bv = STATUS_LABELS[b.row.status] || b.row.status || '';
        result = String(av).localeCompare(String(bv), 'zh-CN');
      }
      if (result === 0) return a.index - b.index;
      return sortDir === 'asc' ? result : -result;
    });
    return withIndex.map((item) => item.row);
  }

  function headerCell(label, key) {
    const th = element('th', { attrs: { scope: 'col' } });
    if (!key) {
      th.textContent = label;
      return th;
    }
    const active = sortKey === key;
    const button = element('button', {
      className: 'table-sort-button',
      attrs: { type: 'button', 'aria-sort': active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none' },
    });
    button.append(element('span', { text: label }));
    if (active) {
      button.append(element('span', {
        className: 'table-sort-indicator', attrs: { 'aria-hidden': 'true' }, text: sortDir === 'asc' ? '▲' : '▼',
      }));
    }
    button.addEventListener('click', () => {
      if (sortKey === key) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      else { sortKey = key; sortDir = 'desc'; }
      renderTable(currentRows);
    });
    th.append(button);
    return th;
  }

  function renderTable(rows) {
    const container = byId('runsTable');
    clear(container);
    if (!rows.length) {
      renderEmpty(container, '该批次没有可展示的对局。');
      return;
    }
    const table = element('table', { className: 'runs-table' });
    const thead = element('thead');
    const headRow = element('tr');
    headRow.append(
      headerCell('序号', null),
      headerCell('种子', null),
      headerCell('状态', 'status'),
      headerCell('推进层数', 'global_floor'),
      headerCell('幕', null),
      headerCell('时间', null),
    );
    thead.append(headRow);
    table.append(thead);
    const tbody = element('tbody');
    sortedRows(rows).forEach((row, index) => {
      const ref = row.ref && typeof row.ref === 'object' && typeof row.ref.id === 'string' && row.ref.id
        ? row.ref
        : null;
      const tr = element('tr', {
        className: `runs-table-row${row.has_map === false ? ' runs-table-row-no-map' : ''}`,
        attrs: ref
          ? { tabindex: '0', role: 'button', 'aria-label': `查看第 ${index + 1} 行对局` }
          : { 'aria-disabled': 'true' },
      });
      tr.append(
        element('td', { text: String(index + 1) }),
        element('td', { text: missingCell(row.seed) }),
        element('td', { text: STATUS_LABELS[row.status] || missingCell(row.status) }),
        element('td', { text: missingCell(row.global_floor) }),
        element('td', { text: missingCell(row.act) }),
        element('td', { text: Number.isFinite(row.started_at) ? formatTime(row.started_at) : '—' }),
      );
      if (row.has_map === false) {
        tr.append(element('td', { className: 'runs-table-no-map-note', text: '无地图' }));
      } else {
        tr.append(element('td', { text: '' }));
      }
      if (ref) {
        const activate = () => navigate(runRoute(state.selectedCohortId, ref));
        tr.addEventListener('click', activate);
        tr.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            activate();
          }
        });
      }
      tbody.append(tr);
    });
    table.append(tbody);
    container.append(table);
  }

  async function render(cohortId) {
    sortKey = null;
    sortDir = 'desc';
    const notice = byId('runsTableNotice');
    notice.hidden = true;
    renderEmpty(byId('runsTable'), '正在读取对局列表…', 'loading-state');
    try {
      const payload = await getJSON(`/api/cohort/runs?id=${encodeURIComponent(cohortId)}`);
      if (state.selectedCohortId !== cohortId) return;
      currentRows = Array.isArray(payload.runs) ? payload.runs : [];
      if (payload.runs_complete === false) {
        notice.hidden = false;
        notice.textContent = `仅展示前 ${currentRows.length} 条记录，未加载全部对局。`;
      }
      renderTable(currentRows);
    } catch (error) {
      if (state.selectedCohortId !== cohortId) return;
      currentRows = [];
      renderEmpty(byId('runsTable'), `对局列表读取失败：${error.message}`, 'error-state');
    }
  }

  function renderEmptyTable() {
    currentRows = [];
    byId('runsTableNotice').hidden = true;
    renderEmpty(byId('runsTable'), '选择左侧批次后查看对局列表。');
  }

  return { render, renderEmpty: renderEmptyTable };
})();
