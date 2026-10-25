"""Replay a plan through the PDDL domain, one state per step.

from_pddl.py uses this to spot races between chefs. Each chef runs its own
queue, so a step planned for later can happen early if it is already legal.
We only need positive preconditions and add/delete effects, which is all
the domain uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Fact = tuple[str, ...]


def _tokens(text: str) -> list[str]:
    text = re.sub(r";[^\n]*", "", text)
    return re.findall(r"\(|\)|[^\s()]+", text.lower())


def _tree(tokens: list[str]) -> list:
    stack: list[list] = [[]]
    for token in tokens:
        if token == "(":
            stack.append([])
        elif token == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(token)
    return stack[0][0]


def _typed(items: list[str]) -> list[tuple[str, str]]:
    out, pending, i = [], [], 0
    while i < len(items):
        if items[i] == "-":
            out += [(name, items[i + 1]) for name in pending]
            pending = []
            i += 2
        else:
            pending.append(items[i])
            i += 1
    return out + [(name, "object") for name in pending]


def _atoms(expr: list) -> tuple[list[Fact], list[Fact]]:
    if not expr:
        return [], []
    if expr[0] == "and":
        pos: list[Fact] = []
        neg: list[Fact] = []
        for sub in expr[1:]:
            p, n = _atoms(sub)
            pos += p
            neg += n
        return pos, neg
    if expr[0] == "not":
        return [], [tuple(expr[1])]
    return [tuple(expr)], []


@dataclass
class Schema:
    params: list[tuple[str, str]]
    pre: list[Fact]
    add: list[Fact]
    delete: list[Fact]


class Domain:
    def __init__(self, text: str) -> None:
        self.actions: dict[str, Schema] = {}
        for section in _tree(_tokens(text))[2:]:
            if section[0] != ":action":
                continue
            body = dict(zip(section[2::2], section[3::2]))
            pre, _ = _atoms(body.get(":precondition", []))
            add, delete = _atoms(body.get(":effect", []))
            self.actions[section[1]] = Schema(
                _typed(body[":parameters"]), pre, add, delete
            )

    def ground(self, name: str, args: tuple[str, ...]):
        schema = self.actions[name]
        binding = {p: a for (p, _), a in zip(schema.params, args)}

        def sub(atom: Fact) -> Fact:
            return (atom[0], *(binding.get(x, x) for x in atom[1:]))

        pre = {sub(a) for a in schema.pre}
        add = {sub(a) for a in schema.add}
        delete = {sub(a) for a in schema.delete} - add
        return pre, add, delete

    def location(self, name: str, args: tuple[str, ...]) -> str | None:
        """The first location parameter: the tile the chef works at."""
        for (_, kind), value in zip(self.actions[name].params, args):
            if kind == "loc":
                return value
        return None


def initial_state(problem: str) -> set[Fact]:
    for section in _tree(_tokens(problem))[2:]:
        if section[0] == ":init":
            return {tuple(fact) for fact in section[1:]}
    return set()


def goal_facts(problem: str) -> set[Fact]:
    for section in _tree(_tokens(problem))[2:]:
        if section[0] == ":goal":
            pos, _ = _atoms(section[1])
            return set(pos)
    return set()


def eliminate_redundant(
    domain: Domain, init: set[Fact], goal: set[Fact], steps: list[tuple[str, tuple]]
) -> list[int]:
    """Greedy action elimination: return the indexes of steps worth keeping.

    Lama-first returns the first plan it finds, which can contain detours
    such as fetching meat and leaving it on a counter. For each step in turn
    we try dropping it, together with any later step that stops being
    applicable without it. If the goals still hold, the steps were not
    needed.
    """
    keep = list(range(len(steps)))

    def run(indexes: list[int]) -> list[int] | None:
        state = set(init)
        done = []
        for i in indexes:
            name, args = steps[i]
            pre, add, delete = domain.ground(name, args)
            if not pre <= state:
                continue
            state = (state - delete) | add
            done.append(i)
        return done if goal <= state else None

    if run(keep) != keep:
        return keep  # the plan does not replay cleanly; leave it alone
    position = 0
    while position < len(keep):
        trial = keep[:position] + keep[position + 1 :]
        result = run(trial)
        if result is not None:
            keep = result
        else:
            position += 1
    return keep
