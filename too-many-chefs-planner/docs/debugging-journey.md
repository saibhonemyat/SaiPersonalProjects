# Debugging journey

Each fix below came from watching a real run fail, reading the dumped PDDL
problem and plan (`TMC_DUMP_DIR`), and finding the cause.

## 1. Chefs racing each other
**Symptom:** a valid plan deadlocked. Two chefs used the same cutboard in the
wrong order.
**Cause:** each chef runs its own queue, so a fast chef reaches a shared
station before the step the plan expected to happen first.
**Fix:** replay the plan in PDDL and cut each queue before any step that
could race. Chefs work in race-free rounds, and the next plan starts when a
round ends.

## 2. Navigation deadlock
**Symptom:** four chefs crowded one pot, and the starter path policy crashed.
**Fix:** give each chef a local working area, and allow only one chef per
round at a station reached through a dead-end pocket.

## 3. Burning meat
**Symptom:** meat cooked in a timed pan with no one free to take it off.
**Fix:** a "catcher on duty" fact. A chef holding a plate must be on duty
before meat goes in, and the duty survives replanning.

## 4. Bowl left in hand
**Symptom:** a chef kept holding a bowl, so the controller never replanned.
**Fix:** a goal that pans and bowls end each plan set down.

## 5. Dead end with a scarce plate
**Symptom:** mid-game, the only plate held half a dish nobody needed any more.
**Fix:** scarce-plate mode lets the planner bin food and free the plate. If a
plan still comes back empty, the controller retries with fewer orders.

## 6. Far pass counters
**Symptom:** in divided kitchens, food went to a pass counter at the far end.
**Fix:** keep only nearby pass counters plus the best shared hand-off spots
for each pair of areas.

## 7. One group doing everything
**Symptom:** in open kitchens with two sets of stations, one side was idle.
**Fix:** cell splitting. Plan each group's share of the orders on its own,
then merge.

## 8. Rush-hour plate order
**Symptom:** a plate collected its parts in a slow order and missed deadlines.
**Fix:** the rescheduler also tries reordering how a plate collects its parts,
and keeps the change only if the estimated finish time drops.

## 9. The 2v2 host does not wait (Level 4)
**Symptom:** my chef stood still for many ticks at the start of each plan.
**Cause:** the host moves to the next tick as soon as every seat answers.
Planning in the background let the game race ahead.
**Fix:** spend up to 160 ms of the 200 ms budget waiting for the planner.

## 10. Teammate blocking (Level 4, still open)
My two chefs sometimes block each other in narrow kitchens. I tried
predicting the teammate's plan. Across 10 matches it averaged 491 points
against 498 without it, so I removed it. Measuring first saved me from
keeping a change that only sounded good.
