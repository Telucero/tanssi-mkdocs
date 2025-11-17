from pathlib import Path
import io
import yaml
from babel.messages.pofile import read_po
from babel.messages.mofile import write_mo
from babel.support import Translations


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
