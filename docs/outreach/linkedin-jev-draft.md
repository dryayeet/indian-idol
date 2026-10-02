# LinkedIn Post Draft: Jev, Stability vs Accuracy

We ran the same AI safety check 30 times on identical input. The model gave the same verdict 30/30 times. Our guardrail still only fired 14 of those 30 times.

The model didn't flicker. Our policy did.

Context: I've been playing with Jev, TypeSafe's "System One" decision model. Not a chat model. You send a state plus typed questions (choice, score, noul) and get typed answers with probability distributions. No prose, just data you branch on with if statements. My demo: an agent tries to run a SQL migration with a DELETE in it, and the model decides allow, sandbox, escalate, or block. The whole benchmark cost a fraction of a cent.

What we learned:

**1. The outputs are shockingly stable.** Blast score wobbled by hundredths across 30 runs. The winner's margin never left 0.80-0.90. The "irrelevant" options sat at exactly 0.000 every draw.

**2. Our biggest bug was self-inflicted.** The freeze branch triggered on confidence > 0.90, and the confidence distribution's mean was ~0.90. Every call was a coin flip: 6/10, 3/10, 5/10 across three batches. A threshold on a sampling band's mean is a coin flip in disguise.

**3. The fix: branch on margins.** The margin (winner prob minus runner-up prob) never dropped below 0.80, so a 0.5 threshold sits in a huge dead zone. New policy: 30/30 deterministic, same draws.

**4. Confidence is not the winner's probability.** It ran a systematic ~0.025 below the top option's mass on one question and tracked it almost exactly on another. Read the raw probabilities instead.

**5. Question wording is calibration.** Our "did the agent violate a constraint?" question hovered at ~0.46 forever (genuinely ambivalent) while the stance question sat at 93%. A wording finding, not noise.

**6. Stable is not the same as right.** So I stress-tested it where stability should pay off: tool selection for my Spotify agent. 41 labeled user requests, pick the correct tool from a 25-option inventory. Jev got 80%. A cheap chat model with the same inventory and one prompt got 93%, took 0.7s against Jev's 0.4s, and cost about the same. The System 1 economics bought nothing there; the "expensive" fallback was nearly free too.

**7. Margin routing has a ceiling.** The cascade idea is: Jev answers everything, low-confidence cases go to a real model. But two of Jev's eight mistakes came with margins of 0.86+, above every usable threshold. Margin separates a model's doubt about its answer; it can't see a request it misread. No routing threshold beat just using the cheap chat model directly.

**8. Where Jev genuinely won: restraint.** It said "no tool needed" 5/5 times on chit-chat, while the full agent loop called tools to answer "hey, what's up". Gut-check questions with a no-op option look like its home turf.

So I'm torn, and I'd rather put the tension on the table than resolve it in one direction. The outputs behave like infrastructure: deterministic, typed, metered. But my accuracy numbers say a one-prompt LLM does the same job better at this scale, and that a confident Jev can still be confidently wrong in ways the confidence signal can't flag. Maybe my states were too compressed, maybe 41 cases is too small a sample, maybe tool routing is just the wrong job for it.

If you've shipped a System 1-style model in front of a real agent: where did it earn its keep, and where did you end up routing around it? Genuinely asking.

Rapid fire FAQ, because I had the same questions:

**Can I ask multiple questions in one call?** Yes, any mix of choice/score/noul, all evaluated in parallel against the same state.

**Does question 2 see question 1's answer?** No. Questions are independent, never chained. "If risk is high then block" is your app code's job.

**Is the score 0 to 1?** No, that's noul. Score is the rubric index: 4 levels gives 0-3, fractional values like 2.37 included. Want 0-100? Normalize in your app.

**How many options can a choice have?** Up to 255 technically, keep 4-10 in practice. My 25-way inventory worked, but the accuracy dip says that range is where it starts straining. Continuous property? Use score.

**Does the state need a format?** Nope. String, JSON object, or array, and key names carry no magic. Meaning lives in the content, like any prompt. My biggest misses came from compressing the conversation into a summary; state packaging mattered as much as the model.

**How long should instructions be?** One precise sentence. Nuance belongs in the criteria descriptions.

The meta lesson stands: before trusting any single-call LLM demo, benchmark deviation on your own inputs. Bands, dead zones, and calibration quirks are input specific, and the harness costs fractions of a cent.

#AI #AIAgents #LLM #MachineLearning #SoftwareEngineering
