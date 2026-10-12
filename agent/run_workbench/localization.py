"""Official Chinese names for the per-game detail view.

The game's own translations live in ``localization_zhs/`` / ``localization_eng/``
at the repository root (``<ID>.title`` for cards, relics and potions,
``<ID>.name`` for monsters, ...).  A game log carries English display names and
ids ("CARD.BASH", "Shrinker Beetle"), the solver's plan carries loc keys
("BASH.title", "SHRINKER_BEETLE.name"); this module maps all of them to the
Chinese text without ever touching the network (``/api/translations`` fetches a
public page and covers neither monsters nor potions).

A name with no translation maps to ``None``: callers keep the English name.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from threading import Lock
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
_ID_PREFIXES = ("CARD.", "POTION.", "RELIC.", "POWER.", "MONSTER.")
_UPGRADE_SUFFIX = re.compile(r"\+\d*$")
_PLURAL = re.compile(r"^(?P<var>[A-Za-z_][\w]*):plural:(?P<forms>.*)$", re.DOTALL)
_MARKUP = re.compile(r"\[/?[a-z_]+(?:=[^\]]*)?\]")
_TEMPLATE = re.compile(r"\{([^{}]*)\}")


def bare_id(value: Any) -> str:
    """``"CARD.BASH"`` / ``"bash"`` -> ``"BASH"`` (the entry the loc files key on)."""
    if isinstance(value, dict):
        value = value.get("en", "")
    text = str(value or "").strip().upper()
    for prefix in _ID_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def strip_markup(text: str) -> str:
    """``[gold]Block[/gold]`` -> ``Block`` (the game's rich-text tags)."""
    return _MARKUP.sub("", text)


def render_template(text: Any, variables: Any) -> str | None:
    """Fill ``{Var}`` / ``{Var:diff()}`` / ``{Var:plural:a|b}`` from ``variables``.

    Best effort, for event option text: anything that cannot be resolved is left
    as written so a reader still sees what the option does.
    """
    if not isinstance(text, str) or not text:
        return None
    values = variables if isinstance(variables, dict) else {}

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        plural = _PLURAL.match(body)
        if plural:
            amount = values.get(plural.group("var"))
            forms = plural.group("forms").split("|")
            if isinstance(amount, (int, float)) and not isinstance(amount, bool) and forms:
                form = forms[0] if amount == 1 or len(forms) == 1 else forms[1]
                return form.replace("{}", str(amount))
            return match.group(0)
        name = body.split(":", 1)[0]
        if name in values and isinstance(values[name], (str, int, float)) and not isinstance(
            values[name], bool
        ):
            return str(values[name])
        return match.group(0)

    return _TEMPLATE.sub(replace, strip_markup(text))


class Localizer:
    """Lazy, cached lookups over the repository's loc tables."""

    def __init__(self, root: Path | str | None = None, lang: str = "localization_zhs") -> None:
        self.root = Path(root) if root is not None else REPO_ROOT
        self.lang = lang
        self._lock = Lock()
        self._tables: dict[str, dict[str, str]] = {}
        self._reverse: dict[str, dict[str, str]] = {}

    # -- loading ---------------------------------------------------------

    def _table(self, directory: str, name: str) -> dict[str, str]:
        key = f"{directory}/{name}"
        with self._lock:
            table = self._tables.get(key)
            if table is None:
                table = {}
                try:
                    raw = json.loads(
                        (self.root / directory / f"{name}.json").read_text(encoding="utf-8")
                    )
                    if isinstance(raw, dict):
                        table = {
                            k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)
                        }
                except (OSError, ValueError):
                    table = {}
                self._tables[key] = table
            return table

    def zh(self, name: str, key: str) -> str | None:
        return self._table(self.lang, name).get(key) or None

    def _english_index(self, name: str, suffix: str) -> dict[str, str]:
        """English display name (lower-cased) -> entry id, from localization_eng."""
        key = f"{name}{suffix}"
        with self._lock:
            index = self._reverse.get(key)
        if index is not None:
            return index
        index = {}
        for loc_key, english in self._table("localization_eng", name).items():
            if loc_key.endswith(suffix) and loc_key.count(".") == suffix.count("."):
                entry = loc_key[: -len(suffix)]
                index.setdefault(english.strip().lower(), entry)
        with self._lock:
            self._reverse[key] = index
        return index

    def _by_english(self, name: str, suffix: str, english: Any) -> str | None:
        if not isinstance(english, str) or not english.strip():
            return None
        entry = self._english_index(name, suffix).get(english.strip().lower())
        return self.zh(name, entry + suffix) if entry else None

    # -- public lookups --------------------------------------------------

    def card(self, card_id: Any = None, name: Any = None) -> str | None:
        entry = bare_id(card_id) if card_id else ""
        if entry:
            found = self.zh("cards", f"{entry}.title")
            if found:
                return found
        if isinstance(name, str):
            return self._by_english("cards", ".title", _UPGRADE_SUFFIX.sub("", name))
        return None

    def potion(self, potion_id: Any = None, name: Any = None) -> str | None:
        entry = bare_id(potion_id) if potion_id else ""
        if entry:
            found = self.zh("potions", f"{entry}.title")
            if found:
                return found
        return self._by_english("potions", ".title", name)

    def relic(self, relic_id: Any = None, name: Any = None) -> str | None:
        entry = bare_id(relic_id) if relic_id else ""
        if entry:
            found = self.zh("relics", f"{entry}.title")
            if found:
                return found
        return self._by_english("relics", ".title", name)

    def monster(self, name: Any = None, key: Any = None) -> str | None:
        """``key`` is the solver's loc key (``"SHRINKER_BEETLE.name"``)."""
        if isinstance(key, str) and key:
            found = self.zh("monsters", key if "." in key else f"{key}.name")
            if found:
                return found
        return self._by_english("monsters", ".name", name)

    def power(self, name: Any = None) -> str | None:
        return self._by_english("powers", ".title", name)

    def event(self, name: Any = None) -> str | None:
        found = self._by_english("events", ".title", name)
        return found or self._by_english("ancients", ".title", name)

    def event_option(self, text_key: Any = None) -> str | None:
        """Title of an event option (``"NEOW.pages.INITIAL.options.SCROLL_BOXES"``).

        An ancient's gift option is named after the relic or card it gives, so
        the last segment is looked up there when the event tables have no entry.
        """
        if not isinstance(text_key, str) or not text_key:
            return None
        for table in ("events", "ancients"):
            found = self.zh(table, f"{text_key}.title")
            if found:
                return found
        last = text_key.rsplit(".", 1)[-1]
        return self.zh("relics", f"{last}.title") or self.zh("cards", f"{last}.title")

    def event_option_description(self, text_key: Any = None, variables: Any = None) -> str | None:
        if not isinstance(text_key, str) or not text_key:
            return None
        for table in ("events", "ancients"):
            found = self.zh(table, f"{text_key}.description")
            if found:
                return render_template(found, variables)
        last = text_key.rsplit(".", 1)[-1]
        found = self.zh("relics", f"{last}.description") or self.zh("cards", f"{last}.description")
        return render_template(found, variables) if found else None

    def rest_option(self, option_id: Any = None) -> str | None:
        if not isinstance(option_id, str) or not option_id:
            return None
        return self.zh("rest_site_ui", f"OPTION_{option_id.upper()}.name")
