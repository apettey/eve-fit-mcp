"""A loaded, mutable fit backed by Pyfa's eos engine."""
import itertools

from . import pilots
from .errors import FitError
from .pyfa_bridge import P, lock

SLOT_NAMES = {"low": "LOW", "med": "MED", "high": "HIGH", "rig": "RIG", "subsystem": "SUBSYSTEM", "service": "SERVICE"}
SLOT_ORDER = ("high", "med", "low", "rig", "subsystem", "service")

# Uniform omni profile; callers can pass any [em, thermal, kinetic, explosive] weights
DEFAULT_DAMAGE_PROFILE = (25, 25, 25, 25)

_fit_ids = itertools.count(1)


def get_item(name_or_id):
    P.load()
    item = P.market.getItem(name_or_id)
    if item is None:
        raise FitError("Unknown item: %r" % (name_or_id,))
    return item


def eft_module_counts(eft):
    """Counter of fitted-module/subsystem names listed in an EFT (no drones, cargo, implants)."""
    import re
    from collections import Counter
    out = Counter()
    for line in eft.strip().splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("[") or re.search(r"\sx\d+\s*$", line):
            continue
        if re.match(r"^\s*\w[\w ]*:\s", line):  # mutation detail lines
            continue
        name = re.sub(r"\s*\[\d+\]\s*$", "", line)
        name = re.sub(r"\s*/offline\s*$", "", name, flags=re.I).split(",")[0].strip()
        item = P.market.getItem(name) if name else None
        if item is not None and (item.isModule or item.isSubsystem):
            out[item.typeName] += 1
    return out


def slot_name(fitting_slot):
    for name, enum_name in SLOT_NAMES.items():
        if fitting_slot == getattr(P.FittingSlot, enum_name):
            return name
    return None


def normalize_links(links):
    """Validate generic command links: [{"type": "armor_all", "strength": 5, "mindlink": true}, ...]."""
    P.load()
    out = []
    for link in links or ():
        if isinstance(link, str):
            link = {"type": link}
        t = link.get("type")
        if t not in P.COMMAND_LINK_DEFS:
            raise FitError("Unknown command link %r; see list_command_links" % t)
        strength = int(link.get("strength", max(P.LINK_STRENGTHS)))
        if strength not in P.LINK_STRENGTHS:
            raise FitError("strength must be one of %s (booster ship bonus %%/level)" % list(P.LINK_STRENGTHS))
        out.append({"type": t, "strength": strength, "mindlink": bool(link.get("mindlink", False))})
    return out


class FitHandle:
    """Wraps an eos Fit. All mutation goes through Pyfa's own module/state rules."""

    def __init__(self, eft, pilot=None, damage_profile=DEFAULT_DAMAGE_PROFILE, drones_active=True,
                 command_links=None):
        P.load()
        with lock():
            fit = P.Port.importEft(eft.strip().splitlines())
            if fit is None:
                raise FitError("Could not parse EFT text (first line must look like [Ship, Name])")
            fit.implantLocation = P.ImplantLocation.FIT
            # Working fits never enter Pyfa's DB session: thousands of throwaway module swaps would
            # leave orphans for SQLAlchemy to flush. We wire the owner links SQLAlchemy would set.
            self.fit = fit
            self.id = next(_fit_ids)
            self.auto_drones = False  # True: relaunch drones automatically when bandwidth/limits change
            self._fixing_drones = False
            self.pilot = pilots.canonical(pilot)
            self.set_damage_profile(damage_profile, recalc=False)
            self.set_command_links(command_links, recalc=False)
            fit.fill()
            self._wire()
            # Pyfa's EFT import silently skips modules that don't fit (too many for the slots/hardpoints).
            # Remember them so stats can flag the fit instead of quietly under-arming it.
            from collections import Counter
            fitted = Counter((m.baseItem if m.isMutated else m.item).typeName
                             for m in fit.modules if not m.isEmpty)
            self.import_dropped = dict(eft_module_counts(eft) - fitted)
            self.recalc()
            if drones_active:
                self.auto_drones = True
                self.activate_drones()

    # ---- lifecycle -------------------------------------------------------------------------

    def close(self):
        self.fit = None

    def _wire(self):
        for thing in itertools.chain(self.fit.modules, self.fit.drones, self.fit.fighters):
            thing.owner = self.fit

    def derive(self, eft, name=None):
        """New handle from `eft` with this fit's pilot, damage profile and command links
        (none of which EFT carries)."""
        h = FitHandle(eft, self.pilot, self.damage_profile, drones_active=True, command_links=self.command_links)
        h.fit.notes = self.fit.notes  # intent notes (e.g. ammo choice) aren't in EFT either
        if name:
            h.fit.name = name
        return h

    def clone(self, name=None):
        # EFT doesn't carry drone state, and export re-sorts the bay, so match stacks by item
        active = {}
        for d in self.fit.drones:
            active.setdefault(d.item.ID, []).append(d.amountActive)
        h = self.derive(self.eft(), name)
        h.auto_drones = self.auto_drones
        for d in h.fit.drones:
            stack = active.get(d.item.ID)
            d.amountActive = stack.pop(0) if stack else 0
        h.recalc()
        return h

    def recalc(self):
        with lock():
            # pilots rebuilds a Character when its skills change; pick that up here
            char = pilots.character(self.pilot)
            if self.fit.character is not char:
                self.fit.character = char
            self._wire()  # free()/replace() create modules with no owner
            self.fit.clear()
            self.fit.calculateModifiedAttributes()
            # e.g. an Interdiction Nullifier halves bandwidth: relaunch within the new limits
            if self.auto_drones and not self._fixing_drones and self.fit.drones:
                if self._drone_plan() != [d.amountActive for d in self.fit.drones]:
                    self._fixing_drones = True
                    try:
                        self.activate_drones()
                    finally:
                        self._fixing_drones = False

    @property
    def name(self):
        return self.fit.name

    @property
    def ship(self):
        return self.fit.ship.item.typeName

    # ---- settings --------------------------------------------------------------------------

    def set_damage_profile(self, profile, recalc=True):
        profile = tuple(float(x) for x in (profile or DEFAULT_DAMAGE_PROFILE))
        if len(profile) != 4 or sum(profile) <= 0:
            raise FitError("damage_profile must be 4 non-negative numbers [em, thermal, kinetic, explosive]")
        self.damage_profile = profile
        with lock():
            self.fit.damagePattern = P.DamagePattern(*profile)
            if recalc:
                self.recalc()

    def set_pilot(self, pilot):
        self.pilot = pilots.canonical(pilot)
        self.recalc()

    def set_command_links(self, links, recalc=True):
        links = normalize_links(links)
        with lock():
            del self.fit.commandLinks[:]
            for l in links:
                self.fit.commandLinks.append(P.CommandLink(l["type"], l["strength"], l["mindlink"]))
            self.command_links = links
            if recalc:
                self.recalc()

    # ---- slots -----------------------------------------------------------------------------

    def _positions(self):
        """Ordered [(key, position, module)] e.g. ('low:0', 7, <Module>)."""
        racks = {}
        for pos, mod in enumerate(self.fit.modules):
            name = slot_name(mod.slot)
            if name is None:
                continue
            racks.setdefault(name, []).append((pos, mod))
        out = []
        for name in SLOT_ORDER:
            for idx, (pos, mod) in enumerate(racks.get(name, ())):
                out.append(("%s:%d" % (name, idx), pos, mod))
        return out

    def _resolve(self, key):
        for k, pos, mod in self._positions():
            if k == key:
                return pos, mod
        raise FitError("No slot %r on this fit; valid keys: %s" % (key, ", ".join(k for k, _, _ in self._positions())))

    def slots(self):
        res = []
        for key, pos, mod in self._positions():
            res.append({
                "key": key,
                "item": None if mod.isEmpty else mod.item.typeName,
                "charge": mod.charge.typeName if (not mod.isEmpty and mod.charge) else None,
                "state": None if mod.isEmpty else P.FittingModuleState(mod.state).name.lower(),
                "mutated": bool(not mod.isEmpty and mod.isMutated),
            })
        return res

    def slots_by_key(self):
        return {s["key"]: s for s in self.slots()}

    def signature(self):
        """Hashable description of the module layout (for caching evaluations)."""
        return tuple(
            (pos, None if m.isEmpty else m.item.ID, m.charge.ID if (not m.isEmpty and m.charge) else None,
             None if m.isEmpty else int(m.state))
            for pos, m in enumerate(self.fit.modules))

    def module_info(self, key):
        pos, mod = self._resolve(key)
        return None if mod.isEmpty else P.ModuleInfo.fromModule(mod)

    def restore_module(self, key, info, recalc=True):
        """Put back a module previously captured with module_info() (None = empty)."""
        with lock():
            pos, _ = self._resolve(key)
            if info is None:
                self.fit.modules.free(pos)
            else:
                self.fit.modules.free(pos)
                mod = info.toModule()
                self.fit.modules.replace(pos, mod)
                mod.owner = self.fit
            if recalc:
                self.recalc()

    def set_module(self, key, item=None, charge="keep", state=None, recalc=True, check=True):
        """Replace the module at `key`. item=None empties the slot.

        charge: "keep" keeps the current charge if still valid, None unloads, or a charge name.
        Returns a list of warnings. Raises FitError if the module cannot go in that slot.
        """
        warnings = []
        with lock():
            pos, old = self._resolve(key)
            old_charge = old.charge if (not old.isEmpty) else None
            if item is None:
                self.fit.modules.free(pos)
                if recalc:
                    self.recalc()
                return warnings
            it = get_item(item)
            try:
                new = P.Module(it)
            except ValueError as e:
                raise FitError("%s is not a fittable module (%s)" % (it.typeName, e))
            if new.slot != old.slot:
                raise FitError("%s goes in a %s slot, not %s" % (it.typeName, slot_name(new.slot), key.split(":")[0]))
            if state is None:
                new.state = P.activeStateLimit(it.ID)
                if not new.isValidState(new.state):  # passive module / rig: online is its top state
                    new.state = P.FittingModuleState.ONLINE
            else:
                new.state = getattr(P.FittingModuleState, state.upper())
            if charge == "keep":
                if old_charge is not None and new.isValidCharge(old_charge):
                    new.charge = old_charge
            elif charge is not None:
                ch = get_item(charge)
                if not new.isValidCharge(ch):
                    raise FitError("%s cannot load %s" % (it.typeName, ch.typeName))
                new.charge = ch
            info_before = None if old.isEmpty else P.ModuleInfo.fromModule(old)
            self.fit.modules.free(pos)
            self.fit.modules.replace(pos, new)
            new.owner = self.fit  # fits() relies on this; Pyfa gets it from a DB flush
            if slot_name(new.slot) == "subsystem":
                # T3C subsystems change the slot layout: recalc so the new slot counts exist, then
                # add/remove empty slots (fitted modules beyond the new layout show up as issues)
                self.recalc()
                self.fit.fill()
                self._wire()
            if check and not new.fits(self.fit):
                # slot/hardpoint/max-group limits - revert like Pyfa's replace command does
                self.restore_module(key, info_before, recalc=recalc)
                raise FitError("%s does not fit (hardpoints, max group fitted or ship restriction)" % it.typeName)
            if recalc:
                self.recalc()
                self._check_states(new)
            if not new.isValidState(new.state):
                warnings.append("%s cannot be %s; set online" % (it.typeName, P.FittingModuleState(new.state).name))
        return warnings

    def _check_states(self, base):
        # Mirrors service.fit.Fit.checkStates for local modules
        for mod in self.fit.modules:
            if mod is base or mod.isEmpty:
                continue
            can = mod.canHaveState(mod.state)
            if can is not True:
                mod.state = can
            elif not mod.isValidState(mod.state):
                mod.state = P.FittingModuleState.ONLINE

    def _drone_plan(self):
        """Active count per drone stack: heaviest first within bandwidth and max-active limits."""
        slots_left = int(self.fit.extraAttributes["maxActiveDrones"] or 0)
        bw_left = float(self.fit.ship.getModifiedItemAttr("droneBandwidth") or 0)
        plan = {}
        for d in sorted(self.fit.drones, key=lambda d: -float(d.getModifiedItemAttr("droneBandwidthUsed") or 0)):
            bw = float(d.getModifiedItemAttr("droneBandwidthUsed") or 0)
            n = d.amount if bw <= 0 else min(d.amount, int(bw_left // bw + 1e-9))
            n = max(0, min(n, slots_left))
            plan[id(d)] = n
            slots_left -= n
            bw_left -= n * bw
        return [plan[id(d)] for d in self.fit.drones]

    def activate_drones(self):
        """Launch drones, heaviest stacks first, up to bandwidth and max-active limits.

        EFT import leaves drones docked; heaviest-first is order independent (EFT export re-sorts the bay)
        and usually the highest-DPS choice. Use set_drones for exact control."""
        with lock():
            for d, n in zip(self.fit.drones, self._drone_plan()):
                d.amountActive = n
            self.recalc()

    def set_drones(self, drones, recalc=True):
        """drones: {name: count}. Replaces the whole drone bay, then launches what bandwidth allows."""
        with lock():
            for d in list(self.fit.drones):
                self.fit.drones.remove(d)
            for name, count in (drones or {}).items():
                it = get_item(name)
                if not it.isDrone:
                    raise FitError("%s is not a drone" % it.typeName)
                d = P.Drone(it)
                d.amount = int(count)
                self.fit.drones.append(d)
                d.owner = self.fit
            self.auto_drones = True
            self.recalc()
            self.activate_drones()

    # ---- output ----------------------------------------------------------------------------

    def eft(self):
        opts = {o.value: True for o in P.PortEftOptions}
        with lock():
            return P.Port.exportEft(self.fit, opts)
