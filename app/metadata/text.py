"""Text helpers shared by the metadata providers: normalisation, title parsing, version detection."""

from __future__ import annotations

import re
import unicodedata

_NOISE = re.compile(
    r"\s*[\(\[【]\s*[^\)\]】]*?\b(official|video|audio|lyrics?|visuali[sz]er|music video|mv|m/v|hd|hq|4k|"
    r"remaster(ed)?\s*\d*|explicit|clean|full\s+song|color\s+coded)\b[^\)\]】]*[\)\]】]",
    re.IGNORECASE,
)
_FEAT = re.compile(r"\s*[\(\[]\s*(feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", re.IGNORECASE)
_FEAT_TAIL = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s.+$", re.IGNORECASE)
_REMASTER_TAIL = re.compile(r"\s+-\s+(\d{4}\s+)?remaster(ed)?(\s+\d{4})?(\s+version)?$", re.IGNORECASE)
_DASHES = (" - ", " – ", " — ", " ~ ")

# Words that mark a non-original recording (kept from the old notebook's matching rules).
NON_ORIGINAL = re.compile(
    r"\b(karaoke|instrumental|cover|tribute|backing\s*track|minus\s*one|no\s*vocals?|without\s*vocals?|"
    r"remake|rendition|in\s*the\s*style\s*of|made\s*famous|originally\s*performed|8d\s*audio|nightcore|"
    r"slowed|sped\s*up|reverb)\b",
    re.IGNORECASE,
)


def fold(text: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def title_key(title: str) -> str:
    """Comparable form of a song title: no 'feat.' credits, remaster suffixes or video noise."""
    t = _NOISE.sub("", title or "")
    t = _FEAT.sub("", t)
    t = _FEAT_TAIL.sub("", t)
    t = _REMASTER_TAIL.sub("", t)
    return fold(t)


def titles_match(a: str, b: str) -> bool:
    """Strict on purpose: '(Live)', '(Acoustic)', '(Remix)' are different recordings."""
    ka, kb = title_key(a), title_key(b)
    return bool(ka) and ka == kb


def artists_overlap(a: list[str], b: list[str]) -> bool:
    fa, fb = {fold(x) for x in a if x}, {fold(x) for x in b if x}
    return bool(fa & fb)


def is_non_original(title: str, wanted_title: str = "") -> bool:
    """True for karaoke/cover/instrumental… versions, unless the wanted title itself says so."""
    return bool(NON_ORIGINAL.search(title or "")) and not NON_ORIGINAL.search(wanted_title or "")


def clean_channel(name: str | None) -> str | None:
    if not name:
        return None
    name = re.sub(r"\s*-\s*Topic$", "", name.strip())
    name = re.sub(r"VEVO$", "", name).strip()
    return name or None


def split_title(title: str) -> tuple[str | None, str]:
    """'Artist - Song (Official Video) | Label' → ('Artist', 'Song')."""
    t = (title or "").split(" | ")[0]
    t = _NOISE.sub("", t).strip()
    for dash in _DASHES:
        if dash in t:
            artist, song = t.split(dash, 1)
            if artist.strip() and song.strip():
                return artist.strip(), song.strip().strip('"\'“”')
    return None, t.strip('"\'“”') or title


def genre_case(name: str) -> str:
    small = {"and", "of", "the", "n", "a", "in"}
    words = name.strip().split()
    out = []
    for i, w in enumerate(words):
        if w.isupper() and len(w) > 1:  # R&B, EDM, UK
            out.append(w)
        elif i and w.lower() in small:
            out.append(w.lower())
        else:
            out.append("-".join(p[:1].upper() + p[1:] for p in w.split("-")))
    return " ".join(out)


def date_precision(date: str | None) -> int:
    """0 = none, 1 = year, 2 = year-month, 3 = full date."""
    if not date:
        return 0
    return {4: 1, 7: 2, 10: 3}.get(len(date), 0)


def normalize_date(value) -> str | None:
    """Accepts 2013 / '2013' / '2013-05' / '2013-05-17' / '20130517'; returns ISO-ish or None."""
    if value is None:
        return None
    s = str(value).strip()
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", s) and 1000 <= int(s[:4]) <= 9999:
        return s
    return None
