# Digital Understudy

The digital understudy is a 30-day, owner-private technical observation
study. It learns workflow patterns without receiving shell, browser,
messaging, repair, or execution tools.

## What it observes

Once per local day it reads only these allowlisted local sources:

- the exact-bound Google Voice owner transcript;
- infrastructure-city entity status and evidence;
- the localFirstTools sentinel verdict;
- AutoHarness and city-publisher health summaries;
- content-free Voice service outcomes;
- universal messaging journal state counts.

Raw account, peer, phone, user, channel, chat, and provider-message IDs are not
collected. Non-owner conversation content is not collected. Conversation
excerpts are enabled explicitly for this owner-selected study and remain in
mode-`0600` local snapshots. Verification-code messages are dropped; email,
phone, IP, URL, repository, and credential-like values are redacted before
snapshot persistence.

## What it may infer

Analysis is limited to:

- technical workflow;
- system reliability;
- tool usage;
- planning process;
- communication process.

Health/medical, demographic, political, religious, sexual, biometric,
financial, legal, family, relationship, immigration, and employment-status
inferences are rejected. Every accepted insight has a confidence value and
must cite a specific allowlisted evidence ID from the snapshot. Untrusted
evidence and system policy are sent to Brainstem in separate message roles
through private files; evidence never appears in process arguments.

## What it may do

It may:

- correlate observations;
- identify recurring technical patterns;
- make bounded 1–30 day predictions;
- prepare a proposed action.

Every proposal has `requires_approval: true` and `execution: null`. The
understudy contains no execution path. It never sends a message, places a
call, edits a file outside its private state, restarts a service, or approves
its own proposal.

## Evidence and lifecycle

Private daily snapshots form an append-only SHA-256 chain. After each validated
analysis, a sanitized `body.twin-pulse` RAPP/1 frame is appended under the
existing twin rappid. Raw conversation excerpts never enter the pulse frame.

The resident LaunchAgent:

- starts at installation;
- checks hourly;
- creates at most one observation and analysis per local day;
- retries explicit failures;
- exits successfully after writing the final report at the exact 30-day end.

`KeepAlive.SuccessfulExit=false` restarts crashes but does not restart a
completed study.

Every run replays and verifies the complete snapshot/analysis/frame chain
before collecting. An exclusive study lock prevents manual and resident runs
from racing. Installation preserves and restores the prior config, plist,
service, and study start if bootstrap or verification fails.

## Commands

```bash
python3 ~/.rappter-chrome/runtime/install_understudy.py install
python3 ~/.rappter-chrome/runtime/install_understudy.py status
python3 ~/.rappter-chrome/runtime/digital_understudy.py --run-once
python3 ~/.rappter-chrome/runtime/digital_understudy.py --status
```

Private state:

```text
~/.rappter-chrome/understudy/
  state.json
  snapshots/YYYY-MM-DD.json
  analyses/YYYY-MM-DD.json
  frames/00000000000000000000.json
  final-report.json
  final-report.txt
```

The final report labels each learned pattern as explicit or inferred, cites
evidence, lists predictions, presents proposals under
`PROPOSALS — NONE EXECUTED`, and records limitations.
