---
name: jev-routing
description: Use Jev before loading large retrieved text, or for bounded inbox, alert and transaction classification.
---

# Jev routing

Version 2.1 includes operator-controlled automatic reasoning routing before the first
model request. Do not call `jev_review` to route the current prompt: middleware
already handles eligible turns once. In shadow mode it only records proposals;
it does not change effort. Tool-need labels are diagnostics, not permissions or
instructions to omit required checks. Never change router mode without Mark's request.

Prefer `jev_review` for a narrow classification or relevance decision over data that is not yet in the main model context. Use deterministic code first for exact matches, duplicate IDs, thresholds and arithmetic. Do not add a Jev call after already doing the same reasoning; that adds cost.

Save an existing query/API response to a file inside your own workspace without printing its full contents. Convert selected fields deterministically into a JSON list of `{ "id": "source-id", "text": "minimal relevant fields" }`; alternatively use a text file. Never copy another profile's data. Keep credentials and mandatory instructions out. At most 160 records, 6,000 characters per record, 120 KB total. Prefilter larger inputs with the source API. Do not create a second canonical store.

Call `jev_review(source_path=<absolute path>, mode=<mode>, query=<one narrow question>)`:

- All bots: `select` to choose relevant retrieved passages before reading a large result. Returned passages are exact text; omitted IDs remain in the original file. Use this only for relevance search, never exhaustive audits, totals, full-document review, instructions, legal/financial completeness, or troubleshooting where every error matters. Review the full source if the selected evidence is insufficient. For text files, chunks have numeric IDs and may cut across paragraphs; read adjacent chunks for context.
- Friday: `inbox` to suggest categories for ambiguous messages after existing deterministic protection rules. A suggestion never changes protected buckets, acknowledges a gate receipt, archives, unsubscribes or suppresses a message. The current inbox skill and approval rules still govern actions; inspect source messages before any mutation.
- Cisco/Jarvis: `alert` to label unfamiliar log/event records for review. Do not suppress alerts, skip scheduled checks or infer health from this label. Exact duplicate elimination stays in code.
- Penny: `transaction` for suggested merchant categories. Ambiguity goes to review. No financial action, fraud determination or ledger write is authorized.
- Merlin/Clearbot/Prudence: `select` for candidate documents, tasks and references. Preserve IDs for lookup and citations; never treat omission as proof a fact does not exist.

Low confidence retains the evidence or returns `review`. Errors request the normal workflow; they never mean no findings. Returned probability is not a guarantee of correctness. No automatic tool-output interception or alteration of old conversation context occurs. Small/simple tasks can cost more when a new agent tool round-trip is added; batch decisions and use only where it replaces substantial reading/reasoning.

The operator enabled Jev 1.13 through OpenRouter's Decisions API for these uses. This is a remote API: inputs go through OpenRouter to TypeSafe. Local logs retain only counts/timing/usage, never record content. Report dollar savings as estimates until measured against an equivalent baseline. To disable, the operator can create `jev.disabled` in the profile home or disable the plugin.

## App model routing v2
The native app uses authenticated route plans and Hermes one-turn model switches. Do not call jev_review to select your model. A blocked Small-model coding tool must stop; request a Medium/Big continuation, never disguise the same write as a read or bypass the block. Router classification is never tool authorization.

V1 is removed. The v2 engine owns the automatic selected-model effort fallback. Official desktop chats keep their selected model; model switching still needs a valid app route plan. Never claim that an enabled setting proves a model switch happened.
