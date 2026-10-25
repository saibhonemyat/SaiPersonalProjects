"""Read the simulator's environment into a ``model.Snapshot``.

This is the only file in the submission, apart from from_pddl.py, that
touches simulator types. Everything it reads is copied into plain
dataclasses, so the model code can be tested without the simulator.
"""

from __future__ import annotations

from collections.abc import Collection

from simulator.entities import (
    Agent,
    Bin,
    Bounds,
    Counter,
    Delivery,
    Equipment,
    Food,
    OvercookedState,
    Plate,
    PlateDispenser,
    Sink,
    Storage,
)
from simulator.environment import Environment
from simulator.mutations.move_agent import is_walkable

from .model import (
    Chef,
    Dispenser,
    Held,
    Order,
    Snapshot,
    Tile,
    Vessel,
)


def kitchen_state(environment: Environment) -> OvercookedState:
    state = environment.get_first_entity_of_type(OvercookedState)
    if state is None:
        raise ValueError("The environment has no OvercookedState")
    return state


def ordered_chefs(
    environment: Environment, controlled_agent_ids: Collection[str] | None
) -> list[Agent]:
    chefs = environment.get_entities_of_type(Agent)
    if controlled_agent_ids is not None:
        wanted = set(controlled_agent_ids)
        chefs = [chef for chef in chefs if chef.id in wanted]
    return sorted(
        (chef for chef in chefs if chef.x is not None and chef.y is not None),
        key=lambda chef: (chef.y, chef.x, chef.id),
    )


def read_kitchen(
    environment: Environment,
    controlled_agent_ids: Collection[str] | None = None,
) -> Snapshot:
    state = kitchen_state(environment)
    timestep = environment.timestep
    tiles: dict[tuple[int, int], Tile] = {}

    def tile_at(x: int, y: int) -> Tile:
        if (x, y) not in tiles:
            tiles[(x, y)] = Tile(x=x, y=y)
        return tiles[(x, y)]

    # --- floor ---
    walkable: set[tuple[int, int]] = set()
    bounds = environment.get_first_entity_of_type(Bounds)
    if bounds is not None:
        bx = bounds.x or 0
        by = bounds.y or 0
        for x in range(bx, bx + bounds.width):
            for y in range(by, by + bounds.height):
                if is_walkable(environment, x, y, ignore_agents=True):
                    walkable.add((x, y))

    # --- recipes and food ---
    cook_recipes = [
        (recipe.ingredient, recipe.equipment, recipe.output)
        for recipe in state.cook_recipes
    ]
    combine_recipes = [
        (frozenset(recipe.ingredients), recipe.output)
        for recipe in state.combine_recipes
    ]
    foods: dict[str, bool] = {
        name: definition.raw for name, definition in state.food_definitions.items()
    }
    for food in environment.get_entities_of_type(Food):
        foods.setdefault(food.name, food.raw)
    cooking_equipment = {equipment for _, equipment, _ in cook_recipes}

    # --- equipment ---
    vessels: list[Vessel] = []
    vessel_ids: set[str] = set()
    for equipment in environment.get_entities_of_type(Equipment):
        if isinstance(equipment, Plate):
            continue
        placed = equipment.x is not None and equipment.y is not None
        if placed and not equipment.portable:
            tile = tile_at(equipment.x, equipment.y)
            tile.supports = (*tile.supports, equipment.name)
        if equipment.name not in cooking_equipment:
            continue
        content = _food_name(environment, equipment.held_item_id)
        elapsed = 0
        if equipment.cooking is not None and (
            equipment.cooking.food_id == equipment.held_item_id
        ):
            elapsed = equipment.cooking.elapsed
        vessels.append(
            Vessel(
                id=equipment.id,
                name=equipment.name,
                portable=equipment.portable,
                requires=equipment.requires,
                content=content,
                tile=(equipment.x, equipment.y) if placed else None,
                on_counter=False,
                self_cooking=equipment.cooks_by_itself,
                cook_time=equipment.cook_time,
                elapsed=elapsed,
            )
        )
        vessel_ids.add(equipment.id)
        if placed:
            tile_at(equipment.x, equipment.y)

    vessels_by_id = {vessel.id: vessel for vessel in vessels}
    fixed_tiles = {
        (equipment.x, equipment.y)
        for equipment in environment.get_entities_of_type(Equipment)
        if not isinstance(equipment, Plate)
        and not equipment.portable
        and equipment.x is not None
    }

    # --- counters ---
    for counter in environment.get_entities_of_type(Counter):
        if counter.x is None or counter.y is None:
            continue
        tile = tile_at(counter.x, counter.y)
        # A counter under a cutboard or stove is a station, not a free counter.
        if (counter.x, counter.y) in fixed_tiles:
            continue
        tile.counter = True
        if counter.held_item_id is None:
            continue
        item = environment.get_entity(counter.held_item_id)
        if item is None:
            continue
        if counter.held_item_id in vessels_by_id:
            vessels_by_id[counter.held_item_id].on_counter = True
        else:
            tile.item = _held(environment, item)

    # --- other stations ---
    for storage in environment.get_entities_of_type(Storage):
        if storage.x is not None and storage.y is not None:
            tile_at(storage.x, storage.y).storage = storage.food_name
    for delivery in environment.get_entities_of_type(Delivery):
        if delivery.x is not None and delivery.y is not None:
            tile_at(delivery.x, delivery.y).delivery = True
    for bin_ in environment.get_entities_of_type(Bin):
        if bin_.x is not None and bin_.y is not None:
            tile_at(bin_.x, bin_.y).bin = True
    for sink in environment.get_entities_of_type(Sink):
        if sink.x is not None and sink.y is not None:
            tile_at(sink.x, sink.y).sink = True
    dispensers = sorted(
        (
            dispenser
            for dispenser in environment.get_entities_of_type(PlateDispenser)
            if dispenser.x is not None and dispenser.y is not None
        ),
        key=lambda dispenser: (dispenser.y, dispenser.x, dispenser.id),
    )
    for dispenser in dispensers:
        tile_at(dispenser.x, dispenser.y).dispenser = Dispenser(
            clean=dispenser.plate_count,
            dirty=dispenser.dirty_plate_count,
            infinite=dispenser.infinite_supply,
            returns_dirty=dispenser.returns_dirty,
        )
    first_dispenser = (dispensers[0].x, dispensers[0].y) if dispensers else None

    # --- chefs ---
    chefs = []
    for agent in ordered_chefs(environment, controlled_agent_ids):
        held = None
        if agent.held_item_id is not None:
            item = environment.get_entity(agent.held_item_id)
            if agent.held_item_id in vessels_by_id:
                held = Held(kind="vessel", vessel=agent.held_item_id)
            elif item is not None:
                held = _held(environment, item)
                if held is None:
                    # Something we cannot describe; the chef sits this plan out.
                    held = Held(kind="unknown")
        chefs.append(Chef(id=agent.id, x=agent.x, y=agent.y, held=held))

    # --- other chefs (teammates and opponents) ---
    mine = {chef.id for chef in chefs}
    my_teams = {
        agent.team for agent in environment.get_entities_of_type(Agent) if agent.id in mine
    }
    others = []
    for agent in environment.get_entities_of_type(Agent):
        if agent.id in mine or agent.x is None or agent.y is None:
            continue
        held = None
        if agent.held_item_id is not None:
            item = environment.get_entity(agent.held_item_id)
            if agent.held_item_id in vessels_by_id:
                held = Held(kind="vessel", food=vessels_by_id[agent.held_item_id].content)
            elif item is not None:
                held = _held(environment, item)
        others.append(
            {
                "id": agent.id,
                "teammate": agent.team is not None and agent.team in my_teams,
                "x": agent.x,
                "y": agent.y,
                "held": held,
            }
        )
    competitive = any(
        agent.team is not None and agent.team not in my_teams
        for agent in environment.get_entities_of_type(Agent)
        if agent.id not in mine
    ) and bool(chefs)

    # --- orders ---
    queue = state.order_queue
    orders = []
    for entry in queue.visible:
        deadline = (
            entry.revealed_at + entry.time_limit if entry.time_limit is not None else None
        )
        if deadline is not None and timestep > deadline:
            continue
        orders.append(Order(name=entry.name, deadline=deadline, reward=entry.reward))

    return Snapshot(
        timestep=timestep,
        walkable=frozenset(walkable),
        chefs=chefs,
        tiles=tiles,
        vessels=vessels,
        foods=foods,
        cook_recipes=cook_recipes,
        combine_recipes=combine_recipes,
        orders=orders,
        strict=queue.strict_ordering or queue.order_reveal == "sequential",
        sequential=queue.order_reveal == "sequential",
        first_dispenser=first_dispenser,
        timed=any(vessel.self_cooking for vessel in vessels),
        extra={"others": others, "competitive": competitive},
    )


def _food_name(environment: Environment, item_id: str | None) -> str | None:
    if item_id is None:
        return None
    food = environment.get_entity_as(item_id, Food)
    return food.name if food is not None else None


def _held(environment: Environment, item: object) -> Held | None:
    if isinstance(item, Food):
        return Held(kind="food", food=item.name)
    if isinstance(item, Plate):
        if item.dirty:
            return Held(kind="dirty")
        return Held(kind="plate", food=_food_name(environment, item.held_item_id))
    return None
