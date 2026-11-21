(() => {
  const root = document.documentElement;
  const menu = document.getElementById('panel-navigation');
  const button = document.querySelector('.topbar-collapse');
  const servers = menu.querySelector('.nav-group');
  const serversButton = servers.querySelector('.nav-group-head');
  const mobile = matchMedia('(max-width: 900px)');
  const backdrop = document.querySelector('.sidebar-backdrop');
  const background = document.querySelectorAll('.content, .topbar-actions, .footer');
  const compactKey = 'roboport.nav.compact';
  const serversKey = 'roboport.nav.servers.open';

  function isCompact() {
    return !mobile.matches && root.classList.contains('sidebar-compact');
  }
  function remember(key, enabled) {
    try { localStorage.setItem(key, enabled ? '1' : '0'); } catch { /* Keep this page usable. */ }
  }
  function sync() {
    const compact = isCompact();
    const expanded = mobile.matches ? root.classList.contains('sidebar-open') : !compact;
    for (const element of background) element.inert = mobile.matches && expanded;
    const label = mobile.matches ? (expanded ? window.rpT('nav.close_menu') : window.rpT('nav.open_menu'))
                                 : (expanded ? window.rpT('nav.collapse_menu') : window.rpT('nav.expand_menu'));
    button.textContent = mobile.matches ? '☰' : (expanded ? '«' : '»');
    button.title = label;
    button.setAttribute('aria-label', label);
    button.setAttribute('aria-expanded', String(expanded));
    serversButton.setAttribute('aria-expanded', String(servers.classList.contains(compact ? 'flyout-open' : 'open')));
  }
  function closeMenus() {
    root.classList.remove('sidebar-open');
    servers.classList.remove('flyout-open');
    sync();
  }
  function dismissDrawer() {
    closeMenus();
    button.focus({preventScroll: true});
  }

  try {
    if (localStorage.getItem(serversKey) === '1') servers.classList.add('open');
  } catch { /* Use the group selected by the current page. */ }
  root.classList.add('navigation-ready');
  sync();

  button.addEventListener('click', () => {
    servers.classList.remove('flyout-open');
    if (mobile.matches) {
      root.classList.toggle('sidebar-open');
    } else {
      remember(compactKey, root.classList.toggle('sidebar-compact'));
    }
    sync();
    if (mobile.matches && root.classList.contains('sidebar-open')) {
      menu.querySelector('.nav-item').focus({preventScroll: true});
    }
  });
  backdrop.addEventListener('click', dismissDrawer);
  serversButton.addEventListener('click', () => {
    if (isCompact()) {
      servers.classList.toggle('flyout-open');
    } else {
      remember(serversKey, servers.classList.toggle('open'));
    }
    sync();
  });
  servers.addEventListener('focusout', event => {
    if (servers.classList.contains('flyout-open') && !servers.contains(event.relatedTarget)) closeMenus();
  });
  document.addEventListener('click', event => {
    if (servers.classList.contains('flyout-open') && !servers.contains(event.target)) {
      closeMenus();
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Tab' && root.classList.contains('sidebar-open')) {
      const first = menu.querySelector('.nav-item');
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        button.focus();
      } else if (!event.shiftKey && document.activeElement === button) {
        event.preventDefault();
        first.focus();
      }
    }
    if (event.key !== 'Escape') return;
    if (servers.classList.contains('flyout-open')) {
      closeMenus();
      serversButton.focus();
    } else if (root.classList.contains('sidebar-open')) {
      dismissDrawer();
    }
  });
  mobile.addEventListener('change', () => {
    if (mobile.matches && menu.contains(document.activeElement)) button.focus();
    closeMenus();
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    try { root.classList.toggle('sidebar-compact', localStorage.getItem(compactKey) === '1'); } catch {}
    closeMenus();
  });
})();
