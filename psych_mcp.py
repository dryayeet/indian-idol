"""Psych MCP server: trait and emotion inference from text, per the abstract.

Env: HF_TOKEN — free, from https://huggingface.co/settings/tokens (read access).
The server starts without it; the tools explain what is missing when called.

Two tools over Hugging Face Inference:
    get_big_five(text)        five OCEAN trait scores in [0, 1]
    get_emotion_labels(text)  probabilities over the 28 GoEmotions labels

The abstract names vladinc/bigfive-regression-model, but no provider serves it
(inference: None, checked 2026-08-18), so Big Five runs on Minej/bert-base-personality,
which is warm and returns the same five floats. Emotions use the abstract's own model.

Run:  python psych_mcp.py           (stdio)
Check: python psych_mcp.py --selfcheck
"""

import os
import time

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

app = FastMCP("psych")
_http = httpx.Client(timeout=60, transport=httpx.HTTPTransport(retries=3))

HF = "https://router.huggingface.co/hf-inference/models/"
BIG_FIVE_MODEL = "Minej/bert-base-personality"
EMOTION_MODEL = "SamLowe/roberta-base-go_emotions"

CHUNK = 1500  # chars per scoring call. BERT truncates at 512 tokens, and a batch of
# lyrics is far bigger, so a single call would score one song and call it the week.
MAX_CHUNKS = 12  # a whole listening history is ~10 chunks; more is diminishing returns
COLD_TRIES = 3  # a warm model still cold-starts after idle: 503, then ~20s of loading

# the model ships no id2label, so the API answers LABEL_0..4. The order is from the
# model card: ['Extroversion', 'Neuroticism', 'Agreeableness', 'Conscientiousness',
# 'Openness']. Name spellings kept too, in case the config ever gains labels.
TRAITS = {
    "label_0": "extraversion",
    "label_1": "neuroticism",
    "label_2": "agreeableness",
    "label_3": "conscientiousness",
    "label_4": "openness",
    "openness": "openness",
    "conscientiousness": "conscientiousness",
    "extroversion": "extraversion",
    "extraversion": "extraversion",
    "agreeableness": "agreeableness",
    "neuroticism": "neuroticism",
}


def _token() -> str:
    tok = os.environ.get("HF_TOKEN")
    if not tok:
        raise RuntimeError(
            "no HF_TOKEN set. Get a free one at https://huggingface.co/settings/tokens "
            "(read access is enough) and put it in .env. Do not retry until it is set."
        )
    return tok


def _chunks(text: str) -> list[str]:
    """Whole text as scoring-sized pieces, split on line breaks where possible."""
    text = text.strip()
    if not text:
        raise ValueError("text is required — pass the words to score")
    out: list[str] = []
    while text and len(out) < MAX_CHUNKS:
        if len(text) <= CHUNK:
            out.append(text)
            break
        cut = text.rfind("\n", CHUNK // 2, CHUNK)
        if cut == -1:
            cut = CHUNK
        out.append(text[:cut])
        text = text[cut:].strip()
    return out


def _classify(model: str, text: str, **params) -> dict[str, float]:
    """One HF text-classification call as {label: score}."""
    headers = {"Authorization": f"Bearer {_token()}"}
    body = {"inputs": text} | ({"parameters": params} if params else {})
    for attempt in range(COLD_TRIES):
        r = _http.post(HF + model, headers=headers, json=body)
        if r.status_code == 503 and attempt < COLD_TRIES - 1:
            # the model is loading; the body says for how long
            wait = float((r.json() or {}).get("estimated_time", 20))
            time.sleep(min(wait, 30))
            continue
        if r.status_code >= 400:
            raise RuntimeError(
                f"Hugging Face refused {model}: {r.status_code} {r.text[:200]}. "
                "If this is 401 the HF_TOKEN is wrong; do not retry with the same one."
            )
        data = r.json()
        # text-classification answers [[{label, score}, ...]] for one input
        rows = data[0] if data and isinstance(data[0], list) else data
        return {x["label"]: float(x["score"]) for x in rows}
    raise RuntimeError(f"{model} did not finish loading after {COLD_TRIES} tries")


def _pooled(model: str, text: str, **params) -> tuple[dict[str, float], int]:
    """Mean scores across chunks, plus how many chunks were scored."""
    pieces = _chunks(text)
    totals: dict[str, float] = {}
    for p in pieces:
        for label, score in _classify(model, p, **params).items():
            totals[label] = totals.get(label, 0.0) + score
    return {k: v / len(pieces) for k, v in totals.items()}, len(pieces)


@app.tool()
def get_big_five(text: str) -> dict:
    """Big Five (OCEAN) trait scores from text, each in [0, 1].

    Pass the words someone chose: lyrics they listen to, things they wrote. Long text
    is scored in chunks and averaged; `chunks` says how many, so a one-chunk score
    should be read as a weak signal, not a profile. These are trait estimates from
    word choice, not a diagnosis, and the reply should treat them that way.
    """
    # sigmoid, not the default softmax: the card's traits are five independent
    # values, and softmax would force them to compete for a total of 1
    scores, n = _pooled(BIG_FIVE_MODEL, text, function_to_apply="sigmoid", top_k=5)
    named = {TRAITS[k.lower()]: round(v, 3) for k, v in scores.items() if k.lower() in TRAITS}
    return {"traits": named, "chunks": n, "model": BIG_FIVE_MODEL}


@app.tool()
def get_emotion_labels(text: str, top: int = 10) -> dict:
    """Emotion probabilities from text, over the 28 GoEmotions labels.

    Returns the strongest `top` emotions with their scores, plus `chunks` for how much
    text carried them. Labels include admiration, amusement, anger, annoyance,
    approval, caring, confusion, curiosity, desire, disappointment, disapproval,
    disgust, embarrassment, excitement, fear, gratitude, grief, joy, love,
    nervousness, optimism, pride, realization, relief, remorse, sadness, surprise,
    and neutral.
    """
    scores, n = _pooled(EMOTION_MODEL, text)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[: max(1, min(top, 28))]
    return {"emotions": {k: round(v, 3) for k, v in ranked}, "chunks": n, "model": EMOTION_MODEL}


if __name__ == "__main__":
    import sys

    if "--selfcheck" in sys.argv:
        # chunking: short text is one piece, long text splits near line breaks
        assert _chunks("hello") == ["hello"]
        long = "\n".join(f"line {i} " + "x" * 60 for i in range(200))
        pieces = _chunks(long)
        assert 1 < len(pieces) <= MAX_CHUNKS, len(pieces)
        assert all(len(p) <= CHUNK for p in pieces), max(len(p) for p in pieces)
        # trait names normalise to the abstract's OCEAN spelling
        assert TRAITS["extroversion"] == "extraversion"
        try:
            _chunks("   ")
            raise AssertionError("empty text must be refused")
        except ValueError:
            pass
        # the missing-token path must be a clean instruction, not a crash
        held = os.environ.pop("HF_TOKEN", None)
        try:
            _token()
            raise AssertionError("missing token must raise")
        except RuntimeError as e:
            assert "huggingface.co/settings/tokens" in str(e)
        finally:
            if held:
                os.environ["HF_TOKEN"] = held
        print("ok")
    else:
        app.run()
