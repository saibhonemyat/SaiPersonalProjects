"""Generate PDDL problems for every kitchen.

``DefaultPDDLController`` creates one ``DefaultProblemGenerator`` and calls
``to_pddl`` in a worker process whenever it needs a plan. We read the
kitchen into a plain ``Snapshot`` (kitchen.py), then write the problem from
it (model.py). The domain in overcooked.pddl is the same for every level.

``should_replan`` runs in the simulator process every tick. We replan when
new orders show up, when an order expires, and when the chefs look stuck.
"""

from __future__ import annotations

import os
import sys
from collections import Counter as Multiset
from collections.abc import Collection
from pathlib import Path

from controllers.pddl.interfaces import PDDLProblem, ProblemGenerator
from simulator.entities import Agent, Equipment
from simulator.environment import Environment

from .kitchen import kitchen_state, read_kitchen
from .model import build_problem

DOMAIN_PATH = Path(__file__).with_name("overcooked.pddl")

# Calls in a row with no timestep passing before we call the chefs stuck.
# In a headless run each call waits about 10 ms.
STUCK_CALLS = 40
# Timesteps with no chef moving and nothing cooking before we replan.
STILL_STEPS = 15
# In a 2v2 match: timesteps our own chef may stand still before we replan.
# Long enough to wait for an oven, short enough to notice a blocked station.
MATCH_STILL_STEPS = 12


class DefaultProblemGenerator(ProblemGenerator):
    def __init__(self) -> None:
        self._visible: tuple | None = None
        self._expired = 0
        self._timestep = -1
        self._same_timestep_calls = 0
        self._agents: tuple | None = None
        self._still_since = 0
        # Set by the 2v2 controller to the chef it drives; None means all.
        self.controlled_ids: set[str] | None = None

    def to_pddl(
        self,
        environment: Environment,
        controlled_agent_ids: Collection[str] | None = None,
    ) -> PDDLProblem:
        snapshot = read_kitchen(environment, controlled_agent_ids)
        result = build_problem(snapshot)
        domain = build_domain()
        dump(environment.timestep, "problem.pddl", result.problem, domain=domain)
        dump(
            environment.timestep,
            "orders.txt",
            "visible: "
            + ", ".join(o.name.split("/")[-1] for o in snapshot.orders)
            + "\nplanned: "
            + ", ".join(f"o{i}" for i in result.chosen_orders)
            + "\n",
        )
        return PDDLProblem(domain=domain, problem=result.problem)

    def should_replan(self, environment: Environment) -> bool:
        state = kitchen_state(environment)
        queue = state.order_queue
        visible = tuple(
            (entry.name, entry.revealed_at, entry.time_limit) for entry in queue.visible
        )
        timestep = environment.timestep
        match = self.controlled_ids is not None
        agents = tuple(
            sorted(
                (agent.id, agent.x, agent.y, agent.held_item_id)
                for agent in environment.get_entities_of_type(Agent)
                if not match or agent.id in self.controlled_ids
            )
        )

        if self._visible is None:
            self._remember(visible, queue.expired_count, timestep, agents)
            return False

        replan = False

        # Any change to the tickets: a new one, an expired one, or a delivery.
        # A plan may cover only the first few orders (see WORK_BUDGET), so
        # after a delivery we plan the next batch from the current kitchen.
        # In a headless run planning costs no timesteps.
        if visible != self._visible:
            if match:
                # Other chefs deliver all the time in a match, and replanning
                # costs our chef idle ticks. The 2v2 controller replans
                # itself when the ticket it is cooking for leaves the board;
                # new tickets are picked up when our dish is done.
                pass
            elif visible:
                replan = True
            self._visible = visible
            self._expired = queue.expired_count

        # A plan may be cut into rounds (see from_pddl.first_round). When a
        # chef that had work runs out of steps, its round is over: plan the
        # next one. This also covers a plan that ends before the orders do,
        # which the controller would otherwise treat as giving up.
        if visible and round_finished():
            replan = True

        # Stuck with the clock stopped: no chef has a legal action and
        # nothing is cooking.
        if timestep == self._timestep:
            self._same_timestep_calls += 1
        else:
            self._timestep = timestep
            self._same_timestep_calls = 0
        if self._same_timestep_calls >= STUCK_CALLS and visible:
            replan = True

        # The clock runs (something is cooking or a chef turns) but nobody
        # gets anywhere.
        if agents != self._agents:
            self._agents = agents
            self._still_since = timestep
        cooking = any(
            equipment.cooking is not None
            for equipment in environment.get_entities_of_type(Equipment)
        )
        if match:
            if visible and timestep - self._still_since >= MATCH_STILL_STEPS:
                replan = True
        elif visible and not cooking and timestep - self._still_since >= STILL_STEPS:
            replan = True

        if replan:
            self._remember(visible, queue.expired_count, timestep, agents)
        return replan

    def _remember(self, visible, expired, timestep, agents) -> None:
        self._visible = visible
        self._expired = expired
        self._timestep = timestep
        self._same_timestep_calls = 0
        self._agents = agents
        self._still_since = timestep


def round_finished() -> bool:
    """Return whether a chef that had steps in the current plan has none left.

    The controller calls should_replan from its get_actions method without
    passing its queues, so we read them from the calling frame. If that ever
    fails we return False and rely on the other replanning triggers.
    """
    try:
        frame = sys._getframe(2)
        controller = frame.f_locals.get("self")
        plans = getattr(controller, "_plans", None)
        totals = getattr(controller, "_plan_totals", None)
        if not isinstance(plans, dict) or not isinstance(totals, dict):
            return False
        return any(total > 0 and not plans.get(agent) for agent, total in totals.items())
    except Exception:  # noqa: BLE001
        return False


def build_domain() -> str:
    return DOMAIN_PATH.read_text(encoding="utf-8")


def dump(timestep: int, name: str, text: str, domain: str | None = None) -> None:
    """Save planning files for debugging when TMC_DUMP_DIR is set.

    Off by default, so it does nothing on the contest server.
    """
    folder = os.environ.get("TMC_DUMP_DIR")
    if not folder:
        return
    try:
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        if domain is not None and not (path / "domain.pddl").exists():
            (path / "domain.pddl").write_text(domain, encoding="utf-8")
        (path / f"t{timestep:04d}_{name}").write_text(text, encoding="utf-8")
    except OSError:
        pass
