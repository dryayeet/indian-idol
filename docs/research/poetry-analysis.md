# Poetry analysis over lyrics

An exploration note for the roadmap item "explore integrating poetry analysis".
Nothing here is built; this records what the idea could add, the ways in, and
the traps. Written 2026-10-09.

## The idea

The psych pipeline already reads lyrics as text: `get_emotion_labels` scores
them against 28 emotion labels and `get_big_five` estimates trait signals from
them. What it ignores is that lyrics are poems. Form is a signal the emotion
classifier does not touch: a dense rhyme scheme over a regular meter reads
differently from free verse at the same valence, and a hook's repetition is
part of why it is a hook.

## What could be measured

- Structure: stanza count, line lengths, refrain or hook repetition.
- Meter and rhythm: syllable counts, stress patterns (scansion).
- Rhyme: scheme, density, exact versus slant.
- Imagery: concrete versus abstract language, the dominant images.
- Devices: metaphor, simile, apostrophe, alliteration, internal rhyme.

## Candidate approaches

1. **Structured extraction by the chat model** (or a cheap second model, the
   multi-model pattern) over lyrics already fetched by `get_lyrics` and
   `listening_lyrics`, returned as JSON. No new dependency, reuses existing
   plumbing, and cacheable: lyrics never change, the same argument the
   embeddings-for-lyric-ranking item makes.
2. **A hosted classifier.** The Big Five precedent is the warning: the Minej
   model was fine until Hugging Face stopped serving it, and its replacement
   changed the numbers enough that old values are not comparable. Any poetry
   model would need the same "heuristic signal, not a measurement" framing.
3. **Local scansion and rhyme libraries.** Deterministic and free, but
   English-centric. This library is half Hindi film music, and the roadmap
   already records go_emotions scoring pure Devanagari as neutral because the
   model cannot read it. A meter tool with the same blind spot would repeat
   that mistake quietly.

## Where it could plug in

- A companion to the psych tools: `get_emotion_labels` says what a listening
  history feels like; poetic features would say how that feeling is carried.
- `playlist_vibe`: alongside the valence and energy averages, a line about the
  form the songs share (or do not).
- The agent's voice: form observations are exactly what the replies are supposed
  to be made of ("every chorus lands on the same open vowel").

## Open questions

- Does form add signal beyond the emotion labels, or does it just cost a call
  per track? The validation problem is the same one the trait-movement item
  has: nothing here proves the extra signal exists.
- Devanagari coverage: any approach must say what it does with Hindi lyrics
  rather than silently scoring them as none.
- Cost and caching policy: per track, or per listening session?

## What would make it a non-starter

If the only workable approaches are English-only, this stays parked until a
multilingual option exists. "Half the library reads as poetry-free" would be
worse than not measuring form at all.
