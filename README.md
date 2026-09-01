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
(answers are held in the conductor's memory and revealed at once —
blindness by absence of data, not by a promise not to look); up to
three rebuttal rounds; a summary that must name where the voices
*disagreed* — disagreement is the product, not a defect.

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
bash smoke.sh                          # 16-step integration smoke
```

The window has three tabs: 💬 room, 🎼 rounds, 🔧 coder (the executor's
chair, review and merge controls). Every voice's model and effort are
adjustable per tab — but only where the dial actually exists.

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
bash smoke.sh                  # 16-step window smoke
```

## License

MIT © 2026 Arr. The table asked itself whether there was anything here
to patent — the round's summary lives in the project journal.

---

*Проект вырос из живой практики: каждый абзац в комментариях кода
оплачен упавшим раундом. Комментарии длиннее кода намеренно.*
