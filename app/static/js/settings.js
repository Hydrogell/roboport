(() => {
  const page = document.querySelector('.settings-page');
  const nav = page.querySelector('.settings-tabs');
  const tabs = Array.from(nav.querySelectorAll('[data-tab]'));
  const panels = Array.from(page.querySelectorAll('[data-tab-panel]'));
  const returnKey = 'roboport.settings.return';

  function activate(slug) {
    const selected = tabs.find(tab => tab.dataset.tab === slug) || tabs[0];
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
    }
    for (const panel of panels) panel.hidden = panel.id !== selected.dataset.tab;
  }
  function select(tab) {
    if (tab.hash !== location.hash) history.pushState(null, '', tab.hash);
    activate(tab.dataset.tab);
    if (tab.dataset.tab === 'audit') refreshAudit();
  }
  nav.setAttribute('role', 'tablist');
  for (const panel of panels) {
    panel.setAttribute('role', 'tabpanel');
    panel.tabIndex = 0;
  }
  tabs.forEach((tab, index) => {
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-controls', tab.dataset.tab);
    tab.addEventListener('click', event => {
      if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      select(tab);
    });
    tab.addEventListener('keydown', event => {
      const positions = {
        ArrowLeft: (index + tabs.length - 1) % tabs.length,
        ArrowRight: (index + 1) % tabs.length, Home: 0, End: tabs.length - 1,
      };
      if (!(event.key in positions)) return;
      event.preventDefault();
      const next = tabs[positions[event.key]];
      next.focus();
      select(next);
    });
  });

  // HTTP referrers omit fragments. Remember only the form identity across a password prompt.
  page.addEventListener('submit', event => {
    const form = event.target;
    if (event.defaultPrevented || form.method.toLowerCase() !== 'post') return;
    const panel = form.closest('[data-tab-panel]');
    if (!panel) return;
    try {
      sessionStorage.setItem(returnKey, JSON.stringify({
        tab: panel.id, form: form.id || form.getAttribute('action'), time: Date.now(),
      }));
    } catch { /* Browser storage can be disabled. */ }
  }, true);
  let pending;
  try {
    pending = JSON.parse(sessionStorage.getItem(returnKey));
    sessionStorage.removeItem(returnKey);
  } catch { /* Use the link fragment. */ }
  const fromPasswordCheck = document.referrer && new URL(document.referrer).pathname === '/stepup';
  const flash = page.querySelector('.alert');
  if (pending && Date.now() - pending.time < 600000 && (fromPasswordCheck || flash)) {
    const tab = tabs.find(item => item.dataset.tab === pending.tab);
    if (tab && (!location.hash || location.hash === tab.hash)) {
      history.replaceState(null, '', tab.hash);
      const form = Array.from(document.getElementById(pending.tab).querySelectorAll('form'))
        .find(item => (item.id || item.getAttribute('action')) === pending.form);
      // An uploaded archive still needs installing. Errors should reopen the field for correction.
      if (form && (fromPasswordCheck || flash?.matches('[role=alert]') || pending.form === '/settings/panel-update/upload')) {
        for (let box = form.closest('details'); box; box = box.parentElement.closest('details')) box.open = true;
      }
    }
  }
  function openLocation() {
    const slug = location.hash.slice(1);
    const target = document.getElementById(slug);
    const panel = target?.matches('details.settings-disclosure') && target.closest('[data-tab-panel]');
    activate(panel ? panel.id : slug);
    if (panel) target.open = true;
  }
  openLocation();
  window.addEventListener('hashchange', () => {
    openLocation();
    if (location.hash === '#audit') refreshAudit();
  });
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href="/settings#account-name"]');
    if (link && link.hash === location.hash) openLocation();
  });

  const editor = document.getElementById('editor-pref-seg');
  const editorStatus = document.getElementById('editor-pref-status');
  const editorNames = {auto: window.rpT('settings.editor_auto'), monaco: 'Monaco', codemirror: 'CodeMirror'};
  let preference;
  try { preference = localStorage.getItem('roboport.editor'); } catch { /* Use auto. */ }
  const currentEditor = Object.hasOwn(editorNames, preference) ? preference : 'auto';
  editor.querySelector('input[value="' + currentEditor + '"]').checked = true;
  editorStatus.textContent = editorNames[currentEditor];
  editor.addEventListener('change', event => {
    const value = event.target.value;
    if (!Object.hasOwn(editorNames, value)) return;
    try { localStorage.setItem('roboport.editor', value); } catch { /* Current visit only. */ }
    editorStatus.textContent = editorNames[value];
  });

  let refreshing = false;
  async function refreshAudit() {
    if (refreshing || document.hidden || location.hash !== '#audit') return;
    refreshing = true;
    try {
      const response = await fetch('/settings/audit-fragment' + location.search, {cache: 'no-store'});
      if (!response.ok) return;
      const fragment = new DOMParser().parseFromString(await response.text(), 'text/html');
      const region = fragment.getElementById('audit-table-region');
      if (region) document.getElementById('audit-table-region').replaceWith(region);
      const counter = fragment.getElementById('audit-counter')?.dataset;
      if (counter) {
        const pages = Number(counter.pages) > 1 ? ' · ' + counter.page + ' / ' + counter.pages : '';
        document.getElementById('audit-summary').textContent = window.rpT('settings.audit_count', {count: counter.total}) + pages;
      }
    } catch { /* Keep the last journal entries when offline. */ }
    finally { refreshing = false; }
  }
  setInterval(refreshAudit, 30000);
  document.addEventListener('visibilitychange', refreshAudit);

  const check = document.getElementById('panel-update-check-btn');
  const state = document.getElementById('panel-update-check-state');
  const result = document.getElementById('panel-update-check-result');
  check.addEventListener('click', async () => {
    check.disabled = true;
    state.hidden = false;
    state.textContent = window.rpT('settings.update_checking');
    result.replaceChildren();
    result.hidden = true;
    try {
      const response = await fetch('/settings/panel-update/check', {credentials: 'same-origin'});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'HTTP ' + response.status);
      if (!data.update_available) {
        state.textContent = window.rpT('settings.up_to_date');
        return;
      }
      const release = data.latest;
      const content = document.getElementById('update-release').content.cloneNode(true);
      content.querySelector('[data-release-title]').textContent = window.rpT('settings.release_available', {version: release.tag});
      content.querySelector('[data-release-date]').textContent = (release.published_at || '').slice(0, 10);
      content.querySelector('[data-release-notes]').hidden = !release.body;
      content.querySelector('pre').textContent = release.body || '';
      content.querySelector('[name=tag]').value = release.tag;
      const releaseLink = content.querySelector('[data-release-link]');
      if (release.html_url?.startsWith('https://github.com/')) releaseLink.href = release.html_url;
      else releaseLink.hidden = true;
      result.append(content);
      result.hidden = false;
      state.textContent = window.rpT('settings.update_found');
    } catch (error) {
      state.textContent = window.rpT('settings.update_check_failed', {error: error.message});
    } finally { check.disabled = false; }
  });

  const restart = document.getElementById('panel-restart-btn');
  restart.addEventListener('click', async () => {
    if (!confirm(window.rpT('settings.restart_confirm'))) return;
    restart.disabled = true;
    const loading = window.roboportLoading;
    loading?.show({title: window.rpT('settings.restarting_panel'), hint: window.rpT('settings.restart_reload')});
    try {
      const response = await fetch('/settings/panel/restart', {method: 'POST'});
      if (response.redirected) {
        const target = new URL(response.url);
        if (target.pathname === '/stepup' || target.pathname === '/login') {
          location.assign(target.pathname + '?next=' + encodeURIComponent('/settings#general'));
          return;
        }
      }
      if (response.status === 401 || response.status === 403) {
        location.assign('/login?next=' + encodeURIComponent('/settings#general'));
        return;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.message || 'HTTP ' + response.status);
      }
      const started = Date.now();
      await new Promise(resolve => setTimeout(resolve, 1500));
      while (Date.now() - started < 32000) {
        try {
          const health = await fetch('/api/v1/health', {cache: 'no-store'});
          const data = health.ok && await health.json();
          if (data.ok && data.ts * 1000 >= started) {
            location.reload();
            return;
          }
        } catch { /* The service is restarting. */ }
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      throw new Error(window.rpT('settings.restart_timeout'));
    } catch (error) {
      loading?.hide();
      restart.disabled = false;
      alert(error.message);
    }
  });
})();
