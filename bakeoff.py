"""Score models on this agent's actual job, not on a generic benchmark.

    python bakeoff.py                          the default panel on OpenRouter
    python bakeoff.py openai/gpt-5.4-mini ...  named models
    python bakeoff.py --provider gemini gemini-3.5-flash
    python bakeoff.py --selfcheck              scoring logic only, no API calls

Each case states what a competent run looks like. Scoring is mechanical, so the
result is reproducible and the failures are inspectable: results land in
bakeoff_results.json alongside the table.

Costs real money. A full panel is roughly 5 model calls per case per model.
"""

import asyncio
import json
import os
import sys
import time

import agent

PANEL = [
    "openai/gpt-5.4-mini",
    "inclusionai/ling-3.0-flash",
    "qwen/qwen3.7-plus",
    "qwen/qwen3.7-flash",
    "qwen/qwen3.7-max",
]

CASES = [
    {
        "name": "affective search",
        "ask": "songs that feel like driving away from my hometown for the last time",
        "want": "search_by_feel",
        "forbid": ["create_playlist"],
        "short_description": True,  # a query, not the user's sentence repeated back
        "want_links": True,
    },
    {
        "name": "lyrics search",
        "ask": "find songs whose lyrics are actually about calling someone at 2am",
        "want": "search_by_lyrics",
        "forbid": ["create_playlist"],
        "want_links": True,
    },
    {
        "name": "read my listening",
        "ask": "what have i been listening to lately, and what mood does it suggest",
        "want": "listening_lyrics",
        "forbid": ["create_playlist"],
        "want_links": False,
    },
    {
        "name": "restraint",  # the tools are there; the right move is to not use them
        "ask": "what does the valence number mean in your search tool? just explain it",
        "want": None,
        "forbid": ["create_playlist", "search_by_feel", "search_by_lyrics"],
        "want_links": False,
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


def score(case: dict, calls: list[str], reply: str, seconds: float, error: str) -> dict:
    """Mechanical scoring. Every point traces to something checkable in the run."""
    names = [_tool_name(c) for c in calls]
    points, notes = 0, []

    if error:
        return {"points": 0, "max": 5, "notes": [f"failed: {error[:60]}"],
                "calls": len(calls), "seconds": round(seconds, 1), "tools": names}

    if case["want"] is None:
        if not names:
            points += 2
        else:
            notes.append(f"called {names[0]} when nothing was needed")
    elif case["want"] in names:
        points += 2
        if names[0] != case["want"]:
            notes.append(f"got there via {names[0]} first")
    else:
        notes.append(f"wanted {case['want']}, used {names or 'nothing'}")

    if used := [n for n in names if n in case["forbid"]]:
        # disqualifying, not a deduction: create_playlist writes to a real account
        return {"points": 0, "max": 5, "notes": [f"used forbidden {used[0]}"],
                "calls": len(calls), "seconds": round(seconds, 1), "tools": names}
    points += 1

    if len(calls) == len(set(calls)):
        points += 1
    else:
        notes.append("repeated an identical call")

    if case["short_description"] if "short_description" in case else False:
        descs = [_description_of(c) for c in calls if _description_of(c)]
        if descs and all(len(d.split()) <= 5 for d in descs):
            points += 1
        elif descs:
            notes.append(f'description too long: "{max(descs, key=len)[:40]}"')
    elif case["want_links"]:
        points += 1 if "open.spotify.com" in reply else 0
        if "open.spotify.com" not in reply:
            notes.append("no track links in the reply")
    else:
        points += 1 if reply.strip() else 0

    return {"points": points, "max": 5, "notes": notes, "calls": len(calls),
            "seconds": round(seconds, 1), "tools": names}


async def run_case(model: str, provider: str, case: dict) -> dict:
    calls, reply, error = [], [], ""
    start = time.time()

    def on_part(kind: str, text: str) -> None:
        (calls if kind == "tool" else reply).append(text)

    try:
        await agent.run(case["ask"], on_part, model=model, provider=provider)
    except Exception as exc:  # noqa: BLE001 - a model that errors scores zero, not crashes
        error = f"{type(exc).__name__}: {exc}"
    return score(case, calls, "".join(reply), time.time() - start, error)


async def main(models: list[str], provider: str) -> None:
    results: dict[str, list[dict]] = {}
    for model in models:
        results[model] = []
        print(f"\n{model}")
        for case in CASES:
            got = await run_case(model, provider, case)
            results[model].append({"case": case["name"], **got})
            mark = "ok  " if got["points"] == got["max"] else "    "
            print(f"  {mark}{case['name']:22} {got['points']}/{got['max']}  "
                  f"{got['calls']} calls  {got['seconds']:5.1f}s  {'; '.join(got['notes'])}")

    print(f"\n{'model':30}{'score':>8}{'calls':>7}{'seconds':>9}")
    ranked = sorted(
        results.items(),
        key=lambda kv: (-sum(r["points"] for r in kv[1]), sum(r["calls"] for r in kv[1])),
    )
    for model, rows in ranked:
        total = sum(r["points"] for r in rows)
        print(f"{model:30}{total:>4}/{sum(r['max'] for r in rows):<3}"
              f"{sum(r['calls'] for r in rows):>7}{sum(r['seconds'] for r in rows):>9.1f}")

    with open("bakeoff_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("\nfull detail in bakeoff_results.json")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    provider = "gemini" if "--provider" in sys.argv and "gemini" in args else "openrouter"
    if provider == "gemini":
        args = [a for a in args if a != "gemini"]

    if "--selfcheck" in sys.argv:
        case = CASES[0]
        good = score(case, ["search_by_feel({'description': 'leaving home'})"],
                     "here: https://open.spotify.com/track/x", 3.0, "")
        assert good["points"] == 5, good
        wordy = score(case, ["search_by_feel({'description': 'driving away from my "
                             "hometown for the last time forever'})"],
                      "https://open.spotify.com/track/x", 3.0, "")
        assert wordy["points"] == 4 and "too long" in wordy["notes"][0], wordy
        repeat = score(case, ["search_by_feel({'description': 'a'})"] * 2,
                       "https://open.spotify.com/track/x", 3.0, "")
        assert "repeated" in repeat["notes"][0], repeat
        assert score(CASES[3], ["create_playlist({})"], "", 1.0, "")["points"] == 0
        assert score(case, [], "", 1.0, "429 quota")["points"] == 0
        print("ok")
    else:
        asyncio.run(main(args or PANEL, provider))
