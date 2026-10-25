"""Reassign plan work between chefs so the kitchen finishes sooner.

Lama-first ignores distance and treats chefs as interchangeable, so a plan
often sends the chef on the far side of the kitchen to fetch rice that sits
next to another chef. We fix that after planning:

1. Estimate the plan's makespan: each chef walks to its next target, waits
   for the steps (by any chef) that produce what it needs, and stations add
   their cook times.
2. Split each chef's steps into chunks that start and end with empty hands.
3. Try giving each chunk to another chef, and try swapping whole chefs. A
   change is kept only if the plan is still valid when replayed in PDDL
   (reach, free hands, station rules) and the estimate drops.

The plan keeps its step order, so everything after this (round splitting,
translation) works the same.
"""

from __future__ import annotations

import itertools
import time
from collections import deque

from .model import NEIGHBOURS, Location, Snapshot, Symbols, parse_loc
from .pddl_sim import Domain, Fact

Step = tuple[str, tuple[str, ...]]

TIME_BUDGET_SECONDS = 1.5
CHEF_ACTIONS_WITHOUT_CHEF = {"auto_cook"}


class Estimator:
    def __init__(self, snapshot: Snapshot, symbols: Symbols, domain: Domain) -> None:
        self.walkable = snapshot.walkable
        self.domain = domain
        self.start = {
            symbols.chef_sym[chef.id]: (chef.x, chef.y) for chef in snapshot.chefs
        }
        self.cook_time = {
            symbols.vessel_sym[vessel.id]: max(1, vessel.cook_time)
            for vessel in snapshot.vessels
        }
        self._bfs: dict[Location, dict[Location, int]] = {}
        self._approach: dict[tuple[Location, Location], tuple[int, Location]] = {}

    def distances(self, source: Location) -> dict[Location, int]:
        if source not in self._bfs:
            dist = {source: 0}
            queue = deque([source])
            while queue:
                x, y = queue.popleft()
                for dx, dy in NEIGHBOURS:
                    cell = (x + dx, y + dy)
                    if cell in self.walkable and cell not in dist:
                        dist[cell] = dist[(x, y)] + 1
                        queue.append(cell)
            self._bfs[source] = dist
        return self._bfs[source]

    def approach(self, source: Location, target: Location) -> tuple[int, Location]:
        """Steps to stand next to ``target``, and where the chef ends up."""
        key = (source, target)
        if key not in self._approach:
            dist = self.distances(source)
            tx, ty = target
            options = [
                (dist[(tx + dx, ty + dy)], (tx + dx, ty + dy))
                for dx, dy in NEIGHBOURS
                if (tx + dx, ty + dy) in dist
            ]
            self._approach[key] = min(options) if options else (10**4, source)
        return self._approach[key]

    def makespan(self, init: set[Fact], steps: list[Step]) -> int | None:
        """Parallel finish time, or None if the plan is invalid in sequence."""
        state = set(init)
        ready: dict[Fact, int] = {}
        position = dict(self.start)
        clock = {chef: 0 for chef in self.start}
        finish = 0
        for name, args in steps:
            pre, add, delete = self.domain.ground(name, args)
            if not pre <= state:
                return None
            state = (state - delete) | add
            dependency = max((ready.get(fact, 0) for fact in pre), default=0)
            if name in CHEF_ACTIONS_WITHOUT_CHEF:
                done = dependency + self.cook_time.get(args[0], 1)
            else:
                chef = args[0]
                start = clock.get(chef, 0)
                tile = self.domain.location(name, args)
                if tile is not None:
                    walk, spot = self.approach(position[chef], parse_loc(tile))
                    position[chef] = spot
                    start += walk
                presses = self.cook_time.get(args[1], 1) if name == "cook" else 1
                done = max(start, dependency) + presses
                clock[chef] = done
            for fact in add:
                ready[fact] = done
            finish = max(finish, done)
        return finish


def _chunks(domain: Domain, init: set[Fact], steps: list[Step]) -> list[list[int]]:
    """Group each chef's steps into runs that start with empty hands."""
    state = set(init)
    current: dict[str, list[int]] = {}
    chunks: list[list[int]] = []
    for index, (name, args) in enumerate(steps):
        if name in CHEF_ACTIONS_WITHOUT_CHEF:
            pre, add, delete = domain.ground(name, args)
            state = (state - delete) | add
            continue
        chef = args[0]
        if ("free", chef) in state and ("off_duty", chef) in state:
            if current.get(chef):
                chunks.append(current[chef])
            current[chef] = []
        current.setdefault(chef, []).append(index)
        pre, add, delete = domain.ground(name, args)
        state = (state - delete) | add
    chunks.extend(chunk for chunk in current.values() if chunk)
    return chunks


# Steps that add one food to the plate a chef holds: plate-content argument,
# food argument and result argument positions.
COLLECT = {
    "scoop": (4, 5, 6),
    "take_food_to_plate": (3, 2, 4),
    "plate_takes_food": (2, 3, 4),
}


def _collect_runs(steps: list[Step]) -> list[list[int]]:
    """Each chef's consecutive steps that build one dish on a held plate."""
    by_chef: dict[str, list[int]] = {}
    for index, (name, args) in enumerate(steps):
        if name not in CHEF_ACTIONS_WITHOUT_CHEF:
            by_chef.setdefault(args[0], []).append(index)
    runs = []
    for indexes in by_chef.values():
        run: list[int] = []
        for index in indexes:
            name, args = steps[index]
            if name in COLLECT:
                if run:
                    prev_name, prev_args = steps[run[-1]]
                    linked = prev_args[COLLECT[prev_name][2]] == args[COLLECT[name][0]]
                    if not linked:
                        if len(run) > 1:
                            runs.append(run)
                        run = []
                run.append(index)
            else:
                if len(run) > 1:
                    runs.append(run)
                run = []
        if len(run) > 1:
            runs.append(run)
    return runs


def _reorder_collects(
    estimator: "Estimator", init: set[Fact], steps: list[Step], best_time: int
) -> tuple[list[Step], int]:
    """Try every order of picking up a dish's parts; keep the fastest."""
    scoop = {(f[1], f[2], f[3]): f[4] for f in init if f[0] == "scoop_rule"}
    plating = {(f[1], f[2]): f[3] for f in init if f[0] == "plating"}

    def rebuild(step: Step, content: str) -> Step | None:
        name, args = step
        x_at, food_at, out_at = COLLECT[name]
        if name == "scoop":
            out = scoop.get((args[3], content, args[food_at]))
        else:
            out = plating.get((content, args[food_at]))
        if out is None:
            return None
        new = list(args)
        new[x_at] = content
        new[out_at] = out
        return (name, tuple(new))

    best = steps
    for run in _collect_runs(steps):
        if len(run) > 5:
            continue
        original = [best[i] for i in run]
        start = original[0][1][COLLECT[original[0][0]][0]]
        final = original[-1][1][COLLECT[original[-1][0]][2]]
        for order in itertools.permutations(original):
            if list(order) == original:
                continue
            content, rebuilt = start, []
            for step in order:
                new = rebuild(step, content)
                if new is None:
                    break
                rebuilt.append(new)
                content = new[1][COLLECT[new[0]][2]]
            else:
                if content != final:
                    continue
                trial = list(best)
                for slot, new in zip(run, rebuilt):
                    trial[slot] = new
                value = estimator.makespan(init, trial)
                if value is not None and value < best_time:
                    best, best_time = trial, value
    return best, best_time


def _units(domain: Domain, init: set[Fact], steps: list[Step]) -> list[list[int]]:
    units = _chunks(domain, init, steps)
    units += [[i] for i, (name, _) in enumerate(steps) if name == "cook"]
    return units


def _placements(steps: list[Step], unit: list[int]):
    """The plan as is, then with the unit moved as one block to each position."""
    yield steps
    moved = [steps[i] for i in unit]
    rest = [step for i, step in enumerate(steps) if i not in set(unit)]
    for position in range(len(rest) + 1):
        if position == unit[0]:
            continue
        yield rest[:position] + moved + rest[position:]


def _relabel(steps: list[Step], indexes: list[int], old: str, new: str) -> list[Step]:
    changed = list(steps)
    for index in indexes:
        name, args = changed[index]
        if args and args[0] == old:
            changed[index] = (name, (new, *args[1:]))
    return changed


def _swap_chefs(steps: list[Step], mapping: dict[str, str]) -> list[Step]:
    return [
        (name, tuple(mapping.get(arg, arg) for arg in args)) for name, args in steps
    ]


def reassign(
    snapshot: Snapshot,
    symbols: Symbols,
    domain: Domain,
    init: set[Fact],
    steps: list[Step],
) -> list[Step]:
    """Return a version of ``steps`` with a shorter estimated makespan."""
    chefs = [symbols.chef_sym[chef.id] for chef in snapshot.chefs]
    if len(chefs) < 2 or not steps:
        return steps
    estimator = Estimator(snapshot, symbols, domain)
    best = steps
    best_time = estimator.makespan(init, steps)
    if best_time is None:
        return steps
    deadline = time.monotonic() + TIME_BUDGET_SECONDS

    # Whole-chef swaps first: cheap, and fix the common "mirror image" case.
    if len(chefs) <= 4:
        for perm in itertools.permutations(chefs):
            mapping = dict(zip(chefs, perm))
            if all(k == v for k, v in mapping.items()):
                continue
            trial = _swap_chefs(steps, mapping)
            value = estimator.makespan(init, trial)
            if value is not None and value < best_time:
                best, best_time = trial, value

    best, best_time = _reorder_collects(estimator, init, best, best_time)

    # Then move units of work to whichever chef finishes them sooner. A unit
    # is a chunk (empty hands to empty hands) or a single hand-worked press,
    # which needs no free hands. A moved unit may also be re-inserted
    # earlier or later in the plan, since the new chef may only be free then.
    improved = True
    while improved and time.monotonic() < deadline:
        improved = False
        for unit in _units(domain, init, best):
            if time.monotonic() >= deadline:
                break
            owner = best[unit[0]][1][0]
            candidate = None
            for chef in chefs:
                if chef == owner:
                    continue
                relabelled = _relabel(best, unit, owner, chef)
                for trial in _placements(relabelled, unit):
                    value = estimator.makespan(init, trial)
                    if value is not None and value < best_time and (
                        candidate is None or value < candidate[0]
                    ):
                        candidate = (value, trial)
            if candidate is not None:
                best_time, best = candidate
                improved = True
                break
        if not improved and time.monotonic() < deadline:
            reordered, value = _reorder_collects(estimator, init, best, best_time)
            if value < best_time:
                best, best_time = reordered, value
                improved = True
    return best
