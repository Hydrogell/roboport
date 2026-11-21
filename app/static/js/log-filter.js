// LogFilter: универсальный chip-based фильтр строк для логов.
// Применяется на странице Терминала (live WS) и Docker логи (static text).
//
// Логика OR/AND:
//   * passFilter(line) =
//       (нет show-фильтров   ИЛИ  любой из show match'ит line)
//     И (нет hide-фильтров   ИЛИ  ни один из hide НЕ match'ит line)
//
// API:
//   const f = LogFilter.attach({
//     container: HTMLElement,            // куда рендерить UI (пустой div)
//     storageKey: 'roboport.foo.<uuid>', // localStorage-ключ для persist
//     onChange: () => { rerender(); },   // вызывается при любом изменении
//     presets: [{ label: '🚫 RCON-spam', hide: ['New RCON connection from IP'] }],
//   });
//   f.matches(line) → bool
//   f.setStat(visible, total) — обновить счётчик «N / total»
//
// Сериализация: { show: string[], hide: string[] } в JSON.
//
// CSS-классы которые нужны на родительской странице (см. style.css):
//   .log-filter-bar, .log-filter-group, .log-filter-chips, .filter-chip,
//   .filter-chip-x, .filter-add, .log-filter-stat

(function (root) {
  function compileSafe(s) {
    try { return new RegExp(s); } catch { return null; }
  }

  function attach(opts) {
    const container = opts.container;
    const storageKey = opts.storageKey;
    const onChange = opts.onChange || (() => {});
    const presets = opts.presets || [];

    // State: массивы строк (raw regex source). compile делается lazy в matches().
    const state = { show: [], hide: [] };

    // Загрузить state из localStorage
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const o = JSON.parse(raw);
        if (Array.isArray(o.show)) state.show = o.show.filter(s => typeof s === 'string');
        if (Array.isArray(o.hide)) state.hide = o.hide.filter(s => typeof s === 'string');
      }
    } catch {}

    function save() {
      // Любая мутация state идёт через save() сразу следом — здесь же
      // помечаем regex-кэш как dirty, чтобы matches() пересобрал.
      compiled.dirty = true;
      try { localStorage.setItem(storageKey, JSON.stringify(state)); } catch {}
    }

    // ---------- Render ----------
    container.innerHTML = '';
    container.classList.add('log-filter-bar');

    const showGroup = mkGroup(window.rpT('log_filter.show'), 'show');
    const hideGroup = mkGroup(window.rpT('log_filter.hide'), 'hide');
    container.appendChild(showGroup);
    container.appendChild(hideGroup);

    // Кнопки пресетов
    presets.forEach(p => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-sm';
      b.textContent = p.label;
      b.title = window.rpT('log_filter.toggle_preset', {hint: p.title || (
        (p.show && p.show.length ? 'show=[' + p.show.join(',') + '] ' : '') +
        (p.hide && p.hide.length ? 'hide=[' + p.hide.join(',') + ']' : '')
      )});
      b.addEventListener('click', () => togglePreset(p));
      container.appendChild(b);
    });

    // Reset
    const resetBtn = document.createElement('button');
    resetBtn.type = 'button';
    resetBtn.className = 'btn btn-sm';
    resetBtn.textContent = '×';
    resetBtn.title = window.rpT('log_filter.reset_all');
    resetBtn.addEventListener('click', () => {
      state.show = []; state.hide = [];
      rerenderChips();
      save(); onChange();
    });
    container.appendChild(resetBtn);

    // Stat
    const stat = document.createElement('span');
    stat.className = 'log-filter-stat muted small';
    container.appendChild(stat);

    function mkGroup(label, kind) {
      const wrap = document.createElement('div');
      wrap.className = 'log-filter-group';
      const lbl = document.createElement('span');
      lbl.className = 'muted small';
      lbl.textContent = window.rpT('log_filter.group_label', {label});
      wrap.appendChild(lbl);

      const chips = document.createElement('div');
      chips.className = 'log-filter-chips';
      chips.dataset.kind = kind;
      wrap.appendChild(chips);

      const add = document.createElement('input');
      add.type = 'search';
      add.className = 'input input-sm mono filter-add';
      add.placeholder = window.rpT('log_filter.regex_placeholder');
      add.title = window.rpT('log_filter.regex_hint');
      add.setAttribute('aria-label', window.rpT('log_filter.add_regex_label', {label}));
      add.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const v = add.value.trim();
          if (!v) return;
          // Дедуп — не добавляем если уже есть
          if (!state[kind].includes(v)) state[kind].push(v);
          add.value = '';
          rerenderChips();
          save(); onChange();
        }
      });
      wrap.appendChild(add);
      return wrap;
    }

    function rerenderChips() {
      ['show', 'hide'].forEach(kind => {
        const chips = container.querySelector(`.log-filter-chips[data-kind="${kind}"]`);
        chips.innerHTML = '';
        state[kind].forEach((s, i) => {
          const isValid = compileSafe(s) !== null;
          const chip = document.createElement('span');
          chip.className = 'filter-chip mono' + (isValid ? '' : ' invalid');
          const label = kind === 'show' ? window.rpT('log_filter.show') : window.rpT('log_filter.hide');
          chip.title = isValid ? window.rpT('log_filter.regex_chip', {label, pattern: s})
            : window.rpT('log_filter.invalid_pattern', {pattern: s});
          const txt = document.createElement('span');
          txt.textContent = s;
          chip.appendChild(txt);
          const x = document.createElement('span');
          x.className = 'filter-chip-x';
          x.textContent = '×';
          x.title = window.rpT('log_filter.remove');
          x.addEventListener('click', () => {
            state[kind].splice(i, 1);
            rerenderChips();
            save(); onChange();
          });
          chip.appendChild(x);
          chips.appendChild(chip);
        });
      });
    }

    function togglePreset(p) {
      // Toggle: если все элементы preset'а уже в state — убираем; иначе — добавляем недостающие.
      const allInShow = (p.show || []).every(s => state.show.includes(s));
      const allInHide = (p.hide || []).every(s => state.hide.includes(s));
      const fullyApplied = allInShow && allInHide;

      ['show', 'hide'].forEach(kind => {
        (p[kind] || []).forEach(s => {
          if (fullyApplied) {
            state[kind] = state[kind].filter(x => x !== s);
          } else if (!state[kind].includes(s)) {
            state[kind].push(s);
          }
        });
      });
      rerenderChips();
      save(); onChange();
    }

    rerenderChips();

    // ---------- Public API ----------
    // Кэш скомпилированных regex'ов. Помечается dirty при любом изменении
    // state'а; recompile() пересобирает массивы при следующем matches().
    // Без этого кэша на live-стриме (N сообщений/сек × M регексов) был
    // заметный CPU-overhead из-за пересоздания RegExp каждое сообщение.
    const compiled = { showRes: [], hideRes: [], dirty: true };

    function recompile() {
      compiled.showRes = state.show.map(compileSafe).filter(Boolean);
      compiled.hideRes = state.hide.map(compileSafe).filter(Boolean);
      compiled.dirty = false;
    }
    function markDirty() { compiled.dirty = true; }

    function matches(line) {
      if (compiled.dirty) recompile();
      return _matches(line, compiled.showRes, compiled.hideRes);
    }

    function matchesBatch(lines) {
      if (compiled.dirty) recompile();
      const sr = compiled.showRes, hr = compiled.hideRes;
      return lines.filter(l => _matches(l, sr, hr));
    }

    function _matches(line, showRes, hideRes) {
      if (showRes.length && !showRes.some(r => r.test(line))) return false;
      if (hideRes.some(r => r.test(line))) return false;
      return true;
    }

    function setStat(visible, total) {
      const hasFilter = state.show.length || state.hide.length;
      const invalid = state.show.filter(s => !compileSafe(s)).length
                    + state.hide.filter(s => !compileSafe(s)).length;
      let txt = '';
      if (invalid) txt = window.rpT('log_filter.invalid_count', {count: invalid});
      else if (hasFilter) txt = window.rpT('log_filter.visible_count', {visible, total});
      else txt = total ? window.rpT('log_filter.line_count', {count: total}) : '';
      stat.textContent = txt;
      stat.style.color = invalid ? '#f55' : '';
    }

    function isEmpty() {
      return state.show.length === 0 && state.hide.length === 0;
    }

    return { matches, matchesBatch, setStat, isEmpty };
  }

  root.LogFilter = { attach };
})(window);
