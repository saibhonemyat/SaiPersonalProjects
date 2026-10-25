"""Kitchen snapshot and PDDL problem construction.

This module has no simulator imports. ``kitchen.py`` reads the simulator's
environment into a ``Snapshot``, and everything after that is plain Python,
so we can test the model offline against the level files.

The flow is:

    Environment --kitchen.py--> Snapshot --build_problem()--> PDDL text

``Symbols`` keeps the mapping between PDDL names and kitchen names, so
from_pddl.py can rebuild the same mapping from the same snapshot.
"""

from __future__ import annotations

import re
from collections import Counter as Multiset
from collections import deque
from dataclasses import dataclass, field

Location = tuple[int, int]

NOTHING = "nothing"
ORDER_END = "o_end"

# How much recipe work one plan may cover, counted in cook and combine
# steps. A cake is about 7, a cheeseburger 4, a hamburger 2. Bigger plans
# take the planner longer, and a solve that runs out of time ends the run,
# so we plan a few orders at a time and replan after each delivery.
WORK_BUDGET = 14
# Optimistic timesteps per recipe step (fetch, carry, combine) when deciding
# whether an order can still make its deadline. Kept low on purpose: we only
# drop an order when even a fast kitchen could not make it in time.
FAST_STEPS_PER_WORK = 4

# Orders per plan. Plans run in rounds and we replan after deliveries, so
# looking further ahead gains little and makes the search much harder.
MAX_ORDERS_PER_PLAN = 3

# A chef may use a station of a type only if it is at most this many steps
# further than the nearest one of that type. In parallel kitchens this keeps
# each chef at its own pot instead of everyone crowding the same one.
STATION_SLACK = 3

# A chef always sees at least this many free counters for setting things
# down, even when the nearest-plus-slack rule would leave fewer.
NEAR_COUNTERS = 2

# Best shared hand-off counters kept for every pair of kitchen areas.
PASS_SHARED = 3

# Handoffs across a pass make the search much harder, so divided kitchens
# plan less at once: two burgers, but one cake.
DIVIDED_WORK_BUDGET = 10


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    """Something a chef holds or a counter carries.

    kind is "food", "plate" (clean, ``food`` is its contents or None),
    "dirty" (a used plate) or "vessel" (``vessel`` is the vessel id).
    """

    kind: str
    food: str | None = None
    vessel: str | None = None


@dataclass
class Vessel:
    """Equipment that holds food: cutboard, pan, pot, oven, fryer, bowl."""

    id: str
    name: str
    portable: bool
    requires: str | None
    content: str | None
    tile: Location | None  # None while a chef holds it
    on_counter: bool  # portable vessel sitting on a plain counter
    self_cooking: bool
    cook_time: int
    elapsed: int  # cook progress on the current content


@dataclass
class Dispenser:
    clean: int
    dirty: int
    infinite: bool
    returns_dirty: bool


@dataclass
class Tile:
    x: int
    y: int
    counter: bool = False  # a plain counter where items can be set down
    item: Held | None = None  # food, plate or dirty plate on the counter
    storage: str | None = None
    delivery: bool = False
    bin: bool = False
    sink: bool = False
    dispenser: Dispenser | None = None
    supports: tuple[str, ...] = ()  # fixed equipment names (stove, mixer)


@dataclass
class Chef:
    id: str
    x: int
    y: int
    held: Held | None


@dataclass
class Order:
    name: str
    deadline: int | None  # last timestep the order is accepted
    reward: int


@dataclass
class Snapshot:
    timestep: int
    walkable: frozenset[Location]
    chefs: list[Chef]
    tiles: dict[Location, Tile]
    vessels: list[Vessel]
    foods: dict[str, bool]  # food name -> may sit on a bare counter (raw)
    cook_recipes: list[tuple[str, str, str]]  # ingredient, equipment, output
    combine_recipes: list[tuple[frozenset[str], str]]
    orders: list[Order]  # visible, not expired, queue order
    strict: bool  # only the first order is accepted
    first_dispenser: Location | None
    timed: bool = False
    extra: dict = field(default_factory=dict)
    sequential: bool = False  # tickets are revealed one at a time


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


def pddl_name(value: str) -> str:
    return re.sub(r"[^0-9a-z_]+", "_", value.lower()).strip("_")


def loc_symbol(location: Location) -> str:
    return f"loc_{location[0]}_{location[1]}"


def parse_loc(symbol: str) -> Location:
    match = re.fullmatch(r"loc_(\d+)_(\d+)", symbol)
    if match is None:
        raise ValueError(f"Unknown location in plan: {symbol}")
    return int(match.group(1)), int(match.group(2))


class Symbols:
    """Two-way mapping between PDDL symbols and kitchen names."""

    def __init__(self, snapshot: Snapshot) -> None:
        self.food: dict[str, str] = {}  # symbol -> name
        self.food_sym: dict[str, str] = {}  # name -> symbol
        names = set(snapshot.foods)
        for _, _, out in snapshot.cook_recipes:
            names.add(out)
        for ingredients, out in snapshot.combine_recipes:
            names |= set(ingredients) | {out}
        for name in sorted(names):
            symbol = "f_" + pddl_name(name)
            self.food[symbol] = name
            self.food_sym[name] = symbol

        self.chef: dict[str, str] = {}
        self.chef_sym: dict[str, str] = {}
        for chef in snapshot.chefs:
            symbol = "c_" + pddl_name(chef.id)
            self.chef[symbol] = chef.id
            self.chef_sym[chef.id] = symbol

        self.vessel: dict[str, str] = {}
        self.vessel_sym: dict[str, str] = {}
        for vessel in snapshot.vessels:
            symbol = "v_" + pddl_name(vessel.id)
            self.vessel[symbol] = vessel.id
            self.vessel_sym[vessel.id] = symbol

        self.vtype_sym: dict[str, str] = {
            vessel.name: "t_" + pddl_name(vessel.name) for vessel in snapshot.vessels
        }

    def content(self, name: str | None) -> str:
        return NOTHING if name is None else self.food_sym[name]

    def content_name(self, symbol: str) -> str | None:
        return None if symbol == NOTHING else self.food[symbol]


# --------------------------------------------------------------------------
# Recipe rules
# --------------------------------------------------------------------------


class Recipes:
    """Answers the same questions the simulator asks when it combines food."""

    def __init__(self, snapshot: Snapshot) -> None:
        self.cook: dict[tuple[str, str], str] = {}
        for ingredient, equipment, output in snapshot.cook_recipes:
            self.cook.setdefault((ingredient, equipment), output)
        self.combine: dict[frozenset[str], str] = {}
        for ingredients, output in snapshot.combine_recipes:
            self.combine.setdefault(ingredients, output)
        self._accepts: dict[tuple[str, str], bool] = {}

    def combine_of(self, a: str, b: str) -> str | None:
        return self.combine.get(frozenset((a, b)))

    def accepts(self, food: str, equipment: str, _seen: frozenset = frozenset()) -> bool:
        """Mirror OvercookedState.equipment_accepts for legal recipes."""
        key = (food, equipment)
        if key in self._accepts:
            return self._accepts[key]
        if (food, equipment) in self.cook:
            self._accepts[key] = True
            return True
        if food in _seen:
            return False
        seen = _seen | {food}
        result = any(
            self.accepts(output, equipment, seen)
            for ingredients, output in self.combine.items()
            if food in ingredients
        )
        if not _seen:
            self._accepts[key] = result
        return result

    def components(self, dish: str) -> set[str]:
        """Every food on any recipe route to ``dish``, including itself."""
        found = {dish}
        stack = [dish]
        while stack:
            output = stack.pop()
            for (ingredient, _), out in self.cook.items():
                if out == output and ingredient not in found:
                    found.add(ingredient)
                    stack.append(ingredient)
            for ingredients, out in self.combine.items():
                if out == output:
                    for ingredient in ingredients - found:
                        found.add(ingredient)
                        stack.append(ingredient)
        return found


# --------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------

NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def flood(walkable: frozenset[Location], start: Location) -> dict[Location, int]:
    """Breadth-first distances over the floor from ``start``."""
    distances = {start: 0}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in NEIGHBOURS:
            cell = (x + dx, y + dy)
            if cell in walkable and cell not in distances:
                distances[cell] = distances[(x, y)] + 1
                queue.append(cell)
    return distances


def dead_end_floor(walkable: frozenset[Location]) -> set[Location]:
    """Floor in pockets and dead-end corridors.

    Repeatedly peel off floor cells with at most one walkable neighbour. A
    chef standing in what gets peeled has only one way out, so another chef
    walking in can trap it, and the navigation policy then raises its
    deadlock error.
    """
    remaining = set(walkable)
    peeled: set[Location] = set()
    changed = True
    while changed:
        changed = False
        for cell in list(remaining):
            x, y = cell
            if sum((x + dx, y + dy) in remaining for dx, dy in NEIGHBOURS) <= 1:
                remaining.discard(cell)
                peeled.add(cell)
                changed = True
    return peeled


def risky_stations(snapshot: Snapshot) -> set[Location]:
    """Tiles that can be worked from a pocket or dead-end corridor."""
    peeled = dead_end_floor(snapshot.walkable)
    return {
        location
        for location in snapshot.tiles
        if any((location[0] + dx, location[1] + dy) in peeled for dx, dy in NEIGHBOURS)
    }


def crowded_chefs(snapshot: Snapshot) -> set[str]:
    """Chefs sharing a floor area with at least two other chefs.

    Two chefs can only box each other in if they stand on the same floor, and
    so far that has only happened with three or more chefs in one room.
    """
    area_of: dict[str, frozenset[Location]] = {}
    for chef in snapshot.chefs:
        area_of[chef.id] = frozenset(flood(snapshot.walkable, (chef.x, chef.y)))
    crowded = set()
    for chef in snapshot.chefs:
        sharing = sum(area_of[other.id] == area_of[chef.id] for other in snapshot.chefs)
        if sharing >= 3:
            crowded.add(chef.id)
    return crowded


def reachable_tiles(snapshot: Snapshot, chef: Chef) -> set[Location]:
    """Tiles a chef can stand next to."""
    floor = flood(snapshot.walkable, (chef.x, chef.y))
    reached = set()
    for location in snapshot.tiles:
        x, y = location
        if any((x + dx, y + dy) in floor for dx, dy in NEIGHBOURS):
            reached.add(location)
    return reached


# --------------------------------------------------------------------------
# Which orders can still be made
# --------------------------------------------------------------------------


def finite_items(snapshot: Snapshot) -> Multiset[str]:
    """Food that exists once in the kitchen and is not replaced by a storage."""
    items: Multiset[str] = Multiset()
    for tile in snapshot.tiles.values():
        if tile.item is not None and tile.item.food is not None:
            items[tile.item.food] += 1
    for vessel in snapshot.vessels:
        if vessel.content is not None:
            items[vessel.content] += 1
    for chef in snapshot.chefs:
        if chef.held is not None and chef.held.food is not None:
            items[chef.held.food] += 1
    return items


# Extra steps added to a dish's cooking estimate when comparing orders in a
# match: walking to storages, the stations and the hatch.
COMPETITIVE_OVERHEAD = 12


def claimed_orders(snapshot: Snapshot, recipes: "Recipes") -> set[int]:
    """Goal recognition: orders another chef is visibly working towards.

    A chef carrying food, a plate of food, or a pan or bowl with food in it
    is assumed to be making the most urgent visible order whose recipe uses
    that food. A finished dish on a plate claims its own order outright. We
    leave those orders to them and pick another, instead of racing for one
    ticket and wasting the work when they deliver first.
    """
    claimed: set[int] = set()
    by_urgency = sorted(
        range(len(snapshot.orders)),
        key=lambda i: (
            snapshot.orders[i].deadline if snapshot.orders[i].deadline is not None else 10**9,
            i,
        ),
    )
    components = {i: recipes.components(snapshot.orders[i].name) for i in by_urgency}
    for other in snapshot.extra.get("others", []):
        held = other.get("held")
        food = getattr(held, "food", None) if held is not None else None
        if food is None:
            continue
        exact = [i for i in by_urgency if i not in claimed and snapshot.orders[i].name == food]
        uses = [i for i in by_urgency if i not in claimed and food in components[i]]
        pick = (exact or uses)[:1]
        claimed.update(pick)
    return claimed


def plan_orders(snapshot: Snapshot, recipes: Recipes) -> list[int]:
    """Return indexes of the orders worth putting in the goal.

    A dish is kept when it can be built from storages (unlimited) plus the
    loose items still in the kitchen (each usable once). This drops orders
    such as a third cake when only two cherries exist and nothing restocks
    them. With strict ordering, an impossible order blocks the ones behind it.
    """
    infinite = {tile.storage for tile in snapshot.tiles.values() if tile.storage}
    budget = finite_items(snapshot)

    def make(food: str, items: Multiset[str], depth: int) -> Multiset[str] | None:
        if food in infinite:
            return items
        if items[food] > 0:
            left = items.copy()
            left[food] -= 1
            return left
        if depth <= 0:
            return None
        for (ingredient, _), output in recipes.cook.items():
            if output == food:
                result = make(ingredient, items, depth - 1)
                if result is not None:
                    return result
        for ingredients, output in recipes.combine.items():
            if output == food and len(ingredients) == 2:
                a, b = sorted(ingredients)
                first = make(a, items, depth - 1)
                if first is not None:
                    second = make(b, first, depth - 1)
                    if second is not None:
                        return second
        return None

    # Plates: one per order unless they come back or can be washed.
    plates = 0
    has_sink = any(tile.sink for tile in snapshot.tiles.values())
    recycled = False
    for tile in snapshot.tiles.values():
        if tile.item is not None and tile.item.kind == "plate":
            plates += 1
        if tile.item is not None and tile.item.kind == "dirty" and has_sink:
            plates += 1
        if tile.dispenser is not None:
            if tile.dispenser.infinite:
                recycled = True
            plates += tile.dispenser.clean
            if has_sink:
                plates += tile.dispenser.dirty
            if not tile.dispenser.returns_dirty or has_sink:
                recycled = True
    for chef in snapshot.chefs:
        if chef.held is not None and chef.held.kind == "plate":
            plates += 1
        if chef.held is not None and chef.held.kind == "dirty" and has_sink:
            plates += 1
    if snapshot.first_dispenser is None:
        recycled = False

    order_ids = list(range(len(snapshot.orders)))
    if not snapshot.strict:
        # Serve the most urgent first when deciding who gets scarce food.
        order_ids.sort(
            key=lambda i: (
                snapshot.orders[i].deadline
                if snapshot.orders[i].deadline is not None
                else 10**9,
                i,
            )
        )
    def work(food: str, depth: int = 12) -> int:
        """Fewest cook and combine steps to make ``food`` from scratch."""
        if food in infinite or depth <= 0:
            return 0
        options = []
        for (ingredient, _), output in recipes.cook.items():
            if output == food:
                options.append(1 + work(ingredient, depth - 1))
        for ingredients, output in recipes.combine.items():
            if output == food:
                options.append(1 + sum(work(i, depth - 1) for i in ingredients))
        return min(options) if options else 0

    regions = {frozenset(reachable_tiles(snapshot, chef)) for chef in snapshot.chefs}
    limit = DIVIDED_WORK_BUDGET if len(regions) > 1 else WORK_BUDGET

    cook_time: dict[str, int] = {}
    for vessel in snapshot.vessels:
        cook_time[vessel.name] = min(cook_time.get(vessel.name, vessel.cook_time), vessel.cook_time)

    def fastest(food: str, depth: int = 12) -> int:
        """Optimistic timesteps to make ``food``, with parallel branches."""
        if food in infinite or budget[food] > 0 or depth <= 0:
            return 0
        options = []
        for (ingredient, equipment), output in recipes.cook.items():
            if output == food:
                options.append(
                    FAST_STEPS_PER_WORK
                    + cook_time.get(equipment, 1)
                    + fastest(ingredient, depth - 1)
                )
        for ingredients, output in recipes.combine.items():
            if output == food:
                branches = [fastest(i, depth - 1) for i in ingredients]
                options.append(FAST_STEPS_PER_WORK + max(branches, default=0))
        return min(options) if options else 0

    def hopeless(index: int) -> bool:
        order = snapshot.orders[index]
        if order.deadline is None:
            return False
        return snapshot.timestep + fastest(order.name) > order.deadline

    max_orders = MAX_ORDERS_PER_PLAN
    if snapshot.extra.get("competitive"):
        # 2v2 match: one chef, one order at a time, best points per step,
        # and stay off orders another chef is visibly working on.
        max_orders = 1
        claimed = claimed_orders(snapshot, recipes)

        def value(i: int) -> tuple:
            order = snapshot.orders[i]
            rate = order.reward / (fastest(order.name) + COMPETITIVE_OVERHEAD)
            return (i in claimed, -rate, i)

        order_ids.sort(key=value)

    chosen: list[int] = []
    skipped: list[int] = []
    # Moving a pan or bowl back where it belongs makes the search much
    # bigger, so plan less recipe work until it is in place.
    spent = 0
    for vessel in snapshot.vessels:
        if vessel.portable and (
            vessel.tile is None or vessel.on_counter or not _supported(snapshot, vessel)
        ):
            spent = limit // 2
            break
    for index in order_ids:
        if not recycled and len(chosen) >= plates:
            break
        # A ticket that cannot be ready in time only wastes the chefs'
        # effort. Under strict ordering it still blocks the queue until it
        # expires, so the next dish waits at the hatch, which is fine.
        if hopeless(index):
            skipped.append(index)
            continue
        cost = work(snapshot.orders[index].name)
        if chosen and (spent + cost > limit or len(chosen) >= max_orders):
            # Leave it for the next plan. Under strict ordering nothing after
            # it can be served first anyway.
            break
        result = make(snapshot.orders[index].name, budget, depth=12)
        if result is None:
            if snapshot.strict:
                break
            continue
        budget = result
        spent += cost
        chosen.append(index)
    if not chosen:
        # Never plan nothing while tickets are open: an empty plan ends the
        # run. Work on the most urgent ticket anyway. Its parts still earn
        # tips, and when it expires the order change triggers a replan.
        for index in skipped:
            if make(snapshot.orders[index].name, budget, depth=12) is not None:
                chosen.append(index)
                break
    return sorted(chosen)


# --------------------------------------------------------------------------
# Problem writer
# --------------------------------------------------------------------------


class Problem:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.facts: dict[str, None] = {}
        self.goals: dict[str, None] = {}

    def obj(self, name: str, kind: str) -> str:
        self.objects.setdefault(name, kind)
        return name

    def fact(self, text: str) -> None:
        self.facts[text] = None

    def goal(self, text: str) -> None:
        self.goals[text] = None

    def text(self) -> str:
        objects = "".join(f"    {n} - {t}\n" for n, t in self.objects.items())
        facts = "".join(f"    {f}\n" for f in self.facts)
        goals = "".join(f"      {g}\n" for g in self.goals)
        return (
            "(define (problem kitchen)\n"
            "  (:domain overcooked)\n"
            f"  (:objects\n{objects}  )\n"
            f"  (:init\n{facts}  )\n"
            f"  (:goal\n    (and\n{goals}    )\n  )\n"
            ")\n"
        )


@dataclass
class BuildResult:
    problem: str
    symbols: Symbols
    chosen_orders: list[int]


def build_problem(snapshot: Snapshot) -> BuildResult:
    symbols = Symbols(snapshot)
    recipes = Recipes(snapshot)
    problem = Problem()
    chosen = plan_orders(snapshot, recipes)

    # Food that matters for the chosen orders. Rules are only written for
    # these foods, which keeps grounding small and stops the planner from
    # cooking things nobody ordered (like burnt meat).
    useful: set[str] = set()
    for index in chosen:
        useful |= recipes.components(snapshot.orders[index].name)

    for symbol in symbols.food:
        problem.obj(symbol, "food")

    describe_chefs(snapshot, symbols, problem)
    describe_tiles(snapshot, symbols, problem)
    describe_vessels(snapshot, symbols, problem)
    describe_station_use(snapshot, symbols, problem)
    describe_rules(snapshot, symbols, recipes, useful, problem)
    describe_orders(snapshot, symbols, chosen, problem)
    describe_prep(snapshot, symbols, recipes, chosen, problem)
    return BuildResult(problem=problem.text(), symbols=symbols, chosen_orders=chosen)


def working_area(snapshot: Snapshot, reach: dict[str, set[Location]]) -> dict[str, set[Location]]:
    """Trim each chef's reachable tiles to the ones it should actually use.

    Lama-first ignores distance, so with every tile in reach it sends chefs
    across the room for a storage or counter that also exists right next to
    them. That wastes time and makes chefs crowd each other. For anything
    the kitchen has several of (storages of one food, delivery hatches,
    bins, empty counters, the same item on several counters) a chef keeps
    the nearest one plus STATION_SLACK steps, and at least NEAR_COUNTERS
    empty counters. Pass counters shared by chefs in different areas,
    stations and anything unique are always kept.
    """
    regions = {chef.id: frozenset(reach[chef.id]) for chef in snapshot.chefs}
    region_sets = set(regions.values())
    passes = {
        location
        for location, tile in snapshot.tiles.items()
        if tile.counter and sum(location in region for region in region_sets) > 1
    }
    occupied = {v.tile for v in snapshot.vessels if v.tile is not None}

    def group(location: Location, tile: Tile):
        if location in occupied or tile.supports:
            return None  # stations: always kept (may_use handles vessels)
        if tile.storage:
            return ("storage", tile.storage)
        if tile.delivery:
            return ("delivery",)
        if tile.bin:
            return ("bin",)
        if tile.sink:
            return ("sink",)
        if tile.dispenser is not None:
            return ("dispenser",)
        if tile.counter and tile.item is not None:
            return ("item", tile.item.kind, tile.item.food)
        if tile.counter:
            return ("counter",)
        return None

    def distance_from(chef: Chef):
        floor = flood(snapshot.walkable, (chef.x, chef.y))

        def distance(location: Location) -> int:
            x, y = location
            near = [floor[(x + dx, y + dy)] for dx, dy in NEIGHBOURS if (x + dx, y + dy) in floor]
            return min(near) if near else 10**6

        return distance

    distance_of = {chef.id: distance_from(chef) for chef in snapshot.chefs}

    # Pass counters. One already holding something stays usable. Empty ones
    # are trimmed like other counters, but every pair of areas also keeps
    # its best shared hand-off spots (lowest walking for both sides), so
    # two chefs always have somewhere to pass food.
    def empty_pass(location: Location) -> bool:
        tile = snapshot.tiles[location]
        return location in passes and tile.item is None and location not in occupied

    shared_best: set[Location] = set()
    region_list = list(region_sets)
    for i, first in enumerate(region_list):
        for second in region_list[i + 1 :]:
            common = [p for p in first & second if empty_pass(p)]
            if not common:
                continue

            def side_cost(region, location):
                costs = [
                    distance_of[c.id](location)
                    for c in snapshot.chefs
                    if regions[c.id] == region
                ]
                return min(costs) if costs else 10**6

            common.sort(key=lambda p: (side_cost(first, p) + side_cost(second, p), p))
            shared_best |= set(common[:PASS_SHARED])

    area: dict[str, set[Location]] = {}
    for chef in snapshot.chefs:
        distance = distance_of[chef.id]
        keep: set[Location] = set()
        groups: dict[tuple, list[tuple[int, Location]]] = {}
        own_passes: list[tuple[int, Location]] = []
        for location in reach[chef.id]:
            if empty_pass(location):
                own_passes.append((distance(location), location))
                continue
            key = group(location, snapshot.tiles[location])
            if key is None or location in passes:
                keep.add(location)
            else:
                groups.setdefault(key, []).append((distance(location), location))
        for key, members in groups.items():
            members.sort()
            nearest = members[0][0]
            keep |= {location for d, location in members if d <= nearest + STATION_SLACK}
            if key == ("counter",):
                keep |= {location for _, location in members[:NEAR_COUNTERS]}
        if own_passes:
            own_passes.sort()
            nearest = own_passes[0][0]
            keep |= {p for d, p in own_passes if d <= nearest + STATION_SLACK}
            keep |= {p for _, p in own_passes} & shared_best
        area[chef.id] = keep
    return area


def burning_duty(snapshot: Snapshot, reach: dict[str, set[Location]]) -> dict[str, str]:
    """Pick a chef to catch food already cooking in a pan that burns.

    A replan would otherwise forget that meat in a timed pan has to come out
    soon. We prefer a chef already holding a plate, then a free chef, and
    the nearest one. Chefs holding anything else are not chosen, since a
    chef on duty cannot put things down.
    """
    recipes = Recipes(snapshot)
    useful: set[str] = set()
    for order in snapshot.orders:
        useful |= recipes.components(order.name)
    duty: dict[str, str] = {}
    if not snapshot.timed:
        return duty
    for vessel in snapshot.vessels:
        if not vessel.self_cooking or vessel.tile is None or vessel.content is None:
            continue
        cooked = recipes.cook.get((vessel.content, vessel.name))
        burnt = recipes.cook.get((cooked, vessel.name)) if cooked else None
        at_risk = (
            cooked in useful and burnt is not None and burnt not in useful
        ) or (vessel.content in useful and cooked is not None and cooked not in useful)
        if not at_risk:
            continue
        options = []
        for chef in snapshot.chefs:
            if chef.id in duty or vessel.tile not in reach[chef.id]:
                continue
            if chef.held is not None and chef.held.kind != "plate":
                continue
            holding_plate = chef.held is not None
            distance = station_distance(snapshot, chef, vessel.tile)
            options.append((not holding_plate, distance if distance is not None else 10**6, chef.id))
        if options:
            duty[min(options)[2]] = vessel.id
    return duty


def describe_chefs(snapshot: Snapshot, symbols: Symbols, problem: Problem) -> None:
    reach = {chef.id: reachable_tiles(snapshot, chef) for chef in snapshot.chefs}
    area = working_area(snapshot, reach)
    for chef in snapshot.chefs:
        for other in snapshot.chefs:
            if other.id != chef.id:
                problem.fact(
                    f"(other {symbols.chef_sym[chef.id]} {symbols.chef_sym[other.id]})"
                )
    # A lone chef has to catch burning food itself.
    for vessel in snapshot.vessels:
        if vessel.self_cooking and vessel.tile is not None:
            helpers = [c for c in snapshot.chefs if vessel.tile in reach[c.id]]
            if len(helpers) < 2:
                problem.fact("(solo_watch)")
                break
    duty = burning_duty(snapshot, reach)
    for chef in snapshot.chefs:
        c = problem.obj(symbols.chef_sym[chef.id], "chef")
        if chef.id in duty:
            # Food is already cooking in a pan and will burn: this chef
            # must catch it before doing anything else.
            problem.fact(f"(on_duty {c} {symbols.vessel_sym[duty[chef.id]]})")
        else:
            problem.fact(f"(off_duty {c})")
        for location in sorted(area[chef.id]):
            problem.fact(f"(reach {c} {problem.obj(loc_symbol(location), 'loc')})")
        held = chef.held
        if held is None:
            problem.fact(f"(free {c})")
        elif held.kind == "food":
            problem.fact(f"(has_food {c} {symbols.food_sym[held.food]})")
        elif held.kind == "plate":
            problem.fact(f"(has_plate {c} {symbols.content(held.food)})")
        elif held.kind == "dirty":
            problem.fact(f"(has_dirty {c})")
        elif held.kind == "vessel":
            problem.fact(f"(has_vessel {c} {symbols.vessel_sym[held.vessel]})")


def describe_tiles(snapshot: Snapshot, symbols: Symbols, problem: Problem) -> None:
    # Count objects bound the number of plates a dispenser can hold.
    total_plates = 0
    for tile in snapshot.tiles.values():
        if tile.dispenser is not None:
            total_plates += tile.dispenser.clean + tile.dispenser.dirty
        if tile.item is not None and tile.item.kind in ("plate", "dirty"):
            total_plates += 1
    for chef in snapshot.chefs:
        if chef.held is not None and chef.held.kind in ("plate", "dirty"):
            total_plates += 1
    counts = [problem.obj(f"n{i}", "count") for i in range(total_plates + 1)]
    problem.fact(f"(zero {counts[0]})")
    for low, high in zip(counts, counts[1:]):
        problem.fact(f"(succ {low} {high})")

    # A pan or bowl set on a plain counter also occupies it.
    vessel_counters = {
        vessel.tile for vessel in snapshot.vessels if vessel.on_counter
    }
    for location, tile in sorted(snapshot.tiles.items()):
        l = problem.obj(loc_symbol(location), "loc")
        if tile.counter:
            problem.fact(f"(counter {l})")
            item = tile.item
            if item is None:
                if location not in vessel_counters:
                    problem.fact(f"(clear {l})")
            elif item.kind == "food":
                problem.fact(f"(food_at {l} {symbols.food_sym[item.food]})")
            elif item.kind == "plate":
                problem.fact(f"(plate_at {l} {symbols.content(item.food)})")
            elif item.kind == "dirty":
                problem.fact(f"(dirty_at {l})")
        if tile.storage:
            problem.fact(f"(storage {l} {symbols.food_sym[tile.storage]})")
        if tile.delivery:
            problem.fact(f"(delivery {l})")
        if tile.bin:
            problem.fact(f"(bin {l})")
        if tile.sink:
            problem.fact(f"(sink {l})")
        if tile.dispenser is not None:
            d = tile.dispenser
            problem.fact(f"(dispenser {l})")
            if d.infinite:
                problem.fact(f"(disp_inf {l})")
            problem.fact(f"(disp_clean {l} {counts[min(d.clean, total_plates)]})")
            problem.fact(f"(disp_dirty {l} {counts[min(d.dirty, total_plates)]})")

    first = snapshot.first_dispenser
    dispenser = snapshot.tiles[first].dispenser if first is not None else None
    if dispenser is None or dispenser.infinite:
        problem.fact("(ret_none)")
    elif dispenser.returns_dirty:
        problem.fact(f"(ret_dirty {loc_symbol(first)})")
    else:
        problem.fact(f"(ret_clean {loc_symbol(first)})")


def usable_plates(snapshot: Snapshot) -> int | None:
    """Plates a chef could still use, or None when the supply is unlimited."""
    has_sink = any(tile.sink for tile in snapshot.tiles.values())
    count = 0
    for tile in snapshot.tiles.values():
        if tile.dispenser is not None:
            if tile.dispenser.infinite:
                return None
            count += tile.dispenser.clean + (tile.dispenser.dirty if has_sink else 0)
        if tile.item is not None and tile.item.kind == "plate":
            count += 1
        if tile.item is not None and tile.item.kind == "dirty" and has_sink:
            count += 1
    for chef in snapshot.chefs:
        if chef.held is not None and chef.held.kind == "plate":
            count += 1
        if chef.held is not None and chef.held.kind == "dirty" and has_sink:
            count += 1
    return count


def station_distance(snapshot: Snapshot, chef: Chef, location: Location) -> int | None:
    """Steps for a chef to stand next to ``location``, or None if it cannot."""
    floor = flood(snapshot.walkable, (chef.x, chef.y))
    x, y = location
    steps = [floor[(x + dx, y + dy)] for dx, dy in NEIGHBOURS if (x + dx, y + dy) in floor]
    return min(steps) if steps else None


def movable_vessels(snapshot: Snapshot) -> set[str]:
    """Pans and bowls the plan may carry: off their support, or plates scarce."""
    plates = usable_plates(snapshot)
    scarce = plates is not None and plates <= 1
    return {
        vessel.id
        for vessel in snapshot.vessels
        if vessel.portable
        and (
            scarce
            or vessel.tile is None
            or vessel.on_counter
            or not _supported(snapshot, vessel)
        )
    }


def describe_station_use(snapshot: Snapshot, symbols: Symbols, problem: Problem) -> None:
    movable = movable_vessels(snapshot)
    for chef in snapshot.chefs:
        c = symbols.chef_sym[chef.id]
        distance = {
            vessel.id: station_distance(snapshot, chef, vessel.tile)
            for vessel in snapshot.vessels
            if vessel.tile is not None
        }
        nearest: dict[str, int] = {}
        for vessel in snapshot.vessels:
            d = distance.get(vessel.id)
            if d is not None:
                nearest[vessel.name] = min(nearest.get(vessel.name, d), d)
        for vessel in snapshot.vessels:
            v = symbols.vessel_sym[vessel.id]
            if vessel.id in movable:
                # It may be carried somewhere else, so anyone may use it.
                problem.fact(f"(may_use {c} {v})")
                continue
            d = distance.get(vessel.id)
            if d is not None and d <= nearest[vessel.name] + STATION_SLACK:
                problem.fact(f"(may_use {c} {v})")


def describe_vessels(snapshot: Snapshot, symbols: Symbols, problem: Problem) -> None:
    # With one plate or fewer, plates cannot double as carriers, so chefs
    # may carry pans and bowls and bin food to free the last plate.
    plates = usable_plates(snapshot)
    scarce = plates is not None and plates <= 1
    if scarce:
        problem.fact("(scarce)")
    requires_by_type: dict[str, str] = {}
    movable = movable_vessels(snapshot)
    reach = {chef.id: reachable_tiles(snapshot, chef) for chef in snapshot.chefs}
    watched = set(burning_duty(snapshot, reach).values())
    for vessel in snapshot.vessels:
        v = problem.obj(symbols.vessel_sym[vessel.id], "vessel")
        t = problem.obj(symbols.vtype_sym[vessel.name], "vtype")
        problem.fact(f"(v_type {v} {t})")
        problem.fact(f"(v_has {v} {symbols.content(vessel.content)})")
        if vessel.id not in watched:
            problem.fact(f"(unwatched {v})")
        if vessel.portable:
            # Every plan ends with pans and bowls set down. The controller
            # will not replan while a chef holds one.
            problem.goal(f"(down {v})")
            if vessel.tile is not None:
                problem.fact(f"(down {v})")
        problem.fact(f"({'auto' if vessel.self_cooking else 'manual'} {t})")
        if vessel.portable:
            problem.fact(f"(portable {v})")
            # A pan already on its stove stays there. Only a vessel that
            # starts somewhere else (on a counter, or in a chef's hands) is
            # worth moving, and that keeps the search small.
            if vessel.id in movable:
                problem.fact(f"(movable {v})")
            if vessel.requires:
                requires_by_type[vessel.name] = vessel.requires
        else:
            problem.fact(f"(fixed {v})")
        if vessel.tile is not None:
            problem.fact(f"(v_at {v} {problem.obj(loc_symbol(vessel.tile), 'loc')})")
            if vessel.on_counter:
                problem.fact(f"(v_counter {v})")
            elif not vessel.portable or _supported(snapshot, vessel):
                problem.fact(f"(works {v})")

    # Supports: a stove takes a pan, a mixer takes a bowl.
    occupied = {
        vessel.tile
        for vessel in snapshot.vessels
        if vessel.portable and vessel.tile is not None and not vessel.on_counter
    }
    for location, tile in sorted(snapshot.tiles.items()):
        types = [
            symbols.vtype_sym[name]
            for name, needed in sorted(requires_by_type.items())
            if needed in tile.supports
        ]
        if not types:
            continue
        l = problem.obj(loc_symbol(location), "loc")
        for t in types:
            problem.fact(f"(support_at {l} {t})")
        if location not in occupied:
            problem.fact(f"(support_free {l})")


def _supported(snapshot: Snapshot, vessel: Vessel) -> bool:
    if not vessel.requires:
        return True
    tile = snapshot.tiles.get(vessel.tile) if vessel.tile else None
    return tile is not None and vessel.requires in tile.supports


def describe_rules(
    snapshot: Snapshot,
    symbols: Symbols,
    recipes: Recipes,
    useful: set[str],
    problem: Problem,
) -> None:
    sym = symbols.food_sym
    foods = sorted(name for name in symbols.food_sym if name in useful)

    for name in foods:
        if snapshot.foods.get(name, True):
            problem.fact(f"(counter_ok {sym[name]})")
    for name in sorted(symbols.food_sym):
        if name not in useful:
            problem.fact(f"(junk {sym[name]})")

    # Plating: an empty plate takes any food; a plate with food combines.
    for a in foods:
        problem.fact(f"(plating {NOTHING} {sym[a]} {sym[a]})")
        for x in foods:
            out = recipes.combine_of(x, a)
            if out in useful:
                problem.fact(f"(plating {sym[x]} {sym[a]} {sym[out]})")

    # Combining loose food on a bare counter only works for counter-safe food.
    for a in foods:
        for b in foods:
            out = recipes.combine_of(a, b)
            if out in useful and snapshot.foods.get(out, True):
                problem.fact(f"(counter_combo {sym[a]} {sym[b]} {sym[out]})")

    types = sorted({v.name for v in snapshot.vessels})
    self_cooking = {v.name for v in snapshot.vessels if v.self_cooking}
    for equipment in types:
        t = symbols.vtype_sym[equipment]

        def put(a: str, y: str | None) -> str | None:
            if y is None:
                return a if recipes.accepts(a, equipment) else None
            out = recipes.combine_of(a, y)
            return out if out is not None and recipes.accepts(out, equipment) else None

        def burns(content: str) -> bool:
            # Food that cooks here into something useful, which then keeps
            # cooking into something useless.
            if not snapshot.timed or equipment not in self_cooking:
                return False
            cooked = recipes.cook.get((content, equipment))
            if cooked is None or cooked not in useful:
                return False
            after = recipes.cook.get((cooked, equipment))
            return after is not None and after not in useful

        def spoils(content: str) -> bool:
            # Food that would cook into something useless if put here.
            if not snapshot.timed or equipment not in self_cooking:
                return False
            cooked = recipes.cook.get((content, equipment))
            return cooked is not None and cooked not in useful

        for a in foods:
            for y in [None, *foods]:
                out = put(a, y)
                if out is not None and out in useful and not spoils(out):
                    kind = "watch_rule" if burns(out) else "put_rule"
                    problem.fact(
                        f"({kind} {t} {sym[a]} {symbols.content(y)} {sym[out]})"
                    )
                elif y is not None:
                    grab = recipes.combine_of(y, a)
                    if grab in useful and put(a, y) is None:
                        problem.fact(f"(grab_rule {t} {sym[a]} {sym[y]} {sym[grab]})")

        # Plate held at a vessel: pour into the vessel if it accepts the
        # result, otherwise take the vessel's food onto the plate.
        for x in foods:
            for y in [None, *foods]:
                out = put(x, y)
                if (
                    out is not None
                    and out in useful
                    and not burns(out)
                    and not spoils(out)
                ):
                    problem.fact(
                        f"(pour_rule {t} {sym[x]} {symbols.content(y)} {sym[out]})"
                    )
        for y in foods:
            for x in [None, *foods]:
                if x is not None and put(x, y) is not None:
                    continue
                out = y if x is None else recipes.combine_of(y, x)
                if out in useful:
                    problem.fact(
                        f"(scoop_rule {t} {symbols.content(x)} {sym[y]} {sym[out]})"
                    )

        for (ingredient, station), out in recipes.cook.items():
            if station == equipment and out in useful and ingredient in sym:
                problem.fact(f"(cook_rule {t} {sym[ingredient]} {sym[out]})")


def describe_prep(
    snapshot: Snapshot,
    symbols: Symbols,
    recipes: Recipes,
    chosen: list[int],
    problem: Problem,
) -> None:
    """Keep a slow, shared ingredient ready when tickets come one at a time.

    With sequential tickets the next order is unknown until the current one
    is served, and a short deadline leaves no time to wait for a pot. If
    every dish on the menu needs the same ingredient that a station cooks by
    itself, and it does not burn there, we add a goal to leave one batch
    ready in that station. Idle chefs do it while the current order cooks.
    """
    if not snapshot.sequential or not snapshot.timed or len(chosen) != 1:
        return
    ingredients = {i for pair, _ in recipes.combine.items() for i in pair}
    ingredients |= {i for (i, _) in recipes.cook}
    outputs = set(recipes.combine.values()) | set(recipes.cook.values())
    dishes = outputs - ingredients
    if not dishes:
        return
    common = set.intersection(*(recipes.components(d) for d in dishes))
    best = None
    for (ingredient, equipment), output in recipes.cook.items():
        if output not in common or (output, equipment) in recipes.cook:
            continue  # not shared, or it would keep cooking (burn)
        for vessel in snapshot.vessels:
            if vessel.name != equipment or not vessel.self_cooking:
                continue
            if vessel.tile is None or vessel.cook_time < 3:
                continue
            if best is None or vessel.cook_time > best[0]:
                best = (vessel.cook_time, vessel, output)
    if best is None:
        return
    _, vessel, food = best
    if food not in symbols.food_sym:
        return
    problem.goal(f"(v_has {symbols.vessel_sym[vessel.id]} {symbols.food_sym[food]})")


def describe_orders(
    snapshot: Snapshot, symbols: Symbols, chosen: list[int], problem: Problem
) -> None:
    order_syms = []
    for index in chosen:
        order = snapshot.orders[index]
        o = problem.obj(f"o{index}", "order")
        order_syms.append(o)
        problem.fact(f"(wants {o} {symbols.food_sym[order.name]})")
        problem.fact(f"(open {o})")
        problem.goal(f"(served {o})")
    if snapshot.strict:
        # Only the first order is accepted; each delivery unlocks the next.
        if order_syms:
            problem.fact(f"(ready {order_syms[0]})")
        for o, p in zip(order_syms, order_syms[1:] + [ORDER_END]):
            problem.fact(f"(next {o} {p})")
    else:
        # Deliver in deadline order. This only shapes the plan, so the most
        # urgent dish is made first; when the chefs run it, any ready dish
        # can still go out as soon as it matches a ticket.
        by_deadline = sorted(
            zip(chosen, order_syms),
            key=lambda pair: (
                snapshot.orders[pair[0]].deadline
                if snapshot.orders[pair[0]].deadline is not None
                else 10**9,
                pair[0],
            ),
        )
        ordered = [o for _, o in by_deadline]
        if ordered:
            problem.fact(f"(ready {ordered[0]})")
        for o, p in zip(ordered, ordered[1:] + [ORDER_END]):
            problem.fact(f"(next {o} {p})")
