"""Bible reference encoding/decoding helpers.

Three encodings interoperate here:
    USFM book code   - 3-letter, e.g. "TIT"           — used in tags + ingest
    Canonical number - 1..66 (Protestant order)       — used in BBCCCVVV
    BBCCCVVV         - 8-digit integer BBCCCVVV       — used in passage_refs

"Romans 3:24" → BBCCCVVV 45003024 → human "Romans 3:24"

Note on numbering: this module uses **canonical Protestant numbering**
(GEN=1 … MAL=39, MAT=40 … REV=66). Door43's filename prefixes use a
different (Paratext) numbering with a skipped slot at 40 — that mapping
is in `ingest.door43` and only affects URL construction, not BBCCCVVV.
"""
from __future__ import annotations

import re

# fmt: off
BOOK_NUMBERS: dict[str, int] = {
    # Old Testament (1–39)
    "GEN":  1, "EXO":  2, "LEV":  3, "NUM":  4, "DEU":  5,
    "JOS":  6, "JDG":  7, "RUT":  8, "1SA":  9, "2SA": 10,
    "1KI": 11, "2KI": 12, "1CH": 13, "2CH": 14, "EZR": 15,
    "NEH": 16, "EST": 17, "JOB": 18, "PSA": 19, "PRO": 20,
    "ECC": 21, "SNG": 22, "ISA": 23, "JER": 24, "LAM": 25,
    "EZK": 26, "DAN": 27, "HOS": 28, "JOL": 29, "AMO": 30,
    "OBA": 31, "JON": 32, "MIC": 33, "NAM": 34, "HAB": 35,
    "ZEP": 36, "HAG": 37, "ZEC": 38, "MAL": 39,
    # New Testament (40–66)
    "MAT": 40, "MRK": 41, "LUK": 42, "JHN": 43, "ACT": 44,
    "ROM": 45, "1CO": 46, "2CO": 47, "GAL": 48, "EPH": 49,
    "PHP": 50, "COL": 51, "1TH": 52, "2TH": 53, "1TI": 54,
    "2TI": 55, "TIT": 56, "PHM": 57, "HEB": 58, "JAS": 59,
    "1PE": 60, "2PE": 61, "1JN": 62, "2JN": 63, "3JN": 64,
    "JUD": 65, "REV": 66,
}

BOOK_NAMES: dict[str, str] = {
    "GEN": "Genesis", "EXO": "Exodus", "LEV": "Leviticus", "NUM": "Numbers", "DEU": "Deuteronomy",
    "JOS": "Joshua", "JDG": "Judges", "RUT": "Ruth", "1SA": "1 Samuel", "2SA": "2 Samuel",
    "1KI": "1 Kings", "2KI": "2 Kings", "1CH": "1 Chronicles", "2CH": "2 Chronicles", "EZR": "Ezra",
    "NEH": "Nehemiah", "EST": "Esther", "JOB": "Job", "PSA": "Psalms", "PRO": "Proverbs",
    "ECC": "Ecclesiastes", "SNG": "Song of Songs", "ISA": "Isaiah", "JER": "Jeremiah", "LAM": "Lamentations",
    "EZK": "Ezekiel", "DAN": "Daniel", "HOS": "Hosea", "JOL": "Joel", "AMO": "Amos",
    "OBA": "Obadiah", "JON": "Jonah", "MIC": "Micah", "NAM": "Nahum", "HAB": "Habakkuk",
    "ZEP": "Zephaniah", "HAG": "Haggai", "ZEC": "Zechariah", "MAL": "Malachi",
    "MAT": "Matthew", "MRK": "Mark", "LUK": "Luke", "JHN": "John", "ACT": "Acts",
    "ROM": "Romans", "1CO": "1 Corinthians", "2CO": "2 Corinthians", "GAL": "Galatians", "EPH": "Ephesians",
    "PHP": "Philippians", "COL": "Colossians", "1TH": "1 Thessalonians", "2TH": "2 Thessalonians",
    "1TI": "1 Timothy", "2TI": "2 Timothy", "TIT": "Titus", "PHM": "Philemon", "HEB": "Hebrews",
    "JAS": "James", "1PE": "1 Peter", "2PE": "2 Peter", "1JN": "1 John", "2JN": "2 John",
    "3JN": "3 John", "JUD": "Jude", "REV": "Revelation",
}
# fmt: on

NUMBER_TO_CODE: dict[int, str] = {n: c for c, n in BOOK_NUMBERS.items()}


def _normalize_alias(name: str) -> str:
    return re.sub(r"\s+", "", name).lower()


# Map normalized natural-language / abbreviation forms → USFM code.
BOOK_ALIASES: dict[str, str] = {}


def _seed_aliases() -> None:
    extras = {
        "GEN": ["genesis", "gen", "ge", "gn"],
        "EXO": ["exodus", "exo", "ex", "exod"],
        "LEV": ["leviticus", "lev", "lv"],
        "NUM": ["numbers", "num", "nm", "nu"],
        "DEU": ["deuteronomy", "deu", "deut", "dt"],
        "JOS": ["joshua", "jos", "josh"],
        "JDG": ["judges", "jdg", "judg", "jgs"],
        "RUT": ["ruth", "rut", "ru"],
        "1SA": ["1samuel", "1sam", "1sa"],
        "2SA": ["2samuel", "2sam", "2sa"],
        "1KI": ["1kings", "1kgs", "1ki"],
        "2KI": ["2kings", "2kgs", "2ki"],
        "1CH": ["1chronicles", "1chr", "1ch"],
        "2CH": ["2chronicles", "2chr", "2ch"],
        "EZR": ["ezra", "ezr"],
        "NEH": ["nehemiah", "neh"],
        "EST": ["esther", "est", "esth"],
        "JOB": ["job", "jb"],
        "PSA": ["psalms", "psalm", "psa", "ps"],
        "PRO": ["proverbs", "pro", "prov", "pr"],
        "ECC": ["ecclesiastes", "ecc", "eccl", "qoh"],
        "SNG": ["songofsongs", "songofsolomon", "song", "sng", "sos"],
        "ISA": ["isaiah", "isa", "is"],
        "JER": ["jeremiah", "jer"],
        "LAM": ["lamentations", "lam"],
        "EZK": ["ezekiel", "ezk", "ezek", "eze"],
        "DAN": ["daniel", "dan", "dn"],
        "HOS": ["hosea", "hos"],
        "JOL": ["joel", "jol"],
        "AMO": ["amos", "amo", "am"],
        "OBA": ["obadiah", "oba", "obad", "ob"],
        "JON": ["jonah", "jon"],
        "MIC": ["micah", "mic", "mi"],
        "NAM": ["nahum", "nam", "nah"],
        "HAB": ["habakkuk", "hab"],
        "ZEP": ["zephaniah", "zep", "zeph"],
        "HAG": ["haggai", "hag"],
        "ZEC": ["zechariah", "zec", "zech"],
        "MAL": ["malachi", "mal"],
        "MAT": ["matthew", "mat", "matt", "mt"],
        "MRK": ["mark", "mrk", "mk"],
        "LUK": ["luke", "luk", "lk"],
        "JHN": ["john", "jhn", "jn"],
        "ACT": ["acts", "act"],
        "ROM": ["romans", "rom", "ro"],
        "1CO": ["1corinthians", "1cor", "1co"],
        "2CO": ["2corinthians", "2cor", "2co"],
        "GAL": ["galatians", "gal"],
        "EPH": ["ephesians", "eph"],
        "PHP": ["philippians", "php", "phil"],
        "COL": ["colossians", "col"],
        "1TH": ["1thessalonians", "1thess", "1th"],
        "2TH": ["2thessalonians", "2thess", "2th"],
        "1TI": ["1timothy", "1tim", "1ti"],
        "2TI": ["2timothy", "2tim", "2ti"],
        "TIT": ["titus", "tit", "ti"],
        "PHM": ["philemon", "phm", "phlm"],
        "HEB": ["hebrews", "heb"],
        "JAS": ["james", "jas", "jms"],
        "1PE": ["1peter", "1pet", "1pe"],
        "2PE": ["2peter", "2pet", "2pe"],
        "1JN": ["1john", "1jn"],
        "2JN": ["2john", "2jn"],
        "3JN": ["3john", "3jn"],
        "JUD": ["jude", "jud", "jud"],
        "REV": ["revelation", "rev", "apocalypse", "apoc"],
    }
    for code, aliases in extras.items():
        for alias in aliases:
            BOOK_ALIASES[_normalize_alias(alias)] = code
        BOOK_ALIASES[_normalize_alias(code)] = code


_seed_aliases()


def encode(book_code: str, chapter: int, verse: int) -> int:
    """Encode (book_code, chapter, verse) → BBCCCVVV integer."""
    book = BOOK_NUMBERS.get(book_code.upper())
    if book is None:
        raise ValueError(f"unknown book code: {book_code}")
    if not (1 <= chapter <= 999) or not (1 <= verse <= 999):
        raise ValueError(f"chapter/verse out of range: {chapter}:{verse}")
    return book * 1_000_000 + chapter * 1_000 + verse


def decode(bbcccvvv: int) -> tuple[str, int, int]:
    """Decode BBCCCVVV integer → (book_code, chapter, verse)."""
    book_num = bbcccvvv // 1_000_000
    chapter = (bbcccvvv // 1_000) % 1_000
    verse = bbcccvvv % 1_000
    code = NUMBER_TO_CODE.get(book_num)
    if code is None:
        raise ValueError(f"unknown book number: {book_num}")
    return code, chapter, verse


def human(start_bbcccvvv: int, end_bbcccvvv: int | None = None) -> str:
    """Render a passage range as 'Romans 3:24' or 'Romans 3:24-25' or 'Romans 3:24-4:2'."""
    s_code, s_ch, s_v = decode(start_bbcccvvv)
    if end_bbcccvvv is None or end_bbcccvvv == start_bbcccvvv:
        return f"{BOOK_NAMES[s_code]} {s_ch}:{s_v}"
    e_code, e_ch, e_v = decode(end_bbcccvvv)
    if s_code != e_code:
        return f"{BOOK_NAMES[s_code]} {s_ch}:{s_v} – {BOOK_NAMES[e_code]} {e_ch}:{e_v}"
    if s_ch == e_ch:
        return f"{BOOK_NAMES[s_code]} {s_ch}:{s_v}-{e_v}"
    return f"{BOOK_NAMES[s_code]} {s_ch}:{s_v}-{e_ch}:{e_v}"


# Match natural-language references like "Titus 1:1", "Rom 3:24-25",
# "1 Corinthians 13:4-7", "1Cor 13", "Genesis 1", "Ruth chapter 1", "Ruth ch 1".
#
# Note: a chapter number is REQUIRED. Bare book names ("Titus", "Ruth") do
# NOT extract a passage filter, because two-letter book aliases like "is"
# (Isaiah), "am" (Amos), "ti" (Titus), "ge" (Genesis) collide catastrophically
# with common English words. If a user wants whole-book scope they should
# write "Titus 1" or "Ruth 1" — explicit chapter disambiguates.
_REF_RE = re.compile(
    r"""
    \b
    ((?:[123]\s*)?[A-Za-z]+)             # book name (optional 1/2/3 prefix)
    \s+(?:chapter\s+|chap\.?\s+|ch\.?\s+)?  # optional "chapter" / "chap." / "ch." filler
    (\d+)                                # chapter number (REQUIRED)
    (?:                                  # optional verse(s)
      :(\d+)
      (?:-(\d+)(?::(\d+))?)?
    )?
    \b
    """,
    re.VERBOSE,
)


def parse_references(text: str) -> list[tuple[int, int]]:
    """Find natural-language refs in `text`, return list of (start, end) BBCCCVVV pairs.

    Notes:
      • Bare book names ("Titus", "Ruth") expand to the whole-book range.
      • Whole-chapter queries ("Titus 1") expand to verse 1..999 of that chapter.
      • Cross-chapter ranges ("Romans 3:24-4:2") are honored.
      • Unknown book names are silently skipped — this is a best-effort
        analyzer, not a validator. The synthesis layer is the trust boundary.
    """
    out: list[tuple[int, int]] = []
    for m in _REF_RE.finditer(text):
        book_raw, ch_s, v_s, v_or_ch_e, v_e = m.groups()
        code = BOOK_ALIASES.get(_normalize_alias(book_raw))
        if not code:
            continue
        ch = int(ch_s)
        try:
            if v_s is None:
                start = encode(code, ch, 1)
                end = encode(code, ch, 999)
            elif v_or_ch_e is None:
                start = encode(code, ch, int(v_s))
                end = start
            elif v_e is None:
                start = encode(code, ch, int(v_s))
                end = encode(code, ch, int(v_or_ch_e))
            else:
                start = encode(code, ch, int(v_s))
                end = encode(code, int(v_or_ch_e), int(v_e))
        except ValueError:
            continue
        out.append((start, end) if start <= end else (end, start))
    return out
