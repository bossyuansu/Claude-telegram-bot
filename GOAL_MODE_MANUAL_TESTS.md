# Goal Mode Manual Test Scenarios

These scenarios validate the live `/goal` experience after the automated unit and
integration tests pass. Run them from a bot session pointed at a disposable test
repository so the autonomous loop can freely edit files.

## Prerequisites

- Bot is running with `TELEGRAM_TOKEN`, `ALLOWED_CHAT_IDS`, and `API_SECRET`.
- Android app or Telegram chat can reach the bot.
- The active bot session points at a disposable project directory.
- `python3 -m pytest -q`, `python3 -m py_compile bot.py api.py loader.py`, and
  `cd android && ./gradlew testDebugUnitTest` pass before manual testing.

## Scenario 1: Simple Goal

Goal:

```text
/goal Add a health check endpoint at /health and include a passing test.
```

Expected result:

- Bot creates a goal plan with milestones and waits for approval.
- After approval, the goal completes in a small number of iterations.
- `/goal status` shows completed progress.
- `/goal journal` records at least one useful learning or completion note.
- The project has a working `/health` implementation and test coverage.

Pass criteria:

- Acceptance criteria are verified by command output or code inspection.
- Goal status is `completed`.
- No unresolved active process remains for the session.

## Scenario 2: Medium Goal

Goal:

```text
/goal Add input validation to all API endpoints with tests for invalid payloads.
```

Expected result:

- Goal decomposes into multiple milestones.
- The loop assesses current API structure before editing.
- Failed checks cause retry or replan instead of silent completion.
- The Android Mission Control Goals tab shows progress and milestones.

Pass criteria:

- Goal completes or pauses with a clear, actionable escalation.
- Verification commands cover the changed endpoints.
- `/goal plan`, `/goal status`, and the Goals tab agree on progress.

## Scenario 3: Failure Recovery

Setup:

1. In a disposable branch, intentionally introduce a failing test or syntax error.
2. Start a goal that should touch the broken area.

Goal:

```text
/goal Fix the broken validation flow and make the test suite pass.
```

Expected result:

- The first assessment identifies the failure.
- The loop records the failed verification evidence.
- After repeated failure, auto-replan or escalation is triggered.
- User can choose Resume, Replan, or Cancel from the inline controls.

Pass criteria:

- Failure is not marked as success.
- Replan preserves completed milestones and updates remaining work.
- `/goal journal` includes what was learned from the failed attempt.

## Scenario 4: Cross-Session Resume

Setup:

1. Start a goal with several milestones.
2. Pause it with `/goal pause` while it is active.
3. Restart the bot process.

Expected result:

- Startup reports the interrupted/paused goal.
- `/goal resume` restarts the loop from the first incomplete milestone.
- Any scheduled check-in is cancelled when the goal resumes.
- `/goal cancel` kills any active subprocess and marks the goal abandoned.

Pass criteria:

- Goal state survives restart in `data/goals/`.
- Resume does not create a duplicate active goal for the same session.
- Cancel clears active task state and emits a goal cancellation update.

## Evidence To Capture

For each scenario, capture:

- Goal id and session name.
- Before/after `/goal status`.
- Relevant `/goal plan` milestone states.
- Verification command output.
- Android Goals tab screenshot if testing mobile UI.
- Any final `/goal journal` entries.
