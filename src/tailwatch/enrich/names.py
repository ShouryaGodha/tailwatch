"""Whether two spellings name the same person, and whether a text names an organization.

Pure. A report and a profile rarely spell a name the same way: a middle name is dropped, a
nickname is used, the order is swapped (family name first), a first name or a surname is cut
to an initial ("A. Lovelace", "Ada L."). A different surname is a different person. The
levels, weakest to strongest: ``different``, ``initials``, ``variant``, ``exact``; the floor
``name_level_min`` says which is enough.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

LEVELS = ("different", "initials", "variant", "exact")

# Groups of first names that are one name. A token may sit in more than one group ("alex").
_GROUPS = (
    "william bill billy will willy liam", "robert bob bobby rob robbie bert", "richard rick ricky rich dick",
    "michael mike mikey mick micky", "james jim jimmy jamie", "thomas tom tommy", "david dave davy",
    "christopher chris kit", "christine chris chrissy christina tina", "katherine catherine kathryn kate katie kathy cathy kat",
    "elizabeth liz lizzie beth betsy eliza libby", "jennifer jen jenny", "daniel dan danny", "joseph joe joey",
    "nicholas nick nicky", "alexander alex sasha xander", "alexandra alex sasha lexi", "andrew andy drew",
    "matthew matt", "samuel sam sammy", "samantha sam", "benjamin ben benny", "edward ed eddie ted ned",
    "anthony tony", "steven stephen steve", "jonathan jon jonny", "john jack johnny", "jacob jake",
    "maximilian max", "maxwell max", "nathan nate nathaniel", "zachary zach zack", "kenneth ken kenny",
    "gregory greg", "peter pete", "patrick pat paddy", "patricia pat patty trish", "francis frank frankie",
    "raymond ray", "lawrence larry laurence", "margaret maggie meg peggy", "susan sue susie suzanne",
    "deborah debbie deb", "rebecca becky becca", "victoria vicky tori", "abigail abby", "theodore ted teddy theo",
    "timothy tim timmy", "frederick fred freddie", "charles charlie chuck", "henry hank harry", "harold harry hal",
    "leonard leo len lenny", "eugene gene", "vincent vince vinny", "jeffrey jeff geoffrey", "gerald gerry jerry",
    "ronald ron ronnie", "donald don donny", "douglas doug", "phillip philip phil", "dmitry dmitri dima",
    "yevgeny evgeny eugene zhenya", "aleksandr alexander sasha", "mikhail michael misha", "ekaterina katya kate",
)
_NICK: dict[str, set[int]] = {}
for _index, _group in enumerate(_GROUPS):
    for _token in _group.split():
        _NICK.setdefault(_token, set()).add(_index)

_HONORIFICS = re.compile(r"\b(?:dr|prof|mr|mrs|ms|mx|sir|phd|md|mba|msc|bsc|jr|sr|ii|iii|iv)\b")
# Words that name no organization on their own: "Research" is in every lab's name.
GENERIC = frozenset({"ai", "labs", "lab", "research", "inc", "llc", "ltd", "the", "group", "technologies", "technology",
                     "systems", "company", "corp", "corporation", "co", "university", "institute", "team", "studio",
                     "studios", "capital", "ventures", "intelligence", "foundation", "network", "networks", "data",
                     "software", "solutions", "cloud", "global", "international", "school", "college"})


def plain(text: object) -> str:
    """Accents, case and repeated spaces removed."""
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.casefold().split())


def tokens(name: object) -> list[str]:
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", plain(name))
    text = re.sub(r"[.,'’\-_/]", " ", text)
    text = _HONORIFICS.sub(" ", text)
    return [t for t in re.split(r"[^a-z0-9]+", text) if t]


def same_first(a: str, b: str) -> bool:
    return a == b or bool(_NICK.get(a, set()) & _NICK.get(b, set()))


def _initial(a: str, b: str) -> bool:
    """One is a single letter and the other starts with it."""
    return (len(a) == 1 or len(b) == 1) and a[:1] == b[:1]


def level(given: object, found: object) -> str:
    """How well ``found`` spells the name ``given``."""
    a, b = tokens(given), tokens(found)
    if not a or not b:
        return "different"
    if a == b:
        return "exact"
    if len(a) == 1 or len(b) == 1:
        return "variant" if len(a) == len(b) == 1 and same_first(a[0], b[0]) else "different"
    fa, la, fb, lb = a[0], a[-1], b[0], b[-1]
    # a middle name dropped, a nickname, a compound surname cut to one of its parts
    if same_first(fa, fb) and (la == lb or (len(lb) > 1 and lb in a[1:]) or (len(la) > 1 and la in b[1:])):
        if len(la) > 1 and len(lb) > 1:
            return "variant"
    # the family name first on one side ("Zhang Wei" / "Wei Zhang")
    if same_first(fa, lb) and la == fb and len(la) > 1 and len(fb) > 1:
        return "variant"
    # an initial for the first name ("A. Lovelace") or the surname ("Ada L.")
    if la == lb and len(la) > 1 and _initial(fa, fb):
        return "initials"
    if same_first(fa, fb) and _initial(la, lb):
        return "initials"
    if (la == fb and _initial(fa, lb)) or (fa == lb and _initial(la, fb)):
        return "initials"
    return "different"


def at_least(found: str, floor: str) -> bool:
    if found not in LEVELS or floor not in LEVELS:
        return False
    return LEVELS.index(found) >= LEVELS.index(floor)


def compatible(given: object, found: object, floor: str = "initials") -> bool:
    return at_least(level(given, found), floor)


# -- organizations ----------------------------------------------------------------------------

def alias_table(*sources: Mapping[str, object]) -> dict[str, set[str]]:
    """Every spelling (lowercased) to the set of spellings of the same organization. A source
    maps an organization to a list of its other spellings (the config's ``priority.aliases``)
    or one spelling to the organization (the ``ENRICHMENT:ALIASES`` setting)."""
    groups: list[set[str]] = []
    for source in sources:
        for key, value in (source or {}).items():
            names = {plain(key)} | {plain(v) for v in (value if isinstance(value, (list, tuple, set)) else [value])}
            names.discard("")
            merged = [g for g in groups if g & names]
            group = set(names).union(*merged) if merged else set(names)
            groups = [g for g in groups if not g & names] + [group]
    table: dict[str, set[str]] = {}
    for group in groups:
        for name in group:
            table[name] = group
    return table


def spellings(org: object, aliases: Mapping[str, set[str]] | None = None) -> set[str]:
    key = plain(org)
    if not key:
        return set()
    return {key} | set((aliases or {}).get(key, set()))


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text)


def _words(text: str) -> str:
    return " " + " ".join(re.split(r"[^a-z0-9]+", plain(text))) + " "


def _short_forms(spelling: str) -> list[str]:
    """``google deepmind`` → ``deepmind``: a multi-word name's last word when it is not generic."""
    parts = spelling.split()
    if len(parts) > 1 and len(parts[-1]) > 3 and parts[-1] not in GENERIC:
        return [parts[-1]]
    return []


def org_in_text(text: object, org: object, aliases: Mapping[str, set[str]] | None = None) -> bool:
    """Whether a bio, a headline, a work history or a page names the organization: as whole
    words ("Meta" is not in "metadata"), by a non-generic last word, or as a handle
    ("@analyticalengines")."""
    body = _words(str(text or ""))
    if body.strip() == "":
        return False
    handles = [_squash(h.casefold()) for h in re.findall(r"@([A-Za-z0-9_]{3,40})", str(text or ""))]
    for spelling in spellings(org, aliases):
        if spelling in GENERIC:
            continue
        for form in [spelling, *_short_forms(spelling)]:
            words = _words(form)
            if words.strip() and words in body:
                return True
        squashed = _squash(spelling)
        if len(squashed) >= 5 and any(h == squashed or h.startswith(squashed) for h in handles):
            return True
    return False


def org_match(a: object, b: object, aliases: Mapping[str, set[str]] | None = None) -> bool:
    """Whether two spellings name the same organization."""
    first, second = spellings(a, aliases), spellings(b, aliases)
    if not first or not second:
        return False
    if first & second:
        return True
    return any(org_in_text(x, y, aliases) or org_in_text(y, x, aliases) for x in first for y in second
               if x not in GENERIC and y not in GENERIC)


def any_org_in(text: object, orgs: Iterable[object], aliases: Mapping[str, set[str]] | None = None) -> str:
    """The first of ``orgs`` the text names, or ''."""
    for org in orgs:
        if org and org_in_text(text, org, aliases):
            return str(org)
    return ""
