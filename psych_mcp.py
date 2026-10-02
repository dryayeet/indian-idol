"""Psych MCP server: trait and emotion inference from text, per the abstract.

Env: HF_TOKEN — free, from https://huggingface.co/settings/tokens with
Inference Providers permission.
The server starts without it; the tools explain what is missing when called.

Two tools over Hugging Face Inference:
    get_big_five(text)        five heuristic OCEAN trait signals in [0, 1]
    get_emotion_labels(text)  probabilities over the 28 GoEmotions labels

No specialized Big Five model is currently served by Hugging Face Inference
Providers. Big Five therefore uses paired high/low OCEAN labels with the hosted
facebook/bart-large-mnli zero-shot classifier. These are heuristic trait signals,
not calibrated psychometric scores. Emotions use the abstract's own model.

Run:  python psych_mcp.py           (stdio)
Check: python psych_mcp.py --selfcheck
"""

import math
import os
import re
import time

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

app = FastMCP("psych")
_http = httpx.Client(timeout=60, transport=httpx.HTTPTransport(retries=3))

HF = "https://router.huggingface.co/hf-inference/models/"
BIG_FIVE_MODEL = "facebook/bart-large-mnli"
EMOTION_MODEL = "SamLowe/roberta-base-go_emotions"

CHUNK = 1500  # chars per scoring call. BERT truncates at 512 tokens, and a batch of
# lyrics is far bigger, so a single call would score one song and call it the week.
BIG_FIVE_BYTES = 384  # BART uses byte-level BPE, so bytes bound the worst-case tokens
# while leaving room under 512 tokens for the hypothesis and special tokens.
BIG_FIVE_SAMPLES = 12  # bound latency while sampling evenly across long listening text
MAX_CHUNKS = 12  # a whole listening history is ~10 chunks; more is diminishing returns
COLD_TRIES = 3  # a warm model still cold-starts after idle: 503, then ~20s of loading
TOKEN_LIMIT = 480  # the models take 512 tokens; headroom for the special tokens
MAX_SCORED = 40  # absolute ceiling on scoring calls per tool call, splits included


def _overflow(err: str) -> int | None:
    """The true token count inside the API's too-long error, or None otherwise.

    CHUNK is sized for English at roughly 4 chars per token, but Hindi, and above all
    Devanagari, tokenises several times denser: 1,450 chars measured 2,167 tokens. The
    400 error carries the real count, so the chunk can be re-cut to fit instead of
    being guessed at or silently truncated.
    """
    # the two models phrase it differently: roberta says "expanded size of the
    # tensor (2167) must match", bert says "size of tensor a (670) must match"
    m = re.search(r"tensor(?: a)? \((\d+)\) must match", err)
    return int(m.group(1)) if m else None


def _split_even(piece: str, tokens: int) -> list[str]:
    """piece re-cut into enough parts that each should fit under TOKEN_LIMIT."""
    n = max(2, math.ceil(tokens / TOKEN_LIMIT))
    size = max(200, math.ceil(len(piece) / n))
    return [piece[i : i + size] for i in range(0, len(piece), size)]

BIG_FIVE_PAIRS = {
    "openness": ("high openness to experience", "low openness to experience"),
    "conscientiousness": (
        "high conscientiousness and self-discipline",
        "low conscientiousness and self-discipline",
    ),
    "extraversion": ("high extraversion and sociability", "low extraversion and sociability"),
    "agreeableness": ("high agreeableness and compassion", "low agreeableness and compassion"),
    "neuroticism": (
        "high neuroticism and emotional instability",
        "low neuroticism and emotional instability",
    ),
}
BIG_FIVE_LABELS = [label for pair in BIG_FIVE_PAIRS.values() for label in pair]
BIG_FIVE_HYPOTHESIS = "This text shows {}."


def _token() -> str:
    tok = os.environ.get("HF_TOKEN")
    if not tok:
        raise RuntimeError(
            "no HF_TOKEN set. Get a free token with Inference Providers permission at "
            "https://huggingface.co/settings/tokens and put it in .env. "
            "Do not retry until it is set."
        )
    return tok


def _chunks(text: str, chunk_chars: int = CHUNK, max_chunks: int = MAX_CHUNKS) -> list[str]:
    """Whole text as scoring-sized pieces, split on line breaks where possible."""
    text = text.strip()
    if not text:
        raise ValueError("text is required — pass the words to score")
    out: list[str] = []
    while text:
        if len(out) >= max_chunks:
            raise ValueError(
                f"text is too long to score safely: more than {max_chunks * chunk_chars} characters"
            )
        if len(text) <= chunk_chars:
            out.append(text)
            break
        cut = text.rfind("\n", chunk_chars // 2, chunk_chars)
        if cut == -1:
            cut = chunk_chars
        out.append(text[:cut])
        text = text[cut:].strip()
    return out


def _byte_chunks(text: str) -> list[str]:
    """All text in BART-safe UTF-8 byte windows, preferring nearby word breaks."""
    text = text.strip()
    if not text:
        raise ValueError("text is required — pass the words to score")
    pieces = []
    while text:
        used = 0
        end = 0
        for end, char in enumerate(text, 1):
            size = len(char.encode("utf-8"))
            if used + size > BIG_FIVE_BYTES:
                end -= 1
                break
            used += size
        else:
            end = len(text)

        if end < len(text):
            candidate = text[:end]
            cut = max(candidate.rfind("\n"), candidate.rfind(" "), candidate.rfind("\t"))
            if cut >= end // 2:
                end = cut
        piece = text[:end].strip()
        if not piece:
            raise RuntimeError("could not split text into Big Five inference windows")
        pieces.append(piece)
        text = text[end:].strip()
    return pieces


def _sampled_chunks(text: str) -> tuple[list[str], int, bool]:
    """Safe Big Five windows plus total length and whether long text was sampled."""
    text = text.strip()
    pieces = _byte_chunks(text)
    if len(pieces) <= BIG_FIVE_SAMPLES:
        return pieces, len(text), False

    last = len(pieces) - 1
    indices = [round(i * last / (BIG_FIVE_SAMPLES - 1)) for i in range(BIG_FIVE_SAMPLES)]
    return [pieces[index] for index in indices], len(text), True


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
        if isinstance(data, dict) and {"labels", "scores"} <= set(data):
            if (
                not isinstance(data["labels"], list)
                or not isinstance(data["scores"], list)
                or len(data["labels"]) != len(data["scores"])
            ):
                raise RuntimeError(f"unexpected response from {model}: {str(data)[:200]}")
            rows = [
                dict(label=label, score=score)
                for label, score in zip(data["labels"], data["scores"])
            ]
        elif isinstance(data, list):
            # text and zero-shot classification usually answer [{label, score}, ...]
            rows = data[0] if data and isinstance(data[0], list) else data
        else:
            raise RuntimeError(f"unexpected response from {model}: {str(data)[:200]}")
        if not isinstance(rows, list) or not all(
            isinstance(row, dict) and {"label", "score"} <= set(row) for row in rows
        ):
            raise RuntimeError(f"unexpected response from {model}: {str(data)[:200]}")
        try:
            return {x["label"]: float(x["score"]) for x in rows}
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"unexpected response from {model}: {str(data)[:200]}") from exc
    raise RuntimeError(f"{model} did not finish loading after {COLD_TRIES} tries")


def _pooled(
    model: str,
    text: str,
    *,
    chunk_chars: int = CHUNK,
    max_chunks: int = MAX_CHUNKS,
    transform=None,
    pieces: list[str] | None = None,
    weights: list[float] | None = None,
    **params,
) -> tuple[dict[str, float], int]:
    """Mean scores across chunks, plus how many pieces were actually scored.

    A chunk the model rejects as too long is re-cut using the token count from the
    error and re-queued, so dense scripts get scored in full rather than erroring or
    being silently cut at 512 tokens. Truncation is the floor only for a piece already
    too small to split further.
    """
    source = list(pieces) if pieces is not None else _chunks(
        text, chunk_chars=chunk_chars, max_chunks=max_chunks
    )
    weighted = weights is not None
    sample_weights = list(weights) if weighted else [1.0] * len(source)
    if len(sample_weights) != len(source) or not all(
        math.isfinite(weight) and weight > 0 for weight in sample_weights
    ):
        raise ValueError("weights must contain one positive finite value per piece")
    work = list(zip(source, sample_weights))
    totals: dict[str, float] = {}
    scored = 0
    total_weight = 0.0
    while work and scored < MAX_SCORED:
        piece, weight = work.pop(0)
        try:
            scores = _classify(model, piece, **params)
        except RuntimeError as e:
            tokens = _overflow(str(e))
            if tokens is None:
                raise
            if len(piece) <= 250:  # cannot usefully split; let the server truncate
                scores = _classify(model, piece, truncation=True, **params)
            else:
                parts = _split_even(piece, tokens)
                if weighted:
                    total_chars = sum(len(part) for part in parts)
                    part_weights = [weight * len(part) / total_chars for part in parts]
                else:
                    part_weights = [1.0] * len(parts)
                work = list(zip(parts, part_weights)) + work
                continue
        if transform is not None:
            scores = transform(scores)
        scored += 1
        total_weight += weight
        for label, score in scores.items():
            totals[label] = totals.get(label, 0.0) + score * weight
    if work:
        raise RuntimeError(f"text needed more than {MAX_SCORED} scoring calls")
    if not scored:
        raise RuntimeError("nothing could be scored")
    return {k: v / total_weight for k, v in totals.items()}, scored


def _trait_scores(scores: dict[str, float]) -> dict[str, float]:
    """Normalize paired high/low zero-shot evidence into five independent values."""
    missing = [label for label in BIG_FIVE_LABELS if label not in scores]
    if missing:
        raise RuntimeError(f"missing Big Five labels from inference response: {missing}")

    traits = {}
    for trait, (high, low) in BIG_FIVE_PAIRS.items():
        high_score, low_score = scores[high], scores[low]
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in (high_score, low_score)):
            raise RuntimeError(f"invalid Big Five score for {trait}: {high_score}, {low_score}")
        total = high_score + low_score
        if total <= 0:
            raise RuntimeError(f"invalid Big Five score pair for {trait}: both scores are zero")
        traits[trait] = high_score / total
    return traits


@app.tool()
def get_big_five(text: str) -> dict:
    """Heuristic Big Five (OCEAN) trait signals from text, each in [0, 1].

    Pass the words someone chose: lyrics they listen to, things they wrote. Short text
    is scored in full; long text is sampled evenly with coverage reported. `chunks`
    says how many samples were scored. Each value compares zero-shot evidence for high
    versus low expression of that trait. These are not calibrated psychometric scores
    or a diagnosis, and the reply should treat them that way.
    """
    pieces, total_chars, sampled = _sampled_chunks(text)
    scores, n = _pooled(
        BIG_FIVE_MODEL,
        text,
        candidate_labels=BIG_FIVE_LABELS,
        hypothesis_template=BIG_FIVE_HYPOTHESIS,
        multi_label=True,
        transform=_trait_scores,
        pieces=pieces,
        weights=[len(piece) for piece in pieces],
    )
    traits = {trait: round(value, 3) for trait, value in scores.items()}
    return {
        "traits": traits,
        "chunks": n,
        "characters_scored": sum(len(piece) for piece in pieces),
        "characters_total": total_chars,
        "sampled": sampled,
        "model": BIG_FIVE_MODEL,
    }


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
        # the length recovery: the real token count is read from the API's error,
        # and the re-cut pieces cover the whole chunk
        msg = "The expanded size of the tensor (2167) must match the existing size (514)"
        assert _overflow(msg) == 2167
        assert _overflow("The size of tensor a (670) must match the size of tensor b (512)") == 670
        assert _overflow("some other error") is None
        piece = "x" * 1450
        parts = _split_even(piece, 2167)
        assert "".join(parts) == piece, "splitting must lose nothing"
        assert all(len(p) <= 300 for p in parts), max(len(p) for p in parts)
        assert len(BIG_FIVE_PAIRS) == 5 and len(BIG_FIVE_LABELS) == 10
        assert "{}" in BIG_FIVE_HYPOTHESIS
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
