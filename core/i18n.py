"""JSON based localization for OpusFun.

Strings live in ``locales/<lang>.json`` and are addressed with dotted keys
(``"reactions.slap"``).  Values may be either a string or a list of strings; a
list means "pick a random variant", which keeps reactions from feeling
repetitive.

The loader never raises on a missing key: it logs a warning and returns a
visible ``⚠️ missing string`` marker so a typo degrades one message instead of
crashing a handler.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List

log = logging.getLogger(__name__)

_MISSING = object()


class Locale:
    """A single loaded language file."""

    __slots__ = ("code", "_data")

    def __init__(self, code: str, data: Dict[str, Any]) -> None:
        self.code = code
        self._data = data

    def raw(self, key: str) -> Any:
        node: Any = self._data
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return _MISSING
        return node

    def has(self, key: str) -> bool:
        return self.raw(key) is not _MISSING


class Translator:
    """Loads locale files and renders strings with ``{placeholder}`` support."""

    def __init__(self, locales_dir: Path, default_language: str = "en") -> None:
        self.locales_dir = Path(locales_dir)
        self.default_language = default_language
        self._locales: Dict[str, Locale] = {}
        self._lock = threading.Lock()
        self.reload()

    # ------------------------------------------------------------------ loading
    def reload(self) -> None:
        """(Re)read every ``*.json`` file in the locales directory."""
        locales: Dict[str, Locale] = {}
        if not self.locales_dir.is_dir():
            raise FileNotFoundError(f"Locales directory not found: {self.locales_dir}")

        for path in sorted(self.locales_dir.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                log.error("Failed to load locale file %s: %s", path.name, exc)
                continue
            if not isinstance(data, dict):
                log.error("Locale file %s must contain a JSON object at the top level", path.name)
                continue
            locales[path.stem] = Locale(path.stem, data)
            log.debug("Loaded locale '%s' from %s", path.stem, path.name)

        if self.default_language not in locales:
            raise FileNotFoundError(
                f"Default locale '{self.default_language}.json' is missing from {self.locales_dir}"
            )

        with self._lock:
            self._locales = locales
        log.info("Loaded %d locale(s): %s", len(locales), ", ".join(sorted(locales)))

    @property
    def languages(self) -> List[str]:
        return sorted(self._locales)

    # ----------------------------------------------------------------- lookups
    def _lookup(self, key: str, language: str | None) -> Any:
        candidates: Iterable[str] = (
            code for code in (language, self.default_language) if code
        )
        for code in candidates:
            locale = self._locales.get(code)
            if locale is None:
                continue
            value = locale.raw(key)
            if value is not _MISSING:
                return value
        return _MISSING

    def has(self, key: str, language: str | None = None) -> bool:
        return self._lookup(key, language) is not _MISSING

    def get(self, key: str, language: str | None = None, **kwargs: Any) -> str:
        """Return the localized string for ``key`` with placeholders applied.

        If the value is a list, one entry is chosen at random.
        """
        value = self._lookup(key, language)
        if value is _MISSING:
            log.warning("Missing localization key: %s (language=%s)", key, language)
            return f"⚠️ missing string: {key}"

        if isinstance(value, list):
            if not value:
                log.warning("Localization key %s resolved to an empty list", key)
                return f"⚠️ missing string: {key}"
            value = random.choice(value)

        if isinstance(value, (list, dict)):
            log.warning("Localization key %s is not a renderable string", key)
            return f"⚠️ missing string: {key}"

        text = str(value)
        if not kwargs:
            return text
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            # A malformed placeholder must never take a handler down.
            log.warning("Failed to format localization key %s: %s", key, exc)
            return text

    __call__ = get


_translator: Translator | None = None


def get_translator() -> Translator:
    """Return the process-wide translator, creating it on first use."""
    global _translator
    if _translator is None:
        from config import get_config

        config = get_config()
        _translator = Translator(config.locales_dir, config.default_language)
    return _translator


def set_translator(translator: Translator) -> None:
    global _translator
    _translator = translator


def tr(key: str, language: str | None = None, **kwargs: Any) -> str:
    """Shorthand used across plugins."""
    return get_translator().get(key, language, **kwargs)
