import io
import json
import re
from pathlib import Path

import yaml
from babel.messages.pofile import read_po
from babel.messages.mofile import write_mo
from babel.support import Translations
from mkdocs.plugins import event_priority


def _flatten(prefix, value, dest):
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else k
            _flatten(key, v, dest)
    else:
        dest[prefix] = value


def _load_yaml_translations(locale_dir):
    translations = {}
    if not locale_dir.exists():
        return translations
    for path in locale_dir.glob("*.yml"):
        data = yaml.safe_load(path.read_text()) or {}
        flat = {}
        _flatten("", data, flat)
        translations[path.stem] = flat
    return translations


def _load_gettext_translations(i18n_dir):
    translations = {}
    if not i18n_dir.exists():
        return translations
    for lang_dir in i18n_dir.iterdir():
        if not lang_dir.is_dir():
            continue
        lc_dir = lang_dir / "LC_MESSAGES"
        if not lc_dir.exists():
            continue
        po_path = lc_dir / "messages.po"
        mo_path = lc_dir / "messages.mo"
        translator = None
        if po_path.exists():
            with po_path.open("r", encoding="utf-8") as po_file:
                catalog = read_po(po_file)
            buffer = io.BytesIO()
            write_mo(buffer, catalog)
            buffer.seek(0)
            translator = Translations(fp=buffer)
        elif mo_path.exists():
            with mo_path.open("rb") as mo_file:
                translator = Translations(mo_file)
        if translator is not None:
            translations[lang_dir.name] = translator
    return translations


def _build_translator(config):
    docs_dir = Path(config.get("docs_dir", "docs"))
    yaml_translations = _load_yaml_translations(docs_dir / "locale")
    gettext_translations = _load_gettext_translations(docs_dir / "i18n")
    default_lang = config.get("theme", {}).get("language", "en")

    def translate(key, lang=None):
        current_lang = lang or default_lang
        translator = gettext_translations.get(current_lang)
        if translator:
            value = translator.gettext(key)
            if value and value != key:
                return value
        yaml_lang = yaml_translations.get(current_lang, {})
        if key in yaml_lang:
            return yaml_lang[key]
        fallback_translator = gettext_translations.get(default_lang)
        if fallback_translator:
            value = fallback_translator.gettext(key)
            if value and value != key:
                return value
        return yaml_translations.get(default_lang, {}).get(key, key)

    return translate


def on_env(env, config, files):
    translator = _build_translator(config)
    env.globals["trans"] = translator
    env.filters["trans"] = translator
    return env


def on_page_context(context, page, config, nav):
    """
    Keep locale pages rooted at their own base so assets/search resolve inside the locale.
    """
    i18n = config.plugins.get("i18n")
    if not i18n or not page or not hasattr(page.file, "locale"):
        return context

    default_lang = next(
        (lang.locale for lang in i18n.config.languages if getattr(lang, "default", False)),
        config.get("theme", {}).get("language", "en"),
    )
    page_locale = page.file.locale or default_lang
    if page_locale == default_lang:
        return context

    base = getattr(page, "base_url", context.get("base", ""))
    if base.startswith("../"):
        base = base[3:] or "."
        page.base_url = base
        context["base"] = base
    return context


def on_post_page(output, page, config):
    """
    Adjust Material's base path so locale pages resolve assets/search inside their locale.
    """
    i18n = config.plugins.get("i18n")
    if not i18n or not page or not hasattr(page.file, "locale"):
        return output

    default_lang = next(
        (lang.locale for lang in i18n.config.languages if getattr(lang, "default", False)),
        config.get("theme", {}).get("language", "en"),
    )
    page_locale = page.file.locale or default_lang
    if page_locale == default_lang:
        return output

    parts = [p for p in (page.url or "").split("/") if p]
    if parts and parts[0] == page_locale:
        depth = max(len(parts) - 1, 0)
    else:
        depth = len(parts)
    new_base = "../" * depth or "."

    m = re.search(r'(<script id="__config" type="application/json">)(.*?)(</script>)', output, flags=re.S)
    if not m:
        return output

    try:
        cfg = json.loads(m.group(2))
    except Exception:
        return output

    cfg["base"] = new_base
    new_json = json.dumps(cfg, separators=(",", ":"))
    return output[: m.start(2)] + new_json + output[m.end(2) :]


@event_priority(-1000)
def on_post_build(config):
    """
    Split the search index per locale so each language only sees its own pages.
    """
    site_dir = Path(config["site_dir"])
    index_path = site_dir / "search" / "search_index.json"
    if not index_path.exists():
        return

    i18n_plugin = config.plugins.get("i18n")
    if not i18n_plugin:
        return

    languages = [lang.locale for lang in i18n_plugin.config.languages]
    default_lang = next((lang.locale for lang in i18n_plugin.config.languages if lang.default), None)
    if not default_lang:
        default_lang = i18n_plugin.config.default_language

    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return

    docs = data.get("docs", [])

    def is_lang_doc(doc, locale):
        location = doc.get("location", "")
        if locale == default_lang:
            # default language lives at the root; exclude other locales
            return not any(location.startswith(f"{lang}/") for lang in languages if lang != default_lang)
        return location.startswith(f"{locale}/")

    # write per-locale indexes; overwrite the root with default only
    for locale in languages:
        filtered_docs = [doc for doc in docs if is_lang_doc(doc, locale)]
        if not filtered_docs:
            continue

        localized = dict(data)
        localized["docs"] = filtered_docs
        localized["config"] = dict(localized.get("config", {}))
        localized["config"]["lang"] = [locale]

        target_path = index_path if locale == default_lang else site_dir / locale / "search" / "search_index.json"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(localized, ensure_ascii=False), encoding="utf-8")
