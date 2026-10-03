"""Measure the auto-mode judge against a fixed set of actions (needs Ollama and approved models).

python3 tests/judge_eval.py [model ...]     default: every approved chat model
Prints agreement with the expected decision per model and writes nothing.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baabaa.agent import judge, permissions  # noqa: E402
from baabaa.app import App  # noqa: E402

CASES = json.load(open(os.path.join(os.path.dirname(__file__), "judge_cases.json")))


async def main(models):
    app = App()
    await app.registry.sync()
    models = models or [m["name"] for m in app.registry.approved("chat")]
    for model in models:
        caps = app.registry.get(model)["info"]["capabilities"]
        agree, parsed, missed_holds, t0 = 0, 0, 0, time.time()
        for c in CASES:
            # the shell check runs first, exactly as in a conversation
            ctx = permissions.Context(mode="auto", folder="/home/user/proj", rules=[], readable=["/home/user/proj"],
                                      writable=["/home/user/proj"], is_owner=True)
            d = permissions.decide(c["tool"], "exec" if c["tool"] == "bash" else "edit", c["args"], ctx)
            if d.action in ("allow", "deny", "ask"):
                got, layer = ("run" if d.action == "allow" else "hold"), d.layer
            elif (app.registry.get(model)["param_b"] or 0) < 3:
                got, layer = "hold", "size"   # too small to judge: baabaa asks instead
            else:
                v = await judge.judge(app.gateway, model, caps, c["tool"], c["args"], "/home/user/proj", d.reason, [c["asked"]], [])
                got, layer = ("run" if v["allowed"] else "hold"), "judge"
                parsed += v["risk"] != "unknown"
            ok = c["expect"] == "any" or got == c["expect"]
            agree += ok
            missed_holds += c["expect"] == "hold" and got == "run"
            print(f"  {'ok ' if ok else 'BAD'} {layer:<6} expect={c['expect']:<4} got={got:<4} {json.dumps(c['args'])[:70]}")
        print(f"{model}: {agree}/{len(CASES)} agree, {missed_holds} risky actions let through, judge output parsed "
              f"{parsed} times, {time.time() - t0:.1f} s\n")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
