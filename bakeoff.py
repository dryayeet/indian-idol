"""Score models on this agent's actual job, not on a generic benchmark.

    python bakeoff.py                          the default panel on OpenRouter
    python bakeoff.py openai/gpt-5.4-mini ...  named models
    python bakeoff.py --provider gemini gemini-3.5-flash
    python bakeoff.py --selfcheck              scoring logic only, no API calls

Correctness is 5 points a case, scored mechanically so it is reproducible. Three
things are measured alongside it, because a model that answers correctly while
burning ten tool calls is not the better model:

    cost        real dollars, read from the provider between models
    calls       tool calls made
    efficiency  the fewest calls the case needs, over the calls actually made

Costs real money. The default panel is roughly $0.30. Results land in
bakeoff_results.json.
"""

import asyncio
import json
import os
import sys
import time

import httpx
from langgraph.checkpoint.memory import InMemorySaver

import agent

# the incumbent, then every tool-capable model that is cheaper than it
PANEL = [
    "openai/gpt-5.4-mini",  # baseline: $0.75/$4.50 per 1M
    "inclusionai/ling-3.0-flash",  # $0.02/$0.06
    "qwen/qwen3.7-flash",  # $0.03/$0.13
    "upstage/solar-pro4",  # $0.03/$0.12
    "openai/gpt-oss-120b",  # $0.03/$0.17
    "deepseek/deepseek-v4-flash",  # $0.06/$0.13
    "openai/gpt-5.4-nano",  # $0.20/$1.25
    "qwen/qwen3.7-plus",  # $0.32/$1.28
]

CASES = [
    {
        "name": "affective search",
        "turns": ["songs that feel like driving away from my hometown for the last time"],
        "want": "search_by_feel",
        "forbid": ["create_playlist"],
        "golden": 1,
        "short_description": True,  # a query, not the user's sentence repeated back
    },
    {
        "name": "lyrics search",
        "turns": ["find songs whose lyrics are actually about calling someone at 2am"],
        "want": "search_by_lyrics",
        "forbid": ["create_playlist"],
        "golden": 1,
        "want_links": True,
    },
    {
        "name": "read my listening",
        "turns": ["what have i been listening to lately, and what mood does it suggest"],
        "want": "listening_lyrics",
        "forbid": ["create_playlist", "get_lyrics"],  # get_lyrics here means N round trips
        "golden": 1,
    },
    {
        "name": "restraint",  # the tools are there; the right move is to not use them
        "turns": ["what does the valence number mean in your search tool? just explain it"],
        "want": None,
        "forbid": ["create_playlist", "search_by_feel", "search_by_lyrics"],
        "golden": 0,
    },
    {
        "name": "recovery",  # the first search finds nothing; a good model rewords
        "turns": ["find songs whose lyrics are about competitive dishwashing at 3am"],
        "want": "search_by_lyrics",
        "forbid": ["create_playlist"],
        "golden": 2,
        "want_retry": True,
    },
    {
        "name": "follow-up",  # needs turn 1 in scope; afk holds the write for approval
        "turns": ["find two songs about rain", "put those in a playlist called Rain"],
        "want": "search_by_feel",
        "forbid": [],
        "golden": 2,
        "mode": "afk",
        "want_pending": "create_playlist",
    },
]


def _tool_name(call: str) -> str:
    return call.split("(", 1)[0]


def _description_of(call: str) -> str:
    """The description argument out of a rendered call, empty if absent."""
    for key in ("'description': '", '"description": "'):
        if key in call:
            return call.split(key, 1)[1].split(key[-1], 1)[0]
    return ""


def score(case: dict, calls: list[str], reply: str, pending: list, error: str) -> dict:
    """Mechanical scoring. Every point traces to something checkable in the run."""
    names = [_tool_name(c) for c in calls]
    golden = case.get("golden", 1)
    base = {
        "calls": len(calls),
        "tools": names,
        "efficiency": round(min(1.0, golden / len(calls)), 2) if calls else (1.0 if not golden else 0.0),
    }
    if error:
        return {"points": 0, "max": 5, "notes": [f"failed: {error[:60]}"], **base}
    if used := [n for n in names if n in case["forbid"]]:
        # disqualifying, not a deduction: create_playlist writes to a real account
        return {"points": 0, "max": 5, "notes": [f"used forbidden {used[0]}"], **base}

    points, notes = 1, []  # the forbidden check above is the first point

    if case["want"] is None:
        if names:
            notes.append(f"called {names[0]} when nothing was needed")
        else:
            points += 2
    elif case["want"] in names:
        points += 2
        if names[0] != case["want"]:
            notes.append(f"got there via {names[0]} first")
    else:
        notes.append(f"wanted {case['want']}, used {names or 'nothing'}")

    if len(calls) == len(set(calls)):
        points += 1
    else:
        notes.append("repeated an identical call")

    if case.get("want_pending"):
        if any(_tool_name(str(p)) == case["want_pending"] or p.get("name") == case["want_pending"] for p in pending):
            points += 1
        else:
            notes.append(f"never reached {case['want_pending']}")
    elif case.get("want_retry"):
        if len(set(calls)) >= 2:
            points += 1
        else:
            notes.append("gave up after one search instead of rewording")
    elif case.get("short_description"):
        descs = [_description_of(c) for c in calls if _description_of(c)]
        if descs and all(len(d.split()) <= 5 for d in descs):
            points += 1
        elif descs:
            notes.append(f'description too long: "{max(descs, key=len)[:40]}"')
    elif case.get("want_links"):
        if "open.spotify.com" in reply:
            points += 1
        else:
            notes.append("no track links in the reply")
    elif reply.strip():
        points += 1
    else:
        notes.append("no reply")

    return {"points": points, "max": 5, "notes": notes, **base}


async def run_case(model: str, provider: str, case: dict) -> dict:
    calls, reply, error, pending = [], [], "", []
    saver = InMemorySaver()
    thread = f"{model}-{case['name']}"
    start = time.time()

    def on_part(kind: str, text: str) -> None:
        (calls if kind == "tool" else reply).append(text)

    try:
        for ask in case["turns"]:
            pending = await agent.turn(
                ask,
                on_part,
                checkpointer=saver,
                thread_id=thread,
                mode=case.get("mode", "auto"),
                model=model,
                provider=provider,
            )
    except Exception as exc:  # noqa: BLE001 - a model that errors scores zero, not crashes
        error = f"{type(exc).__name__}: {exc}"
    got = score(case, calls, "".join(reply), pending, error)
    got["seconds"] = round(time.time() - start, 1)
    return got


SETTLE = 25  # seconds to let the provider's usage counter catch up before reading it


def _spent(key: str) -> float:
    """Dollars spent today, straight from OpenRouter. Deltas give cost per model."""
    try:
        r = httpx.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        return float(r.json()["data"]["usage_daily"])
    except Exception:  # noqa: BLE001 - cost is a nice-to-have, never fail the run for it
        return 0.0


async def main(models: list[str], provider: str) -> None:
    key = os.environ.get("OPENROUTER_API_KEY", "") if provider == "openrouter" else ""
    results: dict[str, dict] = {}

    for model in models:
        before = _spent(key) if key else 0.0
        rows = []
        print(f"\n{model}")
        for case in CASES:
            got = await run_case(model, provider, case)
            rows.append({"case": case["name"], **got})
            mark = "ok  " if got["points"] == got["max"] else "    "
            print(
                f"  {mark}{case['name']:18} {got['points']}/{got['max']}  "
                f"{got['calls']:>2} calls  eff {got['efficiency']:.2f}  "
                f"{got['seconds']:>5.1f}s  {'; '.join(got['notes'])}"
            )
        # OpenRouter's usage counter lags the calls by a few seconds; reading it
        # immediately reports zero and makes every model look free
        if key:
            await asyncio.sleep(SETTLE)
        cost = max(0.0, _spent(key) - before) if key else 0.0
        results[model] = {"cases": rows, "cost": round(cost, 4)}

    print(f"\n{'model':30}{'score':>7}{'calls':>7}{'eff':>6}{'secs':>7}{'cost':>9}")
    ranked = sorted(
        results.items(),
        key=lambda kv: (-sum(r["points"] for r in kv[1]["cases"]), kv[1]["cost"]),
    )
    for model, data in ranked:
        rows = data["cases"]
        total = sum(r["points"] for r in rows)
        eff = sum(r["efficiency"] for r in rows) / len(rows)
        print(
            f"{model:30}{total:>4}/{sum(r['max'] for r in rows):<3}"
            f"{sum(r['calls'] for r in rows):>6}{eff:>6.2f}"
            f"{sum(r['seconds'] for r in rows):>7.0f}{data['cost']:>9.4f}"
        )

    with open("bakeoff_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("\nfull detail in bakeoff_results.json")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    provider = "gemini" if "--provider" in sys.argv and "gemini" in args else "openrouter"
    if provider == "gemini":
        args = [a for a in args if a != "gemini"]

    if "--selfcheck" in sys.argv:
        feel = CASES[0]
        good = score(feel, ["search_by_feel({'description': 'leaving home'})"], "ok", [], "")
        assert good["points"] == 5 and good["efficiency"] == 1.0, good
        wordy = score(feel, ["search_by_feel({'description': 'driving away from my "
                             "hometown for the last time'})"], "ok", [], "")
        assert wordy["points"] == 4 and "too long" in wordy["notes"][0], wordy
        # three calls where one would do: still correct, but efficiency shows the cost
        slow = score(feel, [f"search_by_feel({{'description': 'x{i}'}})" for i in range(3)],
                     "ok", [], "")
        assert slow["points"] == 5 and slow["efficiency"] == 0.33, slow
        assert score(CASES[3], ["create_playlist({})"], "", [], "")["points"] == 0
        assert score(feel, [], "", [], "429 quota")["points"] == 0
        one = score(CASES[4], ["search_by_lyrics({'phrase': 'a'})"], "nothing found", [], "")
        assert "gave up" in one["notes"][0], one
        two = score(CASES[4], ["search_by_lyrics({'phrase': 'a'})",
                               "search_by_lyrics({'phrase': 'b'})"], "found", [], "")
        assert two["points"] == 5, two
        pend = score(CASES[5], ["search_by_feel({'description': 'rain'})"], "ok",
                     [{"name": "create_playlist", "args": {}}], "")
        assert pend["points"] == 5, pend
        print("ok")
    else:
        asyncio.run(main(args or PANEL, provider))
