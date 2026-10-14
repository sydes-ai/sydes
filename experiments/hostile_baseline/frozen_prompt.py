"""The one frozen prompt for the hostile-case benchmark round.

Same generic base prompt as the prior hard-v1 round (experiments/
frontier_baseline), with one addition per this round's explicit
instruction: a structured "coverage protocol" self-report, so a run can be
scored not just on correctness but on whether the model searched broadly
enough. Deliberately still blind to any language/framework name, any
historical Sydes gap, and to which of the 3 cases (if any) has a
genuinely-unknown correct answer -- the prompt must not tip that off.
"""

from __future__ import annotations

FROZEN_PROMPT = (
    "Analyze this diff in the repository. Identify the system entrypoints "
    "and behaviors affected by the change. Trace evidence-backed paths from "
    "those entrypoints to the changed behavior. Identify relevant tests. "
    "Explicitly state anything you could not establish. Cite every "
    "nontrivial hop with source evidence.\n\n"
    "You have list_dir/read_file/grep tools to explore the repository "
    "yourself. Use them as needed before answering.\n\n"
    "In addition to your analysis, end your answer with a section titled "
    "'Coverage protocol' that reports, for THIS investigation, what you "
    "actually checked -- not what exists in the abstract, but what you "
    "personally looked at before answering. One line each:\n"
    "- Entrypoints checked: which entrypoint surfaces (HTTP routes, CLI, "
    "RPC, GraphQL, message handlers, etc.) you looked at, and which "
    "plausible ones you did not check\n"
    "- Async/queue registrations checked: which background job/queue/cron/"
    "event-listener registrations you looked at, and which you did not\n"
    "- Persistence paths checked: which database/storage read or write "
    "paths you looked at, and which you did not\n"
    "- Auth/cross-cutting behavior checked: which authorization/"
    "authentication/middleware/interceptor paths you looked at, and which "
    "you did not\n"
    "- Runtime/framework wiring checked: which framework-level "
    "registration or dispatch mechanisms (decorators, DI containers, "
    "plugin registries, event buses) you looked at, and which you did not\n"
    "- Tests checked: which test files or suites you looked at, and which "
    "you did not\n"
    "- Unresolved areas: anything you did not have time or tool budget to "
    "check, stated plainly\n\n"
    "If the correct answer to 'what does this change affect' is genuinely "
    "unknown or indeterminate from the repository alone -- for example "
    "because it depends on runtime configuration, deployment topology, or "
    "user-supplied data not present in this codebase -- say so explicitly "
    "rather than guessing a specific entrypoint.\n\n"
    "## The diff\n\n```diff\n{diff}\n```\n"
)
