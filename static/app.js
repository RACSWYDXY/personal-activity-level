"use strict";
document.addEventListener('click', function (event) {
  const btn = event.target.closest('[data-toggle]');
  if (btn) {
    const panel = document.getElementById(btn.dataset.toggle);
    if (panel) {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) panel.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
  }
});
document.addEventListener('submit', function (event) {
  const action = event.target.action || '';
  if (action.includes('/archive')) {
    if (!confirm('归档后不会删除历史记录，但此任务将不再显示。确定吗？')) event.preventDefault();
  } else if (action.includes('/redeem')) {
    if (!confirm('确定花费金币兑换这项奖励？')) event.preventDefault();
  }
});
