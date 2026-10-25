"""Translate PDDL plans into simulator mutations.

Every step names its chef, so each chef gets its own queue. The navigation
policy walks the chef next to the step's tile and waits until the mutation
is legal. That waiting is how chefs coordinate: a chef that needs grilled
meat stands at the pan until the meat is ready, and a chef that needs a
handed-over plate waits at the pass until it arrives.

The ``expected_*`` fields make each mutation specific. Without them a chef
could pick up the wrong item, or a Combine could resolve a different way
than the plan assumed.
"""

from __future__ import annotations

from controllers.pddl.interfaces import FromPDDL, PDDLPlanStep
from simulator.entities import Equipment
from simulator.entities.plate import PLATE_NAME
from simulator.environment import Environment
from simulator.mutations import (
    Combine,
    Cook,
    Deliver,
    Discard,
    MutationWithLocation,
    PickUp,
    Place,
    TakeFromStorage,
    TurnAgent,
    Wash,
)

from .kitchen import read_kitchen
from .model import (
    Symbols,
    build_problem,
    crowded_chefs,
    loc_symbol,
    parse_loc,
    risky_stations,
)
from .pddl_sim import Domain, eliminate_redundant, goal_facts, initial_state


# Ticks to wait before planning again after the solver found no plan.
RETRY_WAIT_TICKS = 3


class DefaultFromPDDL(FromPDDL):
    def __init__(
        self, environment: Environment, controlled_agent_ids=None
    ) -> None:
        self._environment = environment
        # Only the chefs this controller drives. In a 2v2 match that is one
        # chef; the others are teammates and opponents we must never command.
        self._snapshot = read_kitchen(environment, controlled_agent_ids)
        self._symbols = Symbols(self._snapshot)
        # Cook presses already made on the food in each vessel.
        self._progress = {
            vessel.id: vessel.elapsed for vessel in self._snapshot.vessels
        }

    def from_pddl(self, steps: list[PDDLPlanStep]) -> list[MutationWithLocation]:
        from .problem_generator import dump

        dump(
            self._environment.timestep,
            "plan.txt",
            "".join(f"{step.name} {' '.join(step.parameters)}\n" for step in steps)
            or "NO PLAN\n",
        )
        steps = self.normalised(steps)
        steps = self.best_plan(steps)
        kept = self.first_round(steps)
        if len(kept) < len(steps):
            dump(
                self._environment.timestep,
                "round.txt",
                "".join(f"{step.name} {' '.join(step.parameters)}\n" for step in kept),
            )
        plan: list[MutationWithLocation] = []
        for step in kept:
            plan.extend(self.translate(step))

        # An empty symbolic plan normally means the controller is finished.
        # There is one important exception: some robustness levels deliberately
        # leave visible tickets that can no longer be made (for example, the
        # out-of-stock cake level has only two cherries for three cakes and no
        # meat for its cheeseburger).  build_problem() intentionally leaves
        # those impossible orders out of the goal, so once every makeable dish
        # has been served the solver quite correctly returns an empty plan.
        #
        # If we return [] here, PDDLController.has_finished() interprets that
        # as giving up while the impossible tickets are still visible.  Keep
        # one chef busy with harmless turns until the next ticket deadline;
        # once the ticket expires, the normal visible-order change triggers a
        # replan (or the run completes if no tickets remain).
        if not plan:
            plan = self.wait_for_unfillable_orders()
        if not plan:
            plan = self.retry_after_failed_solve(steps)
        return plan

    def retry_after_failed_solve(
        self, steps: list[PDDLPlanStep]
    ) -> list[MutationWithLocation]:
        """Wait a few ticks and replan when the solver returned nothing.

        If the planner times out on a real goal, an empty plan would make the
        controller report that it gave up with orders still open. Instead one
        chef turns on the spot for a few ticks; when that short queue runs
        out, problem_generator.should_replan starts a fresh plan from the
        kitchen as it is then.
        """
        if steps or not self._snapshot.orders or not self._snapshot.chefs:
            return []
        if not build_problem(self._snapshot).chosen_orders:
            return []
        chef = self._snapshot.chefs[0].id
        return [
            MutationWithLocation(
                mutation=TurnAgent(agent_id=chef, orientation="n" if i % 2 == 0 else "s"),
                location=None,
            )
            for i in range(RETRY_WAIT_TICKS)
        ]

    # ------------------------------------------------------------------

    def wait_for_unfillable_orders(self) -> list[MutationWithLocation]:
        """Advance time instead of giving up on visible impossible tickets.

        Only activate when our own problem builder selected no order at all.
        That distinguishes a deliberately empty goal (nothing currently
        makeable) from a planner unexpectedly failing to solve a real goal.
        The turns are direct movement mutations, so they need no target tile
        and cannot disturb ingredients, plates, or stations.
        """
        if not self._snapshot.orders or not self._snapshot.chefs:
            return []
        if build_problem(self._snapshot).chosen_orders:
            return []

        future_deadlines = [
            order.deadline
            for order in self._snapshot.orders
            if order.deadline is not None and order.deadline >= self._snapshot.timestep
        ]
        if not future_deadlines:
            return []

        # read_kitchen keeps an order visible through its deadline
        # (it removes it only once timestep > deadline), hence the +1.
        turns = min(future_deadlines) - self._snapshot.timestep + 1
        chef = self._snapshot.chefs[0].id
        return [
            MutationWithLocation(
                mutation=TurnAgent(
                    agent_id=chef,
                    orientation="n" if i % 2 == 0 else "s",
                ),
                location=None,
            )
            for i in range(turns)
        ]

    # --- shared problem facts --------------------------------------------

    def _problem(self) -> str:
        if not hasattr(self, "_problem_text"):
            self._problem_text = build_problem(self._snapshot).problem
        return self._problem_text

    def _init(self) -> set:
        """Initial facts used to check plans after planning.

        The deadline chain (ready/next) only steers the planner towards the
        most urgent dish. The game accepts any visible order, so unless the
        level enforces strict order we relax the chain here. That lets
        post-processing reorder deliveries and merge plans from separate
        cells.
        """
        init = initial_state(self._problem())
        if self._snapshot.strict:
            return init
        orders = {f[1] for f in init if f[0] == "open"}
        init = {f for f in init if f[0] not in ("ready", "next")}
        init |= {("ready", o) for o in orders}
        init |= {("next", o, "o_end") for o in orders}
        return init

    def _goals(self) -> set:
        return goal_facts(self._problem())

    def normalised(self, steps: list[PDDLPlanStep]) -> list[PDDLPlanStep]:
        """Point every delivery at o_end, matching the relaxed chain."""
        if self._snapshot.strict:
            return steps
        out = []
        for step in steps:
            if step.name.lower().startswith("deliver") and len(step.parameters) >= 5:
                params = list(step.parameters)
                params[4] = "o_end"
                step = PDDLPlanStep(step.name, tuple(params))
            out.append(step)
        return out

    def best_plan(self, steps: list[PDDLPlanStep]) -> list[PDDLPlanStep]:
        """Clean and reschedule the plan; in cell kitchens also try a split.

        The split plan (see decompose.py) is used only when its estimated
        finish time is lower.
        """
        candidates = [self.rescheduled(self.without_detours(steps))]
        split = self.split_by_cells()
        if split is not None:
            candidates.append(self.rescheduled(self.without_detours(split)))
        if len(candidates) == 1:
            return candidates[0]
        try:
            from .problem_generator import build_domain
            from .schedule import Estimator

            estimator = Estimator(self._snapshot, self._symbols, Domain(build_domain()))
            init = self._init()

            def finish(plan: list[PDDLPlanStep]) -> int:
                plain = [
                    (step.name.lower(), tuple(p.lower() for p in step.parameters))
                    for step in plan
                ]
                value = estimator.makespan(init, plain)
                return 10**9 if value is None else value

            return min(candidates, key=finish)
        except Exception:  # noqa: BLE001
            return candidates[0]

    def split_by_cells(self) -> list[PDDLPlanStep] | None:
        if self._snapshot.strict:
            return None
        try:
            from .decompose import cells, decompose, urgency
            from .problem_generator import build_domain

            # Only open kitchens: in a divided one no cell can cook alone.
            from .model import flood

            floors = {
                frozenset(flood(self._snapshot.walkable, (c.x, c.y)))
                for c in self._snapshot.chefs
            }
            if len(floors) != 1:
                return None
            init = self._init()
            if len(cells(init)) < 2:
                return None
            if sum(1 for g in self._goals() if g[0] == "served") < 2:
                return None
            domain_text = build_domain()
            solve = getattr(self, "_solve", None) or _fast_downward(domain_text)
            plain = decompose(
                Domain(domain_text), self._problem(), solve, urgency(self._problem())
            )
            if plain is None:
                return None
            return [PDDLPlanStep(name, args) for name, args in plain]
        except Exception:  # noqa: BLE001
            return None

    def without_detours(self, steps: list[PDDLPlanStep]) -> list[PDDLPlanStep]:
        """Drop steps the goals do not need (see pddl_sim.eliminate_redundant)."""
        try:
            from .problem_generator import build_domain

            domain = Domain(build_domain())
            plain = [
                (step.name.lower(), tuple(part.lower() for part in step.parameters))
                for step in steps
            ]
            keep = eliminate_redundant(domain, self._init(), self._goals(), plain)
            return [steps[i] for i in keep]
        except Exception:  # noqa: BLE001 - never lose a plan over this
            return steps

    def rescheduled(self, steps: list[PDDLPlanStep]) -> list[PDDLPlanStep]:
        """Give work to the chefs that finish it soonest (see schedule.py)."""
        try:
            from .problem_generator import build_domain
            from .schedule import reassign

            domain = Domain(build_domain())
            plain = [
                (step.name.lower(), tuple(part.lower() for part in step.parameters))
                for step in steps
            ]
            better = reassign(self._snapshot, self._symbols, domain, self._init(), plain)
            return [PDDLPlanStep(name, args) for name, args in better]
        except Exception:  # noqa: BLE001 - never lose a plan over this
            return steps

    def first_round(self, steps: list[PDDLPlanStep]) -> list[PDDLPlanStep]:
        """Keep the part of the plan the chefs can run without racing.

        Each chef runs its own queue, so a step planned for later can happen
        early if it is already legal. That breaks the plan when two chefs
        use the same counter or station: one chef puts coconut on the
        cutboard before the other has chopped its chocolate there, and the
        chocolate can never go on. We replay the plan in PDDL, and when a
        chef's step at a counter or station could already happen before an
        earlier step by another chef at the same tile, that chef stops
        there for this round. Steps that can only happen afterwards, such
        as scooping meat once it is cooked, are safe and stay. When a chef
        runs out of steps, problem_generator.py replans the next round.
        """
        try:
            return self._first_round(steps)
        except Exception:  # noqa: BLE001 - never lose a plan over this check
            return steps

    def _first_round(self, steps: list[PDDLPlanStep]) -> list[PDDLPlanStep]:
        from .problem_generator import build_domain

        domain = Domain(build_domain())
        state = self._init()
        names = [step.name.lower() for step in steps]
        args = [tuple(part.lower() for part in step.parameters) for step in steps]

        before = []
        for name, params in zip(names, args):
            before.append(state)
            _, add, delete = domain.ground(name, params)
            state = (state - delete) | add

        # Tiles that hold one thing at a time. Storages, bins and the
        # delivery hatch never run out, so chefs can share them freely.
        exclusive = {
            loc_symbol(location)
            for location, tile in self._snapshot.tiles.items()
            if tile.counter or tile.supports
        } | {
            loc_symbol(vessel.tile)
            for vessel in self._snapshot.vessels
            if vessel.tile is not None
        }
        chef = [None if n == "auto_cook" else a[0] for n, a in zip(names, args)]
        tile = [domain.location(n, a) for n, a in zip(names, args)]
        never = len(steps)
        cut: dict[str, int] = {}

        def early(k: int, j: int) -> bool:
            """Could step k already run in the state before step j?"""
            mine = chef[k]
            mixed = {f for f in before[j] if mine not in f[1:]}
            mixed |= {f for f in before[k] if mine in f[1:]}
            pre, _, _ = domain.ground(names[k], args[k])
            return pre <= mixed

        def others_at(k: int):
            for j in range(k):
                if chef[j] is not None and chef[j] != chef[k] and tile[j] == tile[k]:
                    yield j

        for k in range(len(steps)):
            if chef[k] is None or tile[k] not in exclusive:
                continue
            if k >= cut.get(chef[k], never):
                continue
            if any(early(k, j) for j in others_at(k)):
                cut[chef[k]] = k

        # Stations that can be worked from a pocket: one chef per round, in
        # rooms with three or more chefs. Two chefs there at once, even on
        # safe dependent steps, can box one in, and the navigation policy
        # then ends the run with a deadlock error. The burn catcher is
        # exempt; it has to wait at its pan.
        risky = {loc_symbol(location) for location in risky_stations(self._snapshot)}
        crowded = {self._symbols.chef_sym[c] for c in crowded_chefs(self._snapshot)}
        duty_steps = {"scoop_duty", "grab_from_vessel_duty"}
        for k in range(len(steps)):
            if chef[k] is None or tile[k] not in risky or names[k] in duty_steps:
                continue
            if chef[k] not in crowded:
                continue
            if k >= cut.get(chef[k], never):
                continue
            if any(j < cut.get(chef[j], never) for j in others_at(k)):
                cut[chef[k]] = k

        # Food that burns only goes in once the catcher really holds a plate.
        # Otherwise the putter waits here; the catcher's round ends once it
        # is ready, and the next plan puts the food in.
        for k in range(len(steps)):
            if names[k] != "put_in_vessel_watch" or k >= cut.get(chef[k], never):
                continue
            catcher = args[k][1]
            if not any(f[0] == "has_plate" and f[1] == catcher for f in before[0]):
                cut[chef[k]] = k

        def not_carrying(c: str) -> None:
            """Never end a chef's round while it holds a pan or bowl.

            The controller will not replan while a chef carries one, so a
            round that ended there would stall the whole kitchen. Move the
            cut back to before the chef picked it up.
            """
            k = cut.get(c, never)
            if k >= never or not any(
                f[0] == "has_vessel" and f[1] == c for f in before[k]
            ):
                return
            for p in range(k - 1, -1, -1):
                if chef[p] == c and names[p].startswith("pick_vessel"):
                    cut[c] = p
                    return

        # A step that waits on another chef's step that was cut would wait
        # forever, so it is cut too.
        changed = True
        while changed:
            changed = False
            for c in list(cut):
                before_cut = cut[c]
                not_carrying(c)
                changed |= cut[c] != before_cut
            for k in range(len(steps)):
                if chef[k] is None or tile[k] not in exclusive:
                    continue
                if k >= cut.get(chef[k], never):
                    continue
                if any(j >= cut.get(chef[j], never) for j in others_at(k)):
                    cut[chef[k]] = k
                    changed = True

        return [
            step
            for k, step in enumerate(steps)
            if chef[k] is None or k < cut.get(chef[k], never)
        ]

    # ------------------------------------------------------------------

    def translate(self, step: PDDLPlanStep) -> list[MutationWithLocation]:
        name = step.name.lower()
        p = [part.lower() for part in step.parameters]

        # A station that cooks by itself needs no chef.
        if name == "auto_cook":
            return []

        chef = self._chef(p[0])
        food = self._food
        content = self._content

        if name == "take_food":
            _, l, f = p
            mutation = TakeFromStorage(
                agent_id=chef, expected_input_name=food(f), expected_output_name=food(f)
            )
        elif name == "take_food_to_plate":
            _, l, f, x, out = p
            mutation = TakeFromStorage(
                agent_id=chef,
                expected_input_name=food(f),
                expected_output_name=food(out),
            )
        elif name in ("take_plate", "take_plate_inf", "take_dirty_plate"):
            l = p[1]
            mutation = TakeFromStorage(agent_id=chef)
        elif name == "pick_food":
            _, l, f = p
            mutation = PickUp(agent_id=chef, expected_input_name=food(f))
        elif name == "drop_food":
            _, l, f = p
            mutation = Place(agent_id=chef, expected_held_food=food(f))
        elif name == "pick_plate":
            _, l, x = p
            mutation = PickUp(
                agent_id=chef,
                expected_held_equipment_name=PLATE_NAME,
                expected_held_equipment_contents_name=content(x),
            )
        elif name == "drop_plate":
            _, l, x = p
            mutation = Place(
                agent_id=chef,
                expected_held_equipment_name=PLATE_NAME,
                expected_held_equipment_contents_name=content(x),
            )
        elif name == "pick_dirty":
            _, l = p
            mutation = PickUp(agent_id=chef, expected_held_equipment_name=PLATE_NAME)
        elif name == "drop_dirty":
            _, l = p
            mutation = Place(agent_id=chef, expected_held_equipment_name=PLATE_NAME)
        elif name == "wash":
            _, l = p
            mutation = Wash(agent_id=chef, expected_held_equipment_name=PLATE_NAME)
        elif name == "combine_on_counter":
            _, l, a, b, out = p
            mutation = Combine(
                agent_id=chef,
                expected_held_food=food(a),
                expected_target_food_name=food(b),
                expected_output_name=food(out),
            )
        elif name == "food_to_plate":
            _, l, a, x, out = p
            mutation = Combine(
                agent_id=chef,
                expected_held_food=food(a),
                expected_target_equipment_name=PLATE_NAME,
                expected_target_equipment_contents_name=content(x),
                expected_output_name=food(out),
            )
        elif name == "plate_takes_food":
            _, l, x, a, out = p
            mutation = Combine(
                agent_id=chef,
                expected_held_equipment_name=PLATE_NAME,
                expected_held_equipment_contents_name=content(x),
                expected_target_food_name=food(a),
                expected_output_name=food(out),
            )
        elif name in ("put_in_vessel", "put_in_vessel_watch", "put_in_vessel_watch_solo"):
            if name == "put_in_vessel_watch":
                # The second chef is the catcher; it has no mutation here.
                _, _, v, l, t, a, y, out, _ = p
            else:
                _, v, l, t, a, y, out = p
            mutation = Combine(
                agent_id=chef,
                expected_held_food=food(a),
                expected_target_equipment_name=self._vessel_name(v),
                expected_target_equipment_contents_name=content(y),
                expected_output_name=food(out),
            )
        elif name in ("grab_from_vessel", "grab_from_vessel_duty"):
            _, v, l, t, a, y, out = p
            mutation = Combine(
                agent_id=chef,
                expected_held_food=food(a),
                expected_target_equipment_name=self._vessel_name(v),
                expected_target_equipment_contents_name=food(y),
                expected_output_name=food(out),
            )
        elif name == "take_out":
            _, v, l, f = p
            mutation = PickUp(
                agent_id=chef, expected_input_name=food(f), expected_output_name=food(f)
            )
        elif name == "pour_plate":
            _, v, l, t, x, y, out = p
            mutation = Combine(
                agent_id=chef,
                expected_held_equipment_name=PLATE_NAME,
                expected_held_equipment_contents_name=food(x),
                expected_target_equipment_name=self._vessel_name(v),
                expected_target_equipment_contents_name=content(y),
                expected_output_name=food(out),
            )
        elif name in ("scoop", "scoop_duty"):
            _, v, l, t, x, y, out = p
            mutation = Combine(
                agent_id=chef,
                expected_held_equipment_name=PLATE_NAME,
                expected_held_equipment_contents_name=content(x),
                expected_target_equipment_name=self._vessel_name(v),
                expected_target_equipment_contents_name=food(y),
                expected_output_name=food(out),
            )
        elif name == "cook":
            _, v, l, t, f, out = p
            return self._cook(chef, v, l, f, out)
        elif name in ("pick_vessel_counter", "pick_vessel_support", "pick_vessel_junk"):
            _, v, l, y = p
            mutation = PickUp(
                agent_id=chef,
                expected_held_equipment_name=self._vessel_name(v),
                expected_held_equipment_contents_name=content(y),
            )
        elif name in ("place_vessel_support", "place_vessel_counter"):
            v, l = p[1], p[2]
            mutation = Place(
                agent_id=chef, expected_held_equipment_name=self._vessel_name(v)
            )
        elif name == "pour_vessel_to_plate":
            _, v, l, y, x, out = p
            mutation = Combine(
                agent_id=chef,
                expected_held_equipment_name=self._vessel_name(v),
                expected_held_equipment_contents_name=food(y),
                expected_target_equipment_name=PLATE_NAME,
                expected_target_equipment_contents_name=content(x),
                expected_output_name=food(out),
            )
        elif name in ("deliver", "deliver_ret_clean", "deliver_ret_dirty"):
            l, f = p[1], p[2]
            mutation = Deliver(
                agent_id=chef,
                expected_held_equipment_name=PLATE_NAME,
                expected_held_food=food(f),
            )
        elif name == "pour_vessel_to_vessel":
            _, v, w, l, t, y, z, out = p
            mutation = Combine(
                agent_id=chef,
                expected_held_equipment_name=self._vessel_name(v),
                expected_held_equipment_contents_name=food(y),
                expected_target_equipment_name=self._vessel_name(w),
                expected_target_equipment_contents_name=content(z),
                expected_output_name=food(out),
            )
        elif name in (
            "discard_food",
            "discard_plate_food",
            "discard_plate_food_scarce",
            "discard_vessel_food",
        ):
            l = p[1]
            mutation = Discard(agent_id=chef)
        else:
            raise ValueError(f"No translation for this action: {step}")

        return [MutationWithLocation(mutation=mutation, location=parse_loc(l))]

    # ------------------------------------------------------------------

    def _cook(
        self, chef: str, v: str, l: str, f: str, out: str
    ) -> list[MutationWithLocation]:
        """Press the station until the food is done.

        Without timed cooking one press is enough. With it, a hand-worked
        station such as the cutboard needs cook_time presses, minus any
        presses already made on the food it holds now.
        """
        vessel_id = self._symbols.vessel[v]
        vessel = next(x for x in self._snapshot.vessels if x.id == vessel_id)
        # Progress only counts if it belongs to the food we are cooking now.
        done = self._progress.get(vessel_id, 0) if vessel.content == self._food(f) else 0
        self._progress[vessel_id] = 0  # later food in this vessel starts fresh
        presses = max(1, vessel.cook_time - done)
        location = parse_loc(l)
        return [
            MutationWithLocation(
                mutation=Cook(
                    agent_id=chef,
                    expected_held_food=self._food(f),
                    expected_input_name=vessel.name,
                    expected_output_name=self._food(out),
                ),
                location=location,
            )
            for _ in range(presses)
        ]

    def _chef(self, symbol: str) -> str:
        chef_id = self._symbols.chef.get(symbol)
        if chef_id is None:
            raise ValueError(f"Unknown chef in plan: {symbol}")
        return chef_id

    def _food(self, symbol: str) -> str:
        name = self._symbols.food.get(symbol)
        if name is None:
            raise ValueError(f"Unknown food in plan: {symbol}")
        return name

    def _content(self, symbol: str) -> str | None:
        return self._symbols.content_name(symbol)

    def _vessel_name(self, symbol: str) -> str:
        vessel_id = self._symbols.vessel.get(symbol)
        if vessel_id is None:
            raise ValueError(f"Unknown vessel in plan: {symbol}")
        entity = self._environment.get_entity_as(vessel_id, Equipment)
        if entity is None:
            raise ValueError(f"Vessel {vessel_id} is not in the kitchen")
        return entity.name


def _fast_downward(domain_text: str):
    """Solve a sub-problem with the installed planner; None if no plan."""
    from controllers.pddl.interfaces import PDDLProblem
    from controllers.pddl.solvers import solve_problem_with_logging

    def solve(problem_text: str):
        steps = solve_problem_with_logging(
            PDDLProblem(domain=domain_text, problem=problem_text),
            time_limit_seconds=8,
        )
        if not steps:
            return None
        return [
            (step.name.lower(), tuple(p.lower() for p in step.parameters))
            for step in steps
        ]

    return solve
