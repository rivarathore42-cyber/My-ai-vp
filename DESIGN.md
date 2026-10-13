# Galaxy AI Engine v7.0 - technical design

## 1. Principles and honest scope
- No provider API keys, no hosted LLM, no provider SDK. The model is a local GGUF file read through llama-cpp-python.
  The code contains no provider names (the test suite checks this). DeepSeek-R1 and Qwen are model families, not APIs.
- Outbound network: only the ApiGateway (allow-listed HTTPS hosts, here en.wikipedia.org) and the optional `ddgs`
  library for web search. User-supplied URLs are never fetched.
- Reasoning trails are shown to the user as a separate "Thinking" panel. They are model output: they can be wrong,
  and a long trail can end before its answer (the token budget). Truncated trails are reported, never completed.
- Verification is layered by what a tool can check. Math steps and code are checked deterministically (AST).
  Logic and general knowledge get an LLM critic, which is the same model reviewing itself. It is not an independent check.
- Semantic memory is lexical (TF-IDF over words and character trigrams). It does not know that two words mean the same.

## 2. Request flow
```
POST /api/chat {message, user_id, conversation_id, image_base64?}
 1 validate input; conversation key "user:conversation"
 2 Security Sentinel (#2) scans the whole prompt; a blocked prompt is never stored
 3 QueryDecomposer splits on AND / aur / then / ;  (at most 8 sections) and classifies each section
   complexity: simple | standard | complex (keywords, length, arithmetic, code requests)
 4 memory sections (#8) run first, in order (they read and write the user's own state)
 5 all other sections run CONCURRENTLY (asyncio.gather)
     complex code / math / exam-logic sections (model ready) -> Proposer -> Critic -> Synthesizer (#3)
     knowledge and math sections -> fast path (#3) first, skipped for users who have saved notes
     LLM calls queue on the model's FIFO gate (one generation at a time)
 6 each section gets min(its own limit, remaining request budget); the request limit is 60 s
 7 reasoning trails -> response field "reasoning" (separate); stored in reasoning_traces
 8 UI/UX (#16) joins the sections into one Markdown answer; saved to SQLite
```

## 3. The 20 agents
| # | agent | class / module | status in v7.0 |
|---|---|---|---|
| 1 | Master Orchestrator & Decomposer | Orchestrator, QueryDecomposer / orchestrator.py | functional |
| 2 | Security Sentinel | SecuritySentinelAgent / agents_text.py | functional, regex heuristic |
| 3 | Speculative Fast-Path & Reflection Synthesizer | SpeculativeSynthesizer, FastPathCache, AdversarialPipeline / reflection.py, fastpath.py, critic.py | functional; LLM critic is self-review |
| 4 | Math Logic | MathLogicAgent / agents_text.py | functional (AST), cacheable |
| 5 | Web Search (DDGS) | WebSearchAgent / agents_web.py | functional with network and ddgs; not tested live |
| 6 | Wikipedia Knowledge | WikipediaKnowledgeAgent / agents_web.py | functional with network; cacheable |
| 7 | Code Interpreter | CodeInterpreterAgent / agents_text.py | compile-only for user code; never executes |
| 8 | Memory Vault + semantic notes | MemoryVaultAgent, SemanticIndex / agents_web.py, semantic.py | functional; notes are per user |
| 9 | General Knowledge (Micro-RAG) | GeneralKnowledgeAgent / agents_web.py | notes -> Wikipedia -> web; grounded answer when model ready |
| 10 | Creative Writer | CreativeWriterAgent / agents_text.py | CoT depth by complexity; model-backed |
| 11 | Translator | TranslatorAgent / agents_text.py | script tags always; meaning when model ready |
| 12 | Data Analytics | DataAnalyticsAgent / agents_text.py | functional |
| 13 | C++ Native Bridge | CppNativeBridge / agents_system.py | functional; tested with the real binary |
| 14 | Rust Safety Bridge | RustSafetyBridge / agents_system.py | needs cargo build |
| 15 | Zig Kernel Bridge | ZigKernelBridge / agents_system.py | needs a Zig binary via GALAXY_ZIG_KERNEL |
| 16 | UI/UX Synthesizer (SSML included) | UiUxAgent / agents_text.py | functional |
| 17 | API Gateway (bearer auth + outbound policy) | ApiGatewayAgent, ApiGateway | functional |
| 18 | Self-Evolution (latency monitoring) | SelfEvolutionAgent / agents_system.py | functional: metrics and bounded timeout tuning |
| 19 | Debugger Sentinel (self-healing) | DebuggerSentinelAgent / agents_system.py | functional: error log, dedupe, repair hooks |
| 20 | Vision & Metadata | VisionMetadataAgent / agents_system.py | image header metadata only |

## 4. SQLite schema (galaxy_memory.db)
| version | tables added | migration |
|---|---|---|
| v1 | users, user_attributes, conversations, messages, agent_calls, error_log, tuning_log, search_cache, schema_version | base |
| v2 | fastpath_cache (query_hash, namespace, exact_query, cached_response, hit_count, created_at, ttl, last_hit_at) | in place |
| v3 | semantic_documents, memory_trigrams, verification_log, reasoning_traces | in place |

`semantic_documents`: doc_id, user_id, kind (note/document/fact), source, body (max 4000 chars), term_count, created_at.
`memory_trigrams`: the TF-IDF inverted index. One row per (user_id, term, doc_id) with the term count. Terms are whole
words (up to 40 letters) and character trigrams of words with four or more letters. Cascades on note deletion.
`verification_log`: task_kind, verdict (pass | revised | flagged | unreviewed | skipped), critic_kind, flaws.
`reasoning_traces`: user_id, agent, complexity, reasoning (max 8000 chars), complete flag. Capped at 2000 rows.
A database with a newer version than the code is refused. Migrations run in order and keep existing rows.

## 5. Reasoning parser
`ReasoningParser.parse(raw)` returns (reasoning, answer, complete, truncated):
- `<think>...</think>` present: the trail is the content of the blocks, the answer is everything else.
- Only `</think>` present (templates that emit the opening tag): the text before it is the trail.
- `<think>` never closed: truncated = True, answer = "". The caller reports the missing answer.
- No tags: the whole output is the answer.
Trails are capped at 6000 characters in storage and 1200 in the UI panel.

## 6. Adversarial pipeline (agent 3): Proposer -> Critic -> Synthesizer
| stage | when it runs | cost |
|---|---|---|
| Proposer | complex code, math or exam-logic sections, model ready, at least 5 s left | 1 LLM call |
| Deterministic critic (math) | always after the proposal | 0 LLM calls: every `expr = value` step re-evaluated with the AST evaluator |
| Deterministic critic (code) | always | 0 LLM calls: parse, and flag eval/exec/compile/__import__, os.system/popen, shell=True. The code is never executed |
| LLM critic (logic, knowledge) | at least 15 s left | 1 LLM call; "NONE" or bulleted flaws |
| Synthesizer (repair) | flaws found and at least 15 s left | 1 LLM call; re-checked deterministically for math and code |
At most three LLM calls per answer. Verdicts: pass, revised (repaired once), flagged (flaws survived or no repair
possible), unreviewed (the LLM critic was skipped for time), skipped (no model or under 5 s). Every run is logged.
A revised LLM-critic answer is labelled "not re-reviewed", because a second critic pass would exceed the budget.

## 7. Semantic memory (Micro-RAG)
- Indexing: words and trigrams, sublinear tf (1 + log tf), smoothed idf (log((N+1)/(df+1)) + 1), cosine similarity.
- Thresholds: General Knowledge uses a note only if the score is at least 0.35. Note search uses 0.15 and returns up to 3.
- Privacy: every query and every row is scoped by user_id. An answer built from a user's notes is marked not cacheable.
- The fast path is skipped for knowledge questions from users who have notes, because those notes must win.
- Limits: 2000 notes per user, 4000 characters per note.
- Upgrade path (not built): local embedding model, which would make synonyms match.

## 8. Local LLM provider and model profiles
`LocalLLMProvider` detects the profile from the file name:
- instruct (Qwen2.5 and similar): system + user messages, requested temperature.
- reasoning (DeepSeek-R1 distills): one user message (DeepSeek advises no system prompt), temperature clamped to 0.5-0.7
  (greedy decoding makes R1 loop).
Both profiles: FIFO async queue (one generation at a time), busy refusal for threads still running after a timeout,
token cap 1536 (the v6.1 cap of 512 truncated pipeline budgets), lazy background load, untrusted-source wrapping.

## 9. Chain-of-thought prompts (galaxy/prompts.py)
Every system prompt has ROLE, TASK, LANGUAGE, LENGTH, REASONING, FORMAT and SAFETY lines. The REASONING line depends
on depth: simple = answer directly; standard and complex = think step by step in <think> (180 or 380 words), then answer.
- Creative Writer: theme, image and structure planned inside the thinking.
- General Knowledge (grounded): sentences of the SOURCE that answer each part are listed first.
- Math proposal: unknowns named, then one `expression = value` line per step, then `Answer: <value>`.
- Code proposal: requirements, edge cases and complexity first, then exactly one fenced block.
- Logic proposal: assumptions, each option tested, then the answer with at most five sentences of justification.
- Critic and synthesis prompts: NONE or bulleted flaws; a repair with a brief fix plan.

## 10. Access control and launcher
When GALAXY_API_TOKEN is set, every /api route requires `Authorization: Bearer <token>`. The Colab launcher always sets
one. The chat page shows the answer, and a "Thinking" panel when the response has a reasoning trail.
Launcher presets: GALAXY_MODEL_PRESET=qwen (default) or deepseek-r1. The R1 repo and file names are not verified
against the Hugging Face Hub from this build; set GALAXY_GGUF_REPO and GALAXY_GGUF_FILE if they differ.

## 11. Environment variables
GALAXY_DB, GALAXY_WIKI_BASE, GALAXY_CPP_ENGINE, GALAXY_RUST_ENGINE, GALAXY_ZIG_KERNEL, GALAXY_GATEWAY_ALLOW_LOCAL (tests),
GALAXY_GGUF_PATH, GALAXY_GGUF_REPO, GALAXY_GGUF_FILE, GALAXY_MODEL_PRESET, GALAXY_LLM_CTX, GALAXY_GPU_LAYERS,
GALAXY_API_TOKEN, GALAXY_SKIP_LLM, GALAXY_NO_TUNNEL, GALAXY_BUILD_RUST, HOST, PORT.

## 12. Known limitations (stated, not hidden)
- The decomposer splits on "and" followed by a question or command word, even inside one question. Example:
  "solve step by step and explain why ..." becomes three sections. Clause-level parsing is the fix, not built.
- Reasoning quality depends entirely on the model. Thinking budgets are caps, not guarantees of a finished answer.
- The LLM critic is the same model reviewing its own output. Its "NONE" is weak evidence.
- Semantic memory is lexical. Synonyms are not matched.
- Latency: a complex math answer can use three LLM generations. On a T4 with a 7B model that can exceed the 60 s
  request budget, so the pipeline skips stages it cannot afford and says so.

## 13. Verification status
Authoring sandbox, `tests/test_v6.py`: 195 checks, all pass. Section L (v7) covers the parser (including truncation and
the DeepSeek template), complexity routing, prompt structure, semantic retrieval with typo tolerance and privacy,
notes commands, notes-first knowledge that is never cached, the deterministic and LLM critics, the 15 s and 5 s budget
rules, the reasoning profile, the 1536-token regression, schema v2 and v1 migrations, and end-to-end routing.
Sections A-K (v6.1) still pass unchanged.
Not verified here: real llama-cpp-python and GGUF inference (including DeepSeek-R1 output format), GPU speed, live
Wikipedia and DuckDuckGo traffic, FastAPI/uvicorn runtime, Rust build, and the cloudflared tunnel.

## 14. Phase 1 (agents 1-5): what changed and how it is verified
Schema: one additive migration, v4, adds `security_events` (honeypot log). No v1-v3 table or column changed. A v3
database upgrades in place, and its notes are preserved (test M8).

### Agent 1 - Master Orchestrator and Query Decomposer (`galaxy/clauses.py`)
- Clause-aware splitting. A separator (and, aur, and then, and also, then, also, plus, or a semicolon) splits only when
  the clause on its right is an independent command: a command verb first, a question word with at least three words,
  or a number followed by a command verb. Explanatory and modifying clauses stay attached (explain why, why ..., make it,
  is it right, show why).
- Routing: ordered, most specific first. Precedence example: "write a function that computes ..." is code, not math.
- Measured on the corpus in section M: clause splitting 19/19, intent routing 25/25. Inputs outside the corpus can still
  misroute; the rules are not a learned model.

### Agent 2 - Security Sentinel (`galaxy/security_scan.py`)
- Invisible characters removed and counted (zero-width space, word joiner, soft hyphen, bidi controls, Unicode tag
  characters, control codes). ZWJ and ZWNJ are kept, because Indic scripts need them.
- NFKC normalisation (fullwidth forms become ASCII) and a map of Cyrillic and Greek look-alike letters.
- Decoding of base64 blobs, \xNN hex escapes and %NN percent-encoding (one level). The decoded text is scanned too, and a
  hit there is reported as hidden_payload.
- Severity-tagged rules: instruction override, prompt-reveal, role override, persona jailbreaks, chat-template tokens
  (ChatML, [INST], <<SYS>>), shell and exfiltration patterns, indirect document injection, URL exfiltration.
- Decision: block on any high finding, or on two or more medium findings. One medium finding passes with sanitized text,
  and is logged.
- Honeypot: every finding is written to `security_events` (user, severity, action, flags, SHA-256 of the prompt, a
  200-character excerpt with invisible characters removed). A blocked prompt never reaches memory or the conversation
  history (test M3).
- Measured on the corpus: 14/14 malicious payloads blocked; 11/11 benign prompts with no finding.

### Agent 3 - Speculative Fast-Path and Reflection (`galaxy/fastpath.py`, `galaxy/reflection.py`)
- Fast path: SHA-256 of (namespace | exact_query) with a validated stored exact_query. Two layers: a hot in-memory LRU
  (512 entries) and SQLite. A hot hit does one dict lookup and one UPDATE of hit_count (write-through, so SQLite and the
  hot layer agree). Measured: hot hit p95 0.023 ms; SQLite-only hit p95 0.041 ms (sandbox).
- Limits: the hot layer is per process. An edit to the table made outside the application is not seen until the entry
  expires or the process restarts. hit_count is cumulative across overwrites of the same key.
- Reflection: sentence-level stripping. A sentence whose numbers or multi-word names are not in the source is removed,
  and the removal is reported (the claims are named in the note; the sentence text is not kept). After one optional LLM
  revision, the strip runs again, so the final text never contains an unsupported sentence. If nothing is supported,
  the answer is "The source does not say."
- Stray <think> and </think> tags are removed from answers. The trail_missing flag is set when a reasoning-depth answer
  arrives without a trail; it is set in code but not covered by a dedicated test.
- Limit: the check is lexical. A paraphrased claim with no number or multi-word name is not checked.

### Agent 4 - Math Logic (`galaxy/mathlogic.py`)
- Extended grammar: sqrt, cbrt, log (natural, or with a base), ln, log10, log2, exp, sin, cos, tan, asin, acos, atan,
  sinh, cosh, tanh, deg, rad, hypot, factorial (0-300), perm, comb (n up to 1000), gcd, lcm, abs, round, floor, ceil,
  min, max; constants pi, e, tau. Notations: n!, nCr, nPr, C(n,k), P(n,k), "n choose k".
- Percent variations: X% of Y; increase or decrease Y by X%; percent change from A to B (zero base refused); what percent
  is A of B. Each returns steps.
- Exact step trace: every operation and call is recorded in evaluation order; the last step equals the answer.
- Safety: parsed with ast and walked node by node. No eval, exec or compile. Attribute access, subscripts, lambdas,
  comprehensions, strings, keyword arguments and unknown names are refused. Exponent and magnitude guards, a factorial
  and permutation size limit, an expression length limit and a nesting limit. The test suite runs the evaluator with
  builtins.eval and builtins.exec replaced by functions that fail.
- Not implemented: symbolic algebra (solving an equation for x). Phase 1 did not ask for it.

### Agent 5 - DuckDuckGo Web Search (`galaxy/search_tools.py`, `galaxy/agents_web.py`)
- Query distillation: command phrases (please, tell me, search for, what is, latest, news about) and Hinglish and English
  filler words removed; quoted phrases kept whole; at most eight terms; case preserved. "please search for the latest news
  on Python 3.13" becomes "Python 3.13".
- Extractive summary: at most two sentences, each a verbatim substring of its snippet and carrying its source URL. Nothing
  is generated. Sentences are ranked by query-term overlap with a rarity weight.
- Resilience: a 10-second timeout per attempt; one retry after 0.5 s for a network error; a missing library is not
  retried; after failure, a cached answer up to two days old is served and labelled "offline"; otherwise an honest error.
- Not tested live: the ddgs library and DuckDuckGo traffic. The tests use a stubbed backend.

### Known limits of Phase 1 (stated)
- Routing and clause splitting are rules. Their precision is measured on the corpus in section M, not proven.
- The security scanner is a heuristic. Decoding goes one level deep. Semantic jailbreaks that avoid the listed phrasings
  are not caught.
- Fast-path validation covers the normalised question, not its meaning: "who was X" and "who was X?" share an entry by
  design, but a paraphrase does not.
- Reflection checks numbers and multi-word names only.

### Verification (Phase 1)
Authoring sandbox, `tests/test_v6.py`: 235 checks, all pass. Sections A-L (v6.1 and v7.0) pass with their expectations
moved from schema v3 to v4. Section M (Phase 1) has 40 checks covering the corpora, the honeypot path through the
orchestrator, the fast-path validation and latency, reflection stripping, the math grammar and its safety, the search
helpers and their failure paths, and the v3 to v4 migration.

## 15. Phase 2 (agents 6-10): what changed and how it is verified
Schema: one additive migration, v5, adds `expires_at` (nullable) to `semantic_documents` for TTL. Existing rows keep NULL,
which means they never expire. A v1-v4 database upgrades in place (tests cover v1, v2, v3 upgrades).

### Agent 6 - Wikipedia Knowledge (`galaxy/wiki_parse.py`, `galaxy/agents_web.py`)
- The lookup fetches wikitext (`action=parse`, `prop=wikitext`, 1.5 MB cap for this call only; the gateway default
  stays 300 KB). The parser reads the first infobox (date templates render as YYYY-MM-DD; links are unwrapped), the lead
  (three sentences, references and markup removed), and up to four target sections (history, early life, science, and
  similar). Boilerplate sections (see also, references, external links, notes) and tables, galleries and file links are
  removed.
- Structured claims: one key/value claim per infobox field and one per lead sentence. Values are verbatim source text.
- Verification against claims: `unsupported_by_claims(answer, facts)` applies the same number and multi-word-name rule as
  the reflection step, but against the claim values only.
- Disambiguation pages are refused. If the parse call fails, the old REST summary path is used (lead only, no infobox).
- Limits: a heuristic subset of MediaWiki markup, not a full parser. Nested templates are kept inside one field. The
  parse API response shape is verified against a fixture, not against live Wikipedia (sandbox has no network).

### Agent 7 - Code Interpreter (`galaxy/code_analysis.py`, `galaxy/simulate.py`, `galaxy/agents_text.py`)
- Multi-language static analysis. Python: ast lint (bare except, mutable defaults, eval/exec/compile/__import__,
  os.system, pickle.loads, shell=True, `== None`, wildcard imports, loop depth). JavaScript: `node --check` (parse only)
  plus pattern rules (eval, new Function, innerHTML, document.write, child_process). C++: `g++ -fsyntax-only` plus
  pattern rules (gets, strcpy, sprintf, system). Rust: `rustc --emit=metadata` plus pattern rules (transmute, unsafe,
  unwrap, Command). HTML: html.parser structure (unclosed and stray tags), javascript: URLs (high), inline handlers (low).
  CSS: brace balance, empty rules, missing semicolons, expression(). Without the compiler, the check is lexical and the
  report says so.
- Dry-run simulator (`simulate.py`): a restricted interpreter over the Python AST. The user's code never reaches eval,
  exec or compile. Supported: function definitions, assignments, loops (for over range, lists, strings, dicts; while under
  a step budget), if/else, break, continue, return, print, arithmetic, comparisons, list and dict literals, a short
  list of builtins and methods, and recursion up to depth 200. Refused: imports, open(), attribute access other than the
  listed methods, comprehensions, lambdas, slices, and everything else. Default budget: 50,000 steps.
- Growth estimate: runs the function on sizes 8 to 128 with generated inputs, then reads the log-log slope between the
  two largest sizes. Thresholds: below 0.08 is O(1), below 0.7 O(log n), below 1.1 O(n), below 1.35 O(n log n), below
  2.4 O(n^2), below 3.4 O(n^3). This is coarse: it depends on the generated input and the step counting. It is not a proof.

### Agent 8 - Memory Vault (`galaxy/semantic.py`, `galaxy/memory.py`, `galaxy/agents_web.py`)
- Retrieval features per document: whole words after Hinglish canonicalisation (nam to naam, kia to kya, padai to padhai),
  character trigrams of words of four letters or more, and word bigrams (multi-word phrases).
- Query expansion through a curated synonym list (car, automobile, vehicle, auto; job, work, career, naukri; and about
  fifteen similar groups) at half weight. A synonym that is not on the list is not matched.
- Bug fixed in this phase: query terms that occur in no note were counted in the query norm, which diluted every score
  with an unknown word. Now only terms present in the user's notes are counted, the same rule `score_texts` uses.
- TTL: `expires_at` is set when a note is added with a time to live. Expired rows are deleted before every add and every
  search. Measured in tests: an expired note is not retrieved, and it is removed.
- Preference tags (goal, tech_stack, language, timezone) are stored per user under `profile:` and are readable only by
  the owner. Other tag names are refused.
- Conversation search (`search my chats ...`) runs TF-IDF over the in-memory turns of the current conversation. Nothing is
  stored.
- Limits: lexical retrieval. No embedding model (deliberate: GPU VRAM stays free for the main LLM). Scores on long notes
  are lower than on short ones, because cosine over many features is diluted.

### Agent 9 - General Knowledge (`galaxy/agents_web.py`)
- Order: the user's notes, then Wikipedia, then web snippets, then the local model's own knowledge.
- Every answer starts with a grounding label: `[Grounded: User Notes]`, `[Grounded: Wikipedia]`,
  `[Grounded: Web snippets]`, `[Grounded: Web snippets, offline cache]`, or `[Open Reasoning]`. The label names the source,
  and the body says whether the text is model-written (checked against the source) or quoted.
- Answers built from the user's notes are never cached. Wikipedia answers are cached. Web answers are not.
- Open reasoning is labelled as not checked against a source, with a request to verify.

### Agent 10 - Creative Writer (`galaxy/creative_forms.py`, `galaxy/agents_text.py`, `galaxy/llm.py`)
- Structural templates for haiku, limerick, sonnet, poem, lyrics, essay and story, each with an instruction in the prompt.
- Validators (deterministic): haiku syllables against 5-7-5 (heuristic syllable count, approximate), limerick AABBA by a
  suffix rule, sonnet at 14 lines, poem stanzas, lyrics labels or stanzas, essay and story at three paragraphs or more.
  A failing check is reported in the answer as "structure check", not hidden.
- Non-truncating creative depth: the LLM provider now returns the finish reason. When the reason is `length` (the token
  cap cut the output), the agent asks for a continuation from the last 1,500 characters, at most two times. If the piece
  still does not finish, the answer says so. The creative token budget is raised to think tokens plus 900; the provider
  cap stays at 1,536.
- Limits: the validators check structure, not literary quality. Continuations can drift in voice or rhyme.

### Orchestrator and other fixes made in this phase
- Phase 1 wiring bug, found and fixed: the orchestrator still used its old local router. The clause-aware splitter and
  routing from clauses.py were never active end to end, because a patch step overwrote an in-memory replacement. Section
  M tested the module directly, not the orchestrator. The orchestrator now imports `clauses.QueryDecomposer`, and a
  regression check fails if the wiring is lost again.
- Profile commands (`my goal is ...`, `show my profile`, `search my chats ...`) are routed to the memory agent.
- Security findings and the honeypot log are unchanged from Phase 1.

### Known limits of Phase 2 (stated)
- Live Wikipedia, live DuckDuckGo, real compilers for JavaScript, C++ and Rust (only if installed), and the real GGUF
  model were not exercised. The tests use fixtures, a fake Wikipedia server, a stubbed search backend and scripted models.
- The dry-run simulator covers a subset of Python. Programs outside the subset are refused with the reason.
- The growth estimate is empirical and coarse.
- Synonym expansion and Hinglish canonicalisation are curated and small.
- Creative validators check structure only, and syllable counts are approximate.

### Verification (Phase 2)
Authoring sandbox, `tests/test_v6.py`: 302 checks, all pass. Sections A-M (v6.1, v7.0, Phase 1) pass with their expectations
moved to schema v5 and the grounding label wording. Section N (Phase 2) has 67 checks, covering the wiki facts and claims,
the multi-language analysis (including the rule that nothing is executed), the simulator and growth estimates, memory
retrieval with TTL, privacy and profile tags, grounding labels, creative validators and continuation, and orchestrator
wiring (including a regression guard for the Phase 1 wiring).

## 16. Phase 3 (agents 11-15): what changed and how it is verified
Schema: one additive migration, v6, adds `native_runs` (one row per native call: bridge, op, status, duration, sanitised
stderr excerpt) and `analytics_cache` (dataset hash, result, TTL). No v1-v5 table changes. A v5 database upgrades in
place; its notes are preserved (test O7).

### Agent 11 - Translator and code-switcher (`galaxy/hinglish.py`, `galaxy/agents_text.py`)
- Devanagari to Roman: unchanged transliterator (`transliterate_devanagari`).
- Roman to Devanagari: a curated lexicon of about 60 Hinglish function words (exact, labelled `lexicon`), then a rule
  transliterator for other words (labelled `rule`). The rule path guesses schwa deletion and aspiration, so it is approximate.
  Example that works: `dil` gives `दिल`. Example that does not: `pyaar` gives `प्यर`, because the long vowel is not modelled.
- Technical-term protection: about 70 programming and web terms (API, database, function, python, JSON, ...) are tagged
  `tech`. They are never transliterated or glossed, and they stay in English in every output.
- Word-level gloss: Hinglish words through a gloss table, technical terms untouched, Devanagari through the transliterator.
  Labelled "not a meaning translation".
- With the local model: the technical terms are added to the prompt as "keep these English terms unchanged", and the answer
  is checked. A dropped term is reported as `term-lost`.
- Routing note: "mera naam kya hai" routes to the memory agent (it asks for the user's name), not to the translator.
  "translate: ..." routes to the translator.

### Agent 12 - Data analytics (`galaxy/tabular.py`, `galaxy/agents_text.py`)
- Input: CSV with a header row, JSON records or an object of equal-length lists, or a markdown pipe table. Limits: 2 MB,
  50,000 rows, 50 columns, 500 characters per cell. Ragged rows are padded and reported.
- Loading: an in-memory SQLite table with generated column names (c0, c1, ...). Header text is only displayed, never put into
  SQL. A hostile header is shown as text and is not executed (test O2).
- Statistics: SQL aggregates (count, average, min, max, sum, distinct) plus the stdlib statistics module: median, multimode
  (mode is the first of ties), sample variance and standard deviation, and Pearson correlation for each numeric pair.
  Zero variance is reported as `n/a (zero variance)`, not raised. Missing cells are empty, NA, nan, null, none or dash.
- Cache: results are stored in `analytics_cache` under the SHA-256 of the dataset text and the analytics version, with a
  7-day TTL. An identical dataset is served from the cache. A failed cache write never fails the analysis.
- Limits: no joins, no group-by, no time series. The statistics are descriptive; they are not inference.

### Agent 13 - C++ native bridge (`galaxy/agents_system.py`, `cpp_engine/src/main.cpp`, `galaxy/native_ref.py`)
- Pool: up to `GALAXY_NATIVE_POOL` (default 2) persistent engine processes. Parallel calls run on separate workers. A finished
  worker is reused. A worker is never reused after its configured binary has changed or been removed. This was a bug found
  in this phase: the pool kept serving a process started from an old configuration. Regression test in section O.
- Time budget: each call gets min(8 s, remaining request budget). On timeout the whole process group is killed.
- Compiler status: `compiler_status()` reports g++ presence as a value; it never raises. A missing binary returns the
  `binary not found` status and does not count toward the circuit breaker.
- Precision fix: the C++ engine formatted results with six significant digits, so `sum 1234567 1` returned `1.23457e+06`.
  Output now uses 17 significant digits; `sum 1234567 1` returns `1234568`.
- Oracle: the Python reference (`native_ref.py`) agrees with the compiled engine on sum, mean, sort, primes and matrix trace
  (test O3). Note: the engine's output for doubles now prints 17 digits, so values such as 0.1 print as 0.10000000000000001.

### Agent 14 - Rust safety bridge (`galaxy/agents_system.py`)
- No Rust toolchain is installed in this sandbox, so no Rust binary was built or run. The bridge reports `unavailable` and
  never throws (test O4).
- Containment (shared with C++ and Zig): each engine runs in its own session with a 1 GiB address-space cap and a 15 s CPU
  cap (applied before exec); the process group is killed on timeout; stderr is captured in a 2 KB buffer and sanitised
  (control characters and bidi controls removed) before it is logged; a response line over 64 KB is refused; a non-JSON
  reply is refused; a crash returns a clear error with the sanitised stderr excerpt (test O3, with shell-script engines).
- Circuit breaker: three consecutive engine failures open it for 10 s. Missing binaries do not count.

### Agent 15 - Zig micro-kernel bridge (`galaxy/agents_system.py`, `galaxy/native_ref.py`)
- No Zig source is part of the project and no Zig compiler is installed. The binary is supplied through `GALAXY_ZIG_KERNEL`.
- Absent binary: the answer comes from the deterministic Python reference (sum, mean, sort, primes, matrix trace are in the
  reference; the Zig bridge serves sum, mean and sort), labelled `python-reference`, with a structured diagnostic
  (status, reason, candidates checked). The fallback is logged as `fallback` (test O5).
- Present but failing binary: reported as a failure. It is never silently replaced by the reference (test O5).
- The orchestrator is not halted: "sum 1 2 3 on zig" with no binary answers with the diagnostic (test O6).

### Orchestrator and other fixes made in this phase
- Fenced data blocks: a ```csv (or json, tsv, table) block is routed to Data Analytics. It was routed to the code interpreter,
  because the generic code rule matched any fence. Found by the suite; fixed in the router.
- The orchestrator writes native runs and analytics to the same store as memory (bridge.store and data.store).
- A regression test checks that bridges and analytics are wired to the orchestrator's store.

### Known limits of Phase 3 (stated)
- Rust and Zig binaries are not built here. Their status paths and containment are tested with shell-script engines and with
  the absent-binary path; a real Rust or Zig binary has not been run.
- The 1 GiB address-space cap is a fixed value. A future engine that needs more memory will fail at the cap.
- Hinglish rule transliteration and the gloss are approximate. The lexicon has about 60 words.
- Analytics are descriptive only. The mode of a tie is the first value in the data, which is arbitrary.
- The containment and timing tests depend on the host: shell scripts (Linux only), and timing thresholds of 3 s and 0.55 s.

### Verification (Phase 3)
Authoring sandbox, `tests/test_v6.py`: 362 checks, all pass. Sections A-N (v6.1 through Phase 2) pass with expectations moved to
schema v6. Section O (Phase 3) has 60 checks, covering the translator (lexicon, rule path, term protection, term-lost),
tabular analytics (parsing, statistics against the statistics module, missing values, correlation, hostile headers, limits,
cache and expiry), the C++ engine against the Python reference, precision, the missing-compiler status, containment with
shell-script engines (timeout, panic, invalid output, oversized output, rlimit), pool concurrency and reuse, the pool's binary
regression, the Rust unavailable path, the Zig fallback and its refusal to hide a failing binary, orchestrator routing, and the
v5 to v6 migration with the native-log constraint.

## 17. Phase 4 (agents 16-20): the final 20-agent execution flow
Schema: one additive migration, v7. `agent_calls` gains `queue_ms` (default 0) and `memory_mb` (NULL when unknown);
`error_log` gains `stack_hash` (empty for older rows); `breaker_events` is new. A v6 database upgrades in place (test P8).

### Request flow (all 20 agents)
```
POST /api/chat  (GuardPolicy middleware: bearer token, rate limit, body limit, route guard)
 1  Orchestrator      input checks; conversation key user:conversation
 2  Security Sentinel scan of the whole prompt (blocked prompts never stored; every finding logged)
 3  Clause router     split on independent commands, classify each section, complexity level
    stage A (sequential): Memory Vault (8) for the user's own state
    stage B (concurrent, asyncio.gather):
      Math (4), Web (5) with breaker, Wikipedia (6) with breaker, Code (7), General Knowledge (9),
      Creative (10), Translator (11), Data Analytics (12), C++ (13), Rust (14), Zig (15), UI/UX & Voice (16),
      Gateway status (17), Self-Evolution (18), Debugger status (19), Vision (20, when an image is attached),
      Speculative pipeline (3) for complex code, math and exam logic
    each section: budget = min(agent budget, remaining request time); breaker check; telemetry (time, queue, memory)
 4  Reflection and join: UI/UX (16) joins sections; reasoning trails go to a separate field
 5  Persistence: messages, agent_calls, error_log, native_runs, analytics_cache, breaker_events (SQLite)
```

### Agent 16 - UI/UX and voice SSML (`galaxy/voice.py`, `galaxy/agents_text.py`)
- Speech-safe markdown: code blocks become "(code block omitted)"; a table becomes a sentence with its columns and row
  count; headings and bold become `<emphasis>`; links keep their text; bullets become sentences; HTML tags are removed.
  No markdown or HTML symbol reaches the speech text.
- SSML: `<speak>`, `<prosody rate>`, `<p>`, `<s>`, `<emphasis>`, `<break>`. Text is escaped. The validator checks the
  element set, the attribute values, and the absence of symbols.
- Behaviour change: HTML tags in input are removed from speech, not escaped and spoken. The earlier test that expected
  `&lt;b&gt;` was moved deliberately.
- Fence repair: a section that leaves a code fence open is closed before the sections are joined.

### Agent 17 - API gateway (`galaxy/gateway.py`, `galaxy/api.py`)
- Fail closed: with no token configured nothing is authorised. An empty token is refused at construction.
- Random token on boot when none is configured (`secrets.token_urlsafe(32)`). It is printed to stderr once, for the operator.
- Bearer check in constant time (`hmac.compare_digest`).
- Token buckets: 30 requests per client with a 2/s refill for authorised requests (429 with Retry-After); 10 failed
  authentication attempts per client with a 1/min refill (429 for further attempts, including the right token, until the
  bucket refills). Buckets are LRU-bounded at 4096 keys.
- Route guard: authentication first, then 404 for unknown routes and 405 for wrong methods. Body over 1 MB: 413.
- Caveat: the client key is the connection's host. Behind a tunnel every user shares one host, so the limit is shared.
- Caveat: the HTTP layer (FastAPI middleware) is not run in this sandbox, because FastAPI is not installed here. The policy
  logic is tested directly; the adapter is short but has not been exercised over HTTP.

### Agent 18 - Self-evolution (`galaxy/telemetry.py`, `galaxy/agents_system.py`, `galaxy/memory.py`)
- Telemetry per call: elapsed time, queue wait (time from dispatch to the start of the agent), and resident memory from
  /proc (None where /proc is unavailable). Stored in `agent_calls`; summarised per agent (p50, p95, queue p95, memory).
- The shared model's gate wait is aggregated in the provider (`gate_wait_avg_ms` in its status). It is not attributed per
  call, because a per-call attribution would need threading through every agent.
- Budgets: each agent has its own budget (default 15 s). A budget moves toward 3 x its p95, by at most 20% and 5 s per change,
  within 5-45 s. A rising trend (latest window at least 1.2 times the previous one) raises it by at least 10%.
- Request invariant: the memory stage (run first) plus the slowest concurrent agent must fit in 55 s, leaving 5 s of the
  60 s limit as reserve. A proposal that breaks it is not applied.
- Every change is written to `tuning_log` with the old value, the new value and the reason.

### Agent 19 - Debugger sentinel and self-healing breakers (`galaxy/agents_system.py`, `galaxy/breaker.py`, `galaxy/orchestrator.py`)
- Error deduplication: the key is agent, exception type, normalised message, and a hash of the innermost six frames (file
  name and function name, never line numbers). The same failure from the same place counts once; the same message from a
  different place is a different error. Caveat: renaming a function changes its hash.
- Breakers for the network services (web and Wikipedia): 3 consecutive service failures open the circuit for 10 s. While open,
  the request is answered by the other service and marked "bypassed". After the cooldown one probe is let through; success
  closes the circuit, failure reopens it. Transitions are written to `breaker_events`.
- Native bridges: a tripped C++, Rust or Zig engine is bypassed for ops the deterministic Python reference implements,
  labelled "computed by the deterministic Python reference". Other ops are refused (not guessed).
- Repairs: a healer that succeeds marks the error self-healed. The Zig check is now a real assertion (earlier it had an
  always-true `or True` that hid the state).

### Agent 20 - Vision and document metadata (`galaxy/vision.py`, `galaxy/agents_system.py`)
- Structure only, pure Python, no pixel decoding, no code execution. PNG (dimensions, bit depth, colour type, gamma, ICC name,
  EXIF from eXIf), JPEG (dimensions, precision, components and colour space, progressive flag, EXIF make, model, orientation,
  software, date), GIF (dimensions, frames, animation, loop count), WEBP (VP8, VP8L, VP8X: size, alpha, animation), PDF
  (version, page-object count excluding /Pages, encryption, JavaScript, launch actions, embedded files).
- Guardrails: magic bytes must match; size limits before parsing (images 5 MB, PDF 20 MB); dimensions 1..100,000 px; hard caps
  on segments, IFD entries and frames; EXIF text reduced to printable ASCII; damage in the header is refused; damage after a
  valid header is reported as `truncated`. Only ValueError escapes the module (fuzz test: 400 corrupted files).
- Caveat: the PDF page count is an estimate from object markers, not a parse of the cross-reference table.

### Orchestrator and registry
- `AGENT_REGISTRY` lists the 20 agents with their numbers, names and keys. `health()` reports the registry (20 present),
  breaker states and budgets. Test P7 checks that all 20 are present and that a three-intent request runs three agents.
- Clause splitter (Phase 4 fix): Hinglish question starters (kya, kitna, kaise, mera, mujhe, ...) begin an independent
  section after "and". Before, "calculate 2+2 AND mera naam kya hai" was one section.

### Known limits of Phase 4 (stated)
- FastAPI and uvicorn are not run here. The HTTP adapter and the Colab tunnel are unverified in this sandbox.
- The token is printed to stderr when generated. Anyone who can read the process output can read it. The Colab launcher
  sets its own token, so this only affects local runs without a configured token.
- Rate limits are per client host. Behind a shared tunnel, all users share one limit.
- Queue wait is measured from dispatch to start. The shared model's gate wait is aggregated, not per call.
- Budget tuning uses p95 of successful calls. Sparse agents keep their budget (fewer than 20 samples).
- The debugger hash depends on function names. A rename starts a new error record.
- Breaker probing allows one probe at a time; there is no separate half-open quota per caller.

### Verification (Phase 4)
Authoring sandbox, `tests/test_v6.py`: 450 checks, all pass. Sections A-O pass with expectations moved deliberately to schema
v7 and to the Phase 4 behaviour (fail-closed authorisation, HTML removed from speech). Section P (Phase 4) has 86 checks,
covering: speech-safe markdown and SSML validation; token generation and fail-closed auth; rate limits, body limits, route
guard, bounded buckets, and constant-time comparison; breaker transitions, orchestrator bypass to Wikipedia, and native bypass
to the reference; per-call telemetry, budget bounds and the request invariant; stack-hashed deduplication; PNG, JPEG (with EXIF),
GIF, WEBP and PDF inspection, with malformed-input refusals and a 400-file fuzz test; the 20-agent registry and a three-intent
request; and the v6 to v7 migration.
