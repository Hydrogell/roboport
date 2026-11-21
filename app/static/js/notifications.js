// ---------- Уведомления (колокольчик) ----------
(function () {
  const btn = document.getElementById('notif-btn');
  const pop = document.getElementById('notif-pop');
  const body = document.getElementById('notif-body');
  const dot = document.getElementById('notif-dot');
  const onlineEl = document.getElementById('notif-online');
  const account = document.getElementById('account-menu');
  if (!btn || !pop) return;

  let lastSeenTs = 0;
  try { lastSeenTs = Number(localStorage.getItem('roboport.notif.seen') || 0); } catch (e) {}

  const clock = new Intl.DateTimeFormat(document.documentElement.lang, {hour: '2-digit', minute: '2-digit'});
  function fmtRel(ts) {
    return window.rpTimeAgo(Date.now() / 1000 - ts);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  async function refresh() {
    try {
      const r = await fetch('/api/notifications', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      onlineEl.textContent = d.online > 0 ? window.rpT('notifications.online', {count: d.online}) : '';
      const updates = d.updates || [];
      // Точка-индикатор: есть события свежее last-seen ИЛИ есть апдейты.
      const newest = d.events.length ? d.events[0].ts : 0;
      dot.hidden = !((newest > lastSeenTs) || updates.length > 0);

      // ---- Апдейты (без timestamp, всегда сверху) ----
      let updatesHtml = '';
      if (updates.length) {
        const rows = updates.map(u => {
          if (u.kind === 'creds-missing') {
            return `<a class="notif-row update" href="/settings">
              <span class="notif-icon">🔑</span>
              <span class="notif-text">
                <b>${escapeHtml(window.rpT('notifications.factorio_credentials_missing'))}</b>
                <span class="muted small">${escapeHtml(u.message)}</span>
              </span>
            </a>`;
          }
          if (u.kind === 'factorio-update') {
            return `<a class="notif-row update" href="/servers/${u.server_uuid}/config">
              <span class="notif-icon">🔧</span>
              <span class="notif-text">
                <b>${escapeHtml(window.rpT('notifications.factorio_version_available', {version: u.to_version}))}</b> ${escapeHtml(window.rpT('notifications.available_for', {server: u.server_name}))}
                <span class="muted small">${escapeHtml(window.rpT('notifications.current_version', {version: u.from_version}))}</span>
              </span>
            </a>`;
          }
          if (u.kind === 'mod-update') {
            return `<a class="notif-row update" href="/servers/${u.server_uuid}/mods">
              <span class="notif-icon">🆙</span>
              <span class="notif-text">
                <b>${escapeHtml(u.mod_title)}</b> ${escapeHtml(u.from_version)} → ${escapeHtml(u.to_version)}
                <span class="muted small">${escapeHtml(u.server_name)}</span>
              </span>
            </a>`;
          }
          return '';
        }).join('');
        updatesHtml = '<div class="notif-section-head">' + escapeHtml(window.rpT('notifications.updates_available')) + '</div>' + rows;
      }

      // ---- События игроков ----
      let eventsHtml;
      if (!d.events.length) {
        eventsHtml = '<div class="muted small" style="padding:14px">' + escapeHtml(window.rpT('notifications.no_recent_events')) + '</div>';
      } else {
        const sectionHead = updates.length
          ? '<div class="notif-section-head">' + escapeHtml(window.rpT('topbar.events_24h')) + '</div>' : '';
        eventsHtml = sectionHead + d.events.map(e => {
          const icon = e.kind === 'join' ? '🟢' : '🔴';
          const verb = e.kind === 'join' ? window.rpT('notifications.player_joined') : window.rpT('notifications.player_left');
          const isNew = e.ts > lastSeenTs;
          return `<a class="notif-row${isNew ? ' new' : ''}" href="/servers/${e.server_uuid}/players">
            <span class="notif-icon">${icon}</span>
            <span class="notif-text">
              <b>${escapeHtml(e.name)}</b> ${escapeHtml(verb)} · ${escapeHtml(e.server_name)}
            </span>
            <span class="notif-time muted small" title="${window.rpEscapeHtml(clock.format(e.ts * 1000))}">${escapeHtml(fmtRel(e.ts))}</span>
          </a>`;
        }).join('');
      }
      body.innerHTML = updatesHtml + eventsHtml;
    } catch (e) {
      body.innerHTML = '<div class="muted small" style="padding:14px">' + escapeHtml(window.rpT('notifications.load_failed', {error: e.message})) + '</div>';
    }
  }

  function open() {
    account.open = false;
    pop.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    refresh();
    // Помечаем всё как прочитанное по «открытию» — через секунду, чтобы юзер успел увидеть «новое».
    setTimeout(() => {
      lastSeenTs = Math.floor(Date.now() / 1000);
      try { localStorage.setItem('roboport.notif.seen', String(lastSeenTs)); } catch (e) {}
      dot.hidden = true;
    }, 1500);
  }
  function close() {
    pop.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
  }

  btn.addEventListener('click', () => {
    if (pop.hidden) open(); else close();
  });
  function dismissOutside(event) {
    if (account.open && !account.contains(event.target)) account.open = false;
    if (!pop.hidden && !pop.contains(event.target) && !btn.contains(event.target)) close();
  }
  document.addEventListener('click', dismissOutside);
  document.addEventListener('focusin', dismissOutside);
  account.addEventListener('click', event => {
    if (event.target.closest('a[href]')) account.open = false;
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (account.open) {
      account.open = false;
      account.querySelector('summary').focus();
    } else if (!pop.hidden) {
      close();
      btn.focus();
    }
  });

  // Lazy: первый запрос — через 5 секунд после загрузки страницы (чтобы не
  // забивать первое окно ответа), потом раз в 5 минут. /api/notifications
  // ходит на Docker Hub за версиями (1ч TTL-кэш) — на холодную может пожить
  // 1-3 секунды, поэтому отказались от поллинга раз в минуту.
  setTimeout(refresh, 5000);
  setInterval(refresh, 5 * 60 * 1000);
})();
