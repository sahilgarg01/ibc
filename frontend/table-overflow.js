// Wrap cell previews and reveal overflowing content in the original row.
(() => {
  const resize = new ResizeObserver(entries => {
    for (const {target: content} of entries) {
      if (!content.closest('td')) continue;
      if (content.closest('tr').classList.contains('table-row-expanded')) continue;
      const overflow = content.scrollHeight > content.clientHeight + 1 ||
        content.scrollWidth > content.clientWidth + 1 ||
        [...content.querySelectorAll('*')].some(node =>
          node.scrollWidth > node.clientWidth + 1 || node.scrollHeight > node.clientHeight + 1);
      content.nextElementSibling.hidden = !overflow;
    }
  });
  function prepare() {
    document.querySelectorAll(':is(#case-list, #import-job-list, #order-list) > tr > td:not([data-overflow-ready])').forEach(cell => {
      cell.dataset.overflowReady = 'true';
      const content = document.createElement('div');
      content.className = 'table-cell-preview';
      content.append(...cell.childNodes);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'table-cell-more';
      button.textContent = '...';
      button.setAttribute('aria-label', 'Show full cell content');
      button.title = 'Show full cell content';
      button.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      button.onclick = event => {
        event.stopPropagation();
        const row = cell.closest('tr');
        const expanded = row.classList.toggle('table-row-expanded');
        row.querySelectorAll('.table-cell-more').forEach(control => {
          control.textContent = expanded ? 'Less' : '...';
          control.setAttribute('aria-expanded', String(expanded));
          control.setAttribute('aria-label', expanded ? 'Collapse row' : 'Show full cell content');
          control.title = expanded ? 'Collapse row' : 'Show full cell content';
        });
      };
      cell.append(content, button);
      resize.observe(content);
    });
  }
  new MutationObserver(prepare).observe(document.body, {childList: true, subtree: true});
  prepare();
})();
