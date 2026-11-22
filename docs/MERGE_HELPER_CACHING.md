# Merge helper: prompt caching

The merge helper runs Claude as the Claude Code CLI (`claude -p`, one process per PR job). Claude Code places
`cache_control` breakpoints itself (tools and system prompt first, then the conversation), so the helper does not
build requests. Its job is to keep what Claude Code sends small and the same from run to run, and to pick the TTL.

## What was measured (Gaming PC, 2026-11-22, Claude Code 2.1.317, claude-opus-5-5)

From the ledger (subscription runs, 11-21/22): caching already worked inside a run. A PR job read 0.14 to 3.25
million tokens from the cache against 8 to 82 uncached input tokens.

API probes (`claude -p` with the helper's flags, the same short read-only task):

| Setup | Folder | Seconds | Cost | Cache write | Cache read |
| - | - | - | - | - | - |
| before (current flags) | pr-2417 | 12 | $0.181 | 33,168 (5m) | 26,678 |
| lean (this change) | pr-2417 | 26 | $0.170 | 30,002 (5m) | 55,441 |
| lean, next run, another folder | repo | 46 | $0.065 | 4,485 (5m) | 112,155 |

"Reply OK" probes: before, the first request wrote 22,274 tokens ($0.11). Before, in the main checkout, one run
sat idle for 9+ minutes (1.25 s CPU) while the user's MCP servers (blender, silhouette) connected, and plain
runs took 43 to 243 s to start; lean runs took 12 to 46 s.

What was in each request before: the user's MCP server instructions ("blender, silhouette still connecting",
which also changes run to run), the whole skills list (~17,000 characters), and an auto-memory section with a path
derived from the worktree folder, so each `pr-<n>` folder had a different system prompt.

## What the helper does now

- `--strict-mcp-config` (no `--mcp-config`): no user MCP servers. The helper never needs Blender or Silhouette.
- `--disable-slash-commands`: no skills list in every request.
- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and `--exclude-dynamic-system-prompt-sections`: the system prompt is the same
  in every PR worktree, so it is a shared cache entry.
- On "api", `CLAUDE_CODE_PROMPT_CACHE_TTL` is pinned to `cacheTtl` (default `5m`).
- The ledger records `cache_write_tokens` and `cache_write_1h_tokens` next to `cache_read_tokens`.

## Why not the 1-hour TTL

Claude Code applies one TTL to every cache write in the main conversation. A 1-hour write costs 2x the input price,
a 5-minute write 1.25x. Turns inside a run are seconds apart, so the 5-minute cache never expires mid-run. Runs
are at least 10 minutes apart (often hours), so a 1-hour cache would only save the ~30k-token setup prefix
(~$0.15 a run on Opus 5.5) and only when another run comes within the hour, while every in-run write (typically
60k to 150k tokens a run) would cost 60% more ($0.18 to $0.45 a run). It loses money. Set `cacheTtl` to `1h` in
the profile only if runs ever come in tight bursts.

## Check it

Every ledger row (`<state>/merge-helper-runs.jsonl`) has `cache_read_tokens`, `cache_write_tokens` and
`cache_write_1h_tokens` (should be 0 on "api"). A healthy run reads far more than it writes.
