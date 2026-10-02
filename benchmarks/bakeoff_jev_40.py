"""The 40-case expansion of the Jev tool-selection comparison.

    python benchmarks/bakeoff_jev_40.py --selfcheck              labels checked locally
    python benchmarks/bakeoff_jev_40.py jev                      Jev arm, 40 decisions, under a cent
    python benchmarks/bakeoff_jev_40.py dry --model <m>          one-shot classification prompt
    python benchmarks/bakeoff_jev_40.py inloop --model <m>       real agent, first tool call
    python benchmarks/bakeoff_jev_40.py                          jev + dry for both reference models

Extends the six-case Jev pilot to forty labeled utterances spread
across the full inventory: playlist-name traps, ambiguity, chitchat, and the
write tools. No recorded LLM results exist for these, so both LLM arms run live
here. Each arm writes its slice into artifacts/benchmarks/jev_40_results.json as it finishes,
so a timed-out in-loop run keeps its completed cases and a re-run skips them.
Write-up: docs/benchmarks/jev-tool-selection.md.
"""

import asyncio
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ARTIFACTS = os.path.join(ROOT, "artifacts", "benchmarks")
RESULTS = os.path.join(ARTIFACTS, "jev_40_results.json")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent  # noqa: F401 - loads .env and the shared turn machinery
from bakeoff_jev import _options, _playlists, jev, score_turn
from langgraph.checkpoint.memory import InMemorySaver

REFERENCE_MODELS = ["qwen/qwen3.5-flash-02-23", "openai/gpt-5.4-mini"]
CASE_TIMEOUT = 300  # seconds; the bakeoff's worst in-loop case was 137s

# (name, utterance, accepted first tools, mode) — mode only matters in-loop.
# auto is read-only for every case here; the two that expect a write run in afk
# so create_playlist is held for approval instead of touching the account.
CASES = [
    # search by feel
    ("feel-summer", "songs that feel like the last day of summer", ["search_by_feel"], "auto"),
    ("feel-nightdrive", "music that feels like a long night drive alone", ["search_by_feel"], "auto"),
    ("feel-morning", "something that feels like fresh sheets and slow mornings", ["search_by_feel"], "auto"),
    ("feel-club", "tracks that feel like a panic attack in a club", ["search_by_feel"], "auto"),
    # search by lyrics
    ("lyr-growold", "songs whose lyrics mention growing old with someone", ["search_by_lyrics"], "auto"),
    ("lyr-admit", "find songs where the singer admits they were the problem", ["search_by_lyrics"], "auto"),
    ("lyr-train", "tracks about leaving on a train, in the actual words", ["search_by_lyrics"], "auto"),
    # deliberately ambiguous
    ("amb-rain", "songs about rain", ["search_by_feel", "search_by_lyrics"], "auto"),
    ("amb-gym", "music for the gym", ["search_by_feel"], "auto"),
    # listening analysis
    ("listen-mood", "what have i been listening to lately, and what mood does it suggest", ["listening_lyrics"], "auto"),
    ("listen-themes", "analyze the themes across my recent listening", ["listening_lyrics"], "auto"),
    # single-track lyrics
    ("lyric-motion", "give me the lyrics to Motion Sickness by Phoebe Bridgers", ["get_lyrics"], "auto"),
    ("lyric-electric", "what are the words in the song Electric Feel", ["get_lyrics"], "auto"),
    # history reads
    ("hist-yesterday", "what did i play yesterday", ["recently_played"], "auto"),
    ("hist-toptracks", "my most played tracks this month", ["top_tracks"], "auto"),
    ("hist-topartists", "who are my most played artists", ["top_artists"], "auto"),
    ("hist-follows", "which artists do i follow", ["followed_artists"], "auto"),
    ("hist-now", "what song is on right now", ["now_playing"], "auto"),
    # library reads
    ("lib-liked", "show me my liked songs", ["liked_songs"], "auto"),
    ("lib-albums", "what albums have i saved", ["saved_albums"], "auto"),
    ("lib-podcasts", "list my saved podcasts", ["saved_podcasts"], "auto"),
    ("lib-abbey", "what tracks are on the album Abbey Road", ["album_tracks"], "auto"),
    # playlists
    ("pl-list", "list my playlists", ["my_playlists", "playlist_names"], "auto"),
    ("pl-workout", "what songs are on my workout playlist", ["playlist_tracks"], "auto"),
    ("pl-vibe", "how does my chill playlist sound overall", ["playlist_vibe"], "auto"),
    ("pl-trap-static", "what's on my Sad Girl Autumn playlist", ["playlist_tracks", "my_playlists"], "auto"),
    # artist, album, track detail
    ("art-kendrick", "albums by Kendrick Lamar", ["artist_albums"], "auto"),
    ("art-radiohead", "artists similar to Radiohead", ["similar_artists"], "auto"),
    ("feat-blinding", "what is the tempo and valence of Blinding Lights", ["track_features"], "auto"),
    # psych
    ("psych-bigfive", "read this and tell me what it says about my personality: I spend most weekends alone with headphones on, long walks, old records, few plans", ["get_big_five"], "auto"),
    ("psych-emotion", "score this text for emotions: I can't believe it's over, I keep rereading her messages", ["get_emotion_labels"], "auto"),
    # web
    ("web-darkside", "when did Pink Floyd release Dark Side of the Moon", ["web_search"], "auto"),
    ("web-tame", "who sings the live version of Tame Impala's The Less I Know The Better", ["web_search"], "auto"),
    # no tool
    ("none-hello", "hey, what's up", ["none"], "auto"),
    ("none-valence", "what does valence mean in your search tool", ["none"], "auto"),
    ("none-thanks", "thanks, that's all for today", ["none"], "auto"),
    ("none-capacity", "what can you actually do?", ["none"], "auto"),
    ("none-define", "explain the difference between valence and energy", ["none"], "auto"),
    # writes: afk holds create_playlist for approval, nothing touches the account
    ("create-focus", "make me a playlist of songs for studying, call it Focus", ["create_playlist"], "afk"),
    ("create-drive", "put together a playlist called Late Night Drive with songs for driving at 2am", ["create_playlist"], "afk"),
]

DYNAMIC_TRAP = "pl-trap-live"  # uses a real playlist name, added in main() when names load


def all_cases() -> list[tuple]:
    cases = list(CASES)
    names = _playlists()
    if names:
        # the playlist-name trap with a name the LLM's own prompt actually carries
        cases.append((DYNAMIC_TRAP, f"play me something from my {names[0]} playlist", ["playlist_tracks"], "auto"))
    return cases


def _save_run(arm: str, model: str, payload: dict) -> None:
    os.makedirs(ARTIFACTS, exist_ok=True)
    doc = {"runs": {}}
    if os.path.exists(RESULTS):
        with open(RESULTS, encoding="utf-8") as f:
            doc = json.load(f)
    stored = doc.setdefault("runs", {}).setdefault(arm, {}).setdefault(model, {})
    for key, value in payload.items():
        if key == "cases":
            stored.setdefault("cases", {}).update(value)
        else:
            stored[key] = value
    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


def _load_done(arm: str, model: str) -> dict:
    if not os.path.exists(RESULTS):
        return {}
    with open(RESULTS, encoding="utf-8") as f:
        return json.load(f).get("runs", {}).get(arm, {}).get(model, {})


def _first_tool(call: str) -> str:
    return call.split("(", 1)[0]


async def arm_jev() -> None:
    options, _ = await _options()
    rows = dict(_load_done("jev", "typesafe/jev-1.13").get("cases", {}))
    for name, utterance, accepted, _mode in all_cases():
        if name in rows:
            continue
        state = {"user_request": utterance}
        answers, took, usage = jev(state, options)
        got = score_turn(accepted, answers["tool"])
        rows[name] = {"accepted": accepted, **got, "seconds": round(took, 1),
                      "cost": usage.get("cost")}
        mark = "ok  " if got["correct"] else "MISS"
        print(f"  {mark}{name:18} want {'/'.join(accepted):32} got {got['winner']:18} "
              f"margin {got['margin']:.2f}  {took:.1f}s")
        _save_run("jev", "typesafe/jev-1.13", {"cases": rows})
    n = len(rows)
    if n:
        correct = sum(bool(row["correct"]) for row in rows.values())
        secs = sum(float(row.get("seconds") or 0) for row in rows.values())
        cost = sum(float(row.get("cost") or 0) for row in rows.values())
        margins = [float(row["margin"]) for row in rows.values()]
        lo, hi = min(margins), max(margins)
        print(f"jev: {correct}/{n}  avg {secs / n:.1f}s  cost ${cost:.4f}  margin band {lo:.2f}-{hi:.2f}")


async def arm_dry(model: str) -> None:
    options, names = await _options()
    playlists = _playlists()
    inventory = "\n".join(f"- {n}: {d}" for n, d in sorted(options.items()))
    extra = f"\nThe user's playlists are called: {', '.join(playlists)}." if playlists else ""
    system = (
        "You route requests for a music agent. Pick exactly one tool for the user's "
        f"latest request. The tools:\n{inventory}{extra}\n"
        "Reply with only the tool name, or the word none. Nothing else."
    )
    done = _load_done("dry", model)
    rows = dict(done.get("cases", {}))
    llm = agent._llm(model)
    for name, utterance, accepted, _mode in all_cases():
        if name in rows:
            continue
        start = time.time()
        try:
            resp = await llm.ainvoke([("system", system), ("human", utterance)])
            text = resp.content if isinstance(resp.content, str) else str(resp.content)
            words = re.findall(r"[a-z_]+", text.lower())
            picked = next((w for w in words if w in options or w == "none"), "(unparseable)")
        except Exception as exc:  # noqa: BLE001 - an error scores as a miss, like bakeoff
            picked, text = "(error)", f"{type(exc).__name__}: {exc}"
        rows[name] = {"accepted": accepted, "picked": picked, "correct": picked in accepted,
                      "seconds": round(time.time() - start, 1)}
        mark = "ok  " if rows[name]["correct"] else "MISS"
        print(f"  {mark}{name:18} want {'/'.join(accepted):32} got {picked}")
        _save_run("dry", model, {"cases": rows})
    n = len(rows)
    if n:
        correct = sum(bool(row["correct"]) for row in rows.values())
        secs = sum(float(row.get("seconds") or 0) for row in rows.values())
        print(f"dry {model}: {correct}/{n}  avg {secs / n:.1f}s")


async def arm_inloop(model: str) -> None:
    done = _load_done("inloop", model)
    rows = dict(done.get("cases", {}))
    saver = InMemorySaver()
    for i, (name, utterance, accepted, mode) in enumerate(all_cases()):
        if name in rows:
            continue
        calls, reply, error, pending = [], [], "", []

        def on_part(kind: str, text: str) -> None:
            (calls if kind == "tool" else reply).append(text)

        start = time.time()
        try:
            pending = await asyncio.wait_for(
                agent.turn(utterance, on_part, checkpointer=saver,
                           thread_id=f"jev40-{model}-{i}", mode=mode, model=model),
                CASE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - timeout or crash scores as a miss
            error = f"{type(exc).__name__}: {str(exc)[:80]}"
        took = round(time.time() - start, 1)
        if mode == "afk":
            reached = [p if isinstance(p, str) else p.get("name", "") for p in pending]
            picked = "create_playlist" if "create_playlist" in reached else (calls[0].split("(")[0] if calls else "none")
        else:
            picked = _first_tool(calls[0]) if calls else "none"
        ok = picked in accepted and not error
        rows[name] = {"accepted": accepted, "picked": picked, "correct": ok,
                      "seconds": took, "calls": len(calls), "error": error}
        mark = "ok  " if ok else "MISS"
        note = f"  [{error}]" if error else ""
        print(f"  {mark}{name:18} want {'/'.join(accepted):32} got {picked:20} "
              f"{len(calls):>2} calls {took:>5.1f}s{note}")
        _save_run("inloop", model, {"cases": rows})
    n = len(rows)
    if n:
        correct = sum(bool(row["correct"]) for row in rows.values())
        secs = sum(float(row.get("seconds") or 0) for row in rows.values())
        print(f"inloop {model}: {correct}/{n} first-call selection  total {secs:.0f}s")


def _selfcheck() -> None:
    global RESULTS

    static = list(CASES)
    assert len(static) == 40, len(static)
    for name, _, accepted, mode in static:
        assert accepted, f"{name}: empty accepted set"
        for tool in accepted:
            assert tool == "none" or re.fullmatch(r"[a-z_]+", tool), f"{name}: bad label {tool}"
        if "create_playlist" in accepted:
            assert mode == "afk", f"{name}: a write case must run in afk"
        else:
            assert mode == "auto", f"{name}: read case must run in auto"
    import tempfile

    held = RESULTS
    try:
        RESULTS = os.path.join(tempfile.mkdtemp(), "results.json")
        _save_run("dry", "model", {"cases": {"first": {"correct": True}}})
        _save_run("dry", "model", {"cases": {"second": {"correct": False}}})
        assert set(_load_done("dry", "model")["cases"]) == {"first", "second"}
    finally:
        RESULTS = held
    print(f"ok: {len(static)} cases, labels well-formed, write cases held in afk")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selfcheck" in args:
        _selfcheck()
    else:
        arm = args[0] if args else "all"
        model = args[args.index("--model") + 1] if "--model" in args else None
        if arm in ("all", "jev"):
            asyncio.run(arm_jev())
        if arm in ("all", "dry"):
            for m in ([model] if model else REFERENCE_MODELS):
                asyncio.run(arm_dry(m))
        if arm == "inloop":
            if not model:
                raise SystemExit("inloop needs --model <openrouter model id>")
            asyncio.run(arm_inloop(model))
