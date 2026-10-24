"""Special search goals shared by sweep_fit and optimize_fit (beyond the plain stats metrics).

  applied:<target preset>@<km>          applied DPS vs a target preset at range
  matchup:<enemy fit_id>@<km>[x<n>v<m>]  1v1 (or n v m) win ratio vs a loaded enemy fit
  fleet:<doctrine>@<km>[x<count>]        fleet-battle score of <count> of this fit vs a saved doctrine
  fleet:ctx<id>                          a registered fleet objective (counter_fit: support ships, several
                                         ranges, calibrated kill orders)

Fleet score: 1 for a win (+ up to 0.2 for winning fast) + fraction of the enemy killed - fraction of ours
lost; range about -1 .. 2.2. Expensive goals are only computed for valid fits that meet other constraints.
"""
import re

from .errors import FitError

ENEMY_RESOLVER = None  # set by the server: fit_id -> FitHandle (for matchup:<id>)
_CONTEXTS = {}


def register_fleet_objective(obj):
    key = "fleet:ctx%d" % (len(_CONTEXTS) + 1)
    _CONTEXTS[key] = obj
    return key


def is_special(key):
    return key.startswith(("applied:", "matchup:", "fleet:"))


class Specials:
    def __init__(self, keys):
        self.keys = [k for k in keys if is_special(k)]
        self.calls = {}
        for k in self.keys:
            self.calls[k] = self._parse(k)

    def __bool__(self):
        return bool(self.keys)

    def _parse(self, k):
        if k.startswith("applied:"):
            try:
                tgt, km = k[len("applied:"):].split("@")
                km = float(km)
            except ValueError:
                raise FitError("applied goals look like applied:<target preset>@<km>, e.g. applied:structure@60")
            from . import application
            return lambda h: application.applied_dps(h, [tgt], [km])["results"][0]["applied_dps"]
        if k.startswith("matchup:"):
            m = re.match(r"^matchup:(\d+)@([\d.]+)(?:x(\d+)v(\d+))?$", k)
            if not m or ENEMY_RESOLVER is None:
                raise FitError("matchup goals look like matchup:<enemy fit_id>@<km> or "
                               "matchup:<enemy fit_id>@<km>x<our count>v<their count>")
            enemy = ENEMY_RESOLVER(int(m.group(1)))
            spec = {"eft": enemy.eft(), "pilot": enemy.pilot, "name": "%s, %s" % (enemy.ship, enemy.name)}
            km, ours, theirs = float(m.group(2)), int(m.group(3) or 1), int(m.group(4) or 1)
            from . import matchup
            return lambda h: matchup.ratio(h, spec, km, ours, theirs)
        if k in _CONTEXTS:
            obj = _CONTEXTS[k]
            return lambda h: obj(h)["score"]
        m = re.match(r"^fleet:(.+)@([\d.]+)(?:x(\d+))?$", k)
        if not m:
            raise FitError("fleet goals look like fleet:<doctrine name>@<km> or fleet:<doctrine>@<km>x<count>")
        from . import fleet
        obj = fleet.FleetObjective(m.group(1), int(m.group(3) or 1), [float(m.group(2))])
        return lambda h: obj(h)["score"]

    def compute(self, handle, stats):
        for k, f in self.calls.items():
            stats[k] = float(f(handle))
        return stats
