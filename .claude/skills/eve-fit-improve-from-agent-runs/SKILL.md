---
name: eve-fit-improve-from-agent-runs
description: Use after a fitting task (often done by a subagent) to improve eve-fit itself - when the user asks to "look at how the agent used Pyfa", "give it better tools", or after an agent reports tool feedback. Turns what agents had to hand-write or got wrong into new or fixed eve-fit tools, verified and documented.
---

# Improving eve-fit from real agent runs

eve-fit grows by watching how agents actually use it. Every fitting task is also a usability test.

## 1. Run the task with a well-briefed agent
- Brief the agent from the `eve-fit-design-fit` skill: the goal, the pilot, the library API, the
  "don't edit the repo" rule, the lessons so far, and **"end with a tool feedback section"**.
- Give it its own scratch folder for scripts and results, so they can be reviewed afterwards.

## 2. Verify its deliverable before relaying it
- Re-evaluate every fit it saved (`evaluate.evaluate(eft, pilot=…)`): valid, no missing skills, and
  numbers that match its report.
- Re-import any `.xml` through Pyfa's file path (`P.Port.importAuto(text, path)`) to confirm modules and cargo
  survive.
- Spot-check surprising claims against item data (`items.item_info(name, attributes="all")`), such as
  restrictions or required skills. A user-reported problem ("it imported wrong") is a bug to reproduce
  exactly, never to guess at.

## 3. Mine the run for friction
Read the agent's scripts (not its transcript; that's too big) and its feedback, and look for:
- **Hand-written loops or formulas**: enumeration, Pareto filters, ammo tables, range falloff, refit diffs,
  per-profile EHP, skill closures. Each one is a missing tool or helper.
- **Wrong conclusions caused by the tools**: metrics that mislead (e.g. the minimum range across mixed
  weapons), defaults that hide reality (prop always on), invalid states the engine created (drone bandwidth).
- **Missing data views**: T3C layouts, applied DPS, travel (no-prop) stats, cargo.
- **Noise**: spurious warnings, results that hide fields unless certain options are set.

## 4. Turn friction into tools
For each finding, follow the `eve-fit-add-tool` skill:
- Prefer **reusing Pyfa** (its graph maths, port formats, fitting rules) over new maths.
- Keep `evaluate_fit@1` additive. Tell the eve-esi sessions about any change that affects them.
- Add a test that reproduces the original failure (e.g. mixed light missiles plus artillery range, a
  nullifier halving drone bandwidth, a Pyfa `.eft` file import).
- Update `docs/AGENT_GUIDE.md` and the `eve-fit-design-fit` skill so the next agent knows the new tool
  exists and when to use it.

## 5. Close the loop
- Re-run a slice of the original task with the new tool to show the hand-written code is no longer
  needed (e.g. `skill_plan` reproduced a hand-built plan exactly in 2 s instead of 4 minutes).
- Report to the user: what the agent produced (verified), what friction you found, and what you changed (with
  commit hashes). Keep their deliverables (fits, plans) in `fits/`, which is gitignored and private.
