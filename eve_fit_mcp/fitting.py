"""A loaded, mutable fit backed by Pyfa's eos engine."""
import itertools

from .pyfa_bridge import P, lock

SLOT_NAMES = {"low": "LOW", "med": "MED", "high": "HIGH", "rig": "RIG", "subsystem": "SUBSYSTEM", "service": "SERVICE"}
SLOT_ORDER = ("high", "med", "low", "rig", "subsystem", "service")

# Uniform omni profile; callers can pass any [em, thermal, kinetic, explosive] weights
DEFAULT_DAMAGE_PROFILE = (25, 25, 25, 25)

_characters = {}
_fit_ids = itertools.count(1)


class FitError(ValueError):
    pass


def get_item(name_or_id):
    P.load()
    item = P.market.getItem(name_or_id)
    if item is None:
        raise FitError("Unknown item: %r" % (name_or_id,))
    return item


def character(skill_level=5):
    """Pyfa character with every skill at `skill_level` (0-5)."""
    P.load()
    skill_level = int(skill_level)
    if not 0 <= skill_level <= 5:
        raise FitError("skill_level must be 0-5")
    if skill_level == 5:
        return P.Character.getAll5()
    if skill_level == 0:
        return P.Character.getAll0()
    if skill_level not in _characters:
        char = P.Character("All %d" % skill_level, skill_level)
        P.db.save(char)
        _characters[skill_level] = char
    return _characters[skill_level]


def slot_name(fitting_slot):
    for name, enum_name in SLOT_NAMES.items():
        if fitting_slot == getattr(P.FittingSlot, enum_name):
            return name
    return None


class FitHandle:
    """Wraps an eos Fit. All mutation goes through Pyfa's own module/state rules."""

    def __init__(self, eft, skill_level=5, damage_profile=DEFAULT_DAMAGE_PROFILE, drones_active=True):
        P.load()
        with lock():
            fit = P.Port.importEft(eft.strip().splitlines())
            if fit is None:
                raise FitError("Could not parse EFT text (first line must look like [Ship, Name])")
            fit.implantLocation = P.ImplantLocation.FIT
            fit.character = character(skill_level)
            # Working fits never enter Pyfa's DB session: thousands of throwaway module swaps would
            # leave orphans for SQLAlchemy to flush. We wire the owner links SQLAlchemy would set.
            self.fit = fit
            self.id = next(_fit_ids)
            self.skill_level = int(skill_level)
            self.set_damage_profile(damage_profile, recalc=False)
            fit.fill()
            self._wire()
            self.recalc()
            if drones_active:
                self.activate_drones()

    # ---- lifecycle -------------------------------------------------------------------------

    def close(self):
        self.fit = None

    def _wire(self):
        for thing in itertools.chain(self.fit.modules, self.fit.drones, self.fit.fighters):
            thing.owner = self.fit

    def clone(self, name=None):
        # EFT doesn't carry drone state, and export re-sorts the bay, so match stacks by item
        active = {}
        for d in self.fit.drones:
            active.setdefault(d.item.ID, []).append(d.amountActive)
        h = FitHandle(self.eft(), self.skill_level, self.damage_profile, drones_active=False)
        for d in h.fit.drones:
            stack = active.get(d.item.ID)
            d.amountActive = stack.pop(0) if stack else 0
        h.recalc()
        if name:
            h.fit.name = name
        return h

    def recalc(self):
        with lock():
            self._wire()  # free()/replace() create modules with no owner
            self.fit.clear()
            self.fit.calculateModifiedAttributes()

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

    def set_skill_level(self, level):
        with lock():
            self.fit.character = character(level)
            self.skill_level = int(level)
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

    def activate_drones(self):
        """Launch drones, heaviest stacks first, up to bandwidth and max-active limits.

        EFT import leaves drones docked; heaviest-first is order independent (EFT export re-sorts the bay)
        and usually the highest-DPS choice. Use set_drones for exact control."""
        with lock():
            slots_left = int(self.fit.extraAttributes["maxActiveDrones"] or 0)
            bw_left = float(self.fit.ship.getModifiedItemAttr("droneBandwidth") or 0)
            stacks = sorted(self.fit.drones, key=lambda d: -float(d.getModifiedItemAttr("droneBandwidthUsed") or 0))
            for d in stacks:
                bw = float(d.getModifiedItemAttr("droneBandwidthUsed") or 0)
                n = d.amount if bw <= 0 else min(d.amount, int(bw_left // bw + 1e-9))
                n = max(0, min(n, slots_left))
                d.amountActive = n
                slots_left -= n
                bw_left -= n * bw
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
            self.recalc()
            self.activate_drones()

    # ---- output ----------------------------------------------------------------------------

    def eft(self):
        opts = {o.value: True for o in P.PortEftOptions}
        with lock():
            return P.Port.exportEft(self.fit, opts)
