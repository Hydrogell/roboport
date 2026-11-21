// ---------- Flash-message URL cleanup ----------
// Серверные роуты используют ?msg=...&err=... как post-redirect flash-сообщения.
// После рендера панель показывает alert, но параметры остаются в URL — это
// (а) percent-encoded мусор в адресной строке, (б) F5 повторно показывает
// сообщение, (в) при Copy URL → отправляешь чужому со старым флешем.
// Удаляем их сразу через replaceState — alert уже отрендерен Jinja'ой.
(function () {
  try {
    const url = new URL(location.href);
    if (url.searchParams.has('msg') || url.searchParams.has('err')) {
      url.searchParams.delete('msg');
      url.searchParams.delete('err');
      // toString() сохраняет hash (#integrations и т.п.) — не теряем активную вкладку.
      history.replaceState(null, '', url.toString());
    }
  } catch (e) {}
})();

// ---------- Тема (тёмная / светлая) ----------
(function () {
  const btn = document.getElementById('theme-btn');
  if (!btn) return;
  function syncTheme() {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    btn.setAttribute('aria-pressed', String(isLight));
    btn.title = isLight ? window.rpT('topbar.switch_dark_theme') : window.rpT('topbar.switch_light_theme');
  }
  btn.addEventListener('click', () => {
    const html = document.documentElement;
    const isLight = html.getAttribute('data-theme') === 'light';
    if (isLight) {
      html.removeAttribute('data-theme');
      try { localStorage.setItem('roboport.theme', 'dark'); } catch (e) {}
    } else {
      html.setAttribute('data-theme', 'light');
      try { localStorage.setItem('roboport.theme', 'light'); } catch (e) {}
    }
    syncTheme();
  });
  syncTheme();
})();

// ---------- Auto-mark UUIDs/IPs in DOM as .confidential ----------
// Авто-сканер: при загрузке страницы проходит по всем текстовым нодам и
// оборачивает UUID-ы и IPv4-адреса в <span class="confidential">. Это
// избавляет от необходимости вручную помечать каждый шаблон. .confidential
// без streamer-mode визуально не отличается, поэтому safe в обе стороны.
(function () {
  const UUID_RE = /\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/gi;
  // IPv4 — без слишком жадного матча; пропускаем когда оба октета 0..255.
  const IPV4_RE = /\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?::\d{1,5})?\b/g;
  // Не лезем внутрь этих тегов и уже-помеченных элементов.
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'NOSCRIPT', 'IFRAME']);

  function wrapMatches(textNode, regex, filter) {
    const text = textNode.nodeValue;
    regex.lastIndex = 0;
    if (!regex.test(text)) return false;
    regex.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m, anyWrapped = false;
    while ((m = regex.exec(text)) !== null) {
      const matchedText = m[0];
      if (filter && !filter(matchedText)) continue;
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const span = document.createElement('span');
      span.className = 'confidential';
      span.textContent = matchedText;
      frag.appendChild(span);
      last = m.index + matchedText.length;
      anyWrapped = true;
    }
    if (!anyWrapped) return false;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    textNode.parentNode.replaceChild(frag, textNode);
    return true;
  }

  function walk(node) {
    if (node.nodeType === 1) {  // element
      if (SKIP_TAGS.has(node.tagName)) return;
      if (node.classList && node.classList.contains('confidential')) return;
      // contenteditable / любой input-like — не трогаем
      if (node.isContentEditable) return;
      let child = node.firstChild;
      while (child) {
        const next = child.nextSibling;
        walk(child);
        child = next;
      }
    } else if (node.nodeType === 3) {  // text node
      // Сначала UUID, потом IP. UUID не пересекается с IP — порядок не важен.
      const wrappedUUID = wrapMatches(node, UUID_RE);
      // После wrap'а текстовая нода может быть удалена/разрезана. Перезапрашиваем.
      // Маскируем ВСЕ IPv4 (включая LAN) — стримеры не хотят светить и
      // топологию домашней сети.
      if (!wrappedUUID && node.parentNode) {
        wrapMatches(node, IPV4_RE);
      }
    }
  }
  walk(document.body);
})();


// ---------- Streamer mode (👁) ----------
// Класс .streamer-mode на <html> блюрит все элементы .confidential. Клик по
// заблюренному элементу раскрывает его на 5 секунд (для копирования и т.д.).
(function () {
  const btn = document.getElementById('streamer-btn');
  if (!btn) return;
  const html = document.documentElement;
  function syncStreamer() {
    const on = html.classList.contains('streamer-mode');
    btn.setAttribute('aria-pressed', String(on));
    btn.title = on
      ? window.rpT('streamer.enabled_hint')
      : window.rpT('streamer.enable_hint');
  }
  btn.addEventListener('click', () => {
    const isOn = html.classList.toggle('streamer-mode');
    try { localStorage.setItem('roboport.streamer', isOn ? '1' : '0'); } catch (e) {}
    syncStreamer();
  });
  // Click на заблюренный элемент → раскрыть. Длительность настраивается
  // через data-reveal-sec="N" (по умолчанию 5 сек, для логов/терминала — 60).
  document.addEventListener('click', e => {
    if (!html.classList.contains('streamer-mode')) return;
    const el = e.target.closest('.confidential');
    if (!el || el.classList.contains('revealed')) return;
    const sec = parseInt(el.dataset.revealSec || '5', 10) || 5;
    el.classList.add('revealed');
    // Сохраняем таймер на самом элементе чтобы повторный клик мог сбросить
    if (el._revealT) clearTimeout(el._revealT);
    el._revealT = setTimeout(() => el.classList.remove('revealed'), sec * 1000);
  });
  syncStreamer();
})();


// ---------- Sampler liveness в футере ----------
// Видимый сигнал «панель жива и собирает метрики». Тикает каждые 30с —
// в темпе sampler'а. Если сборщик вернёт sampler_alive=false или last_sample_ts
// застыл больше 5 минут — показываем красным.
(function () {
  const el = document.getElementById('sampler-status');
  if (!el) return;
  async function tick() {
    try {
      const r = await fetch('/health/sampler', { cache: 'no-store' });
      if (!r.ok) throw new Error();
      const d = await r.json();
      if (!d.sampler_alive) {
        el.textContent = window.rpT('metrics.sampler_unavailable');
        el.style.color = 'var(--danger, #f55)';
        return;
      }
      if (!d.last_sample_ts) {
        el.textContent = window.rpT('metrics.sampler_no_data');
        el.style.color = '';
        return;
      }
      const age = Math.max(0, d.ts - d.last_sample_ts);
      el.textContent = window.rpT('metrics.sampler_age', {age: window.rpTimeAgo(age)});
      el.style.color = age > 300 ? 'var(--danger, #f55)' : '';
    } catch (e) {
      el.textContent = window.rpT('metrics.panel_unavailable');
      el.style.color = 'var(--danger, #f55)';
    }
  }
  tick();
  setInterval(tick, 30000);
})();

// Autoresize textarea с классом .motd-autoresize. Растёт по содержимому,
// не уменьшается ниже css-min-height (атрибут rows / inline min-height).
(function () {
  const tas = document.querySelectorAll('textarea.motd-autoresize');
  function fit(t) {
    t.style.height = 'auto';
    // 2px чтобы не было скачка скроллбара на ровно-полной строке
    t.style.height = (t.scrollHeight + 2) + 'px';
  }
  tas.forEach(t => {
    fit(t);
    t.addEventListener('input', () => fit(t));
  });
})();
