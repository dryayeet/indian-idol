"""Jev vs the bakeoff's top-2 models, on tool selection as a classification task.

    python benchmarks/bakeoff_jev.py
    python benchmarks/bakeoff_jev.py --selfcheck     scoring logic only, no API calls

The bakeoff showed every serious model picks the right tool; the spread was in
persistence, not selection. Picking one tool out of a fixed inventory from a user
utterance is classification, which is exactly what Jev's `choice` primitive is.
This harness runs Jev over the bakeoff's own six cases (same wording, from
bakeoff.CASES) and compares against the LLM runs of 2026-08-16 that are already
recorded in archived benchmark output; those runs are NOT repeated.

Parity notes:
- The LLM saw every tool from both MCP servers (agent loads them over MCP), so
  Jev's choice criteria are the same full inventory plus "none".
- The LLM's prompt carries the user's playlist names (agent._prompt), so they go
  into Jev's state too. Best effort: if Spotify is unreachable the state ships
  without them, which only matters for playlist-name cases, of which there are
  none in the six.
- The LLM was scored on behavior in the loop (tool call sequences, retries,
  replies); Jev is scored on the classification alone. Recovery's "rewording"
  behavior has no Jev equivalent: only its turn-one tool choice is compared.

Results land in artifacts/benchmarks/bakeoff_jev_results.json. The write-up is in
docs/benchmarks/archive/jev-pilot.md.
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ARTIFACTS = os.path.join(ROOT, "artifacts", "benchmarks")
RESULTS = os.path.join(ARTIFACTS, "bakeoff_jev_results.json")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

MODEL = "typesafe/jev-1.13"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
TRIES = 3

# The same six cases the LLMs ran, imported so the wording cannot drift.
from bakeoff import CASES  # noqa: E402


def _expected(case: dict) -> list[list[str]]:
    """Per-turn accepted tool sets, derived from the case's own rubric fields.

    want=None (restraint) becomes ["none"]. want_pending (follow-up turn 2)
    becomes create_playlist. In the real bakeoff the follow-up only scores
    create_playlist reaching *pending*; here the classification itself is scored.
    """
    want = case["want"]
    first = [want] if isinstance(want, str) else (list(want) if want else [])
    turns = [first or ["none"]]
    if case.get("want_pending"):
        turns.append([case["want_pending"]])
    return turns


def _state(case: dict, upto: int, playlists: list[str]) -> dict:
    """What the classifier sees for turn `upto` (0-indexed): the conversation so far."""
    turns = case["turns"][: upto + 1]
    if len(turns) == 1:
        convo = turns[0]
    else:
        convo = "\n".join(f"Turn {i + 1}: {t}" for i, t in enumerate(turns))
    state = {"user_request": convo}
    if playlists:
        state["user_playlists"] = ", ".join(playlists)
    return state


async def _options() -> tuple[dict[str, str], list[str]]:
    """Every tool from both MCP servers, exactly what the agent loads: name -> description."""
    import psych_mcp
    import spotify_mcp

    options: dict[str, str] = {
        "none": "No tool is needed; the request is conversation, explanation, or chit-chat.",
    }
    for server in (spotify_mcp.app, psych_mcp.app):
        for tool in await server.list_tools():
            desc = (tool.description or "").strip().splitlines()[0]
            options[tool.name] = desc[:140]
    # a write tool needs the agent's judgment about being asked; keep it in the
    # inventory anyway — the LLM had it too, and the follow-up case expects it
    names = sorted(n for n in options if n != "none")
    return options, names


def _playlists() -> list[str]:
    try:
        from spotify_mcp import playlist_names

        return playlist_names()
    except Exception:  # noqa: BLE001 - parity garnish must never fail the run
        return []


def jev(state: dict, options: dict[str, str]) -> tuple[dict, float, dict]:
    """One decisions call. Returns (answers, seconds, usage). Raises on final failure."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("missing OPENROUTER_API_KEY — fill .env first")
    body = json.dumps(
        {"model": MODEL, "state": state, "questions": {
            "tool": {
                "type": "choice",
                "instructions": (
                    "Which single tool should the agent call first to answer the "
                    "user's latest request? If no tool is needed, pick none."
                ),
                "criteria": options,
            }
        }}
    ).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    last = None
    for attempt in range(TRIES):
        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            return data["answers"], time.time() - start, data.get("usage", {})
        except Exception as exc:  # noqa: BLE001 - retry, then report the last error
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"jev call failed after {TRIES} tries: {last}")


def score_turn(expected: list[str], answer: dict) -> dict:
    """Correctness plus the calibration signals, mechanically."""
    winner = answer["choice"]
    probs = answer.get("probabilities", {})
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    margin = round(ranked[0][1] - ranked[1][1], 3) if len(ranked) > 1 else 1.0
    return {
        "expected": expected,
        "winner": winner,
        "correct": winner in expected,
        "confidence": answer.get("confidence"),
        "p_expected": probs.get(expected[0], 0.0),
        "margin": margin,
        "probabilities": probs,
    }


# --- the LLM reference, read out of the recorded runs, not re-run here -------------
# bakeoff2.log, the final 2026-08-16 run (rubric fixed): per-case lines give
# points/calls/seconds; selection correctness is reconstructed from 5/5 or the
# notes ("wanted X, used Y" is a selection miss; "repeated an identical call" and
# "description too long" are not). The reasoning-off qwen rerun with per-case
# seconds and tool lists is in bakeoff_results.json.
LLM_REFERENCE = {
    "openai/gpt-5.4-mini": {
        "affective search": {"selected": True, "secs": 12.2, "calls": 3},
        "lyrics search": {"selected": True, "secs": 27.1, "calls": 4},
        "read my listening": {"selected": True, "secs": 8.5, "calls": 1},
        "restraint": {"selected": True, "secs": 4.5, "calls": 0},
        "recovery": {"selected": True, "secs": 16.5, "calls": 2},
        "follow-up": {"selected": True, "secs": 13.9, "calls": 5},
    },
    "qwen/qwen3.5-flash-02-23": {
        "affective search": {"selected": True, "secs": 17.5, "calls": 4},
        "lyrics search": {"selected": True, "secs": 27.9, "calls": 5},
        "read my listening": {"selected": True, "secs": 18.8, "calls": 1},
        "restraint": {"selected": True, "secs": 4.6, "calls": 0},
        "recovery": {"selected": True, "secs": 24.2, "calls": 3},
        "follow-up": {"selected": True, "secs": 12.9, "calls": 3},
    },
}


async def main() -> None:
    from spotify_mcp import app as spotify_app  # noqa: F401 - registers tools
    import psych_mcp
    import spotify_mcp

    sys.modules.setdefault("spotify_mcp", spotify_mcp)
    sys.modules.setdefault("psych_mcp", psych_mcp)

    options, names = await _options()
    print(f"inventory: {len(names)} tools + none, model {MODEL}\n")
    playlists = _playlists()

    results, turns_total, turns_correct, secs_total, cost_total = {}, 0, 0, 0.0, 0.0
    for case in CASES:
        expected = _expected(case)
        rows = []
        for i, accepted in enumerate(expected):
            state = _state(case, i, playlists)
            answers, secs, usage = jev(state, options)
            got = score_turn(accepted, answers["tool"])
            got["seconds"] = round(secs, 1)
            got["cost"] = usage.get("cost")
            rows.append({"turn": i + 1, **got})
            turns_total += 1
            turns_correct += got["correct"]
            secs_total += secs
            cost_total += float(usage.get("cost") or 0)
            mark = "ok  " if got["correct"] else "MISS"
            print(
                f"  {mark}{case['name']:18} t{i + 1}  want {'/'.join(accepted):16} "
                f"got {got['winner']:18} conf {got['confidence']}  "
                f"margin {got['margin']:.2f}  {got['seconds']:>4.1f}s"
            )
        results[case["name"]] = rows

    n_cases = len(CASES)
    print(
        f"\njev: selection {turns_correct}/{turns_total} turns  "
        f"avg {secs_total / turns_total:.1f}s per turn  "
        f"total {secs_total:.1f}s  measured cost ${cost_total:.4f}"
    )
    for model, ref in LLM_REFERENCE.items():
        sel = sum(1 for c in ref.values() if c["selected"])
        t = sum(c["secs"] for c in ref.values())
        print(f"{model:30} selection {sel}/{n_cases} cases  total {t:.0f}s  (recorded 2026-08-16, not re-run)")

    os.makedirs(ARTIFACTS, exist_ok=True)
    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump(
            {"model": MODEL, "inventory": names, "turns_correct": f"{turns_correct}/{turns_total}",
             "total_seconds": round(secs_total, 1), "cost_measured": round(cost_total, 4),
             "cases": results, "llm_reference": LLM_REFERENCE},
            f, indent=2,
        )
    print(f"\nfull detail in {RESULTS}; write-up in docs/benchmarks/archive/jev-pilot.md")


def _selfcheck() -> None:
    assert _expected(CASES[0]) == [["search_by_feel"]]
    assert _expected(CASES[3]) == [["none"]]  # restraint
    assert _expected(CASES[5]) == [["search_by_feel", "search_by_lyrics"], ["create_playlist"]]
    # follow-up turn 2 state carries both turns
    st = _state(CASES[5], 1, ["Rain"])
    assert "Turn 1" in st["user_request"] and "playlist called Rain" in st["user_request"]
    assert st["user_playlists"] == "Rain"
    ok = score_turn(["none"], {"choice": "none", "confidence": 0.9,
                               "probabilities": {"none": 0.9, "get_lyrics": 0.1}})
    assert ok["correct"] and ok["margin"] == 0.8
    miss = score_turn(["search_by_feel"], {"choice": "search_by_lyrics", "confidence": 0.5,
                                           "probabilities": {"search_by_lyrics": 0.5, "search_by_feel": 0.5}})
    assert not miss["correct"] and miss["margin"] == 0.0 and miss["p_expected"] == 0.5
    print("ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        asyncio.run(main())
