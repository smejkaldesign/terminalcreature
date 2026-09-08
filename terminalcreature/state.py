"""State: roster, per-creature XP banking, settings, and the XP cache.

The banking rule is the whole design. XP is measured globally off the memory
system, but it's credited to ONE creature: the focused one. Without that, a
second egg hatched at level 100 would spawn at level 100, because the memory
that earned the first hundred levels is still sitting there.
"""

import json
import os
import random
import time

from . import creature as creature_mod
from . import metric

STATE_DIR = os.path.expanduser("~/.claude/terminalcreature")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
CACHE_PATH = os.path.join(STATE_DIR, "xp.cache")
CACHE_TTL = 60
LATEST_PATH = os.path.join(STATE_DIR, "latest-version")
UPDATE_TTL = 24 * 3600

# bumped when the file's shape changes. `migrate` is what actually reads it, and
# an upgrade that de-levels someone's buddy is the one failure with no undo
STATE_VERSION = 2

DEFAULT_SETTINGS = {
    "provider": "auto",         # "auto" | "agents" | "claude" | "vault" | "folder"
    "vault_root": "",
    "weights": {},
    "xp_max": metric.XP_MAX_DEFAULT,
    "density": "compact",       # "compact" | "minimal" | "full" | "sprite"
    "columns": 0,               # right-align sprite mode to this width, 0 = flush left
    "sprite_height": 5,         # 5 rows, or 3 to keep the footer closer
    "unicode": True,
    "hidden": False,          # keep the creature out of the statusline without uninstalling
    "border": True,           # box the compose column. costs two rows of height
    "update_check": False,    # opt-in: refresh may ask pypi for the latest version once a day
    "update_check_asked": False,  # the one-time offer was shown; never show it again
    "hookcard": "changes",    # turn-end card on hook hosts: "always" | "changes" | "off"
}

HOOKCARD_MODES = ("always", "changes", "off")
# in "changes" mode a quiet session still gets a card this often, so it's not forgotten
HOOKCARD_EVERY = 10


# enough for the handful of sessions anyone has open at once. the oldest baseline
# drops out rather than the file growing for the life of the install
SESSION_KEEP = 8


def default_state():
    return {"version": STATE_VERSION, "high_water_xp": 0, "focused": None, "creatures": [],
            "sessions": {}, "settings": dict(DEFAULT_SETTINGS)}


def session_gain(state, session_id, banked):
    """XP the focused creature has put on since this session first drew itself.

    Returns (gain, is_new). Baselining on first sight is what makes it a session
    counter: without it every session would open claiming credit for the whole
    vault. Concurrent sessions each get their own mark, because there are
    usually several open and one shared mark would have them overwriting
    each other's starting point.
    """
    if not session_id:
        return 0, False
    sessions = state.setdefault("sessions", {})
    row = sessions.get(session_id)
    # a total below the mark means focus moved to a different creature, so
    # re-baseline instead of rendering a negative
    if row is None or row.get("at", 0) > banked:
        sessions[session_id] = {"at": banked, "ts": int(time.time()),
                                "level": metric.level_for(banked, state["settings"]["xp_max"])}
        if len(sessions) > SESSION_KEEP:
            stale = sorted(sessions, key=lambda k: sessions[k].get("ts", 0))[:len(sessions) - SESSION_KEEP]
            for key in stale:
                del sessions[key]
        return 0, True
    gain = banked - row["at"]
    # a counter higher than the last one drawn is the moment it ate. stamp it,
    # and ask to be saved, so the next renders can play out the meal. a level
    # that moved with it gets its own stamp, for the wide eyes
    if gain > row.get("seen", 0):
        level = metric.level_for(banked, state["settings"]["xp_max"])
        if level > row.get("level", level):
            row["leveled_at"] = time.time()
        row["level"] = level
        row["seen"] = gain
        row["fed_at"] = time.time()
        return gain, True
    return gain, False


def hookcard_turn(state, session_id, gain, stage_index, mode):
    """Whether this turn's card is shown, and whether the stage moved since the
    last one shown. Counts the turn either way. (show, stage_changed).

    "changes" shows the first turn, any turn where the session's xp or stage
    moved since the card was last shown, and every HOOKCARD_EVERY-th turn
    otherwise. Without a session id there is nothing to count against, so the
    card shows: a creature that turns up beats one that never does.
    """
    if mode == "off":
        return False, False
    if not session_id:
        return True, False
    sessions = state.setdefault("sessions", {})
    row = sessions.get(session_id)
    if row is None:
        row = sessions[session_id] = {"at": 0, "ts": int(time.time())}
    row["turns"] = row.get("turns", 0) + 1
    seen = "shown_gain" in row
    stage_changed = seen and stage_index != row.get("shown_stage")
    show = (mode == "always" or not seen or stage_changed or gain != row["shown_gain"]
            or row["turns"] % HOOKCARD_EVERY == 0)
    if show:
        row["shown_gain"] = gain
        row["shown_stage"] = stage_index
    return show, stage_changed


# a feed is a meal: chewing frames that alternate every CHEW_BEAT for
# CHEW_HOLD, then a happy resting face (blinks still land on it) for
# HAPPY_HOLD. a face that swaps back to neutral the moment the mouthful is
# down looked like it hadn't enjoyed it
CHEW_HOLD = 1.5
CHEW_BEAT = 0.35
HAPPY_HOLD = 10 * 60
# a level that moved gets wide eyes first, then the meal carries on
WOW_HOLD = 1.0
# a blink is a beat, not a state: BLINK_HOLD shut, then open for a gap drawn
# between BLINK_GAP_MIN and BLINK_GAP_MAX. the schedule comes off the clock in
# BLINK_BLOCK-second blocks, so every redraw agrees on it with nothing stored,
# and a fixed window would tick like a clock. each block starts its own draw,
# so the gap across a block edge can run to twice the max; an hour keeps that rare
BLINK_HOLD = 0.5
BLINK_GAP_MIN = 1.0
BLINK_GAP_MAX = 10.0
BLINK_BLOCK = 3600
# a session that stops redrawing for SLEEP_AFTER was asleep. the redraw that
# ends the gap is you coming back, so it wakes: sleepy eyes for WAKE_HOLD, then
# whatever the day holds. the last redraw is stamped at most every SEEN_EVERY so
# the state file isn't rewritten on every frame
SLEEP_AFTER = 15 * 60
WAKE_HOLD = 3.0
SEEN_EVERY = 30
# nothing eaten in HUNGRY_AFTER, across every session, and it's upset about it
HUNGRY_AFTER = 4 * 3600


def touch(state, session_id, now=None):
    """Note that this session just drew the creature. Returns whether the row
    changed and wants saving. A gap of SLEEP_AFTER since the last draw means
    it was asleep and this draw is the wake-up, so that's stamped too.
    """
    now = time.time() if now is None else now
    row = (state.get("sessions") or {}).get(session_id) if session_id else None
    if row is None:
        return False
    last = row.get("seen_at")
    if last is None:
        row["seen_at"] = now
        return True
    if now - last >= SLEEP_AFTER:
        row["woke_at"] = now
    elif now - last < SEEN_EVERY:
        return False
    row["seen_at"] = now
    return True


def mood(state, session_id, now=None):
    """The face to draw right now, most urgent first. "sleepy" for WAKE_HOLD
    after a session woke; "wow" for WOW_HOLD after it saw a level rise; the
    "chew"/"happy" frames of a fresh meal; a "blink" when the schedule says
    the eyes are shut; "happy" for HAPPY_HOLD after a feed; "upset" once the
    creature has gone HUNGRY_AFTER without one; else None. Clock-based rather
    than counted: a statusline redraws whenever the host feels like it, so a
    count would blink at random.
    """
    now = time.time() if now is None else now
    row = (state.get("sessions") or {}).get(session_id) if session_id else None
    fed = row.get("fed_at", 0) if row else 0
    if row:
        if 0 <= now - row.get("woke_at", 0) < WAKE_HOLD:
            return "sleepy"
        if 0 <= now - row.get("leveled_at", 0) < WOW_HOLD:
            return "wow"
        if 0 <= now - fed < CHEW_HOLD:
            return "chew" if int((now - fed) / CHEW_BEAT) % 2 == 0 else "happy"
    if blinking(now):
        return "blink"
    # the meal's afterglow and hunger are the creature's, not one session's:
    # a feed another window saw still counts
    c = focused(state)
    last = max(fed, (c or {}).get("fed_at") or 0)
    if last and 0 <= now - last < HAPPY_HOLD:
        return "happy"
    if last and now - last >= HUNGRY_AFTER:
        return "upset"
    return None


def _blinks(block):
    """Seconds into block `block` at which each of its blinks starts."""
    draw = random.Random(block).uniform
    out, t = [], draw(BLINK_GAP_MIN, BLINK_GAP_MAX)
    while t < BLINK_BLOCK:
        out.append(t)
        t += BLINK_HOLD + draw(BLINK_GAP_MIN, BLINK_GAP_MAX)
    return out


def _shut_in(block, at):
    """Whether second `at` of block `block` falls inside one of its blinks."""
    return any(t <= at < t + BLINK_HOLD for t in _blinks(block))


def blinking(now):
    """Whether the eyes are shut at this instant. A blink that starts at the
    end of one block finishes in the next rather than being cut short.
    """
    block, at = divmod(now, BLINK_BLOCK)
    block = int(block)
    return _shut_in(block, at) or _shut_in(block - 1, at + BLINK_BLOCK)


def migrate(data):
    """Bring a state file of any older shape forward. Additive, never lossy.

    Upgrades are the one moment a buddy can silently disappear, so this only
    ever fills in what's missing. Nothing is dropped, including keys written by
    a version we don't know about. Species, rarity and shiny aren't stored at
    all, they come back off the seed, so there's nothing derived to rebuild.
    """
    state = default_state()
    state.update(data)
    settings = dict(DEFAULT_SETTINGS)
    settings.update(data.get("settings") or {})
    state["settings"] = settings

    creatures = [c for c in (state.get("creatures") or []) if isinstance(c, dict)]
    for i, c in enumerate(creatures):
        # every read path indexes these directly, so one written before the key
        # existed, or edited by hand, crashes the statusline instead of degrading.
        # setdefault isn't enough: a hand-edited "name": null has the key and
        # still crashes every command that lowercases it
        if c.get("seed") is None:
            c["seed"] = c.get("id") or "creature-%d" % i
        if c.get("id") is None:
            c["id"] = c["seed"]
        if c.get("name") is None:
            c["name"] = creature_mod.suggest_name(c["seed"])
        c.setdefault("hatched_at", None)
        if c.get("xp_banked") is None:
            c["xp_banked"] = 0
        if c.get("last_stage_seen") is None:
            c["last_stage_seen"] = 0
        # a creature from before feeds were stamped counts as just fed, so an
        # upgrade doesn't wake it up upset
        if c.get("fed_at") is None:
            c["fed_at"] = time.time()
    state["creatures"] = creatures

    if state.get("focused") not in {c["id"] for c in creatures}:
        # a focus pointing at nothing renders as "no buddy", which reads as a
        # wiped roster even though every creature is still sitting in the file
        alive = [c for c in creatures if not c.get("retired_at")]
        state["focused"] = alive[0]["id"] if alive else None

    seen = state.get("version")
    # a newer terminalcreature's stamp survives a downgrade rather than being relabelled
    state["version"] = seen if isinstance(seen, int) and seen > STATE_VERSION else STATE_VERSION
    return state


def load(path=STATE_PATH):
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default_state()
    if not isinstance(data, dict):
        return default_state()
    return migrate(data)


def save(state, path=STATE_PATH, own_settings=False):
    """Persist state. Only `config` owns settings.

    Background refreshes hold a whole-state copy for as long as the vault scan
    takes, so writing it back wholesale reverts any setting changed meanwhile.
    That silently undid config edits. Everyone except config re-reads settings
    off disk at write time.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not isinstance(state.get("version"), int):
        state = dict(state, version=STATE_VERSION)
    if not own_settings:
        try:
            with open(path, "r") as f:
                on_disk = json.load(f).get("settings")
            if on_disk:
                state = dict(state)
                state["settings"] = on_disk
        except (OSError, ValueError):
            pass
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def resolve_provider(settings):
    """The provider that will actually be measured. auto is decided here, off
    directory existence alone, so the answer is the same on every call until
    an agent is installed or removed.
    """
    provider = settings.get("provider") or "auto"
    if provider != "auto":
        return provider
    if len(metric.found_agents()) >= metric.AUTO_MIN_AGENTS:
        return "agents"
    return "claude"


def sources_for(settings):
    """(root, sources). For agents the root is home and the sources are the
    whole AGENT_ROOTS table: callers that only want a directory to name get
    one, and the ones that measure branch on the table identity.
    """
    provider = resolve_provider(settings)
    root = os.path.expanduser(settings.get("vault_root") or "")
    if provider == "vault" and root:
        return root, metric.VAULT_SOURCES
    if provider == "folder" and root:
        return root, metric.FOLDER_SOURCES
    if provider == "agents":
        return os.path.expanduser("~"), metric.AGENT_ROOTS
    return metric.default_claude_root(), metric.CLAUDE_SOURCES


def source_status(settings):
    """Whether there's a memory system to count at all. Counts, never a path.

    Three zeroes look identical in the statusline and mean different things: a
    root that isn't there, a real root that's empty, and a root full of files
    the provider's layout doesn't match. Only the middle one means "keep
    writing", so they can't share one message.

    The agents provider adds "agents" (per-agent counts), "found" and
    "missing" (agent keys) so `sources` can say who was counted.
    """
    root, sources = sources_for(settings)
    if sources is metric.AGENT_ROOTS:
        xp, per_agent, found = metric.measure_agents(settings.get("weights"))
        missing = [k for k in metric.agent_keys() if k not in found]
        extra = {"agents": per_agent, "found": found, "missing": missing}
        counts = metric.flatten_agent_counts(per_agent)
        if not found:
            return dict({"state": "missing_root", "xp": 0, "counts": {}}, **extra)
        if xp:
            return dict({"state": "ok", "xp": xp, "counts": counts}, **extra)
        return dict({"state": "empty", "xp": 0, "counts": counts}, **extra)
    if not os.path.isdir(root):
        return {"state": "missing_root", "xp": 0, "counts": {}}
    xp, counts = metric.measure(root, sources, settings.get("weights"))
    if xp:
        return {"state": "ok", "xp": xp, "counts": counts}
    # only vault keys off directory names, so it's the only one that can score
    # zero on a root that's full of notes. that's a mismatch, not an empty vault.
    if sources is metric.VAULT_SOURCES:
        _, stray = metric.measure(root, metric.FOLDER_SOURCES)
        found = sum(stray.values())
        if found:
            return {"state": "layout_mismatch", "xp": 0, "counts": counts, "stray": found}
    return {"state": "empty", "xp": 0, "counts": counts}


def measure_now(settings):
    """(xp, counts). Agents fold to one count per source key, the shape the
    cache, the card and the stats already read.
    """
    root, sources = sources_for(settings)
    if sources is metric.AGENT_ROOTS:
        xp, per_agent, _ = metric.measure_agents(settings.get("weights"))
        return xp, metric.flatten_agent_counts(per_agent)
    return metric.measure(root, sources, settings.get("weights"))


def read_cache(path=CACHE_PATH):
    """Returns (xp, counts, age_seconds) or None. Never raises."""
    try:
        st = os.stat(path)
        with open(path, "r") as f:
            data = json.load(f)
        return data["xp"], data.get("counts", {}), time.time() - st.st_mtime
    except (OSError, ValueError, KeyError):
        return None


def write_cache(xp, counts, path=CACHE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump({"xp": xp, "counts": counts}, f)
    os.replace(tmp, path)


def set_setting(key, value, path=STATE_PATH):
    """Set one setting against the file as it is NOW, not as it was loaded.

    Callers holding a state snapshot across anything slow (a card's cold-start
    scan, say) must not write their stale settings back wholesale; that is the
    same revert `save`'s own_settings rule exists to stop.
    """
    st = load(path)
    st["settings"][key] = value
    save(st, path=path, own_settings=True)


def read_latest(path=None):
    """Returns (version_or_empty, age_seconds) or None. Never raises.

    An empty version is a stamped failed attempt: the TTL still counts from
    it, so an offline machine retries tomorrow rather than on every refresh.
    path resolves at call time so tests can point LATEST_PATH elsewhere.
    """
    path = path or LATEST_PATH
    try:
        st = os.stat(path)
        with open(path, "r") as f:
            data = json.load(f)
        return str(data.get("version") or ""), time.time() - st.st_mtime
    except (OSError, ValueError):
        return None


def write_latest(version, path=None):
    path = path or LATEST_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump({"version": version or ""}, f)
    os.replace(tmp, path)


def focused(state):
    fid = state.get("focused")
    for c in state.get("creatures", []):
        if c["id"] == fid:
            return c
    return None


def sync(state, current_xp):
    """Credit new XP to the focused creature. Returns an evolution event or None.

    High water only ever rises, so deleting memories can't de-level anyone.
    Good hygiene shouldn't be punished.
    """
    c = focused(state)
    if c is None:
        # nobody to credit, so leave the mark alone. move it here and a render
        # before the first hatch burns the xp, hatching you at level 0.
        return None

    high = state.get("high_water_xp", 0)
    delta = current_xp - high
    if delta > 0:
        state["high_water_xp"] = current_xp
        c["xp_banked"] = c.get("xp_banked", 0) + delta
        c["fed_at"] = time.time()

    level = metric.level_for(c["xp_banked"], state["settings"]["xp_max"])
    idx, name = metric.stage_for(level)
    # an egg still banks xp, it just doesn't announce evolutions nobody can see.
    # the hatch ceremony is the reveal, so don't burn the beat before it
    if not is_hatched(c):
        return None
    if idx > c.get("last_stage_seen", 0):
        c["last_stage_seen"] = idx
        return {"creature": c["name"], "stage_index": idx, "stage": name, "level": level}
    return None


def create(state, name=None, focus=True):
    """Add a creature as an unhatched egg. New creatures start at zero banked XP.

    hatched_at stays None until `reveal`, which is what makes the egg a state
    rather than a level band.
    """
    c = creature_mod.new_creature(name=name)
    state.setdefault("creatures", []).append(c)
    if focus or state.get("focused") is None:
        state["focused"] = c["id"]
    return c


def is_hatched(c):
    return bool(c and c.get("hatched_at"))


def reveal(state):
    """Hatch the focused egg. Returns the creature, or None if there's nothing to open."""
    c = focused(state)
    if c is None or is_hatched(c):
        return None
    c["hatched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # the reveal shows whatever stage the egg banked its way to, so record it
    # here or the next sync fires an evolution notice for a stage just displayed
    level = metric.level_for(c.get("xp_banked", 0), state["settings"]["xp_max"])
    c["last_stage_seen"] = metric.stage_for(level)[0]
    return c


def active(state):
    return [c for c in state.get("creatures", []) if not c.get("retired_at")]


def retire(state, ident):
    """Retire by id or name. Keeps the record and its banked XP; focus can undo it."""
    c = find(state, ident)
    if c is None:
        return None
    c["retired_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if state.get("focused") == c["id"]:
        rest = active(state)
        state["focused"] = rest[0]["id"] if rest else None
    return c


def find(state, ident):
    for c in state.get("creatures", []):
        if c["id"] == ident or c["name"].lower() == str(ident).lower():
            return c
    return None


def focus(state, ident):
    """Focus by id or name (case-insensitive). Un-retires. Returns the creature or None."""
    c = find(state, ident)
    if c is None:
        return None
    c.pop("retired_at", None)
    state["focused"] = c["id"]
    return c
