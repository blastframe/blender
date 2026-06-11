#!/usr/bin/env python3
"""Lookup Blender UI term translations across Blender locales.

This utility can:
1) Lookup explicit terms across all Blender languages.
2) Extract probable Blender UI terms from external add-on English translation JSON files,
   then lookup those terms across all languages.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set


BLENDER_LANGUAGES: Dict[str, str] = {
    "DEFAULT": "Automatic (Default)",
    "ab": "Abkhaz - Аԥсуа бызшәа",
    "ar_EG": "Arabic - ﺔﻴﺑﺮﻌﻟﺍ",
    "eu_EU": "Basque - Euskara",
    "be": "Belarusian - Беларуская",
    "bg_BG": "Bulgarian - Български",
    "ca_AD": "Catalan - Català",
    "zh_HANS": "Chinese (Simplified) - 简体中文",
    "zh_HANT": "Chinese (Traditional) - 繁體中文",
    "cs_CZ": "Czech - Čeština",
    "da": "Danish - Dansk",
    "nl_NL": "Dutch - Nederlands",
    "en_GB": "English (UK)",
    "en_US": "English (US)",
    "eo": "Esperanto - Esperanto",
    "fi_FI": "Finnish - Suomi",
    "fr_FR": "French - Français",
    "ka": "Georgian - ქართული",
    "de_DE": "German - Deutsch",
    "el_GR": "Greek - Ελληνικά",
    "he_IL": "Hebrew - תירִבְעִ",
    "hi_IN": "Hindi - हिन्दी",
    "hu_HU": "Hungarian - Magyar",
    "id_ID": "Indonesian - Bahasa indonesia",
    "it_IT": "Italian - Italiano",
    "ja_JP": "Japanese - 日本語",
    "ko_KR": "Korean - 한국어",
    "ky_KG": "Kyrgyz - Кыргыз тили",
    "lt": "Lithuanian - Lietuviškai",
    "ml": "Malayalam - മലയാളം",
    "nb": "Norwegian (Bokmål) - Norsk bokmål",
    "fa_IR": "Persian - ﯽﺳﺭﺎﻓ",
    "pl_PL": "Polish - Polski",
    "pt_BR": "Portuguese (Brazil) - Português brasileiro",
    "pt_PT": "Portuguese (Portugal) - Português europeu",
    "ro_RO": "Romanian - Român",
    "ru_RU": "Russian - Русский",
    "sr_RS": "Serbian (Cyrillic) - Српски",
    "sr_RS@latin": "Serbian (Latin) - Srpski latinica",
    "sk_SK": "Slovak - Slovenčina",
    "sl": "Slovenian - Slovenščina",
    "es": "Spanish - Español",
    "sw": "Swahili - Kiswahili",
    "sv_SE": "Swedish - Svenska",
    "ta": "Tamil - தமிழ்",
    "th_TH": "Thai - ภาษาไทย",
    "tr_TR": "Turkish - Türkçe",
    "uk_UA": "Ukrainian - Українська",
    "ur": "Urdu - وُدرُا",
    "vi_VN": "Vietnamese - Tiếng Việt",
}


LOCALE_TO_PO_STEM: Dict[str, str] = {
    "ab": "ab",
    "ar_EG": "ar",
    "eu_EU": "eu",
    "be": "be",
    "bg_BG": "bg",
    "ca_AD": "ca",
    "zh_HANS": "zh_HANS",
    "zh_HANT": "zh_HANT",
    "cs_CZ": "cs",
    "da": "da",
    "nl_NL": "nl",
    "en_GB": "en_GB",
    "eo": "eo",
    "fi_FI": "fi",
    "fr_FR": "fr",
    "ka": "ka",
    "de_DE": "de",
    "el_GR": "el",
    "he_IL": "he",
    "hi_IN": "hi",
    "hu_HU": "hu",
    "id_ID": "id",
    "it_IT": "it",
    "ja_JP": "ja",
    "ko_KR": "ko",
    "ky_KG": "ky",
    "lt": "lt",
    "ml": "ml",
    "nb": "nb",
    "fa_IR": "fa",
    "pl_PL": "pl",
    "pt_BR": "pt_BR",
    "pt_PT": "pt",
    "ro_RO": "ro",
    "ru_RU": "ru",
    "sr_RS": "sr",
    "sr_RS@latin": "sr@latin",
    "sk_SK": "sk",
    "sl": "sl",
    "es": "es",
    "sw": "sw",
    "sv_SE": "sv",
    "ta": "ta",
    "th_TH": "th",
    "tr_TR": "tr",
    "uk_UA": "uk",
    "ur": "ur",
    "vi_VN": "vi",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-/]*")
_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "at",
    "be",
    "before",
    "by",
    "for",
    "from",
    "in",
    "is",
    "more",
    "no",
    "not",
    "of",
    "on",
    "one",
    "or",
    "the",
    "to",
    "two",
    "up",
    "with",
}


@dataclass
class Entry:
    msgid: str = ""
    msgstr: str = ""
    in_plural: bool = False


def _po_unescape(quoted_with_delimiters: str) -> str:
    return ast.literal_eval(quoted_with_delimiters)


def parse_po_catalog(path: Path) -> Dict[str, str]:
    catalog: Dict[str, str] = {}
    entry = Entry()
    active: str | None = None

    def flush() -> None:
        nonlocal entry
        if entry.msgid and not entry.in_plural:
            catalog.setdefault(entry.msgid, entry.msgstr)
        entry = Entry()

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped:
                flush()
                active = None
                continue
            if stripped.startswith("#"):
                continue

            if stripped.startswith("msgid_plural"):
                entry.in_plural = True
                active = None
                continue
            if stripped.startswith("msgstr["):
                active = None
                continue

            if stripped.startswith("msgid"):
                active = "msgid"
                quoted = stripped[len("msgid") :].strip()
                entry.msgid = _po_unescape(quoted) if quoted.startswith('"') else ""
                continue

            if stripped.startswith("msgstr"):
                active = "msgstr"
                quoted = stripped[len("msgstr") :].strip()
                entry.msgstr = _po_unescape(quoted) if quoted.startswith('"') else ""
                continue

            if stripped.startswith('"') and stripped.endswith('"') and active:
                fragment = _po_unescape(stripped)
                if active == "msgid":
                    entry.msgid += fragment
                elif active == "msgstr":
                    entry.msgstr += fragment

    flush()
    return catalog


def load_all_catalogs(po_dir: Path) -> Dict[str, Dict[str, str]]:
    catalogs: Dict[str, Dict[str, str]] = {}
    for locale_code in BLENDER_LANGUAGES:
        if locale_code in {"DEFAULT", "en_US"}:
            continue
        po_stem = LOCALE_TO_PO_STEM.get(locale_code)
        if po_stem is None:
            catalogs[locale_code] = {}
            continue
        po_path = po_dir / f"{po_stem}.po"
        catalogs[locale_code] = parse_po_catalog(po_path) if po_path.exists() else {}
    return catalogs


def normalize_text(value: str) -> str:
    return value.replace("\u2011", "-").strip()


def extract_interface_terms(texts: Iterable[str], msgids: Set[str]) -> List[str]:
    found: Set[str] = set()

    for text in texts:
        clean = normalize_text(text)
        if not clean:
            continue

        tokens = _TOKEN_RE.findall(clean)
        for n in range(1, min(4, len(tokens)) + 1):
            for start in range(0, len(tokens) - n + 1):
                phrase = " ".join(tokens[start : start + n]).strip(" .,:;!?()[]{}")
                if len(phrase) < 2:
                    continue
                candidates = {phrase}
                if phrase.endswith("s") and len(phrase) > 3:
                    candidates.add(phrase[:-1])
                for candidate in candidates:
                    low = candidate.lower()
                    if candidate not in msgids:
                        continue
                    if low in _STOPWORDS:
                        continue
                    if candidate.islower() and " " not in candidate and "-" not in candidate:
                        continue
                    if any(ch.isalpha() for ch in candidate):
                        found.add(candidate)

    return sorted(found, key=lambda term: (term.lower(), term))


def lookup_terms(terms: Sequence[str], catalogs: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    for raw_term in terms:
        term = normalize_text(raw_term)
        if not term:
            continue
        per_locale: Dict[str, str] = {}
        for locale_code in BLENDER_LANGUAGES:
            if locale_code in {"DEFAULT", "en_US"}:
                per_locale[locale_code] = term
                continue
            catalog = catalogs.get(locale_code, {})
            translated = catalog.get(term)
            per_locale[locale_code] = translated if translated else term
        result[term] = per_locale
    return result


def load_addon_messages(paths: Sequence[Path]) -> List[str]:
    messages: List[str] = []
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        messages.extend(data.keys())
    return messages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--po-dir", type=Path, default=Path("locale/po"))
    parser.add_argument("--terms", nargs="*", default=[])
    parser.add_argument("--addon-json", nargs="*")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--print-summary", action="store_true")
    args = parser.parse_args()

    catalogs = load_all_catalogs(args.po_dir)
    all_msgids: Set[str] = set()
    for catalog in catalogs.values():
        all_msgids.update(catalog.keys())

    extracted_terms: List[str] = []
    addon_paths = [Path(value) for value in (args.addon_json or [])]
    if addon_paths:
        extracted_terms = extract_interface_terms(load_addon_messages(addon_paths), all_msgids)

    final_terms = sorted({normalize_text(term) for term in [*args.terms, *extracted_terms] if term.strip()})

    payload = {
        "languages": BLENDER_LANGUAGES,
        "terms": final_terms,
        "extracted_terms": extracted_terms,
        "translations": lookup_terms(final_terms, catalogs),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.print_summary:
        print(f"Extracted {len(extracted_terms)} probable Blender UI terms from add-on strings.")
        if extracted_terms:
            print(", ".join(extracted_terms))
        print(f"Wrote translation lookup to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
