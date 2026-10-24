"""Single point of contact with the vendored Pyfa checkout.

Everything that imports from Pyfa (eos / service / gui helpers) goes through here, so when
upstream moves things around this is the only file that should need fixing.
"""
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYFA_DIR = os.path.join(ROOT, "vendor", "pyfa")
DATA_DIR = os.path.join(ROOT, "data")

_lock = threading.RLock()
_booted = False


def boot():
    """Initialise Pyfa headlessly. Idempotent."""
    global _booted
    with _lock:
        if _booted:
            return
        if not os.path.isdir(os.path.join(PYFA_DIR, "eos")):
            raise RuntimeError(
                "Pyfa submodule missing at %s - run `git submodule update --init`" % PYFA_DIR)
        if PYFA_DIR not in sys.path:
            sys.path.insert(0, PYFA_DIR)

        import config
        from db_update import db_needs_update, update_db

        # Pyfa's own bootstrap (settings files, gamedata path) pointed at our data dir
        config.defPaths(DATA_DIR)
        if db_needs_update() is True:
            update_db()

        # Fits are ephemeral here: our library lives as EFT files, not in Pyfa's saveddata
        import eos.config
        eos.config.saveddata_connectionstring = "sqlite:///:memory:"

        import eos.db
        import eos.events  # noqa: F401  (registers SQLAlchemy event hooks)
        eos.db.saveddata_meta.create_all(eos.db.saveddata_engine)

        # service.* has a circular import that only resolves if gui.mainFrame loads first
        # (Pyfa's own tests do the same). wx gets imported but no App/window is created.
        import gui.mainFrame  # noqa: F401
        _booted = True


def lock():
    """Pyfa's object model is not thread safe; serialise every engine call through this."""
    return _lock


# eos keeps one gamedata DB session per thread, and items loaded in one session misbehave in
# another, so all engine work is pinned to a single thread (the one that boots Pyfa).
_engine = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyfa-engine")
_engine_thread = []


def _mark_engine_thread():
    _engine_thread.append(threading.get_ident())


_engine.submit(_mark_engine_thread).result()


def on_engine(fn, *args, **kwargs):
    """Run fn on the engine thread and wait for it (direct call if already there)."""
    if threading.get_ident() in _engine_thread:
        return fn(*args, **kwargs)
    return _engine.submit(fn, *args, **kwargs).result()


class P:
    """Lazy namespace of Pyfa symbols, populated after boot()."""

    @classmethod
    def load(cls):
        boot()
        if getattr(cls, "_loaded", False):
            return cls
        import eos.db as db
        from eos.const import FittingModuleState, FittingSlot, FittingHardpoint, ImplantLocation
        from eos.saveddata.character import Character
        from eos.saveddata.damagePattern import DamagePattern
        from eos.saveddata.module import Module
        from eos.saveddata.drone import Drone
        from eos.saveddata.implant import Implant
        from eos.saveddata.booster import Booster
        from eos.gamedata import Item
        from service.port import Port
        from service.const import PortEftOptions
        from service.market import Market
        from gui.fitCommands.helpers import activeStateLimit, ModuleInfo
        from eos.saveddata.commandLink import CommandLink, COMMAND_LINK_DEFS, STRENGTHS

        cls.db = db
        cls.FittingModuleState = FittingModuleState
        cls.FittingSlot = FittingSlot
        cls.FittingHardpoint = FittingHardpoint
        cls.ImplantLocation = ImplantLocation
        cls.Character = Character
        cls.DamagePattern = DamagePattern
        cls.Module = Module
        cls.Drone = Drone
        cls.Implant = Implant
        cls.Booster = Booster
        cls.Item = Item
        cls.Port = Port
        cls.PortEftOptions = PortEftOptions
        cls.market = Market.getInstance()
        cls.activeStateLimit = activeStateLimit
        cls.ModuleInfo = ModuleInfo
        cls.CommandLink = CommandLink
        cls.COMMAND_LINK_DEFS = COMMAND_LINK_DEFS
        cls.LINK_STRENGTHS = STRENGTHS
        cls._loaded = True
        return cls
