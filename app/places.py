"""Place names to tag slugs. The curator writes names the way an article does ("Britain",
"the US", "Aussie"); these collapse them so one place is one tag. Unknown places fall back to
a slug of whatever was written."""
from __future__ import annotations

from .normalize import slugify

PLACES = {
    "uk": ("UK", ["united kingdom", "britain", "great britain", "england", "scotland", "wales", "northern ireland", "british"]),
    "us": ("US", ["united states", "usa", "u.s.", "america", "united states of america", "american"]),
    "eu": ("EU", ["european union"]),
    "europe": ("Europe", ["european"]),
    "germany": ("Germany", ["deutschland", "german"]),
    "france": ("France", ["french"]),
    "ireland": ("Ireland", ["irish", "republic of ireland"]),
    "netherlands": ("Netherlands", ["holland", "dutch"]),
    "spain": ("Spain", ["spanish"]),
    "italy": ("Italy", ["italian"]),
    "nordics": ("Nordics", ["scandinavia"]),
    "australia": ("Australia", ["australian", "aussie"]),
    "new-zealand": ("New Zealand", ["nz", "kiwi"]),
    "canada": ("Canada", ["canadian"]),
    "china": ("China", ["chinese", "prc", "people's republic of china"]),
    "hong-kong": ("Hong Kong", []),
    "taiwan": ("Taiwan", ["taiwanese"]),
    "japan": ("Japan", ["japanese"]),
    "south-korea": ("South Korea", ["korea", "korean", "republic of korea"]),
    "india": ("India", ["indian"]),
    "singapore": ("Singapore", []),
    "israel": ("Israel", ["israeli"]),
    "uae": ("UAE", ["united arab emirates", "emirates", "dubai", "abu dhabi"]),
    "saudi-arabia": ("Saudi Arabia", ["saudi"]),
    "russia": ("Russia", ["russian"]),
    "ukraine": ("Ukraine", ["ukrainian"]),
    "brazil": ("Brazil", ["brazilian"]),
    "mexico": ("Mexico", ["mexican"]),
    "africa": ("Africa", ["african"]),
    "london": ("London", []),
    "california": ("California", ["californian"]),
    "new-york": ("New York", ["nyc", "new york city"]),
}
_LOOKUP = {}
for slug, (label, aliases) in PLACES.items():
    _LOOKUP[slug] = slug
    _LOOKUP[label.lower()] = slug
    for a in aliases:
        _LOOKUP[a] = slug


def place_tag(name: str) -> tuple[str, str] | None:
    """(slug, label) for a place name, or None if it's empty."""
    key = (name or "").strip().lower().removeprefix("the ").strip(" .")
    if not key:
        return None
    slug = _LOOKUP.get(key)
    if slug:
        return slug, PLACES[slug][0]
    s = slugify(key, 40)
    return (s, name.strip()) if s else None


def all_place_names() -> list[tuple[str, str]]:
    """(text to look for, slug) for crude matching in the dry-run curator."""
    out = []
    for slug, (label, aliases) in PLACES.items():
        for n in [label, *aliases]:
            if len(n) > 3:
                out.append((n, slug))
    return out
