# Level 4: 2v2 match

Two teams of two chefs share one kitchen. A match is two games of 300 ticks,
and each seat has 200 ms per tick to answer.

## Design

- **Reuses the planner.** `open/submission/controller.py` subclasses the
  Levels 1-3 controller and plans only for its own chef.
- **Waits for the plan.** The host advances once every seat answers, so the
  controller uses up to 160 ms of its budget to wait for the planner.
- **Avoids other chefs.** Paths route around all three other chefs.
- **One order at a time,** picked by points per step.
- **Goal recognition.** An order another chef is visibly working towards is
  left to them.
- **Replans when its dish leaves the board,** for example when someone else
  delivers it first.
- **Crash-proof.** Any error falls back to a safe action, so the seat never
  misses a tick.

## Results

| Match | Score |
| --- | --- |
| vs example team | 717–0 |
| solo vs example team | 439–0 |
| mirror (same controller both sides) | 314–384 |
| final vs previous version | 252–190, 303–175, 270–181 |

Raw numbers: [`results/level4_matches.csv`](../results/level4_matches.csv).
