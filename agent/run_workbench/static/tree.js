'use strict';

// Left navigation tree: fetches nothing itself -- app.js's bootstrap loads
// /api/tree once and calls Tree.render(tree). Renders 游戏版本 › 角色 › 批次
// exactly in server order (newest first, the null-version bucket last) and
// never re-sorts client-side. Supports expand/collapse, a selected leaf
// marked with aria-current="true", and roving-tabindex arrow-key navigation.

window.Tree = (() => {
  let currentSelectedId = '';

  function versionLabel(version) {
    return typeof version === 'string' && version ? version : '未知版本（未归档）';
  }

  function characterLabel(character) {
    return typeof character === 'string' && character ? character : '未标注角色';
  }

  function toggleGroup(node, { expanded = null } = {}) {
    const wasExpanded = node.getAttribute('aria-expanded') === 'true';
    const nextExpanded = expanded === null ? !wasExpanded : expanded;
    node.setAttribute('aria-expanded', String(nextExpanded));
    const group = node.nextElementSibling;
    if (group && group.getAttribute('role') === 'group') group.hidden = !nextExpanded;
    const twisty = node.querySelector('.tree-twisty');
    if (twisty) twisty.textContent = nextExpanded ? '▾' : '▸';
    return nextExpanded;
  }

  function groupNode(label, level, childCount) {
    const node = element('div', {
      className: 'tree-item tree-group',
      attrs: {
        role: 'treeitem',
        tabindex: '-1',
        'aria-expanded': 'true',
        'aria-level': String(level),
      },
    });
    node.append(
      element('span', { className: 'tree-twisty', attrs: { 'aria-hidden': 'true' }, text: '▾' }),
      element('span', { className: 'tree-group-label', text: label }),
      element('span', { className: 'tree-group-count', text: `${childCount} 个批次` }),
    );
    node.addEventListener('click', () => toggleGroup(node));
    return node;
  }

  function cohortLeaf(cohort) {
    const cohortId = typeof cohort.cohort_id === 'string' ? cohort.cohort_id : '';
    const leaf = element('div', {
      className: `tree-item tree-leaf${cohort.unarchived ? ' tree-leaf-unarchived' : ''}`,
      attrs: {
        role: 'treeitem',
        tabindex: '-1',
        'aria-level': '3',
        'data-cohort-id': cohortId,
      },
    });
    // 平均推进 is the number batches are actually compared on, so it belongs
    // next to the run count rather than only on the detail page. It stays a
    // dash when no run in the batch recorded a floor -- never 0.
    const avgFloor = cohort.avg_global_floor;
    const hasAvg = typeof avgFloor === 'number' && Number.isFinite(avgFloor);
    leaf.append(
      element('span', { className: 'tree-leaf-label', text: cohort.label }),
      element('span', {
        className: 'tree-leaf-count',
        text: hasAvg
          ? `${formatMissing(cohort.run_count, 0)} 局 · 平均 ${formatMissing(avgFloor)}`
          : `${formatMissing(cohort.run_count, 0)} 局 · 平均 —`,
      }),
    );
    if (Number(cohort.technical_count) > 0) {
      leaf.append(element('span', {
        className: 'tree-badge tree-badge-technical',
        text: `${cohort.technical_count} 技术失败`,
      }));
    }
    if (!cohortId) leaf.setAttribute('aria-disabled', 'true');
    else leaf.addEventListener('click', () => navigate(cohortRoute(cohortId)));
    return leaf;
  }

  function render(tree) {
    const container = byId('cohortTree');
    clear(container);
    container.setAttribute('role', 'tree');
    if (!Array.isArray(tree) || !tree.length) {
      renderEmpty(container, '没有发现训练记录。');
      return;
    }
    // Render exactly in the order the server sent -- no client-side sort.
    tree.forEach((versionEntry) => {
      const characters = Array.isArray(versionEntry.characters) ? versionEntry.characters : [];
      const cohortTotal = characters.reduce(
        (sum, character) => sum + (Array.isArray(character.cohorts) ? character.cohorts.length : 0), 0,
      );
      const versionNode = groupNode(versionLabel(versionEntry.game_version), 1, cohortTotal);
      container.append(versionNode);
      const versionGroup = element('div', { className: 'tree-group-children', attrs: { role: 'group' } });
      characters.forEach((characterEntry) => {
        const cohorts = Array.isArray(characterEntry.cohorts) ? characterEntry.cohorts : [];
        const characterNode = groupNode(characterLabel(characterEntry.character), 2, cohorts.length);
        versionGroup.append(characterNode);
        const characterGroup = element('div', { className: 'tree-group-children', attrs: { role: 'group' } });
        cohorts.forEach((cohort) => characterGroup.append(cohortLeaf(cohort)));
        versionGroup.append(characterGroup);
      });
      container.append(versionGroup);
    });
    applySelection(currentSelectedId);
    resetRovingTabindex();
  }

  function allItems() {
    return Array.from(byId('cohortTree').querySelectorAll('[role="treeitem"]'));
  }

  function visibleItems() {
    return allItems().filter((item) => !isHiddenByAncestor(item));
  }

  function isHiddenByAncestor(item) {
    let node = item.parentElement;
    while (node && node !== byId('cohortTree')) {
      if (node.hidden) return true;
      node = node.parentElement;
    }
    return false;
  }

  function resetRovingTabindex(preferred = null) {
    const items = allItems();
    items.forEach((item) => item.setAttribute('tabindex', '-1'));
    const visible = items.filter((item) => !isHiddenByAncestor(item));
    const target = (preferred && visible.includes(preferred))
      ? preferred
      : (visible.find((item) => item.getAttribute('aria-current') === 'true') || visible[0]);
    if (target) target.setAttribute('tabindex', '0');
  }

  function applySelection(cohortId) {
    currentSelectedId = cohortId || '';
    allItems().forEach((item) => item.removeAttribute('aria-current'));
    if (!currentSelectedId) return;
    const selected = byId('cohortTree').querySelector(`.tree-leaf[data-cohort-id="${CSS.escape(currentSelectedId)}"]`);
    if (!selected) return;
    selected.setAttribute('aria-current', 'true');
    // Reveal the selected leaf: expand both ancestor groups.
    let node = selected.parentElement;
    while (node && node !== byId('cohortTree')) {
      if (node.getAttribute('role') === 'group') {
        node.hidden = false;
        const owner = node.previousElementSibling;
        if (owner && owner.getAttribute('role') === 'treeitem') {
          owner.setAttribute('aria-expanded', 'true');
          const twisty = owner.querySelector('.tree-twisty');
          if (twisty) twisty.textContent = '▾';
        }
      }
      node = node.parentElement;
    }
  }

  function setSelected(cohortId) {
    applySelection(cohortId);
    resetRovingTabindex(byId('cohortTree').querySelector(`.tree-leaf[data-cohort-id="${cohortId ? CSS.escape(cohortId) : '__none__'}"]`));
  }

  function activate(item) {
    if (item.classList.contains('tree-leaf')) {
      item.click();
    } else {
      toggleGroup(item);
    }
  }

  function handleKeydown(event) {
    const items = visibleItems();
    if (!items.length) return;
    const active = document.activeElement;
    const currentIndex = items.indexOf(active);
    if (currentIndex === -1) return;
    const focus = (item) => {
      items.forEach((candidate) => candidate.setAttribute('tabindex', '-1'));
      item.setAttribute('tabindex', '0');
      item.focus();
    };
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      focus(items[Math.min(items.length - 1, currentIndex + 1)]);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      focus(items[Math.max(0, currentIndex - 1)]);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      if (active.classList.contains('tree-group')) {
        const expanded = toggleGroup(active, { expanded: true });
        if (expanded) {
          const next = visibleItems();
          const nextIndex = next.indexOf(active);
          if (next[nextIndex + 1]) focus(next[nextIndex + 1]);
        }
      }
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      if (active.classList.contains('tree-group') && active.getAttribute('aria-expanded') === 'true') {
        toggleGroup(active, { expanded: false });
      } else {
        const level = Number(active.getAttribute('aria-level')) || 1;
        for (let index = currentIndex - 1; index >= 0; index -= 1) {
          const candidateLevel = Number(items[index].getAttribute('aria-level')) || 1;
          if (candidateLevel < level) {
            focus(items[index]);
            break;
          }
        }
      }
    } else if (event.key === 'Home') {
      event.preventDefault();
      focus(items[0]);
    } else if (event.key === 'End') {
      event.preventDefault();
      focus(items[items.length - 1]);
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activate(active);
    }
  }

  byId('cohortTree').addEventListener('keydown', handleKeydown);

  return { render, setSelected };
})();
