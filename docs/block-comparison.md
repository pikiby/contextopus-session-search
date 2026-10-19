# Bounded standalone block-size and dimension comparison

These measurements were taken on 2026-10-14 UTC using an existing local snapshot
of `Qwen3-Embedding-0.6B`, revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`: CPU, float32, two PyTorch threads,
one block per batch. No model download or working-index mutation occurred.
They describe the standalone implementation and do not measure the new Core module.

## Findings and limits

The selected standalone profile remains 512 tokens and 1024 dimensions: 12/12
exact target spans appeared in the first five results. At 512 dimensions, 11/12
were found and measured SQLite storage roughly halved. Truncating a vector does
not reduce full model computation. The 256-dimensional profile missed two target
spans and ranked the target first less often. The sample shows a storage/quality
tradeoff; it does not establish an optimum for all conversation history.

2048-token blocks achieved 12/12 at each dimension, but there were only eight
unique blocks. A top-five check is therefore weak. Larger blocks also locate
answers less precisely and take longer to embed.

## Measured results

| Tokens/block | Dimensions | Vectors | Embedding + parse, s | Write, s | Total, s | Database bytes | Recall@5 | Hit@1 | MRR@5 | Mean search, ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 512 | 256 | 27 | 64.18 | 0.19 | 64.37 | 1343488 | 10/12 | 0.333 | 0.556 | 0.296 |
| 512 | 512 | 27 | 64.18 | 0.17 | 64.35 | 2392064 | 11/12 | 0.667 | 0.771 | 0.968 |
| 512 | 1024 | 27 | 64.18 | 0.22 | 64.40 | 4493312 | 12/12 | 0.667 | 0.787 | 1.401 |
| 1024 | 256 | 14 | 63.34 | 0.15 | 63.50 | 1306624 | 10/12 | 0.417 | 0.625 | 0.383 |
| 1024 | 512 | 14 | 63.34 | 0.15 | 63.50 | 2355200 | 11/12 | 0.583 | 0.736 | 1.630 |
| 1024 | 1024 | 14 | 63.34 | 0.17 | 63.52 | 4456448 | 11/12 | 0.667 | 0.767 | 3.033 |
| 2048 | 256 | 8 | 70.66 | 5.46 | 76.12 | 1302528 | 12/12 | 0.667 | 0.794 | 0.471 |
| 2048 | 512 | 8 | 70.66 | 0.74 | 71.40 | 2351104 | 12/12 | 0.667 | 0.812 | 1.761 |
| 2048 | 1024 | 8 | 70.66 | 0.36 | 71.03 | 4452352 | 12/12 | 0.583 | 0.757 | 2.551 |

Peak process RSS was 3788.1 MiB in every row. This is the cumulative high-water
mark over the process lifetime, including model loading; individual variants'
peaks were not measured independently. Search timing used one pass over twelve
queries with a warm cache and should not be generalized. The 5.46 s write time
for 2048/256 is an outlier in this single run; no timing repeats were performed.

Median stored block lengths, across all blocks rather than only retrieved ones:

- 512 tokens: 1524 characters (range 725–1718).
- 1024 tokens: 3062.5 characters (range 1157–3329).
- 2048 tokens: 6051.5 characters (range 1112–6578).

## Fixed corpus and gold spans

Two contiguous JSONL byte ranges contain 149 canonical user/assistant messages,
55,525 characters before role prefixes. All twelve targets are in the Session
Search conversation; a deskstate conversation adds unrelated content. This is a
narrow corpus. The normal reader excludes environment context, reasoning,
summaries and tool records. The source paths and hashes below identify that historical sample.
`$CODEX_HOME` replaces the personal source-root path; these are not
installation prerequisites.

| Session ID | JSONL path with source-root placeholder | Byte range [start, end) | Range SHA-256 |
| --- | --- | --- | --- |
| 00000000-0000-4000-8000-000000000001 | $CODEX_HOME/sessions/2026/09/14/rollout-2026-09-14T10-34-14-00000000-0000-4000-8000-000000000001.jsonl | 120506510–130584275 | f1328217236024e79c43500526519750bc7c5cb99c46171c3465c936bf4e329f |
| 00000000-0000-4000-8000-000000000002 | $CODEX_HOME/sessions/2026/09/23/rollout-2026-09-23T11-42-44-00000000-0000-4000-8000-000000000002.jsonl | 7502909–8788912 | 95607e871e51440fb764b73fcfe6aef4667606d940ff43db91cf317cbdfe9fe7 |

Questions and exact record/answer spans were fixed in a local manifest before
model search. The table translates the original Russian questions into English
for documentation; reproduce the original-language queries from the original
manifest to compare directly with these numbers. Character spans refer to the
cleaned canonical message text. Conversation bodies are not included here.

| Question (English translation) | Record byte offset | Answer character span [start, end) |
| --- | ---: | --- |
| Which session states are searched by default? | 126854689 | 80–91 |
| Are existing embeddings retained when archived sessions are excluded? | 126975031 | 238–274 |
| How can the conversation tail be updated without recalculating the entire session? | 127082386 | 963–998 |
| Should the incomplete final block be indexed? | 127082386 | 1254–1296 |
| What is the risk of two-stage search? | 130371265 | 631–670 |
| How much float32 memory does a 256-dimensional vector use? | 130398415 | 415–429 |
| Does vector dimensionality limit input text length? | 130398415 | 568–609 |
| Can the current embedding model write descriptions itself? | 130421925 | 186–210 |
| Why can a brief summary lose important decisions? | 130441012 | 8–52 |
| Does deduplicating embeddings delete messages? | 130463492 | 352–388 |
| How does SHA-256 prevent rerunning the model? | 130484125 | 164–192 |
| Does using larger blocks require a different model? | 130549439 | 31–57 |

## Method and reproduction

The same bounded text was parsed with token limits 512, 1024 and 2048 using the
actual Qwen tokenizer. Each unique block was embedded once at the original 1024
dimensions, with input truncation forbidden. Lower-dimensional vectors used the
first 256 or 512 coordinates followed by normalization; query vectors used the
same transformation. All nine indexes used the normal standalone Store in fresh
temporary directories.

A query succeeded when a top-five unique block contained the exact session ID,
record byte offset and complete pre-labelled answer span in its source metadata.
Hit@1 and MRR@5 use the same target. Database size includes SQLite, WAL and SHM
after a WAL checkpoint. sqlite-vec allocation makes these small indexes much
larger than their vectors alone.

Original temporary manifest: `/tmp/session-block-gold.json`.
SHA-256: `0ee496f2bcb2b13e15e2086b415f6ffad7362ddb3412c52163c8c05a31d6c562`.
Original output: `/tmp/session-block-comparison`. These paths describe the
historical run and may no longer exist. Temporary output is separate from source
transcripts.

To run a new experiment, supply a manifest with `corpus` entries (`path`,
`session_id`, `start_offset`, `end_offset`) and original-language `queries`
(`question`, `session_id`, `record_offset`, `char_start`, `char_end`). Use an
existing environment containing the standalone model dependencies and weights:

```bash
/path/to/existing/python scripts/compare-blocks.py --manifest /path/to/manifest.json --output /tmp/session-block-comparison-new --threads 2
```

The script refuses an existing comparison database. Model loading and the twelve
query embeddings are excluded from indexing time; parsing, block embeddings and
SQLite writes are included. Cold startup, full-history performance, independent
memory peaks and statistical confidence were not evaluated. The historical
manifest is needed to reproduce the exact original query text and scores.
