"""strategy.py — Swappable map/non-combat strategies for game agents.

Each strategy implements the same interface: choose(state, choices) -> command dict.
Swap strategies globally via set_map_strategy() or pass directly.
"""
from dataclasses import dataclass, field
from typing import Protocol

from agent.card_scoring import SMITH_PRIORITY_THRESHOLD, best_smith_target
from agent.route_planner import enumerate_paths, node_index, node_key


class MapStrategy(Protocol):
    def choose(self, state: dict, choices: list[dict], trace=None) -> dict:
        """Select a map node. Returns a full command dict for the engine.

        trace (agent.greedy_trace.GreedyTrace) is optional evidence recording;
        greedy_action passes it when tracing, so every strategy must accept it."""
        ...


class Act1SafeStrategy:
    """Act 1 safe strategy: avoid fights early, avoid elites before floor 3.

    Priority: RestSite > Shop (if gold sufficient) > Event/Treasure > Monster > Elite.
    Boss is unavoidable (only choice) so it ranks lowest to never be picked over alternatives.
    On floors 1-2: Elite is deprioritized heavily (penalty +10).
    """
    SHOP_GOLD_THRESHOLD = 100
    ELITE_AVOID_FLOOR = 3  # avoid elites on floors below this

    # Lower = higher priority. Boss=99 because it's never alongside alternatives.
    PRIORITY = {
        "RestSite": 0,
        "Shop": 1,
        "Event": 2,
        "Treasure": 3,
        "Unknown": 4,
        "Ancient": 4,
        "Monster": 5,
        "Elite": 6,
        "Boss": 99,
    }

    def choose(self, state: dict, choices: list[dict], trace=None) -> dict:
        """trace is accepted so greedy_action can pass it to any map strategy;
        this strategy records nothing."""
        gold = state.get("player", {}).get("gold", 0)
        floor = state.get("floor") or state.get("context", {}).get("floor", 99)
        scored = []
        for i, c in enumerate(choices):
            p = self.PRIORITY.get(c.get("type", "Unknown"), 4)
            # Shop is unattractive when broke
            if c.get("type") == "Shop" and gold < self.SHOP_GOLD_THRESHOLD:
                p += 5
            # Avoid elites on early floors
            if c.get("type") == "Elite" and isinstance(floor, int) and floor < self.ELITE_AVOID_FLOOR:
                p += 10
            scored.append((p, i, c))
        scored.sort()
        best = scored[0][2]
        return {"cmd": "action", "action": "select_map_node",
                "args": {"col": best["col"], "row": best["row"]}}


class HpAwareMapStrategy(Act1SafeStrategy):
    """HP-aware map routing: avoids elites when current HP is low.

    Inherits Act1SafeStrategy's floor-based elite avoidance and adds
    HP-ratio penalties so a damaged run doesn't feed into an elite.
    When HP > 75% and past floor 6, elites are actively preferred over
    monsters (relic gain is worth the risk).
    Acts 2+ (from state["act"]; floors are act-local) are handled separately:
    always avoid elites unless very healthy (>90%), heal aggressively, shop actively.
    """
    HP_DANGER = 0.40   # below this: strong elite avoidance
    HP_LOW    = 0.60   # below this: moderate elite avoidance
    HP_STRONG = 0.85   # above this (and floor > 6): slight elite preference

    ELITE_PREFER_FLOOR = 6  # only prefer elites after Act 1 mid-game
    ACT2_BOSS_ZONE = 13      # act-local floor from which acts 2+ save HP for the boss

    def choose(self, state: dict, choices: list[dict], trace=None) -> dict:
        player = state.get("player", {})
        hp = player.get("hp", 80)
        max_hp = max(player.get("max_hp", 80), 1)
        hp_ratio = hp / max_hp
        gold = player.get("gold", 0)
        floor = state.get("floor") or state.get("context", {}).get("floor", 99)
        act = state.get("act") or (state.get("context") or {}).get("act")
        # true for acts 2 and 3; names say act2 for history
        in_act2 = isinstance(act, int) and not isinstance(act, bool) and act >= 2

        scored = []
        for i, c in enumerate(choices):
            base = self.PRIORITY.get(c.get("type", "Unknown"), 4)
            p = base
            comps = [("map.base_priority", base, c.get("type", "Unknown"))]
            # Shop value scales up in Act 2 — lower gold threshold
            act2_shop_threshold = 75 if in_act2 else self.SHOP_GOLD_THRESHOLD
            if c.get("type") == "Shop" and gold < act2_shop_threshold:
                p += 5
                comps.append(("map.shop_low_gold", 5, None))
            if c.get("type") == "Elite":
                # Floor-based early avoidance (inherited logic)
                if isinstance(floor, int) and floor < self.ELITE_AVOID_FLOOR:
                    p += 10
                    comps.append(("map.elite_early_floor", 10, None))
                # HP-based avoidance — takes priority over floor preference
                if hp_ratio < self.HP_DANGER:
                    p += 15
                    comps.append(("map.elite_hp_danger", 15, None))
                elif hp_ratio < self.HP_LOW:
                    p += 7
                    comps.append(("map.elite_hp_low", 7, None))
                elif in_act2:
                    # Act 2 elites: only seek them at 90%+ HP; otherwise avoid
                    if hp_ratio >= 0.90:
                        # Elite 6-1.0 = 5.0 ties Monster 5: node order decides (see tied_best).
                        # Known, unretuned -- a change needs an A/B.
                        p -= 1.0  # healthy enough to risk Act 2 elite for relic
                        comps.append(("map.elite_act2_healthy", -1.0, None))
                    else:
                        p += 5  # Act 2 elites are significantly harder
                        comps.append(("map.elite_act2", 5, None))
                elif hp_ratio >= self.HP_STRONG and isinstance(floor, int) and floor > self.ELITE_PREFER_FLOOR and floor <= 12:
                    p -= 1.9  # very healthy (85%+) + mid-game: elites worth it for relics
                    # Use 1.9 not 2.0: prevents Elite from tying Unknown/Event (p=4)
                    # when both appear — tied priority resolves by index (Elite=0 wins), which
                    # would silently prefer combat over a safe event room.
                    # HP_STRONG raised to 0.85: runs at 75-85% HP shouldn't seek elites.
                    comps.append(("map.elite_strong_midgame", -1.9, None))
                elif isinstance(floor, int) and floor >= 13:
                    p += 3  # pre-boss zone: save HP for boss fight, avoid elites
                              # Jun 10 attempted +10 boost (strong avoid) regressed boss-reach
                              # 13%→0% even though avg_floor rose — paths around elite force
                              # tougher unavoidable fights. Reverted to +3.
                    comps.append(("map.elite_pre_boss", 3, None))
                if in_act2 and isinstance(floor, int) and floor >= self.ACT2_BOSS_ZONE:
                    p += 4  # pre-boss in acts 2+: strongly avoid elites regardless of HP
                    comps.append(("map.elite_act2_boss_zone", 4, None))
            if trace is not None:
                trace.option(i, kind="node", score=p, components=comps,
                             detail={"col": c.get("col"), "row": c.get("row"),
                                     "type": c.get("type")})
            scored.append((p, i, c))
        scored.sort()
        best = scored[0][2]
        if trace is not None:
            best_p = scored[0][0]
            trace.rule("map.pick_min_priority", "fired", best_index=scored[0][1],
                       tied_best=[i for p, i, _ in scored if p == best_p],
                       hp_ratio=hp_ratio, gold=gold, floor=floor, in_act2=in_act2)
        return {"cmd": "action", "action": "select_map_node",
                "args": {"col": best["col"], "row": best["row"]}}


def _interp(curve: tuple, x: float) -> float:
    """Piecewise-linear y at x on a curve of ascending (x, y) knots; flat outside."""
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


@dataclass(frozen=True)
class PathEval:
    """One scored route. ``value`` is higher-is-better and is the sum of ``parts``
    (LookaheadMapStrategy.PART_KEYS -> contribution, zero parts omitted)."""
    value: float
    parts: dict = field(default_factory=dict)
    types: str = ""          # one letter per room, see LookaheadMapStrategy.LETTERS
    hp_boss: float = 0.0     # projected HP on entering the boss
    elites: int = 0
    rests: int = 0
    p_boss: float = 1.0      # projected chance of reaching the boss alive


class LookaheadMapStrategy:
    """Whole-path map routing: score every route from each offered node to the
    boss and take the first step of the best one.

    HpAwareMapStrategy scores only the NEXT node, so it walks into corridors
    whose only continuation is an elite at low HP (live Regent a3: A3F9 only
    child Elite at 67/75 -> 40/75, then A3F11 only child Elite at 40/75; two of
    three live losses were set up that way). Here each route is simulated room
    by room with EXPECTED HP (rests heal, fights cost), and every combat adds the
    chance of dying in it at the HP the route arrives with. Value is in points,
    1 point ~ 1 HP of healing; the points are a judgement call, tune them only
    with a paired A/B (STS2_ROUTE_POLICY=hpaware vs =lookahead).

    A map is required (``state["map"]`` -- the engine's get_map reply -- or
    ``map_json=``). Without one, or when an offered node is not on it, the choice
    is made by ``fallback`` (HpAwareMapStrategy) exactly as before.

    Trace (agent.greedy_trace): the map_select panel sorts LOWER-is-better, like
    HpAware's priority, so each option's score is the route's COST (minus its
    value) and its components are the negated parts: a reward shows negative.
    """

    # ---- expected HP lost entering a room; index = act - 1 (acts 4+ use act 3) ----
    # Evidence: agent/data/route_model_a1.json, 191 headless a1 games, survivors'
    # mean HP lost: Monster 1.7 / 3.7 / 4.7, Elite 6.9 / 12.2 / 14.1 (pooled 3.1 /
    # 10.5), Unknown 0.8 / 0.3 / 1.1. Live a3-a10 runs: Monster 3.2, Elite 13,
    # Boss 29, Unknown/event 1.5 (pooled over acts); Act-3 elites ran 26-59 HP
    # (Soul Nexus). Elites are therefore the headless numbers x ~1.2 for Acts 1-2
    # and 22 for Act 3; the Boss split by act is a guess around the live mean 29.
    MONSTER_COST = (2.0, 4.0, 5.0)
    ELITE_COST = (8.0, 15.0, 22.0)
    BOSS_COST = (25.0, 30.0, 35.0)
    UNKNOWN_COST = 1.5

    # ---- the same costs by ascension (the tables above are the "base" band) ----
    # Real-game autopilot campaign (history files), mean damage_taken per room,
    # survivors only (n in brackets), acts 1/2/3:
    #              a1-4                     a5-7                      a8-10
    #   monster  1.3(49) 3.0(40) 3.1(26) | 1.7(93) 3.0(79) 5.6(58) | 3.8(122) 6.3(88) 8.2(46)
    #   elite    2.5(10) 8.5(10) 13.3(11)| 9.2(27) 10.8(20) 13.7(13)| 14.9(30) 25.1(30) 27.6(8)
    #   boss    27.8(8)  16.9(7) 31.4(7) | 22.7(17) 33.5(16) 45.3(14)| 32.6(22) 43.1(17) 39.9(9)
    # Ascension <= 5 keeps the base table EXACTLY (the a5 A/B and the golden corpus
    # were measured with it). "a6-7" is the a5-7 column, but a cell with n < 15 is
    # smoothed with the base value: max(base, data) for elites and bosses (elite A3
    # 13.7(13) -> 22; boss A3 45.3(14) stays 45.3 because it is above 35); monsters
    # take the data. "a8+" is the a8-10 column, elite A3 n=8 -> max(27.6, 22).
    # The death curves (keyed on HP / cost) turn these survivor means into the tail.
    COST_BANDS = {
        "base": (MONSTER_COST, ELITE_COST, BOSS_COST),
        "a6-7": ((1.7, 3.0, 5.6), (9.2, 10.8, max(13.7, 22.0)), (22.7, 33.5, max(45.3, 35.0))),
        "a8+": ((3.8, 6.3, 8.2), (14.9, 25.1, max(27.6, 22.0)), (32.6, 43.1, max(39.9, 35.0))),
    }

    # Act 1 routes with the base band at EVERY ascension: its costs and its elite
    # reward window / factor (= exactly ascension <= 5, the A/B-validated setting);
    # only acts 2 and 3 use the run's own band. Live a8-10 runs (f59b0aa did not
    # flip it): with the a6-7 / a8+ act-1 costs the planner skipped every act-1
    # elite below ~85% HP at 75-80 max HP, yet act-1 deaths at a8-10 are almost all
    # at the act-1 BOSS with weak decks (Vantom, Waterfall Giant, Soul Fysh,
    # Lagavulin Matriarch; one elite death) -- the act-1 bottleneck is deck / relic
    # strength, not elite HP. Acts 2-3 elites are the real killers (Decimillipede,
    # Infested Prisms, Entomancer, Mecha Knight), so those keep the data costs.
    @staticmethod
    def table_band(band: str, act_index: int) -> str:
        """The cost / reward table an act uses: "base" in act 1, else ``band``."""
        return "base" if act_index == 0 else band

    @staticmethod
    def cost_band(ascension) -> str:
        """Cost table name for a run's ascension; unknown / non-int -> "base"."""
        if not isinstance(ascension, int) or isinstance(ascension, bool) or ascension <= 5:
            return "base"
        return "a6-7" if ascension <= 7 else "a8+"

    @staticmethod
    def state_ascension(state: dict, map_json=None):
        """The run's ascension from a decision: top-level ``ascension`` (the mod adds
        it to map_select), else ``context.ascension``, else the map's context; None
        when the decision carries none (the headless engine's does not)."""
        for src in (state, state.get("context"),
                    (map_json or {}).get("context") if isinstance(map_json, dict) else None):
            if isinstance(src, dict):
                v = src.get("ascension")
                if isinstance(v, int) and not isinstance(v, bool):
                    return v
        return None

    # ---- chance of dying in a combat, by (HP on entering) / (that fight's cost) ----
    # Evidence: the same cells, deaths / (deaths + survivors) binned by pre-room
    # HP / the act's mean cost. Elite: <1.5: 50%, 1.5-2.5: 22%, 2.5-3.5: 9.3%,
    # 3.5-4.5: 3.4%, >=4.5: 0.6% (n = 20 / 45 / 75 / 118 / 675). Monster: <3: 25%,
    # 3-6: 11%, 6-10: 1.2%, 10-15: 0.5%, 15-25: 0.3%, >=25: 0 (n = 28 / 28 / 84 /
    # 380 / 1120 / 1112). The boss uses the elite curve. Knots are bin centres.
    ELITE_DEATH = ((0.0, 1.0), (1.0, 0.50), (2.0, 0.22), (3.0, 0.09), (4.0, 0.034), (5.0, 0.008))
    MONSTER_DEATH = ((0.0, 1.0), (2.0, 0.25), (4.5, 0.11), (8.0, 0.012), (12.0, 0.005),
                     (20.0, 0.003), (30.0, 0.0))
    BOSS_DEATH = ELITE_DEATH

    # ---- points (1 point ~ 1 HP of healing) ----
    # A dead run forfeits the rest of it: ~20 floors x ~20 points a floor. An
    # elite's relic (+ gold, potion) is worth ~12: with these costs and death
    # curves that sends a healthy Act 1 route through ~2.2 elites and Act 2
    # ~1.5 (a uniformly random route over the same six real a5 maps: 2.1 an act;
    # HpAware ~1.2), measured by walking them with sampled losses. 12 is a judgement call -- the 3b route A/B
    # showed that skipping elites for rests LOSES floors, so it is not set lower.
    # The reward is paid in full from ELITE_COMFORT_FRAC of max HP (HpAware's
    # HP_STRONG: "runs at 75-85% HP shouldn't seek elites") and not at all at
    # ELITE_DANGER_FRAC. HP is worth little by itself (a rest refunds ~22 of it);
    # it matters through the death curves and HP_VALUE per HP on entering the boss.
    DEATH_PENALTY = 400.0
    ELITE_REWARD = 12.0
    ELITE_DANGER_FRAC = 0.55
    ELITE_COMFORT_FRAC = 0.85
    # The high-ascension bands (a6-7, a8+) pay the elite relic differently, because
    # their elite costs are real data (act-1 elite 9.2 / 14.9 HP) and the flat
    # 12 points x the 85%/55% window made the planner skip EVERY act-1 elite whenever
    # HP was below ~85% (live Regent a9: 58-64 of 75-81 HP all act 1 -> act-1 boss
    # with 3 relics and a 15-card deck, lost; Necrobinder a9 the same, 5 relics).
    #  * act factor: a relic found in act 1 serves ~45 floors, in act 2 ~28, in
    #    act 3 ~10, so the 12 points scale 1.5 / 1.0 / 0.6 (acts 4+ use act 3);
    #  * window: paid in full from 75% HP and not at all at 45% (a rest refunds
    #    ~22 HP, so an elite at 75% is recoverable).
    # Ascension <= 5 ("base") keeps factor 1 and 85%/55% exactly (the a5 A/B).
    # Act 1 itself now uses the base row at every ascension (see table_band), so
    # in practice the 1.5 act-1 factor is unused; acts 2-3 use the band's rows.
    ELITE_ACT_FACTOR = {"base": (1.0, 1.0, 1.0), "a6-7": (1.5, 1.0, 0.6), "a8+": (1.5, 1.0, 0.6)}
    ELITE_WINDOW = {"base": (ELITE_DANGER_FRAC, ELITE_COMFORT_FRAC),
                    "a6-7": (0.45, 0.75), "a8+": (0.45, 0.75)}
    MONSTER_REWARD = 4.0      # card reward + ~16 gold
    UNKNOWN_REWARD = 4.0      # events: upgrade / relic / gold on average
    TREASURE_REWARD = 15.0    # a relic
    SMITH_REWARD = 8.0        # a rest taken as an upgrade (HP above the heal line)
    SHOP_REWARD = 10.0        # a shop reached with enough gold
    HP_VALUE = 0.3            # per HP on entering the boss

    # ---- rest sites ----
    REST_HEAL_FRAC = 0.30     # of max HP, as the engine's Rest option heals
    # gold the route is projected to pick up (cells' mean gold change), spent at a shop
    GOLD_GAIN = {"Monster": 16.0, "Elite": 44.0, "Treasure": 55.0}
    SHOP_GOLD = HpAwareMapStrategy.SHOP_GOLD_THRESHOLD   # HpAware's own thresholds
    SHOP_GOLD_ACT2 = 75

    MAX_ACT = 3
    TIE_EPS = 1e-9

    # one letter per room in PathEval.types / the trace
    LETTERS = {"Monster": "M", "Elite": "E", "RestSite": "R", "Shop": "$",
               "Unknown": "?", "Treasure": "T", "Ancient": "A"}
    # PathEval.parts keys -> the trace component each becomes (negated)
    PART_KEYS = {"room_reward": "map.la_room_reward", "elite_reward": "map.la_elite_reward",
                 "elite_danger": "map.la_elite_danger", "fight_danger": "map.la_fight_danger",
                 "boss_danger": "map.la_boss_danger", "hp_end": "map.la_hp_end"}

    def __init__(self, fallback=None):
        self.fallback = fallback if fallback is not None else HpAwareMapStrategy()

    @staticmethod
    def rest_heal_below(floor: int) -> float:
        """HP fraction under which rest_site_action heals rather than smiths
        (its heal_threshold by act-local floor; the must-upgrade override is not
        modelled)."""
        if floor >= 16:
            return 0.95
        if floor >= 11:
            return 0.85
        if floor >= 6:
            return 0.80
        return 0.75

    def evaluate_path(self, path: list, hp: float, max_hp: float, gold: float,
                      act: int, ascension=None) -> PathEval:
        """Walk ``path`` (map nodes, first = the room entered next) from ``hp``
        and return its value. Expected HP is deterministic; the chance of dying
        is carried along (``p``) so a risky room discounts everything after it."""
        a = min(max(act, 1), self.MAX_ACT) - 1
        band = self.table_band(self.cost_band(ascension), a)
        monster_cost, elite_cost, boss_cost = self.COST_BANDS[band]
        elite_factor = self.ELITE_ACT_FACTOR[band][a]
        danger_frac, comfort_frac = self.ELITE_WINDOW[band]
        max_hp = max(float(max_hp), 1.0)
        hp = min(max(float(hp), 1.0), max_hp)
        p = 1.0
        parts = dict.fromkeys(self.PART_KEYS, 0.0)
        elites = rests = 0
        letters = []
        for node in path:
            kind = node.get("type")
            floor = int(node["row"]) + 1
            letters.append(self.LETTERS.get(kind, "?"))
            if kind in ("Monster", "Elite"):
                elite = kind == "Elite"
                cost = elite_cost[a] if elite else monster_cost[a]
                death = _interp(self.ELITE_DEATH if elite else self.MONSTER_DEATH, hp / cost)
                parts["elite_danger" if elite else "fight_danger"] -= p * death * self.DEATH_PENALTY
                if elite:
                    elites += 1
                    gate = min(max((hp / max_hp - danger_frac)
                                   / (comfort_frac - danger_frac), 0.0), 1.0)
                    reward_key, reward = "elite_reward", self.ELITE_REWARD * elite_factor * gate
                else:
                    reward_key, reward = "room_reward", self.MONSTER_REWARD
                p *= 1.0 - death
                parts[reward_key] += p * reward
                hp = max(hp - cost, 1.0)
                gold += self.GOLD_GAIN[kind]
            elif kind == "RestSite":
                rests += 1
                if hp / max_hp < self.rest_heal_below(floor):
                    hp = min(hp + self.REST_HEAL_FRAC * max_hp, max_hp)
                else:
                    parts["room_reward"] += p * self.SMITH_REWARD
            elif kind == "Shop":
                if gold >= (self.SHOP_GOLD_ACT2 if act >= 2 else self.SHOP_GOLD):
                    parts["room_reward"] += p * self.SHOP_REWARD
                    gold = 0.0
            elif kind == "Treasure":
                parts["room_reward"] += p * self.TREASURE_REWARD
                gold += self.GOLD_GAIN["Treasure"]
            else:  # Unknown, and any type this table does not know (HpAware: priority 4)
                hp = max(hp - self.UNKNOWN_COST, 1.0)
                parts["room_reward"] += p * self.UNKNOWN_REWARD
        boss_death = _interp(self.BOSS_DEATH, hp / boss_cost[a])
        parts["boss_danger"] -= p * boss_death * self.DEATH_PENALTY
        parts["hp_end"] += p * self.HP_VALUE * hp
        parts = {k: v for k, v in parts.items() if abs(v) > 1e-12}
        return PathEval(value=sum(parts.values()), parts=parts, types="".join(letters),
                        hp_boss=hp, elites=elites, rests=rests, p_boss=p)

    def _best_paths(self, state: dict, choices: list, map_json) -> list | str:
        """[(best PathEval, paths scored)] per choice, or the reason the map cannot
        be used."""
        if not isinstance(map_json, dict) or not map_json.get("rows"):
            return "no_map"
        index = node_index(map_json)
        player = state.get("player") or {}
        hp, max_hp = player.get("hp", 80), max(player.get("max_hp", 80), 1)
        gold = player.get("gold", 0)
        act = state.get("act") or (state.get("context") or {}).get("act")
        act = act if isinstance(act, int) and not isinstance(act, bool) else 1
        ascension = self.state_ascension(state, map_json)
        out = []
        for choice in choices:
            key = node_key(choice)
            if key not in index:
                return "choice_not_on_map"
            try:
                paths = enumerate_paths(map_json, key)
            except ValueError:
                return "too_many_paths"
            best = None
            for path in paths:
                scored = self.evaluate_path(path, hp, max_hp, gold, act, ascension)
                if best is None or scored.value > best.value + self.TIE_EPS:
                    best = scored
            out.append((best, len(paths)))
        return out

    def choose(self, state: dict, choices: list[dict], trace=None, map_json=None) -> dict:
        if len(choices) < 2:  # nothing to plan: HpAware takes the only node
            return self._fallback(state, choices, trace, None)
        if map_json is None:
            map_json = state.get("map")
        planned = self._best_paths(state, choices, map_json)
        if isinstance(planned, str):
            return self._fallback(state, choices, trace, planned)

        best_value = max(ev.value for ev, _ in planned)
        tied = [i for i, (ev, _) in enumerate(planned) if ev.value >= best_value - self.TIE_EPS]
        pick = tied[0]
        if len(tied) > 1:
            # Equal routes: HpAware's priority, then (col, row) order -- not the
            # engine's list order, so the pick does not depend on how it lists them.
            tied.sort(key=lambda i: node_key(choices[i]))
            cmd = self.fallback.choose(state, [choices[i] for i in tied])
            args = cmd["args"]
            pick = next(i for i in tied if node_key(choices[i]) == (args["col"], args["row"]))
        best = choices[pick]

        if trace is not None:
            player = state.get("player") or {}
            max_hp = max(player.get("max_hp", 80), 1)
            for i, (ev, n_paths) in enumerate(planned):
                trace.option(
                    i, kind="node", score=-ev.value,
                    components=[(self.PART_KEYS[k], -v, None) for k, v in ev.parts.items()],
                    detail={"col": choices[i].get("col"), "row": choices[i].get("row"),
                            "type": choices[i].get("type"), "path": ev.types,
                            "hp_boss": round(ev.hp_boss, 1), "elites": ev.elites,
                            "rests": ev.rests, "paths": n_paths})
            band = self.cost_band(self.state_ascension(state, map_json))
            act_n = state.get("act") or (state.get("context") or {}).get("act")
            act_n = act_n if isinstance(act_n, int) and not isinstance(act_n, bool) else 1
            table = self.table_band(band, min(max(act_n, 1), self.MAX_ACT) - 1)
            ev = planned[pick][0]
            others = [e for i, (e, _) in enumerate(planned) if i != pick]
            runner = max(others, key=lambda e: e.value)
            if len(tied) > 1:
                decider = "tied"
            else:
                gap = {k: ev.parts.get(k, 0.0) - runner.parts.get(k, 0.0) for k in self.PART_KEYS}
                decider = max(gap, key=gap.get)
                if gap[decider] <= self.TIE_EPS:
                    decider = "no_gap"
            trace.rule("map.la_best_path", "fired", best_index=pick, tied_best=tied,
                       n_choices=len(choices), n_paths=sum(n for _, n in planned),
                       path=ev.types, elites=ev.elites, rests=ev.rests,
                       hp_ratio=player.get("hp", 80) / max_hp,
                       hp_boss_ratio=ev.hp_boss / max_hp,
                       margin=ev.value - runner.value, decider=decider,
                       cost_band=band, act_table=table,
                       elite_reward_factor=self.ELITE_ACT_FACTOR[table][min(max(act_n, 1), self.MAX_ACT) - 1],
                       elite_window=list(self.ELITE_WINDOW[table]),
                       act=state.get("act") or (state.get("context") or {}).get("act"),
                       floor=state.get("floor") or (state.get("context") or {}).get("floor"))
        return {"cmd": "action", "action": "select_map_node",
                "args": {"col": best["col"], "row": best["row"]}}

    def _fallback(self, state, choices, trace, reason):
        if trace is not None and reason is not None:
            trace.rule("map.la_fallback", "fired", reason=reason)
        return self.fallback.choose(state, choices, trace=trace)


PRE_BOSS_HEAL_THRESHOLD = 0.85


def pre_boss_heal_threshold():
    """STS2_PRE_BOSS_HEAL: HP ratio below which the pre-boss zone rests instead of
    smithing. Unset / blank = PRE_BOSS_HEAL_THRESHOLD; a float in (0, 1] = that
    ratio; `legacy` = None, i.e. the floor-graduated heal_threshold (0.95 at floor
    16+). Anything else raises ValueError."""
    import os
    raw = os.environ.get("STS2_PRE_BOSS_HEAL", "").strip().lower()
    if not raw:
        return PRE_BOSS_HEAL_THRESHOLD
    if raw == "legacy":
        return None
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not 0.0 < value <= 1.0:     # also rejects NaN
        raise ValueError(
            f"STS2_PRE_BOSS_HEAL={raw!r}: want a float in (0, 1], 'legacy', or blank")
    return value


def rest_site_action(state: dict, options: list[dict], trace=None) -> dict:
    """Decide heal vs upgrade at a rest site.

    Rules, checked in this order; the first that applies decides (a heal rule needs
    an enabled HEAL option, a smith rule an enabled SMITH one):
      1. rest.critical_heal    HP < CRITICAL_HEAL ratio → heal, no exceptions
      2. rest.vantom_mid_boss  STS2_VANTOM_REST_HEAL on, Vantom boss, floors 11-13,
                               HP < 70% → heal
      3. rest.pre_boss_heal    floor ≥ 14 and HP < PRE_BOSS_HEAL_THRESHOLD (0.85;
                               STS2_PRE_BOSS_HEAL, `legacy` = the floor-graduated
                               heal threshold) → heal. Above it the rest's 30% is
                               mostly wasted and rule 4 smiths instead.
      4. rest.must_upgrade     deck has an un-upgraded high-value card
                               (score ≥ SMITH_PRIORITY_THRESHOLD) → SMITH that card
                               (a +1 upgrade pays off every remaining combat, while
                               +30% HP from rest is one-shot value)
      5. rest.heal_threshold   HP < the floor-graduated heal threshold → heal
      6. rest.default_smith    otherwise → SMITH
    If none applies: rest.fallback takes HEAL, else the first enabled option;
    with nothing enabled, rest.no_choice proceeds.

    The SMITH-priority override is the change requested 2026-05-06: "even at low
    HP, upgrade if there's a must-upgrade card; don't always rest."

    trace (agent.greedy_trace.GreedyTrace) records each rule checked, in order, up to
    the one that decided.
    """
    player = state.get("player", {})
    hp = player.get("hp", 0)
    max_hp = max(player.get("max_hp", 80), 1)
    hp_ratio = hp / max_hp
    floor = state.get("floor") or state.get("context", {}).get("floor", 0)
    # NOTE on `floor`: `context.floor` is _runState.ActFloor — ACT-LOCAL. Two of
    # the three bands below want exactly that (`heal_threshold` = "later in the
    # act, heal more readily"; `pre_boss_zone` = each act's boss sits at floor
    # 17). CRITICAL_HEAL is the odd one out: its comments describe act semantics
    # ("Act 1: prioritize survival", "Act 2 body") that act-local floors do not
    # implement — floors 1-9 of *every* act take the Act 1 band.
    #
    # MEASURED 2026-09-01 and it does not matter: keying CRITICAL_HEAL off the
    # global floor instead scored 18.117 vs 18.333 over 60 paired seeds
    # (17000+, Defect ppo_defect_2048k, a1) — diff -0.217, p=0.62. The
    # inconsistency is real but behaviourally inert, so the code stays act-local
    # rather than carrying a config switch for a null. Don't re-litigate it.
    deck = player.get("deck") or []
    boss = (state.get("context") or {}).get("boss") or {}
    boss_id = boss.get("id") if isinstance(boss, dict) else boss
    is_vantom = "VANTOM" in str(boss_id or "").upper()

    # Heal threshold by floor — used when no must-upgrade card present.
    if isinstance(floor, int) and floor >= 16:
        heal_threshold = 0.95
    elif isinstance(floor, int) and floor >= 11:
        heal_threshold = 0.85
    elif isinstance(floor, int) and floor >= 6:
        heal_threshold = 0.80
    else:
        heal_threshold = 0.75

    # Below this we always heal regardless of upgrade opportunities.
    # Raised 2026-05-19 (verbose eval evidence): with old 0.30, Act 1 runs died
    # because at 40/85 HP (47%) the agent SMITH'd then walked into Elite at 40.
    # Act 1 has no recovery slack; one upgrade is worth ~0.5-1 floor, but
    # 30% HP heal is worth ~2-3 floors of survival cushion.
    if isinstance(floor, int) and floor <= 9:
        CRITICAL_HEAL = 0.55  # Act 1: prioritize survival
    elif isinstance(floor, int) and floor <= 15:
        CRITICAL_HEAL = 0.40  # Act 2 body: balance
    else:
        CRITICAL_HEAL = 0.30  # Pre-boss zone

    enabled = [o for o in options if o.get("is_enabled", True)]
    heal  = next((o for o in enabled if "heal"  in (o.get("option_id") or "").lower()), None)
    smith = next((o for o in enabled if "smith" in (o.get("option_id") or "").lower()), None)

    smith_target = best_smith_target(deck) if smith is not None else None
    has_must_upgrade = smith_target is not None

    # SMITH-override pays off when many combats remain; loses when boss is next.
    # Floor 14+ = pre-boss zone (boss at 17, max 2 fights remain), so HEAL wins.
    # Earlier scope (10+) regressed -1.0 floor by removing too many good SMITHs.
    pre_boss_zone = isinstance(floor, int) and floor >= 14
    pre_boss_threshold = pre_boss_heal_threshold()
    if pre_boss_threshold is None:       # STS2_PRE_BOSS_HEAL=legacy
        pre_boss_threshold = heal_threshold
    import os
    vantom_rest_heal_enabled = os.environ.get(
        "STS2_VANTOM_REST_HEAL", "") in ("1", "true", "on")
    vantom_mid_boss_low = (
        vantom_rest_heal_enabled
        and is_vantom
        and isinstance(floor, int)
        and 11 <= floor <= 13
        and hp_ratio < 0.70
    )
    # Conditions are side-effect-free booleans, evaluated up front. `available` =
    # the option the rule would pick exists and is enabled.
    heal_ok, smith_ok = heal is not None, smith is not None
    checks = (
        ("rest.critical_heal", hp_ratio < CRITICAL_HEAL and heal_ok, heal,
         {"hp_ratio": hp_ratio, "threshold": CRITICAL_HEAL, "floor": floor,
          "available": heal_ok}),
        ("rest.vantom_mid_boss", vantom_mid_boss_low and heal_ok, heal,
         {"enabled": vantom_rest_heal_enabled, "is_vantom": is_vantom,
          "hp_ratio": hp_ratio, "floor": floor, "available": heal_ok}),
        ("rest.pre_boss_heal",
         pre_boss_zone and hp_ratio < pre_boss_threshold and heal_ok, heal,
         {"hp_ratio": hp_ratio, "threshold": pre_boss_threshold, "floor": floor,
          "available": heal_ok}),
        ("rest.must_upgrade", has_must_upgrade, smith,
         {"deck_index": smith_target, "threshold": SMITH_PRIORITY_THRESHOLD,
          "available": smith_ok}),
        ("rest.heal_threshold", hp_ratio < heal_threshold and heal_ok, heal,
         {"hp_ratio": hp_ratio, "threshold": heal_threshold, "floor": floor,
          "available": heal_ok}),
        ("rest.default_smith", smith_ok, smith, {}),
    )
    choice = None
    for key, applies, option, values in checks:
        if applies:
            choice = option
            if trace is not None:
                trace.rule(key, "fired", **values)
            break
        if trace is not None:
            trace.rule(key, "skipped", **values)
    else:  # no rule applied
        choice = heal or (enabled[0] if enabled else None)
        if trace is not None and choice:
            trace.rule("rest.fallback", "fired")

    if choice:
        return {"cmd": "action", "action": "choose_option",
                "args": {"option_index": choice["index"]}}
    if trace is not None:
        trace.rule("rest.no_choice", "fired")
    return {"cmd": "action", "action": "proceed"}
