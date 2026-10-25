"""Split work between independent cells of chefs.

In a parallel kitchen (two full sets of stations, a pair of chefs at each)
lama-first tends to cook every order on one side and leave the other pair
idle, because it ignores how long things take. Here we group chefs into
cells by the stations they may use, give the orders to the cells in turn
(most urgent first), and plan each cell on its own. Each cell is planned
from the state the previous cell's plan leaves behind, so they never fight
over plates or counters. The merged plan is checked in PDDL and only used
when its estimated finish time beats the normal plan.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .pddl_sim import Domain, Fact, goal_facts, initial_state

Step = tuple[str, tuple[str, ...]]
Solve = Callable[[str], list[Step] | None]


def cells(init: set[Fact]) -> list[list[str]]:
    """Group chefs that may use exactly the same stations."""
    chefs = sorted({f[1] for f in init if f[0] in ("free", "off_duty", "on_duty")})
    uses: dict[str, frozenset[str]] = {
        chef: frozenset(f[2] for f in init if f[0] == "may_use" and f[1] == chef)
        for chef in chefs
    }
    groups: dict[frozenset[str], list[str]] = {}
    for chef in chefs:
        groups.setdefault(uses[chef], []).append(chef)
    return list(groups.values())


def _with(problem: str, init: set[Fact], goals: set[Fact]) -> str:
    facts = "".join(f"    ({' '.join(f)})\n" for f in sorted(init))
    goal_text = "".join(f"      ({' '.join(g)})\n" for g in sorted(goals))
    head = problem[: problem.index("(:init")]
    return f"{head}(:init\n{facts}  )\n  (:goal\n    (and\n{goal_text}    )\n  )\n)\n"


def decompose(
    domain: Domain, problem: str, solve: Solve, orders_by_urgency: list[str]
) -> list[Step] | None:
    """Plan each cell's share of the orders; None if it does not apply."""
    init = initial_state(problem)
    goals = goal_facts(problem)
    groups = [g for g in cells(init) if g]
    served = [g for g in goals if g[0] == "served"]
    if len(groups) < 2 or len(served) < 2:
        return None
    # Deliveries no longer wait on each other across cells.
    base = {f for f in init if f[0] not in ("ready", "next")}
    orders = [o for o in orders_by_urgency if ("served", o) in goals]
    base |= {("ready", o) for o in orders}
    base |= {("next", o, "o_end") for o in orders}

    # Most urgent order goes to the first cell, the next to the second, ...
    share: list[list[str]] = [[] for _ in groups]
    for i, order in enumerate(orders):
        share[i % len(groups)].append(order)

    other_goals = {g for g in goals if g[0] != "served"}
    state = set(base)
    merged: list[Step] = []
    for group, mine in zip(groups, share):
        if not mine:
            continue
        others = {c for g in groups if g is not group for c in g}
        visible = {f for f in state if not (set(f[1:]) & others)}
        sub_goals = {("served", o) for o in mine} | other_goals
        steps = solve(_with(problem, visible, sub_goals))
        if steps is None:
            return None
        for name, args in steps:
            pre, add, delete = domain.ground(name, args)
            if not pre <= state:
                return None
            state = (state - delete) | add
        merged += steps
    if not goals <= state:
        return None
    return merged


def urgency(problem: str) -> list[str]:
    """Orders in the deadline chain order the generator wrote."""
    init = initial_state(problem)
    nxt = {f[1]: f[2] for f in init if f[0] == "next"}
    ready = [f[1] for f in init if f[0] == "ready"]
    order: list[str] = []
    seen: set[str] = set()
    for start in sorted(ready, key=lambda o: int(re.sub(r"\D", "", o) or 0)):
        o = start
        while o in nxt and o not in seen and o != "o_end":
            order.append(o)
            seen.add(o)
            o = nxt[o]
    return order
