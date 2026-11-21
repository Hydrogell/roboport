# Translating Roboport

Every interface language has one UTF-8 JSON file in [app/locales](../app/locales). The panel discovers these files at startup. The language menu on the sign-in page and in the panel shows each language under its native name. The browser's preferred language is used until the visitor makes a choice. Selecting “Browser language” restores automatic selection.

## Add or improve a language

1. To improve a translation, edit its existing file. To add a language, copy `en.json` and name the copy after its BCP 47 tag, such as `da.json` or `fr-CA.json`.
2. Set `name` to the language's native name. Add `"direction": "rtl"` for a right-to-left language. Left-to-right is the default.
3. Translate the values under `messages`. Each key identifies one message. Keep the keys and named placeholders such as `{filename}` exactly as written. Placeholders can move to fit the sentence. Keep commands, paths, environment variables and product names intact.
4. For a count, use a JSON object with the plural categories your language needs. Always include `other`. The panel selects a form using the language's CLDR rules.
5. From the project root, run `python -m app.i18n --check` with the project dependencies installed. Restart the panel to load edits and select the language to review the affected screens.

```json
{
  "name": "English",
  "messages": {
    "nav.open_menu": "Open menu",
    "settings.audit_count": {
      "one": "{count} entry",
      "other": "{count} entries"
    }
  }
}
```

## Add an interface message

Use a stable key describing the meaning of the message, such as `nav.open_menu`. Reuse an existing key when its meaning and context match. Add the message to every shipped catalog. Keep complete phrases together so translators can change word order.

| Where | Example |
| --- | --- |
| Python | `tr("nav.open_menu")` after importing `tr` from `app.i18n` |
| Jinja | `{{ t('settings.audit_count', count=count) }}` |
| JavaScript | `element.textContent = window.rpT('settings.audit_count', {count})` |

Pass dynamic values as named parameters. Treat the result as plain text. Templates use their usual HTML escaping, and browser code can assign translations to `textContent`. When embedding a translated value in a script inside a template, serialize it with Jinja's `tojson` filter. Keep server names, log lines, user-entered text and machine-readable API values in their original form.

The English catalog supplies fallback text and the reference set for validation. The checker reports missing keys, extra keys, mismatched placeholders and invalid plural definitions. The initial catalogs include machine translations. Corrections from speakers are welcome. The language set follows the [Factorio interface language list on Steam](https://store.steampowered.com/app/427520/Factorio/?l=english).
