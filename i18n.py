"""Message dictionary: lang/<code>.json, chosen by the YT_LANG environment variable."""
import json
import os
from pathlib import Path

LANG_DIR = Path(__file__).parent / "lang"
DEFAULT_LANG = "he"
LANG = os.environ.get("YT_LANG") or DEFAULT_LANG


def _load(lang):
    try:
        return json.loads((LANG_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# Missing keys fall back to the default language, then to the key itself.
_MSG = {**_load(DEFAULT_LANG), **_load(LANG)}


def t(key, **kw):
    return _MSG.get(key, key).format(**kw)


def has(key):
    return key in _MSG
