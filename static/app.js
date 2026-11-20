document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('scan-form');
  const input = document.getElementById('payload');
  if (form && input) {
    input.focus();
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        form.submit();
      }
    });
  }
  const mode = document.getElementById('create_mode');
  const range = document.getElementById('range-fields');
  const single = document.getElementById('single-field');
  if (mode && range && single) {
    const update = () => { const isRange = mode.value === 'range'; range.hidden = !isRange; single.hidden = isRange; };
    mode.addEventListener('change', update); update();
  }
  if (window.location.pathname === '/production-management') {
    const scrollKey = 'production-management-scroll';
    const savedScroll = sessionStorage.getItem(scrollKey);
    if (savedScroll !== null) {
      sessionStorage.removeItem(scrollKey);
      requestAnimationFrame(() => window.scrollTo(0, Number(savedScroll)));
    }
    document.querySelectorAll('form[action*="/production-management/"]').forEach((actionForm) => {
      actionForm.addEventListener('submit', () => {
        sessionStorage.setItem(scrollKey, String(window.scrollY));
      });
    });
  }
  document.querySelectorAll('.auto-assignment-trigger').forEach((trigger) => {
    trigger.addEventListener('click', () => document.getElementById(trigger.dataset.dialogId).showModal());
  });
  document.querySelectorAll('.dialog-close').forEach((button) => {
    button.addEventListener('click', () => button.closest('dialog').close());
  });
  document.querySelectorAll('[data-default-quantity]').forEach((modelSelect) => {
    modelSelect.addEventListener('change', () => {
      const quantity = document.getElementById(modelSelect.dataset.defaultQuantity);
      if (modelSelect.value && quantity && !quantity.value) quantity.value = '1';
    });
  });
});

