"""Part 4 controller: one chef in a 2v2 match.

We reuse the planner from Levels 1-3 (in ``planner/``), driving only our own
chef. Planning runs in a background process, so every tick returns well
inside the 200 ms limit; the chef just waits while a plan is computed.

Match-specific behaviour lives in the planner and switches on when the
kitchen has another team:

- one order per plan, chosen by points per estimated step;
- goal recognition: an order another chef is visibly working towards
  (holding one of its ingredients or the finished dish) is left to them;
- replanning only when a ticket leaves the board or our chef is stuck.

An exception from a controller ends the set as a loss, so every tick is
wrapped: on an error we drop the plan and try again next tick, and after
repeated errors the chef simply stands still.
"""

from __future__ import annotations

import os
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, wait
from collections.abc import Collection

from controllers.navigation import DefaultMutationInterpolationPolicy
from controllers.pddl.controller import PDDLController
from simulator.context import Context
from simulator.entities import Agent, OvercookedState
from simulator.mutations import Deliver
from simulator.environment import Environment
from simulator.mutations import Mutation

from .planner.from_pddl import DefaultFromPDDL
from .planner.problem_generator import DefaultProblemGenerator

# Errors in a row before the chef gives up and stands still for the game.
MAX_ERRORS = 5
# The host moves on as soon as every seat has answered, so a seat that
# answers instantly while still planning lets the game race ahead. We wait
# for our planner for up to this long each tick, inside the 200 ms limit.
TICK_BUDGET_SECONDS = 0.16
# How long to wait for the worker processes while the game is being set up
# (the host allows 30 s for that).
WARM_UP_SECONDS = 20.0


class AvoidOtherChefsPolicy(DefaultMutationInterpolationPolicy):
    """Route around chefs we do not control.

    The provided policy plans through other chefs and expects them to step
    aside, which only works for chefs it drives itself. Teammates and
    opponents never make way for us, so we treat the tiles they stand on as
    walls when finding a path. The path is checked again every tick, so it
    opens up as soon as they move.
    """

    def _prepare_path(
        self,
        environment,
        *,
        agent_id,
        current_location,
        target_location,
        reserved_positions,
    ):
        others = {
            (agent.x, agent.y)
            for agent in environment.get_entities_of_type(Agent)
            if agent.id != agent_id and agent.x is not None and agent.y is not None
        }
        return super()._prepare_path(
            environment,
            agent_id=agent_id,
            current_location=current_location,
            target_location=target_location,
            reserved_positions=set(reserved_positions) | others,
        )


class OpenController(PDDLController):
    def _target_gone(self, environment: Environment) -> bool:
        """Whether the dish our plan delivers is no longer on the board.

        Another chef delivered it or it expired. Our queued delivery step
        names the dish, so we look for it among the visible tickets.
        """
        if not self._has_planned or self.is_busy():
            return False
        dishes = [
            mutation_with_location.mutation.expected_held_food
            for steps in self._plans.values()
            for _, mutation_with_location in steps
            if isinstance(mutation_with_location.mutation, Deliver)
        ]
        if not dishes:
            return False
        state = environment.get_first_entity_of_type(OvercookedState)
        if state is None:
            return False
        visible = {entry.name for entry in state.order_queue.visible}
        return any(dish not in visible for dish in dishes)

    def __init__(self) -> None:
        super().__init__(AvoidOtherChefsPolicy)
        self._problem_generator = DefaultProblemGenerator()
        self._errors = 0
        self._started = False
        try:
            futures = self.warm_up()
            wait(futures, timeout=WARM_UP_SECONDS)
            self._started = True
        except Exception:  # noqa: BLE001 - start lazily on the first tick instead
            pass

    def problem_generator(self) -> DefaultProblemGenerator:
        return self._problem_generator

    def from_pddl(self, environment: Environment) -> DefaultFromPDDL:
        ids = None if self._controlled_agent_ids is None else sorted(self._controlled_agent_ids)
        return DefaultFromPDDL(environment, ids)

    def set_controlled_agents(self, agent_ids: Collection[str] | None) -> None:
        super().set_controlled_agents(agent_ids)
        self._problem_generator.controlled_ids = (
            None if agent_ids is None else set(agent_ids)
        )

    def get_actions(self, environment: Environment, context: Context) -> list[Mutation]:
        if self._errors >= MAX_ERRORS:
            return []
        try:
            if not self._started:
                # Start the worker processes early; this does not block.
                self._started = True
                self.warm_up()
            if self._target_gone(environment):
                self._replan_due = True
            deadline = time.monotonic() + TICK_BUDGET_SECONDS
            while True:
                actions = super().get_actions(environment, context)
                if actions or not self.is_busy():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                pending = [f for f in self.pending_work() if not f.done()]
                if pending:
                    wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            mine = [
                action
                for action in actions
                if getattr(action, "agent_id", None) is None
                or self.controls_agent(action.agent_id)
            ]
            self._errors = 0
            return mine
        except Exception:  # noqa: BLE001 - a crash would forfeit the set
            if os.environ.get("TMC_DEBUG"):
                traceback.print_exc()
            self._errors += 1
            try:
                self._invalidate_plan()
            except Exception:  # noqa: BLE001
                pass
            return []
