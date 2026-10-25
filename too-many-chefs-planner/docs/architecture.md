# Architecture

The controller runs a planning cycle again and again. Each cycle reads the
kitchen, builds a PDDL problem, solves it, improves the plan, and hands each
chef a safe part of it.

## Files

| File | Job |
| --- | --- |
| `overcooked.pddl` | The domain: about 35 task-level actions, the same for every kitchen |
| `kitchen.py` | Reads the simulator state into a plain-Python `Snapshot` |
| `model.py` | Picks orders, prunes each chef's area, writes the PDDL problem |
| `problem_generator.py` | Entry point for the controller; decides when to replan |
| `from_pddl.py` | Turns plan steps into simulator actions, runs the post-processing |
| `schedule.py` | Makespan estimate and local search over who does what |
| `decompose.py` | Splits orders between independent groups of chefs |
| `pddl_sim.py` | A tiny PDDL interpreter used to check and replay plans |

`model.py` has no simulator imports. That let me test the model offline
against every level file with my own small planner before running the real
game.

## 1. The model

- **No movement in PDDL.** Static `(reach chef tile)` facts say which tiles a
  chef can work. Divided kitchens and pass counters need no special cases.
- **Food by type, plates by contents.** A counter holds one item, so
  `(food_at tile bun)` is enough. `(has_plate chef nothing)` is an empty plate.
- **Recipes as generated facts.** Rules such as `put_rule`, `scoop_rule` and
  `plating` are computed from the level's recipes. They copy how the simulator
  resolves a combine, for example whether the result stays in the station or
  goes back to the chef's hand. One domain therefore covers every kitchen.
- **Safety rules.** A chef with a plate must be on duty before meat goes into a
  timed pan. The bin only takes food no order needs. Pans and bowls must end
  each plan set down, because the controller will not replan while a chef
  holds one.

## 2. Choosing orders

- Only orders that can be made from storages and the remaining loose items.
- Orders that cannot make their deadline are skipped, unless they are the only
  ones left.
- At most 3 orders and about 14 recipe steps per plan (less in divided
  kitchens). This keeps Fast Downward well inside its time limit.
- The goal asks for urgent orders first, through a delivery chain.

## 3. Distance without action costs

Lama-first ignores action costs, so it happily sends a chef across the room.
Instead of costs, I prune what each chef can use:

- the nearest storage of each food, plus a little slack;
- the nearest station of each type;
- nearby free counters;
- nearby pass counters, plus the best shared hand-off spots for each pair of
  areas, so two areas can always pass food.

## 4. Improving the plan

1. **Action elimination.** Drop each step in turn; if the goals still hold,
   it was a detour.
2. **Cell splitting.** In open kitchens with two full sets of stations, plan
   each group's share of the orders on its own and merge.
3. **Rescheduling.** Estimate the parallel finish time from real walking
   distances, waits on other chefs and cook times. Then try swapping chefs,
   moving jobs or single chopping presses to another chef, and reordering how
   a plate collects its parts. Keep a change only if the plan still replays in
   PDDL and gets faster.

## 5. Executing safely

Each chef runs its own queue, so steps can happen earlier than planned. The
plan is replayed in PDDL, and a chef's queue is cut before any step that could
race another chef at the same counter or station. In crowded rooms, only one
chef per round uses a station reachable from a dead-end pocket, which avoids
navigation deadlocks. When a chef finishes its round, the next plan starts.
