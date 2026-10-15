# Demo: one benchmark entry, every call

`HumanEval_42/` traces HumanEval/42 `incr_list` (the correct version and a buggy mutant) through the pipeline
with Claude Opus (`claude -p`, isolated) and Dafny 4.11. The run was recorded live on 2026-10-15 with
`nfv run --arm pipeline --tasks 42 --trace`: 12 LLM calls, 12 verifier runs, 2 correct verdicts.

| file | what |
|---|---|
| `demo.gif` | animated walkthrough (52 frames, ~4 min, loops) |
| `demo.pdf` | the same deck, one slide per page (25 pages) |
| `deck.html` | the deck itself; open it in a browser and use ←/→ (`#s=N&p=K` selects slide N, phase K) |
| `trace.md` | readable dump of every prompt, response, verifier command, input file and output |
| `run/` | raw records: `trace.jsonl`, `transcript.jsonl`, `results.jsonl`, per-stage artifacts |

Rebuild from a traced run:

```bash
python3 scripts/dump_trace.py demo/HumanEval_42/run demo/HumanEval_42/trace.md
nix develop .#demo -c python3 scripts/make_demo.py demo/HumanEval_42/run demo/HumanEval_42/deck.html
nix develop .#demo -c python3 scripts/render_demo.py demo/HumanEval_42     # frames/, demo.gif, demo.pdf
```

The `demo` dev shell provides headless Chromium, Pillow and pinned fonts (Inter, JetBrains Mono). Chromium runs
with `--no-sandbox` because its setuid sandbox helper is not configured on this machine; it only loads the
local deck.
