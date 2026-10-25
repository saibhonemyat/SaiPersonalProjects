; Too Many Chefs domain.
;
; The domain plans at the task level. Movement is left to the navigation
; policy, which walks a chef next to the tile an action targets. We model
; which tiles each chef can reach with static (reach ?c ?l) facts, so
; divided kitchens and pass-through counters come out of the model for free.
;
; Food is described by type, not by instance. A counter holds at most one
; item, so (food_at ?l ?f) is enough to tell two buns apart.
;
; Plates are described by what is on them: (has_plate ?c nothing) is a chef
; holding an empty plate. Vessels (cutboard, pan, pot, oven, fryer, mixing
; bowl) are separate objects because their contents and position matter.
;
; Every recipe rule is a static fact computed in problem_generator.py from
; the level's recipes, so one domain covers every kitchen. The rule tables
; already encode which way the simulator resolves a Combine, for example
; whether food goes into a station or back into the chef's hand.
;
; All preconditions are positive, so pyperplan can read this domain too.

(define (domain overcooked)
  (:requirements :strips :typing)

  (:types
    chef loc vessel vtype order count content - object
    food - content
  )

  (:constants
    nothing - content
    o_end - order
  )

  (:predicates
    ; --- chefs ---
    (reach ?c - chef ?l - loc)
    (free ?c - chef)
    (has_food ?c - chef ?f - food)
    (has_plate ?c - chef ?x - content)
    (has_dirty ?c - chef)
    (has_vessel ?c - chef ?v - vessel)
    ; a chef on duty must collect food from a pan before it burns
    (off_duty ?c - chef)
    (on_duty ?c - chef ?v - vessel)
    (other ?c - chef ?d - chef)
    ; set when one chef must catch food alone (no second chef can reach)
    (solo_watch)

    ; --- tiles ---
    (counter ?l - loc)
    (clear ?l - loc)
    (food_at ?l - loc ?f - food)
    (plate_at ?l - loc ?x - content)
    (dirty_at ?l - loc)
    (storage ?l - loc ?f - food)
    (delivery ?l - loc)
    (bin ?l - loc)
    (sink ?l - loc)
    (dispenser ?l - loc)
    (disp_inf ?l - loc)
    (disp_clean ?l - loc ?n - count)
    (disp_dirty ?l - loc ?n - count)
    (zero ?n - count)
    (succ ?n - count ?m - count)
    (ret_none)
    (ret_clean ?l - loc)
    (ret_dirty ?l - loc)

    ; --- vessels ---
    (v_at ?v - vessel ?l - loc)
    (v_has ?v - vessel ?x - content)
    (v_type ?v - vessel ?t - vtype)
    (fixed ?v - vessel)
    (portable ?v - vessel)
    ; only vessels that start away from their stove or mixer get moved
    (movable ?v - vessel)
    (v_counter ?v - vessel)
    (works ?v - vessel)
    ; no chef is waiting to catch food from this vessel
    (unwatched ?v - vessel)
    ; a pan or bowl that is set down, not in a chef's hands (a goal at the end)
    (down ?v - vessel)
    ; the chef is close enough to this station to use it (parallel kitchens)
    (may_use ?c - chef ?v - vessel)
    (support_at ?l - loc ?t - vtype)
    (support_free ?l - loc)
    (manual ?t - vtype)
    (auto ?t - vtype)

    ; --- recipe rules (static) ---
    (counter_ok ?f - food)
    ; food no chosen order needs; only junk goes in the bin
    (junk ?f - food)
    ; one plate or fewer left: allow emptying plates and carrying vessels
    (scarce)
    (plating ?x - content ?a - food ?out - food)
    (counter_combo ?a - food ?b - food ?out - food)
    (put_rule ?t - vtype ?a - food ?y - content ?out - food)
    (watch_rule ?t - vtype ?a - food ?y - content ?out - food)
    (grab_rule ?t - vtype ?a - food ?y - food ?out - food)
    (pour_rule ?t - vtype ?x - food ?y - content ?out - food)
    (scoop_rule ?t - vtype ?x - content ?y - food ?out - food)
    (cook_rule ?t - vtype ?f - food ?out - food)

    ; --- orders ---
    (wants ?o - order ?f - food)
    (open ?o - order)
    (ready ?o - order)
    (next ?o - order ?p - order)
    (served ?o - order)
  )

  ; ======================= storage and dispensers =======================

  (:action take_food
    :parameters (?c - chef ?l - loc ?f - food)
    :precondition (and (reach ?c ?l) (free ?c) (storage ?l ?f))
    :effect (and (has_food ?c ?f) (not (free ?c)))
  )

  ; Holding a plate at a storage puts the ingredient straight on the plate.
  (:action take_food_to_plate
    :parameters (?c - chef ?l - loc ?f - food ?x - content ?out - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_plate ?c ?x)
                       (storage ?l ?f) (plating ?x ?f ?out))
    :effect (and (has_plate ?c ?out) (not (has_plate ?c ?x)))
  )

  (:action take_plate
    :parameters (?c - chef ?l - loc ?n - count ?m - count)
    :precondition (and (reach ?c ?l) (free ?c) (dispenser ?l)
                       (disp_clean ?l ?n) (succ ?m ?n))
    :effect (and (has_plate ?c nothing) (not (free ?c))
                 (disp_clean ?l ?m) (not (disp_clean ?l ?n)))
  )

  (:action take_plate_inf
    :parameters (?c - chef ?l - loc)
    :precondition (and (reach ?c ?l) (free ?c) (disp_inf ?l))
    :effect (and (has_plate ?c nothing) (not (free ?c)))
  )

  ; A dispenser hands out dirty plates once its clean ones run out.
  (:action take_dirty_plate
    :parameters (?c - chef ?l - loc ?z - count ?n - count ?m - count)
    :precondition (and (reach ?c ?l) (free ?c) (off_duty ?c) (dispenser ?l)
                       (disp_clean ?l ?z) (zero ?z)
                       (disp_dirty ?l ?n) (succ ?m ?n))
    :effect (and (has_dirty ?c) (not (free ?c))
                 (disp_dirty ?l ?m) (not (disp_dirty ?l ?n)))
  )

  ; ============================ counters ============================

  (:action pick_food
    :parameters (?c - chef ?l - loc ?f - food)
    :precondition (and (reach ?c ?l) (free ?c) (food_at ?l ?f))
    :effect (and (has_food ?c ?f) (not (free ?c))
                 (clear ?l) (not (food_at ?l ?f)))
  )

  (:action drop_food
    :parameters (?c - chef ?l - loc ?f - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_food ?c ?f)
                       (counter ?l) (clear ?l) (counter_ok ?f))
    :effect (and (food_at ?l ?f) (not (clear ?l))
                 (free ?c) (not (has_food ?c ?f)))
  )

  (:action pick_plate
    :parameters (?c - chef ?l - loc ?x - content)
    :precondition (and (reach ?c ?l) (free ?c) (plate_at ?l ?x))
    :effect (and (has_plate ?c ?x) (not (free ?c))
                 (clear ?l) (not (plate_at ?l ?x)))
  )

  (:action drop_plate
    :parameters (?c - chef ?l - loc ?x - content)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_plate ?c ?x)
                       (counter ?l) (clear ?l))
    :effect (and (plate_at ?l ?x) (not (clear ?l))
                 (free ?c) (not (has_plate ?c ?x)))
  )

  (:action pick_dirty
    :parameters (?c - chef ?l - loc)
    :precondition (and (reach ?c ?l) (free ?c) (off_duty ?c) (dirty_at ?l))
    :effect (and (has_dirty ?c) (not (free ?c))
                 (clear ?l) (not (dirty_at ?l)))
  )

  (:action drop_dirty
    :parameters (?c - chef ?l - loc)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_dirty ?c)
                       (counter ?l) (clear ?l))
    :effect (and (dirty_at ?l) (not (clear ?l))
                 (free ?c) (not (has_dirty ?c)))
  )

  (:action wash
    :parameters (?c - chef ?l - loc)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_dirty ?c) (sink ?l))
    :effect (and (has_plate ?c nothing) (not (has_dirty ?c)))
  )

  ; Held food onto loose food on a counter. Only food that may sit on a
  ; bare counter can be made this way.
  (:action combine_on_counter
    :parameters (?c - chef ?l - loc ?a - food ?b - food ?out - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_food ?c ?a)
                       (food_at ?l ?b) (counter_combo ?a ?b ?out))
    :effect (and (food_at ?l ?out) (not (food_at ?l ?b))
                 (free ?c) (not (has_food ?c ?a)))
  )

  ; Held food onto a plate sitting on a counter.
  (:action food_to_plate
    :parameters (?c - chef ?l - loc ?a - food ?x - content ?out - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_food ?c ?a)
                       (plate_at ?l ?x) (plating ?x ?a ?out))
    :effect (and (plate_at ?l ?out) (not (plate_at ?l ?x))
                 (free ?c) (not (has_food ?c ?a)))
  )

  ; Held plate takes loose food from a counter.
  (:action plate_takes_food
    :parameters (?c - chef ?l - loc ?x - content ?a - food ?out - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_plate ?c ?x)
                       (food_at ?l ?a) (plating ?x ?a ?out))
    :effect (and (has_plate ?c ?out) (not (has_plate ?c ?x))
                 (clear ?l) (not (food_at ?l ?a)))
  )

  ; ============================ vessels ============================

  (:action put_in_vessel
    :parameters (?c - chef ?v - vessel ?l - loc ?t - vtype
                 ?a - food ?y - content ?out - food)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (off_duty ?c) (has_food ?c ?a) (unwatched ?v)
                       (v_at ?v ?l) (v_type ?v ?t) (v_has ?v ?y)
                       (put_rule ?t ?a ?y ?out))
    :effect (and (v_has ?v ?out) (not (v_has ?v ?y))
                 (free ?c) (not (has_food ?c ?a)))
  )

  ; Food that burns if left in the pan. Another chef, already holding a
  ; plate, goes on duty to catch it as soon as it is cooked.
  (:action put_in_vessel_watch
    :parameters (?c - chef ?d - chef ?v - vessel ?l - loc ?t - vtype
                 ?a - food ?y - content ?out - food ?x - content)
    :precondition (and (may_use ?c ?v) (may_use ?d ?v) (reach ?c ?l) (off_duty ?c) (has_food ?c ?a)
                       (other ?c ?d) (reach ?d ?l) (off_duty ?d) (has_plate ?d ?x)
                       (unwatched ?v)
                       (v_at ?v ?l) (v_type ?v ?t) (v_has ?v ?y)
                       (watch_rule ?t ?a ?y ?out))
    :effect (and (v_has ?v ?out) (not (v_has ?v ?y))
                 (free ?c) (not (has_food ?c ?a))
                 (on_duty ?d ?v) (not (off_duty ?d)) (not (unwatched ?v)))
  )

  ; With nobody to help, the chef that puts the food in catches it itself.
  (:action put_in_vessel_watch_solo
    :parameters (?c - chef ?v - vessel ?l - loc ?t - vtype
                 ?a - food ?y - content ?out - food)
    :precondition (and (may_use ?c ?v) (solo_watch) (reach ?c ?l) (off_duty ?c) (has_food ?c ?a)
                       (unwatched ?v)
                       (v_at ?v ?l) (v_type ?v ?t) (v_has ?v ?y)
                       (watch_rule ?t ?a ?y ?out))
    :effect (and (v_has ?v ?out) (not (v_has ?v ?y))
                 (free ?c) (not (has_food ?c ?a))
                 (on_duty ?c ?v) (not (off_duty ?c)) (not (unwatched ?v)))
  )

  ; Held food combines with the vessel's food, and the result comes back
  ; into the chef's hand because the vessel cannot hold it.
  (:action grab_from_vessel
    :parameters (?c - chef ?v - vessel ?l - loc ?t - vtype
                 ?a - food ?y - food ?out - food)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (off_duty ?c) (has_food ?c ?a) (unwatched ?v)
                       (v_at ?v ?l) (v_type ?v ?t) (v_has ?v ?y)
                       (grab_rule ?t ?a ?y ?out))
    :effect (and (has_food ?c ?out) (not (has_food ?c ?a))
                 (v_has ?v nothing) (not (v_has ?v ?y)))
  )

  (:action grab_from_vessel_duty
    :parameters (?c - chef ?v - vessel ?l - loc ?t - vtype
                 ?a - food ?y - food ?out - food)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (on_duty ?c ?v) (has_food ?c ?a)
                       (v_at ?v ?l) (v_type ?v ?t) (v_has ?v ?y)
                       (grab_rule ?t ?a ?y ?out))
    :effect (and (has_food ?c ?out) (not (has_food ?c ?a))
                 (v_has ?v nothing) (not (v_has ?v ?y))
                 (off_duty ?c) (not (on_duty ?c ?v)) (unwatched ?v))
  )

  ; Take food out of a fixed station (cutboard, oven, fryer) by hand.
  (:action take_out
    :parameters (?c - chef ?v - vessel ?l - loc ?f - food)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (free ?c) (off_duty ?c) (fixed ?v)
                       (v_at ?v ?l) (v_has ?v ?f))
    :effect (and (has_food ?c ?f) (not (free ?c))
                 (v_has ?v nothing) (not (v_has ?v ?f)))
  )

  ; Held plate empties its food into a vessel.
  (:action pour_plate
    :parameters (?c - chef ?v - vessel ?l - loc ?t - vtype
                 ?x - food ?y - content ?out - food)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (off_duty ?c) (has_plate ?c ?x)
                       (v_at ?v ?l) (v_type ?v ?t) (v_has ?v ?y)
                       (pour_rule ?t ?x ?y ?out))
    :effect (and (v_has ?v ?out) (not (v_has ?v ?y))
                 (has_plate ?c nothing) (not (has_plate ?c ?x)))
  )

  ; Held plate takes the vessel's food.
  (:action scoop
    :parameters (?c - chef ?v - vessel ?l - loc ?t - vtype
                 ?x - content ?y - food ?out - food)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (off_duty ?c) (has_plate ?c ?x) (unwatched ?v)
                       (v_at ?v ?l) (v_type ?v ?t) (v_has ?v ?y)
                       (scoop_rule ?t ?x ?y ?out))
    :effect (and (has_plate ?c ?out) (not (has_plate ?c ?x))
                 (v_has ?v nothing) (not (v_has ?v ?y)))
  )

  (:action scoop_duty
    :parameters (?c - chef ?v - vessel ?l - loc ?t - vtype
                 ?x - content ?y - food ?out - food)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (on_duty ?c ?v) (has_plate ?c ?x)
                       (v_at ?v ?l) (v_type ?v ?t) (v_has ?v ?y)
                       (scoop_rule ?t ?x ?y ?out))
    :effect (and (has_plate ?c ?out) (not (has_plate ?c ?x))
                 (v_has ?v nothing) (not (v_has ?v ?y))
                 (off_duty ?c) (not (on_duty ?c ?v)) (unwatched ?v))
  )

  ; A chef works a station by hand (chopping, or any station when cooking
  ; is not timed). from_pddl.py repeats the press cook_time times.
  (:action cook
    :parameters (?c - chef ?v - vessel ?l - loc ?t - vtype ?f - food ?out - food)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (off_duty ?c) (v_at ?v ?l) (v_type ?v ?t)
                       (manual ?t) (works ?v) (v_has ?v ?f)
                       (cook_rule ?t ?f ?out))
    :effect (and (v_has ?v ?out) (not (v_has ?v ?f)))
  )

  ; A station that cooks by itself. No chef is needed, so from_pddl.py
  ; emits no mutation; the next step that uses the food waits for it.
  (:action auto_cook
    :parameters (?v - vessel ?t - vtype ?f - food ?out - food)
    :precondition (and (v_type ?v ?t) (auto ?t) (works ?v) (v_has ?v ?f)
                       (cook_rule ?t ?f ?out))
    :effect (and (v_has ?v ?out) (not (v_has ?v ?f)))
  )

  ; ======================= portable vessels =======================

  (:action pick_vessel_counter
    :parameters (?c - chef ?v - vessel ?l - loc ?y - content)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (free ?c) (off_duty ?c) (movable ?v)
                       (v_at ?v ?l) (v_counter ?v) (v_has ?v ?y))
    :effect (and (not (down ?v)) (has_vessel ?c ?v) (not (free ?c))
                 (not (v_at ?v ?l)) (not (v_counter ?v)) (clear ?l))
  )

  (:action pick_vessel_support
    :parameters (?c - chef ?v - vessel ?l - loc ?y - content)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (free ?c) (off_duty ?c) (movable ?v)
                       (v_at ?v ?l) (works ?v) (v_has ?v ?y))
    :effect (and (not (down ?v)) (has_vessel ?c ?v) (not (free ?c))
                 (not (v_at ?v ?l)) (not (works ?v)) (support_free ?l))
  )

  (:action place_vessel_support
    :parameters (?c - chef ?v - vessel ?l - loc ?t - vtype)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_vessel ?c ?v)
                       (v_type ?v ?t) (support_at ?l ?t) (support_free ?l))
    :effect (and (down ?v) (v_at ?v ?l) (works ?v) (not (support_free ?l))
                 (free ?c) (not (has_vessel ?c ?v)))
  )

  (:action place_vessel_counter
    :parameters (?c - chef ?v - vessel ?l - loc)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_vessel ?c ?v)
                       (counter ?l) (clear ?l))
    :effect (and (down ?v) (v_at ?v ?l) (v_counter ?v) (not (clear ?l))
                 (free ?c) (not (has_vessel ?c ?v)))
  )

  ; A pan holding junk (burnt meat) is lifted off its stove, emptied into
  ; the bin with discard_vessel_food, then put back with place_vessel_support.
  (:action pick_vessel_junk
    :parameters (?c - chef ?v - vessel ?l - loc ?y - food)
    :precondition (and (may_use ?c ?v) (reach ?c ?l) (free ?c) (off_duty ?c) (portable ?v)
                       (v_at ?v ?l) (works ?v) (v_has ?v ?y) (junk ?y))
    :effect (and (not (down ?v)) (has_vessel ?c ?v) (not (free ?c))
                 (not (v_at ?v ?l)) (not (works ?v)) (support_free ?l))
  )

  (:action discard_vessel_food
    :parameters (?c - chef ?l - loc ?v - vessel ?y - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_vessel ?c ?v)
                       (v_has ?v ?y) (bin ?l) (junk ?y))
    :effect (and (v_has ?v nothing) (not (v_has ?v ?y)))
  )

  ; A held pan or bowl pours its food onto a plate on a counter.
  (:action pour_vessel_to_plate
    :parameters (?c - chef ?v - vessel ?l - loc ?y - food ?x - content ?out - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_vessel ?c ?v)
                       (v_has ?v ?y) (plate_at ?l ?x) (plating ?x ?y ?out))
    :effect (and (plate_at ?l ?out) (not (plate_at ?l ?x))
                 (v_has ?v nothing) (not (v_has ?v ?y)))
  )

  ; A held pan or bowl pours its food into another vessel, such as batter
  ; from the mixing bowl into the oven.
  (:action pour_vessel_to_vessel
    :parameters (?c - chef ?v - vessel ?w - vessel ?l - loc ?t - vtype
                 ?y - food ?z - content ?out - food)
    :precondition (and (may_use ?c ?w) (reach ?c ?l) (off_duty ?c) (has_vessel ?c ?v)
                       (v_has ?v ?y) (v_at ?w ?l) (v_type ?w ?t) (v_has ?w ?z)
                       (unwatched ?w) (pour_rule ?t ?y ?z ?out))
    :effect (and (v_has ?w ?out) (not (v_has ?w ?z))
                 (v_has ?v nothing) (not (v_has ?v ?y)))
  )

  ; ======================= delivery and bin =======================

  ; ?p is the order that becomes ready next under strict ordering. Without
  ; strict ordering every order points at o_end. The three variants cover
  ; where the used plate goes: nowhere, back clean, or back dirty.
  (:action deliver
    :parameters (?c - chef ?l - loc ?f - food ?o - order ?p - order)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_plate ?c ?f) (delivery ?l)
                       (wants ?o ?f) (open ?o) (ready ?o) (next ?o ?p) (ret_none))
    :effect (and (served ?o) (not (open ?o)) (ready ?p)
                 (free ?c) (not (has_plate ?c ?f)))
  )

  (:action deliver_ret_clean
    :parameters (?c - chef ?l - loc ?f - food ?o - order ?p - order
                 ?d - loc ?n - count ?m - count)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_plate ?c ?f) (delivery ?l)
                       (wants ?o ?f) (open ?o) (ready ?o) (next ?o ?p)
                       (ret_clean ?d) (disp_clean ?d ?n) (succ ?n ?m))
    :effect (and (served ?o) (not (open ?o)) (ready ?p)
                 (free ?c) (not (has_plate ?c ?f))
                 (disp_clean ?d ?m) (not (disp_clean ?d ?n)))
  )

  (:action deliver_ret_dirty
    :parameters (?c - chef ?l - loc ?f - food ?o - order ?p - order
                 ?d - loc ?n - count ?m - count)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_plate ?c ?f) (delivery ?l)
                       (wants ?o ?f) (open ?o) (ready ?o) (next ?o ?p)
                       (ret_dirty ?d) (disp_dirty ?d ?n) (succ ?n ?m))
    :effect (and (served ?o) (not (open ?o)) (ready ?p)
                 (free ?c) (not (has_plate ?c ?f))
                 (disp_dirty ?d ?m) (not (disp_dirty ?d ?n)))
  )

  (:action discard_food
    :parameters (?c - chef ?l - loc ?f - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_food ?c ?f) (bin ?l)
                       (junk ?f))
    :effect (and (free ?c) (not (has_food ?c ?f)))
  )

  (:action discard_plate_food
    :parameters (?c - chef ?l - loc ?f - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_plate ?c ?f) (bin ?l)
                       (junk ?f))
    :effect (and (has_plate ?c nothing) (not (has_plate ?c ?f)))
  )

  ; With plates this scarce, any food may be binned to free the plate.
  (:action discard_plate_food_scarce
    :parameters (?c - chef ?l - loc ?f - food)
    :precondition (and (reach ?c ?l) (off_duty ?c) (has_plate ?c ?f) (bin ?l)
                       (scarce))
    :effect (and (has_plate ?c nothing) (not (has_plate ?c ?f)))
  )
)
