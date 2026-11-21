// Named fields are replaced once. Values stay plain text until their output boundary.
window.rpT = function (key, values = {}) {
  const messages = window.roboportMessages || {};
  let message = Object.hasOwn(messages, key) ? messages[key] : key;
  if (typeof message === 'object') {
    const category = pluralRules.select(Number(values.count));
    message = message[category] || message.other;
  }
  return message.replace(/\{([A-Za-z_][A-Za-z_0-9]*)\}/g, (token, name) =>
    Object.hasOwn(values, name) ? String(values[name]) : token);
};
const pluralRules = new Intl.PluralRules(document.documentElement.lang);
const relativeTime = new Intl.RelativeTimeFormat(document.documentElement.lang, {style: 'short'});
window.rpTimeAgo = function (seconds) {
  const age = Math.max(0, Math.floor(seconds));
  if (age < 60) return relativeTime.format(-age, 'second');
  if (age < 3600) return relativeTime.format(-Math.floor(age / 60), 'minute');
  if (age < 86400) return relativeTime.format(-Math.floor(age / 3600), 'hour');
  return relativeTime.format(-Math.floor(age / 86400), 'day');
};
window.rpEscapeHtml = function (text) {
  const entities = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
  return String(text).replace(/[&<>"']/g, character => entities[character]);
};
window.rpTHtml = (key, values) => window.rpEscapeHtml(window.rpT(key, values));
document.addEventListener('change', event => {
  if (!event.target.matches('.language-picker select')) return;
  const form = event.target.form;
  form.elements.next.value = location.pathname + location.search + location.hash;
  form.requestSubmit();
});
