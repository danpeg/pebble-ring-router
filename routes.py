#!/usr/bin/env python3
"""Route tool for the ring: see where notes went, fix a wrong route, and check the definitions.

  python3 routes.py show                         the definitions, the limit, and the count of examples
  python3 routes.py last [N]                     the last N routed notes (default 5): file, receiver, how
  python3 routes.py add ROUTE --note FILE        correct that note and add its text to the examples
  python3 routes.py add ROUTE "TEXT"             add a typed example
  python3 routes.py check                        test each example without using it to pick its own route
  python3 routes.py check --with ROUTE "TEXT"    the same, with a new definition for ROUTE that is NOT saved
  python3 routes.py set ROUTE "TEXT"             save a new definition for ROUTE (config.json on this Mac)
  python3 routes.py move FILE ROUTE              deliver a note that went to the wrong receiver

ROUTE is a receiver from the ring config. `check` can send examples to the decision model service.
Examples: `route-examples.tsv` beside this file (no private names) and `~/.ring-router/route-examples.tsv`
(your real notes; private; never in git). Read README.md, "Receivers".
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import receiver  # noqa: E402
from route_store import WEEK, note_route, routed_notes  # noqa: E402

HOME = receiver.HOME


def need_route(name):
    if not receiver.known_route(name):
        raise SystemExit("unknown receiver. Use one of: " + ", ".join(receiver.live("route_descriptions")))
    return name


def show_result(result):
    if result.status != 200:
        raise SystemExit(result.message)
    print(result.message)
    return 0


def corrections_this_week():
    now = time.time()
    start, end = receiver.iso(now - WEEK), receiver.iso(now)
    try:
        with open(os.path.join(HOME, "receiver.log"), encoding="utf-8") as f:
            return sum(start <= line[:20] <= end and line[21:].rstrip() == "route corrected" for line in f)
    except FileNotFoundError:
        return 0


def cmd_show():
    for route, text in receiver.live("route_descriptions").items():
        print("%s:\n  %s" % (route, text))
    ex = receiver.EXAMPLES.all()
    print("limit: a pick below %.2f goes to the default receiver (%s)" % (receiver.live("jev_min_confidence"), receiver.live("default_route")))
    print("examples: %d (%d in the repo, %d on this Mac only)" %
          (len(ex), sum(row.origin == "repo" for row in ex), sum(row.origin == "mac" for row in ex)))
    print("corrections in the last 7 days: %d" % corrections_this_week())
    return 0


def cmd_last(count):
    notes = routed_notes(HOME)
    if not notes:
        print("no routed notes")
    for _, path, meta, _ in notes[-count:]:
        print("%s | %-12s | %s" % (path.name, note_route(meta), meta["routed_by"][:46]))
    return 0


def cmd_add(route, rest):
    need_route(route)
    if rest[:1] == ["--note"] and len(rest) == 2:
        return show_result(receiver.correct(os.path.basename(rest[1]), route, learn_existing=True))
    if len(rest) != 1 or rest[0] == "--note":
        raise SystemExit('usage: add ROUTE --note FILE   or   add ROUTE "TEXT"')
    if not rest[0].strip():
        raise SystemExit("the note has no text")
    if route == "muse" and receiver.live("keep_muse_examples") is not True:
        raise SystemExit("set keep_muse_examples to save examples for Muse")
    added = receiver.learn_example(route, rest[0])
    print("added an example for %s" % route if added else "already an example")
    return 0


def cmd_check(rest):
    descriptions = dict(receiver.live("route_descriptions"))
    if rest[:1] == ["--with"] and len(rest) == 3:
        descriptions[need_route(rest[1])] = rest[2]
        print("trying a new definition for %s (not saved)" % rest[1])
    elif rest:
        raise SystemExit("usage: check [--with ROUTE \"TEXT\"]")
    ex = receiver.EXAMPLES.all()
    if not ex:
        raise SystemExit("no examples")
    wrong = skipped = 0
    allowed = receiver.jev_example_routes()
    for i, example in enumerate(ex):
        want = example.route
        # Stored examples subject to the privacy rule stay local even during the check.
        got, how = receiver.choose_route(example.match_text, False, descriptions=descriptions,
                                         examples=ex[:i] + ex[i + 1:],
                                         bookmark_author="" if example.kind != "ring" else None,
                                         allow_model=example.kind == "ring" and want in allowed)
        privacy_fallback = how == receiver.PRIVATE_EXAMPLE_FALLBACK
        ok = got == want and (privacy_fallback or not how.startswith("default ("))
        wrong += not ok
        skipped += ok and privacy_fallback
        if privacy_fallback:
            how = "model check skipped"
        print("%s example %d | %-12s -> %-12s | %s" % ("ok  " if ok else "FAIL", i + 1, want, got, how[:40]))
    print("%d of %d passed (%d model check skipped)" % (len(ex) - wrong, len(ex), skipped))
    return 1 if wrong else 0


def cmd_set(route, text):
    need_route(route)
    text = " ".join(text.split())
    if len(text) < 20:
        raise SystemExit("a definition needs one full sentence")
    p = os.path.join(HOME, "config.json")
    config = {}
    if os.path.exists(p):
        with open(p) as f:
            config = json.load(f)
    descriptions = dict(receiver.live("route_descriptions"))
    print("old: %s" % descriptions[route])
    descriptions[route] = text
    config["route_descriptions"] = descriptions
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    print("new: %s" % text)
    print("saved. The receiver reads it for the next note; no restart.")
    return 0


def cmd_move(name, route):
    return show_result(receiver.correct(os.path.basename(name), need_route(route)))


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "show" and not rest:
        return cmd_show()
    if cmd == "last" and len(rest) <= 1:
        return cmd_last(int(rest[0]) if rest else 5)
    if cmd == "add" and rest:
        return cmd_add(rest[0], rest[1:])
    if cmd == "check":
        return cmd_check(rest)
    if cmd == "set" and len(rest) == 2:
        return cmd_set(rest[0], rest[1])
    if cmd == "move" and len(rest) == 2:
        return cmd_move(rest[0], rest[1])
    raise SystemExit("unknown command. Run: python3 routes.py --help")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
