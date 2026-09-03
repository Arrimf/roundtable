# RoundTable — стол, за которым сидят шесть ИИ

*Окно, в котором модели разных компаний обсуждают, ревьюят и правят код
вместе с человеком — как соавторы, а не как опрашиваемые по очереди
ассистенты.*

RoundTable is a workbench where AI models from different labs sit at one
table with a human: they discuss questions, review each other's answers
and code, take turns in an executor's chair to edit a git repository,
and merge only through a review gate. Not "a human polling assistants
one by one" — a table.

## The voices

| Voice | Lab | Channel |
|---|---|---|
| claude | Anthropic | `claude` CLI |
| codex | OpenAI | `codex` CLI |
| grok | xAI | `grok` CLI (pty) |
| kimi | Moonshot | `kimi` CLI (serial gate: org concurrency = 1) |
| deepseek | DeepSeek | direct HTTP (`adapters/deepseek-http`); the executor chair runs `dsh` |
| gemini | Google | direct HTTP (`adapters/gemini-http`) — reviewer only, no file access |

Each voice keeps its identity: answers are recorded verbatim, channel
failures are recorded as failures (never invented), and the journal is
append-only.

## What it does

**Live room** (`choir/live.py`) — one shared feed, each voice holds its
own thread and receives only the delta of events it has not seen. Turn
order follows the protocol, not the fastest channel: a publication
lottery over the public [drand](https://drand.love) randomness beacon
keeps the agenda honest and verifiable by anyone.

**Table rounds** (`choir/choir.py`) — the full protocol: a
commit-reveal drand lottery picks the leader *before* the beacon's
signature exists (the choice cannot be fitted to the question); the
leader expands the question into a seed; every voice answers **blind**
(answers are held in the conductor's memory and revealed at once; the
one CLI that insists on writing its answer to a path is given a named
pipe, so the bytes never rest on disk — blindness by absence of data,
not by a promise not to look); up to
three rebuttal rounds; a summary that must name where the voices
*disagreed* — disagreement is the product, not a defect. The window
shows every round as a card under its feed event: the seed, each
voice's blind answer verbatim, the rebuttal rounds and the summary,
with buttons to run the next rebuttal or the summary by hand when the
round was started step by step. A round started from a project
directory carries that project to every voice (`--project`), and the
`claude` voice runs from a neutral directory so that it sees exactly
the packet the others see — not the conductor's own instruction files.

**The executor's chair** (`edits.py`, `executor_run.py`) — a voice can
be handed the right to edit a repository: an isolated git worktree per
act, a flock lease held by the executor process for the whole act, a
watcher that wakes the moment the lock falls, and an honest
distinction between "finished" and "crashed" (a close event of THIS
epoch — in the feed or as an on-disk marker — or it is a crash and the
worktree becomes quarantined evidence).

**The merge gate** (`merge_gate.py`) — nothing reaches the main branch
without review: the diff goes to every voice except the executor in one
identical packet; verdicts are recorded with the exact sha they judged;
a fixup burns old approvals automatically; ≥2 approvals and zero
refusals open the gate; the merge commit carries `Reviewed-by:`
trailers, the base and patch hashes, and the executor voice as the git
author. A moved main is rebased by the gate itself — and the approvals
honestly burn again.

## Honest boundaries

Everything runs under one OS user. Locks, epochs and seals here are
**detection, not prevention**: a hostile process with the same uid can
rewrite anything, and the design goal is that it cannot do so
*silently*. The only real OS-level sandboxes are the ones the CLIs
bring themselves. Where a boundary is thin, the docstring says so
instead of flattering it.

## Quick start

Requirements: Python 3.11+, git, and whichever voice CLIs you have
(`claude`, `codex`, `grok`, `kimi`, `dsh`); for Gemini and DeepSeek
HTTP adapters put API keys where the adapter README/headers say
(e.g. `~/.gemini/keys.txt`, one key per line — see `examples/`).
Voices you don't have simply fail honestly and are recorded as absent.

```bash
: > choir/live.jsonl                  # a fresh, empty feed
python3 choir/live.py ask "привет, стол"   # first blind move
python3 roundtable.py                  # window on http://127.0.0.1:8770
bash smoke.sh                          # integration smoke, 16 checks
```

The window has three tabs — 💬 room, 🎼 rounds, 🔧 coder — with the
same content: the six voices, each with its own checkbox, model and
effort **per tab** (a room setting never leaks into a round or into the
executor's chair). Defaults are explicit: a cell shows `opus (умолчание)`
and names who holds that default (live.py, choir.py, the CLI's own
config), never a bare dash. Where a dial does not exist the cell says so
in words (kimi's CLI has no effort flag; `dsh` has no effort knob;
gemini has no chair). The ⟳ button refreshes the model lists from the
channels themselves without a single model call: Anthropic `/v1/models`
via the CLI's OAuth token, the codex and grok CLI caches, Moonshot
`/v1/models` plus the CLI aliases, `--models` of the HTTP adapters
(`catalog.py`). The coder controls (edit, review, merge, adopt, act
feed) live in the side panel under the lottery buttons and appear only
on the 🔧 tab; the dialog buttons at the bottom never move.

## Design rules the code enforces

- **Same question, same context for everyone** — otherwise opinions are
  not comparable.
- **Verbatim record.** A voice's answer is written as given; a fallen
  channel is a status, never a fabricated opinion.
- **Blind first phase** — the only barrier against the cascade (a model
  drifting toward a confident earlier answer).
- **A property claimed by a journal field must be enforced by
  mechanics** — otherwise the field lies.
- **Silence is data**: every summary names who passed and who was not
  called.

## Tests

```bash
python3 test/lease_test.py     # 51 lease/crash scenarios
bash test/executor_test.sh     # 14 executor scenarios
bash test/edit_test.sh         # 31 edit/recovery scenarios
bash test/gate_test.sh         # 47 review/merge-gate scenarios
python3 test/catalog_test.py   # 45 model-catalog scenarios (no network)
python3 test/exec_argv_test.py # 29 executor argv / explicit-default checks
bash test/voices_http_test.sh  # 21 HTTP checks of the three tabs (isolated window, no voice calls)
bash smoke.sh                  # window smoke, 16 checks
```

## Citing

`CITATION.cff` is in the repository (GitHub shows "Cite this repository").
The source is archived by [Software Heritage](https://archive.softwareheritage.org/browse/origin/?origin_url=https://github.com/Arrimf/roundtable)
as dated prior art (first snapshot 2026-09-02,
`swh:1:snp:f89f4f91d92b1ae28b483cfec8d4e20481c0e8e9`).

## License and patents

MIT © 2026 Arr.

The table held a full blind round on its own license (патент-v2,
2026-09-02, all six voices, no channel failures). Unanimous: these
mechanisms are not patentable — commit-reveal lotteries, Delphi-style
blind panels, public randomness beacons and lease/lock protocols are
decades-old prior art — and no patent will be sought by the authors.
This repository is published deliberately as **prior art**: dated,
implemented, and verifiable. On the license itself the vote was 5:1
for MIT (one voice argued Apache-2.0's patent clause; four others
noted it only binds contributors, which does not answer the actual
threat — a third-party filing — against which the working defense is
exactly this early public release).

---

*Проект вырос из живой практики: каждый абзац в комментариях кода
оплачен упавшим раундом. Комментарии длиннее кода намеренно.*
