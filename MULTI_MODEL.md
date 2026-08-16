# Using more than one model

Notes for later. Nothing here is built yet; this is the reasoning so it does not
have to be redone. Written 2026-08-16, grounded in the bake-off data in
[MODEL_BAKEOFF.md](MODEL_BAKEOFF.md) and in current published work.

## The finding that shapes all of it

Cost is not this project's bottleneck. Two full bake-offs, 22 model runs over six
cases each, cost **$0.24 in total**. A day of ordinary use costs cents.

That matters because most multi-model architecture exists to cut cost. Here it would
be optimising the wrong thing. The real constraints are answer quality (does the
agent find music that actually fits) and latency (a chat turn takes 15 to 100
seconds). Any multi-model idea should be judged against those two, not against
tokens.

## Worth building, in order

### 1. Embeddings for lyric ranking

Not an LLM at all, which is the point.

`_lyric_score` in [spotify_mcp.py](spotify_mcp.py) counts shared words between the
request and the lyrics, so "headlights" never matches "high beams". A small
embedding model does paraphrase properly, costs about $0.01 per million tokens, and
is deterministic and cacheable. Lyrics for a track never change, so the embedding is
computed once and reused forever.

This is the highest-value model addition on the list, and it competes with no LLM:
it is the right tool for a job an LLM would do worse and slower.

### 2. A relevance judge over search results

`_relevant()` is a four-character substring check. It kills gibberish, which is what
it was built for, but it cannot tell that three tracks called "Rainy Days" all match
the words "rainy day" while none of them are about driving away from home at night.

A second, cheap model scoring each candidate against the original request is the
independent-verifier pattern. Reported gains are large where the producing model is
confidently wrong, which is exactly the failure mode here.

Two rules from the literature that matter for the implementation:

- The judge **must not share context with the producer**. Give it the user's
  request and the candidate list, never the conversation. A judge inside the same
  reasoning loop agrees with itself.
- Verification is worth doing at more than one level: per candidate, then over the
  final answer.

### 3. Failover when the model is unavailable

Everything now runs through OpenRouter, so a dead key or an exhausted balance stops
the agent completely. Two dead sessions during development would have been rescued by
falling back to another OpenRouter model, or to a second key, on 402 and 429.

Not clever, but the best reliability per line of code on this page.

## Considered and rejected

### A cheap-first cascade

Route every turn to a cheap model, escalate to a strong one when the cheap answer
looks bad. The bake-off shows the traffic profile that suits this: almost every
model scores 5/5 on the four easy cases, and separation appears only on `recovery`
and `follow-up`. Published cascades report 50-98% cost savings, and a 2026
cluster-route-escalate framework holds 97-99% of top-model accuracy.

Rejected anyway, for now:

- The savings are on a base of $0.24. There is nothing to save.
- A cascade makes latency **worse**, because a failed cheap attempt is pure
  overhead added before the real answer.
- Escalation rate is a live variable that fails quietly. One documented case had a
  provider-side formatting change push about 90% of traffic to the expensive tier
  without anyone noticing.

Revisit if usage grows by orders of magnitude, or if the escalation signal becomes
free. Note that this codebase already has an unusually clean signal available:
`search_by_feel` returning an empty list is a hard, non-model indication that the
turn just got difficult.

### A crew of agents

A planner, a searcher, a critic, passing messages. Rejected outright. Multi-agent
systems are reported to fail between 41% and 86.7% of the time on standard
benchmarks, mostly through context divergence, and single agents match or beat them
when compute is held constant. This agent's entire job is one to three tool calls
inside a small context. Coordination would buy nothing and cost reliability.

## The pattern that is already working

Worth noticing that this project is multi-model in the right way already:

- The psych server (still to build) is two specialist Hugging Face models, not two
  chat models arguing.
- `search_by_lyrics` reranks locally instead of asking an LLM to rank.
- `_relevant` and `_feel_query` are plain code where plain code suffices.

Specialist models and ordinary code for narrow jobs; one LLM for the conversation.
Items 1 and 2 above extend that pattern. A cascade or a crew would replace it with a
weaker one.

## Sources

- [Model routing and cascades](https://tianpan.co/blog/2025-11-03-llm-routing-model-cascades)
- [Cluster, Route, Escalate (2026)](https://arxiv.org/html/2606.27457)
- [Why multi-agent LLM systems fail](https://futureagi.substack.com/p/why-do-multi-agent-llm-systems-fail)
- [LLM agent evaluation metrics 2026](https://www.confident-ai.com/blog/llm-agent-evaluation-complete-guide)
