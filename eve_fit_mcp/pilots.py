"""Named pilots (skill sets) used to calculate fits.

Kinds:
  "All 0" .. "All 5"  built in, every skill at that level
  custom              a base level plus per-skill overrides, saved in data/pilots/<name>.json
  esi                 a skill snapshot pulled from EVE via ESI (see esi.py), saved the same way

Pilots are persisted as JSON so they survive restarts; eos Character objects are built lazily
(on the engine thread) and cached until the pilot changes.
"""
import datetime
import json
import os
import re

from .errors import FitError
from .pyfa_bridge import DATA_DIR, P, lock

PILOT_DIR = os.path.join(DATA_DIR, "pilots")
DEFAULT_PILOT = "All 5"
_BUILTIN = re.compile(r"^\s*all\s*([0-5])\s*$|^\s*([0-5])\s*$", re.I)

_cache = {}  # canonical name -> eos Character
_inline = {}  # in-memory pilots from evaluate_fit(skills=...), keyed by content hash


def _builtin_level(name):
    m = _BUILTIN.match(str(name))
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _path(name):
    safe = re.sub(r"[^\w\- ]+", "_", name).strip()
    if not safe:
        raise FitError("bad pilot name %r" % name)
    return os.path.join(PILOT_DIR, safe + ".json")


def _load_record(name):
    if str(name) in _inline:
        return _inline[str(name)]
    p = _path(name)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _save_record(rec):
    os.makedirs(PILOT_DIR, exist_ok=True)
    with open(_path(rec["name"]), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    _cache.pop(rec["name"].lower(), None)


def canonical(name):
    """Normalise a pilot name; raises if unknown."""
    if name is None:
        return DEFAULT_PILOT
    if isinstance(name, int) or _builtin_level(name) is not None:
        return "All %d" % _builtin_level(name)
    rec = _load_record(name)
    if rec is None:
        raise FitError("Unknown pilot %r; known: %s" % (name, ", ".join(p["name"] for p in list_pilots())))
    return rec["name"]


def _skill_items():
    """{lowercase skill name: Item} for every published skill."""
    return {name.lower(): item for name, item in P.Character.getSkillNameMap().items()}


def resolve_skill(name, items=None):
    items = items or _skill_items()
    item = items.get(name.lower())
    if item is None:
        close = [i.typeName for k, i in items.items() if name.lower() in k][:8]
        raise FitError("Unknown skill %r%s" % (name, (" (did you mean: %s?)" % ", ".join(close)) if close else ""))
    return item


def character(name=None):
    """eos Character for a pilot name (cached)."""
    P.load()
    cname = canonical(name)
    key = cname.lower()
    with lock():
        char = _cache.get(key)
        if char is not None:
            return char
        level = _builtin_level(cname)
        if level == 5:
            char = P.Character.getAll5()
        elif level == 0:
            char = P.Character.getAll0()
        elif level is not None:
            char = P.Character(cname, level)
        else:
            rec = _load_record(cname)
            if rec["kind"] == "custom":
                char = P.Character(cname, int(rec["base_level"]))
                ids = P.Character.getSkillIDMap()
                for type_id, lvl in rec["skills"].items():
                    item = ids.get(int(type_id))
                    if item is not None:
                        char.getSkill(item).setLevel(int(lvl), ignoreRestrict=True)
            else:  # esi / imported snapshot: untrained skills stay at 0
                char = P.Character(cname, None, initSkills=False)
                char.apiUpdateCharSheet(
                    [{"typeID": int(t), "level": int(l)} for t, l in rec["skills"].items()],
                    rec.get("security_status", 0.0))
        _cache[key] = char
        return char


def list_pilots():
    out = [{"name": "All %d" % n, "kind": "builtin"} for n in (5, 4, 3, 2, 1, 0)]
    if os.path.isdir(PILOT_DIR):
        for fn in sorted(os.listdir(PILOT_DIR)):
            if fn.endswith(".json"):
                with open(os.path.join(PILOT_DIR, fn), encoding="utf-8") as f:
                    rec = json.load(f)
                info = {"name": rec["name"], "kind": rec["kind"], "skills_set": len(rec["skills"])}
                if rec["kind"] == "custom":
                    info["base_level"] = rec["base_level"]
                else:
                    info.update({k: rec.get(k) for k in ("character_id", "fetched_at", "total_sp")})
                out.append(info)
    return out


def save_custom(name, base_level=4, skills=None, replace=False):
    """Create/update a custom pilot. skills: {"Drones": 5, ...}; merged into existing unless replace."""
    P.load()
    if _builtin_level(name) is not None:
        raise FitError("%r is a built-in pilot name" % name)
    base_level = int(base_level)
    if not 0 <= base_level <= 5:
        raise FitError("base_level must be 0-5")
    rec = _load_record(name)
    if rec is not None and rec["kind"] != "custom":
        raise FitError("%r is an ESI pilot; refresh it with esi_refresh_skills instead" % name)
    current = {} if (rec is None or replace) else dict(rec["skills"])
    items = _skill_items()
    for skill, lvl in (skills or {}).items():
        lvl = int(lvl)
        if not 0 <= lvl <= 5:
            raise FitError("level for %s must be 0-5" % skill)
        current[str(resolve_skill(skill, items).ID)] = lvl
    rec = {"name": name, "kind": "custom", "base_level": base_level, "skills": current}
    _save_record(rec)
    return describe(name)


def save_esi(name, character_id, skills, total_sp=None, security_status=0.0):
    """Store an ESI skill snapshot. skills: {type_id: level}."""
    rec = {"name": name, "kind": "esi", "character_id": int(character_id),
           "skills": {str(k): int(v) for k, v in skills.items()}, "total_sp": total_sp,
           "security_status": security_status,
           "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    _save_record(rec)
    return rec


def normalize_skills(skills):
    """Accept skills in the shapes other tools produce and return {type_id: level}.

    - {"Drones": 5, "3436": 4, 3300: 3}          names or type IDs
    - [{"skill_id": 3436, "active_skill_level": 5}, ...]   ESI /characters/{id}/skills/ entries
    - {"skills": [...], "total_sp": ...}           the whole ESI response
    - [{"skill": "Drones", "level": 5}, ...] / [{"type_id": .., "level": ..}]
    ESI entries use active_skill_level (alpha clone limits apply), falling back to trained_skill_level.
    """
    P.load()
    if isinstance(skills, dict) and isinstance(skills.get("skills"), list):
        skills = skills["skills"]
    pairs = []
    if isinstance(skills, dict):
        pairs = list(skills.items())
    elif isinstance(skills, list):
        for row in skills:
            if not isinstance(row, dict):
                raise FitError("skill list entries must be objects, got %r" % (row,))
            key = row.get("skill_id", row.get("type_id", row.get("typeID", row.get("skill", row.get("name")))))
            lvl = row.get("active_skill_level", row.get("level", row.get("trained_skill_level")))
            pairs.append((key, lvl))
    else:
        raise FitError("skills must be an object or a list")
    ids = P.Character.getSkillIDMap()
    items = None
    out = {}
    for key, lvl in pairs:
        if key is None or lvl is None:
            raise FitError("skill entry missing id/name or level: %r" % ((key, lvl),))
        lvl = int(lvl)
        if not 0 <= lvl <= 5:
            raise FitError("skill level must be 0-5 (got %s for %s)" % (lvl, key))
        if isinstance(key, int) or str(key).isdigit():
            if int(key) not in ids:
                continue  # unpublished/removed skill: ignore rather than fail the whole import
            out[int(key)] = lvl
        else:
            items = items or _skill_items()
            out[resolve_skill(str(key), items).ID] = lvl
    return out


def save_imported(name, skills, character_id=None, source=None):
    """Store a full skill snapshot supplied by another tool (e.g. eve-esi-mcp's character_skills).
    Skills not listed count as untrained."""
    if _builtin_level(name) is not None:
        raise FitError("%r is a built-in pilot name" % name)
    levels = normalize_skills(skills)
    rec = {"name": name, "kind": "imported", "source": source, "character_id": character_id,
           "skills": {str(k): v for k, v in levels.items()},
           "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    _save_record(rec)
    return describe(name)


def inline(skills):
    """Anonymous, in-memory pilot for one-off evaluation; returns its name."""
    import hashlib
    levels = normalize_skills(skills)
    blob = json.dumps(sorted(levels.items())).encode()
    name = "_inline:" + hashlib.sha1(blob).hexdigest()[:12]
    if name not in _inline:
        _inline[name] = {"name": name, "kind": "imported", "skills": {str(k): v for k, v in levels.items()}}
    return name


def delete(name):
    cname = canonical(name)
    if _builtin_level(cname) is not None:
        raise FitError("cannot delete a built-in pilot")
    os.remove(_path(cname))
    _cache.pop(cname.lower(), None)


def describe(name, skills=None):
    """Pilot summary; `skills` (list of names or substrings) returns those skill levels."""
    P.load()
    cname = canonical(name)
    info = next(p for p in list_pilots() if p["name"] == cname)
    if skills:
        char = character(cname)
        items = _skill_items()
        levels = {}
        for q in skills:
            for k, item in items.items():
                if q.lower() in k:
                    levels[item.typeName] = char.getSkill(item).level or 0
        info["levels"] = dict(sorted(levels.items()))
    return info


def can_use(char, item):
    """True if `char` meets every required skill of `item`."""
    for skill_item, need in item.requiredSkills.items():
        if (char.getSkill(skill_item).level or 0) < need:
            return False
    return True


def missing_skills(fit):
    """Required skills the fit's pilot doesn't meet, per item: {item: {skill: (have, need)}}."""
    char = fit.character
    out = {}
    things = [m for m in fit.modules if not m.isEmpty] + list(fit.drones) + list(fit.fighters)
    seen = set()
    for thing in [fit.ship] + things:
        for item in filter(None, (thing.item, getattr(thing, "charge", None))):
            if item.ID in seen:
                continue
            seen.add(item.ID)
            for skill_item, need in item.requiredSkills.items():
                have = char.getSkill(skill_item).level or 0
                if have < need:
                    out.setdefault(item.typeName, {})[skill_item.typeName] = [have, need]
    return out
