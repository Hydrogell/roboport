// ---------- CSRF auto-injection ----------
// 1. На submit любой POST-формы — добавляем hidden _csrf если его ещё нет.
// 2. Перехватываем fetch — добавляем X-CSRF-Token header на POST/PUT/DELETE/PATCH.
// Так не приходится править все 50+ форм в шаблонах руками.
(function () {
  if (!window.__csrf) return;  // на login-странице нет, и не нужно

  // Forms
  document.addEventListener('submit', function (e) {
    const form = e.target;
    if (!form || form.tagName !== 'FORM') return;
    const action = e.submitter?.getAttribute('formaction') ?? form.action;
    const method = e.submitter?.getAttribute('formmethod') ?? form.method;
    if (method.toUpperCase() !== 'POST' || new URL(action, document.baseURI).origin !== location.origin) return;
    if (form.querySelector('input[name="_csrf"]')) return;
    const inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = '_csrf';
    inp.value = window.__csrf;
    form.appendChild(inp);
  }, true);  // capture-фаза — раньше любых других submit-обработчиков

  // Fetch
  const _origFetch = window.fetch;
  if (_origFetch) {
    window.fetch = function (input, init) {
      const req = input instanceof Request ? input : null;
      const method = (init?.method || req?.method || 'GET').toUpperCase();
      if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method) &&
          new URL(req ? req.url : input, document.baseURI).origin === location.origin) {
        const headers = new Headers(init?.headers ?? req?.headers);
        if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', window.__csrf);
        init = {...init, headers};
      }
      return _origFetch.call(this, input, init);
    };
  }
})();

// ---------- Password input toggle (👁 рядом с полем) ----------
// Любая <span class="pw-input"><input type=password>...<button class="pw-toggle">
// поведение: клик по toggle меняет input.type ↔ text, иконка/цвет обновляются.
(function () {
  document.addEventListener('click', e => {
    const btn = e.target.closest('.pw-toggle');
    if (!btn) return;
    const wrap = btn.closest('.pw-input');
    if (!wrap) return;
    const input = wrap.querySelector('input');
    if (!input) return;
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    btn.classList.toggle('shown', !showing);
    btn.textContent = showing ? '👁' : '🙈';
  });
})();


// Localized form dialogs also work under the page's strict script policy.
(function () {
  document.addEventListener('submit', event => {
    if (event.defaultPrevented || !(event.target instanceof HTMLFormElement)) return;
    const form = event.target;
    if (form.dataset.prompt) {
      const field = form.elements.namedItem(form.dataset.promptField);
      const answer = window.prompt(form.dataset.prompt, field.value);
      if (answer === null || (form.hasAttribute('data-prompt-required') && !answer)) {
        event.preventDefault();
        return;
      }
      field.value = answer;
    }
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) event.preventDefault();
  }, true);

  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-confirm]');
    if (link && !window.confirm(link.dataset.confirm)) event.preventDefault();
  });

  document.addEventListener('change', event => {
    if (event.target.hasAttribute('data-auto-submit')) event.target.form.requestSubmit();
  });
})();

// ---------- Loading overlay для form-submit'ов ----------
// Любая <form> при сабмите получает overlay со спиннером. Текст настраивается
// через data-loading="..." на <form>. Кастомные шаги — data-loading-steps="A|B|C".
// Чтобы НЕ показывать overlay — поставить data-no-loading="1" на форме.
//
// Overlay скрывается автоматически при следующей загрузке страницы (он в DOM
// текущей страницы, redirect его уносит). На pageshow (back/forward cache)
// гарантированно скрываем — Safari может показать DOM в состоянии «overlay on».
(function () {
  const overlay = document.getElementById('loading-overlay');
  if (!overlay) return;
  const titleEl = document.getElementById('lc-title');
  const hintEl  = document.getElementById('lc-hint');
  const stepsEl = document.getElementById('lc-steps');

  function show(opts) {
    titleEl.textContent = opts.title || window.rpT('loading.processing');
    hintEl.textContent  = opts.hint  || window.rpT('loading.wait');
    if (opts.steps && opts.steps.length) {
      stepsEl.innerHTML = '';
      opts.steps.forEach(s => {
        const li = document.createElement('li');
        li.textContent = s;
        stepsEl.appendChild(li);
      });
      stepsEl.hidden = false;
      // Анимация активного шага по таймеру (rough estimate). Каждый шаг ~3s,
      // последний живёт пока страница не уйдёт в redirect (overlay скроется).
      const stepEls = stepsEl.querySelectorAll('li');
      let i = 0;
      stepEls[0].classList.add('active');
      const advance = () => {
        if (i >= stepEls.length - 1) return;
        stepEls[i].classList.remove('active');
        stepEls[i].classList.add('done');
        i++;
        stepEls[i].classList.add('active');
        setTimeout(advance, 3000 + Math.random() * 1500);
      };
      setTimeout(advance, 1500);
    } else {
      stepsEl.hidden = true;
      stepsEl.innerHTML = '';
    }
    overlay.classList.add('show');
  }
  function hide() { overlay.classList.remove('show'); }

  document.addEventListener('submit', e => {
    // Если другой listener уже отменил submit (например delete-confirm: юзер
    // нажал Cancel в prompt'е) — overlay показывать НЕ надо. Иначе спиннер
    // висит после явной отмены и сбивает с толку.
    if (e.defaultPrevented) return;
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.noLoading === '1') return;
    if (form.method && form.method.toLowerCase() === 'get') return;
    const steps = (form.dataset.loadingSteps || '')
      .split('|').map(s => s.trim()).filter(Boolean);
    show({
      title: form.dataset.loading || window.rpT('loading.processing'),
      hint:  form.dataset.loadingHint || window.rpT('loading.wait'),
      steps: steps,
    });
  });

  // BF-cache (back/forward) на Safari/Chrome может вернуть страницу с
  // включённым overlay'ем — гарантированно скрываем при показе.
  window.addEventListener('pageshow', hide);
  // Доступно глобально, мало ли (например, для AJAX-операций модов).
  window.roboportLoading = { show, hide };
})();

// Form-persist для step-up'а. Перед submit'ом формы с [data-persist]
// сохраняем поля в sessionStorage; при следующем рендере той же страницы
// (как раз случай step-up'а: форма → 303 на /stepup → 303 обратно)
// поля восстанавливаются. Пароли и file-инпуты не сохраняем по очевидным
// причинам. TTL 10 мин — потом запись протухает сама.
(function () {
  const TTL_MS = 10 * 60 * 1000;
  const PREFIX = 'roboport.formstash.';

  function keyFor(form) {
    // path + (id|action) даёт стабильный ключ. У большинства форм есть id
    // или достаточно уникальный action; конфликтов почти не бывает.
    const tag = form.id || form.getAttribute('action') || form.name || 'form';
    return PREFIX + location.pathname + '#' + tag;
  }
  function shouldSkipField(el) {
    if (!el.name) return true;
    const t = (el.type || '').toLowerCase();
    if (t === 'password' || t === 'file') return true;
    // CSRF исключаем явно — токен генерится на сервере per-session, restore
    // его обратно бессмысленно (он уже есть в свежем рендере).
    if (el.name === '_csrf' || el.name === 'csrf_token') return true;
    // Hidden с явным opt-out — например data-orig-* атрибуты на самой форме
    // через data-no-persist — скажем «не сохраняй меня». Это позволяет
    // delete/clone-формам снапшотить confirm_name/new_name/new_port (они
    // hidden) — после step-up'а данные подъедет обратно и второй prompt
    // не нужен.
    if (el.dataset && el.dataset.noPersist === '1') return true;
    return false;
  }
  function snapshot(form) {
    const data = {};
    form.querySelectorAll('input, textarea, select').forEach(el => {
      if (shouldSkipField(el)) return;
      if (el.type === 'checkbox') {
        // Several webhook events share one field name.
        if (!data[el.name]) data[el.name] = [];
        if (el.checked) data[el.name].push(el.value);
      } else if (el.type !== 'radio' || el.checked) {
        data[el.name] = el.value;
      }
    });
    try {
      sessionStorage.setItem(keyFor(form), JSON.stringify({
        ts: Date.now(), data,
      }));
    } catch { /* quota / private mode — игнорируем */ }
  }
  function restore(form) {
    let raw;
    try {
      const key = keyFor(form);
      raw = sessionStorage.getItem(key);
      if (raw) sessionStorage.removeItem(key);
    } catch { return; }
    if (!raw || form.hasAttribute('data-saved')) return;
    let payload;
    try { payload = JSON.parse(raw); } catch { return; }
    if (!payload || (Date.now() - (payload.ts || 0)) > TTL_MS) return;
    const data = payload.data || {};
    Object.entries(data).forEach(([name, value]) => {
      if (name.startsWith('__cb__')) return;  // служебные
      const els = form.querySelectorAll(`[name="${CSS.escape(name)}"]`);
      els.forEach(el => {
        if (shouldSkipField(el)) return;
        if (el.type === 'checkbox') {
          el.checked = Array.isArray(value) ? value.includes(el.value) : !!data['__cb__' + name];
        } else if (el.type === 'radio') {
          el.checked = (el.value === value);
        } else {
          el.value = value;
        }
      });
    });
  }

  document.querySelectorAll('form[data-persist]').forEach(f => {
    restore(f);
    f.addEventListener('submit', () => snapshot(f));
  });
})();
