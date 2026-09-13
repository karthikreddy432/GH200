---
chapter: 1
title: Author and Manage Workflows (20-25%)
weight: 1
last_verified: 2026-09-13
---

# Chapter 1: Author and Manage Workflows (20–25% of exam)

*Last verified against upstream GitHub/Microsoft sources: September 13, 2026. Facts tied to a date (deprecations, defaults, retirements) can shift after this point — see the note in the README about re-checking time-sensitive claims.*

The single biggest domain on GH-200. This chapter covers: events/triggers, `workflow_dispatch` inputs, contexts and expressions, job dependencies (`needs`), matrix builds, service containers, and permissions/scoping at the workflow level.

## 🎯 Learning Outcomes

- [ ] Trigger workflows on push, PR, schedule, manual dispatch, and repository_dispatch
- [ ] Define typed, validated `workflow_dispatch` inputs
- [ ] Use GitHub's built-in contexts (`github`, `env`, `secrets`, `needs`, `matrix`) and expression syntax
- [ ] Control job ordering and conditional execution with `needs` and `if`
- [ ] Build and tune matrix strategies (include/exclude/fail-fast/max-parallel)
- [ ] Attach service containers (databases, queues) to a job
- [ ] Scope `permissions:` correctly at workflow and job level

**Estimated time:** 4-5 hours

**On this page**
- [1.1 Events That Trigger Workflows](#11-events-that-trigger-workflows)
- [1.2 workflow_dispatch Inputs — The Full Picture](#12-workflow_dispatch-inputs--the-full-picture)
- [1.3 Contexts and Expressions](#13-contexts-and-expressions)
- [1.4 Job Dependencies, Conditionals, and Execution Control](#14-job-dependencies-conditionals-and-execution-control)
- [1.5 Matrix Builds](#15-matrix-builds)
- [1.6 Service Containers](#16-service-containers)
- [1.7 Permissions Scoping (Workflow & Job Level)](#17-permissions-scoping-workflow--job-level)

---

## 1.1 Events That Trigger Workflows

```yaml
on:
  push:
    branches: [main, 'release/**']
    paths: ['src/**', '!**.md']       # ignore doc-only changes

  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened]   # default types if omitted

  schedule:
    - cron: '0 3 * * *'               # 3 AM UTC daily — always UTC

  workflow_dispatch:                  # manual "Run workflow" button
    inputs:
      environment:
        type: choice
        options: [staging, production]
        required: true

  repository_dispatch:                # triggered by external system via API
    types: [deploy-requested]
```

**Key facts:**
- `schedule:` cron is **always UTC**, and GitHub explicitly says scheduled runs can be **delayed during high load** — the exam likes to test that a `schedule` trigger is not a real-time guarantee.
- `pull_request` defaults to types `opened`, `synchronize`, `reopened` if you don't specify `types:` — a common gotcha is expecting it to fire on `labeled` without adding that type explicitly.
- `paths:` filters use glob patterns; a leading `!` negates (excludes) a pattern.
- `repository_dispatch` is how **external systems** (not GitHub events) can trigger a workflow via a `POST` to the GitHub API — useful for triggering CI from an outside tool.

> **🌍 Real-world example.** A team's docs-only PRs were triggering the full 25-minute test suite every time, burning CI minutes for zero benefit. The fix: add `paths-ignore: ['**.md', 'docs/**']` to the `pull_request` trigger, so changes touching only Markdown files never fire the expensive test job at all. This exact "should this event fire the workflow" filtering logic is heavily tested — the exam wants you to know `paths` vs `paths-ignore`, `branches` vs `branches-ignore`, and that these filters apply *before* any job even considers running.

---

## 1.2 workflow_dispatch Inputs — The Full Picture

Five input types exist: `string`, `number`, `boolean`, `choice`, `environment`. Max **10 inputs** per `workflow_dispatch` trigger.

```yaml
on:
  workflow_dispatch:
    inputs:
      environment:
        description: 'Target environment'
        type: environment      # renders a dropdown of your configured Environments
        required: true
      version:
        description: 'Version tag to deploy'
        type: string
        required: false
        default: 'latest'
      replica_count:
        description: 'Number of replicas'
        type: number
        default: 3
      dry_run:
        description: 'Simulate without applying changes'
        type: boolean
        default: false
      log_level:
        description: 'Verbosity'
        type: choice
        options: [debug, info, warn, error]
        default: info

jobs:
  deploy:
    runs-on: ubuntu-latest
    if: inputs.dry_run == false      # boolean inputs behave as real booleans in `if:`
    steps:
      - run: echo "Deploying ${{ inputs.version }} to ${{ inputs.environment }}"
      - run: |
          # In shell, booleans arrive as the STRING "true"/"false" — not real booleans
          if [ "${{ inputs.dry_run }}" == "true" ]; then
            echo "This is a dry run"
          fi
```

🔴 **Exam trap, memorize this:** in the workflow's `if:` expression syntax, `inputs.dry_run` behaves as a genuine boolean. But once you drop into a `run:` shell block and reference `${{ inputs.dry_run }}`, it's interpolated as the literal text `"true"` or `"false"` — a string. Comparing it with `== "true"` in bash is correct; treating it as a shell truthy value directly is a bug.

> **📚 Theory.** The `type: environment` input is special — it doesn't just validate against a string list, it renders a dropdown of your repo's actual configured **Environments** (Settings → Environments), and critically, **environment protection rules are enforced** when that selected value is used as the job's `environment:`. This is the mechanism connecting a manual dispatch input to approval gates — covered in full in Chapter 4 §4.6 (Environments — Protection Rules, Required Reviewers, and Deployment Branch Policies).

---

## 📝 TASK 1.1 — Fix the Broken Dispatch Input

**Task:** This workflow is meant to let someone manually trigger a deploy to either `staging` or `production`, but pressing "Run workflow" always deploys — even during testing when they want to preview only. Find and fix the bug.

```yaml
on:
  workflow_dispatch:
    inputs:
      target:
        type: choice
        options: [staging, production]
      preview_only:
        type: boolean
        default: false

jobs:
  deploy:
    runs-on: ubuntu-latest
    if: inputs.preview_only == "false"
    steps:
      - run: echo "Deploying to ${{ inputs.target }}"
```

<details>
<summary>🔎 Click to reveal solution</summary>

**Bug:** `if: inputs.preview_only == "false"` compares a **boolean** against the **string** `"false"`. In the `if:` expression context (not inside `run:`), `inputs.preview_only` is a real boolean, so `true == "false"` is always evaluating a type mismatch — in GitHub Actions expression semantics this doesn't error, but it doesn't do what the author intended, so the job condition is unreliable and effectively always proceeds since the strict-equality-across-types check fails in a way that isn't `true`... which means the `if:` never blocks it.

**Fix:** Compare against a real boolean, or better — invert with `!`:
```yaml
if: inputs.preview_only == false
# or, more idiomatic:
if: "!inputs.preview_only"
```

The broader lesson: inside `if:` and other `${{ }}` expression contexts, don't quote booleans as if they were strings — that's a `run:`-shell habit leaking into YAML expression syntax, and it's one of the most common review-turned-bug patterns.

</details>

---

## 1.3 Contexts and Expressions

Contexts are objects Actions exposes to your workflow, accessed via `${{ context.property }}`.

| Context | What it holds | Example |
|---|---|---|
| `github` | Event payload, repo, actor, sha, ref | `github.actor`, `github.event_name`, `github.sha` |
| `env` | Env vars defined at workflow/job/step level | `env.NODE_ENV` |
| `vars` | Repository/org/environment **variables** (non-secret) | `vars.DEPLOY_REGION` |
| `secrets` | Encrypted secrets | `secrets.API_KEY` |
| `needs` | Outputs from jobs listed in `needs:` | `needs.build.outputs.artifact_id` |
| `matrix` | Current matrix combination | `matrix.os`, `matrix.node-version` |
| `steps` | Outputs from previous steps in the same job | `steps.build.outputs.version` |
| `inputs` | workflow_dispatch or workflow_call inputs | `inputs.environment` |
| `job` | Info about the currently running job | `job.status` |
| `runner` | Info about the runner itself | `runner.os`, `runner.temp` |

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      version: ${{ steps.get_version.outputs.version }}
    steps:
      - id: get_version
        run: echo "version=1.2.3" >> "$GITHUB_OUTPUT"

  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: echo "Deploying version ${{ needs.build.outputs.version }}"
```

**Setting a step output (the modern way):** write to the `$GITHUB_OUTPUT` environment file — the old `::set-output` command is deprecated.

```bash
echo "version=1.2.3" >> "$GITHUB_OUTPUT"
```

**`vars` vs `secrets`:** both are configured in repo/org/environment settings, but `vars` values are **plaintext** (visible in the UI, fine for non-sensitive config like a region name), while `secrets` are **encrypted at rest** and **masked in logs**. A common exam scenario: "you need a non-sensitive per-environment config value — should it be a secret?" No — that's what `vars` is for; reserving `secrets` for genuinely sensitive data keeps blast radius smaller and avoids unnecessary masking overhead.

> **🌍 Real-world example.** A workflow accidentally printed an API token to the logs because someone stored it in a `vars.API_TOKEN` instead of `secrets.API_TOKEN`. Because `vars` are not masked, the token appeared in plaintext in the public Actions log for a public repo — a real credential leak. The fix was both technical (move it to `secrets`) and procedural (add a review step for anyone adding new repo variables, to confirm nothing sensitive lands in `vars` by mistake).

---

## 📝 TASK 1.2 — Debug the Missing Output

**Task:** The `notify` job should print the version built by `build`, but it prints `unknown`. Find the bug.

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - id: version_step
        run: echo "::set-output name=version::2.0.0"

  notify:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: echo "Version is ${{ needs.build.outputs.version }}"
```

<details>
<summary>🔎 Click to reveal solution</summary>

**Two bugs:**

1. `build` never declares an `outputs:` map at the job level. Step outputs (`steps.<id>.outputs.x`) are only visible **within that same job** unless the job explicitly re-exposes them via `jobs.<job_id>.outputs`.

2. `::set-output name=version::2.0.0` is the **deprecated** workflow command syntax. It was disabled for security reasons; the current mechanism is writing to the `$GITHUB_OUTPUT` file.

**Fix:**
```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      version: ${{ steps.version_step.outputs.version }}   # (1) expose it at job level
    steps:
      - id: version_step
        run: echo "version=2.0.0" >> "$GITHUB_OUTPUT"       # (2) modern output syntax

  notify:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: echo "Version is ${{ needs.build.outputs.version }}"
```

This two-layer bug (step→job output plumbing, plus a deprecated command) is a realistic composite of mistakes you'll see in scenario-style GH-200 questions.

</details>

---

## 1.4 Job Dependencies, Conditionals, and Execution Control

```yaml
jobs:
  lint:
    runs-on: ubuntu-latest
    steps: [{run: echo linting}]

  test:
    runs-on: ubuntu-latest
    steps: [{run: echo testing}]

  build:
    needs: [lint, test]              # waits for BOTH to succeed
    runs-on: ubuntu-latest
    steps: [{run: echo building}]

  deploy:
    needs: build
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    steps: [{run: echo deploying}]

  notify-failure:
    needs: [lint, test, build]
    runs-on: ubuntu-latest
    if: failure()                    # runs ONLY if something upstream failed
    steps: [{run: echo "pipeline failed, notifying"}]
```

**Critical default behavior:** if any job in a `needs:` chain **fails**, downstream jobs are **skipped by default** — they do not run at all. This is why `notify-failure` needs an explicit `if: failure()` to run specifically in that scenario; without it, that job would simply never execute when it's needed most.

**The status-check functions:**

| Function | Meaning |
|---|---|
| `success()` | Default implicit condition — all `needs` jobs succeeded |
| `failure()` | At least one upstream job in `needs` failed |
| `cancelled()` | The workflow was cancelled |
| `always()` | Runs regardless of upstream status — commonly paired with cleanup/notification steps |

🔴 **Exam tip:** "How do I make a cleanup job run even if the build fails?" → `if: always()`. This is one of the most frequently tested behaviors in the entire exam.

> **🌍 Real-world example.** A team's Slack notification step never fired on failed builds — only on successes — because they forgot that `if:` conditions on a step default to `success()` when omitted. Adding `if: always()` to the notification step (not the whole job) fixed it: the step now runs regardless of the job's earlier step outcomes, and inside it they used `job.status` to decide whether to post a red or green message.

---

## 📝 TASK 1.3 — Design the Dependency Graph

**Task:** You have four jobs: `unit-tests`, `integration-tests`, `build-image`, `push-image`. Requirements:
- `unit-tests` and `integration-tests` should run in parallel (no dependency on each other)
- `build-image` should only start after **both** test jobs succeed
- `push-image` should only run after `build-image` succeeds, **and only on pushes to `main`** (not on PRs)

Write the `needs:` and `if:` for each job (you can omit `steps:` content, just show the control-flow keys).

<details>
<summary>🔎 Click to reveal solution</summary>

```yaml
jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps: [...]

  integration-tests:
    runs-on: ubuntu-latest
    steps: [...]
    # No `needs:` on either — they have no dependency on each other,
    # so both start immediately and run in parallel.

  build-image:
    needs: [unit-tests, integration-tests]   # waits for BOTH
    runs-on: ubuntu-latest
    steps: [...]

  push-image:
    needs: build-image
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    steps: [...]
```

Key reasoning: omitting `needs:` entirely (not setting it to an empty list) is what allows `unit-tests` and `integration-tests` to run in parallel. `build-image` needing **both** as a list (not two separate `needs:` jobs) enforces the "both must succeed" gate. `push-image`'s `if:` combines a ref check and an event check — PRs against main would have `github.event_name == 'pull_request'`, so the event check alone would already exclude them, but combining both makes the intent explicit and defends against odd edge cases (like someone re-running a PR workflow after merge).

</details>

**The dependency graph, visualized** — this is the shape the `needs:` keys above actually produce at runtime:

```mermaid
flowchart LR
    lint([lint]) --> build[[build]]
    test([test]) --> build
    build --> deploy([deploy<br/>if: main push])
    lint -.-> notify[["notify-failure<br/>if: failure()"]]
    test -.-> notify
    build -.-> notify

    classDef normal fill:#e8f0fe,stroke:#3b6ecc,color:#1a1a1a
    classDef gate fill:#fff4e5,stroke:#cc8a3b,color:#1a1a1a
    classDef watcher fill:#fbe9e9,stroke:#cc4b4b,color:#1a1a1a
    class lint,test,deploy normal
    class build gate
    class notify watcher
```

Solid arrows are the normal `success()`-gated path: `lint` and `test` run in parallel with no dependency on each other, `build` waits on **both**, and `deploy` only fires on top of a successful `build` (plus its own `if:` ref/event check). The dashed arrows into `notify-failure` represent its `needs: [lint, test, build]` combined with `if: failure()` — it's watching the same three jobs but is wired to trigger on the failure path that the solid arrows skip by default.

---

## 1.5 Matrix Builds

> **🔗 Try this in the Interactive Companion.** The exclude-then-include ordering below is much easier to internalize by manipulating it than by reading it — open `08_interactive_companion.html#matrix` (Matrix Math tab) and change the dimensions/exclude/include yourself to watch the stage-by-stage expansion recalculate live.

```yaml
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false        # default is TRUE — one failure cancels the rest
      max-parallel: 4         # cap concurrent matrix jobs (default: unlimited)
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
        node-version: [18, 20, 22]
        exclude:
          - os: macos-latest
            node-version: 18    # don't test old Node on macOS
        include:
          - os: ubuntu-latest
            node-version: 22
            experimental: true  # add an extra property to one specific combination
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node-version }}
      - run: npm test
```

**Key facts:**
- Base combination count = Cartesian product of all matrix dimensions (here: 3 × 3 = 9), **minus** `exclude` matches, **plus** `include` additions.
- **Order of application: exclude runs first, then include** — so you can exclude a whole category then selectively re-add one edge case.
- **`fail-fast` defaults to `true`** — one matrix job failing cancels all other in-progress/queued matrix jobs. Set `false` explicitly if you want to see every combination's result (common during debugging).
- **Hard cap: 256 jobs per matrix.** If you need more, the common workaround is splitting across multiple reusable workflow calls, or restructuring your matrix dimensions.
- `max-parallel: 1` effectively serializes the matrix — useful when matrix jobs would otherwise contend for a shared external resource (e.g., a shared staging deployment slot).

> **🌍 Real-world example.** A library needed to test across 3 OSes × 4 Python versions × 2 database backends = 24 combinations, but two of those combinations (Windows + one legacy DB driver) were structurally unsupported and always failed for reasons unrelated to code quality. Rather than let those 2 combinations show up red and mislead the team, they added `exclude` entries for exactly those pairs — keeping the matrix honest (22 meaningful jobs) instead of noisy (24 jobs, 2 permanently and irrelevantly red).

**How the three stages combine**, using the `os` × `node-version` example from §1.5 above (3 OSes × 3 node versions = 9 base combinations):

```mermaid
flowchart TD
    subgraph S1["1 . Cartesian product"]
        direction LR
        A["3 os × 3 node-version<br/>= 9 combinations"]
    end
    subgraph S2["2 . exclude (applied first)"]
        direction LR
        B["remove (macos-latest, 18)<br/>= 8 combinations"]
    end
    subgraph S3["3 . include (applied second)"]
        direction LR
        C["add (ubuntu-latest, 22, experimental:true)<br/>= 9 combinations"]
    end
    S1 --> S2 --> S3

    classDef step fill:#e8f0fe,stroke:#3b6ecc,color:#1a1a1a
    class A,B,C step
```

The order is fixed and exam-relevant: **exclude always runs before include**. That's what lets you exclude an entire category and then selectively re-add one specific edge case — if the order were reversed, a later `exclude` could accidentally remove the combination an `include` had just added.

---

## 📝 TASK 1.4 — Matrix Math

**Task:** Given this matrix, how many jobs actually run?

```yaml
strategy:
  matrix:
    os: [ubuntu-latest, windows-latest]
    python: ['3.10', '3.11', '3.12']
    exclude:
      - os: windows-latest
        python: '3.10'
    include:
      - os: macos-latest
        python: '3.12'
```

<details>
<summary>🔎 Click to reveal solution</summary>

**Answer: 6 jobs.**

Walkthrough:
1. Base Cartesian product: 2 OSes × 3 Python versions = **6 combinations**:
   `(ubuntu,3.10) (ubuntu,3.11) (ubuntu,3.12) (windows,3.10) (windows,3.11) (windows,3.12)`
2. Apply `exclude`: remove `(windows, 3.10)` → **5 combinations** remain.
3. Apply `include`: add `(macos-latest, 3.12)` as a new combination → **6 combinations** total.

Final set: `(ubuntu,3.10) (ubuntu,3.11) (ubuntu,3.12) (windows,3.11) (windows,3.12) (macos,3.12)` = **6 jobs**.

The trap here is forgetting that `include` **adds a new combination outright** if it doesn't match any existing dimension pairing — it's not merely "modifying" an existing cell, it can genuinely introduce entries outside the original Cartesian grid (here, `macos-latest` was never in the base matrix at all).

</details>

---

## 1.6 Service Containers

Service containers give a job access to a dependent service (database, cache, queue) for the duration of that job — commonly used for integration tests.

```yaml
jobs:
  integration-test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: testpass
          POSTGRES_DB: testdb
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

      redis:
        image: redis:7
        ports:
          - 6379:6379

    steps:
      - uses: actions/checkout@v4
      - run: npm test
        env:
          DATABASE_URL: postgresql://postgres:testpass@localhost:5432/testdb
          REDIS_URL: redis://localhost:6379
```

**Key facts:**
- Service containers run **alongside** your job's steps, on the **same runner network** (`localhost` reaches them directly when using GitHub-hosted runners).
- The `options:` health-check block is important — without it, your test step might start before Postgres is actually ready to accept connections, causing flaky "connection refused" failures.
- Services are **automatically torn down** when the job ends — no manual cleanup step needed.
- Service containers are only available when the job itself runs in a container OR directly on a Linux runner — **not supported on Windows or macOS runners** in the same way (this is a real, frequently-tested limitation).

> **📚 Theory.** Why "not on Windows/macOS runners"? Service containers rely on Docker networking that GitHub-hosted Linux runners provide natively. Windows and macOS hosted runners don't run your job steps inside a container by default, so the "sibling container on the same Docker network" model doesn't apply the same way. If you need integration-test-style dependencies on Windows, the common workaround is installing/running the dependency directly as a Windows service or process within the job, not via `services:`.

---

## 📝 TASK 1.5 — Fix the Flaky Integration Test

**Task:** This job's tests intermittently fail with "connection refused" against Postgres, especially on a freshly-created workflow run. What's missing?

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: test
        ports:
          - 5432:5432
    steps:
      - uses: actions/checkout@v4
      - run: npm test
```

<details>
<summary>🔎 Click to reveal solution</summary>

**Bug:** No health check is configured on the `postgres` service. The container starts, but Postgres itself takes a moment to initialize and start accepting connections — the runner considers the *container* started long before Postgres is actually *ready*. Without a health check, GitHub Actions has no way to know to wait, so `npm test` can start running (and attempting connections) before the database is truly up.

**Fix:** Add health-check options:
```yaml
services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_PASSWORD: test
    ports:
      - 5432:5432
    options: >-
      --health-cmd pg_isready
      --health-interval 10s
      --health-timeout 5s
      --health-retries 5
```

With this, GitHub Actions won't proceed to your job's steps until `pg_isready` reports healthy, eliminating the race condition. This exact "intermittent CI failure caused by missing health check" scenario is a classic troubleshooting-domain question (see Chapter 2) that's rooted in an authoring-domain mistake (this chapter) — the exam frequently blends the two.

</details>

---

## 1.7 Permissions Scoping (Workflow & Job Level)

```yaml
permissions:
  contents: read          # default-safe: only read repo contents
  # by omission, everything else defaults to 'none'

jobs:
  comment-on-pr:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write   # override JUST for this job — least privilege
      contents: read
    steps:
      - run: echo "commenting..."
```

**Key facts:**
- The default `GITHUB_TOKEN` permissions are controlled at the **repository or organization level** (Settings → Actions → General), and can additionally be scoped in the workflow YAML itself.
- Setting `permissions:` at the **workflow level** applies to all jobs unless a job overrides it with its own `permissions:` block.
- **Least privilege is the exam's expected default answer**: if a job only reads code and runs tests, it should not have `contents: write` or `issues: write` — even if the repo's default token has broader permissions, you should narrow it in the workflow.

🔴 **Exam tip:** A common scenario question: "A workflow needs to comment on a PR — what's the minimum permission required?" → `pull-requests: write` (not `contents: write`, not blanket `write-all`). Knowing the individual permission scopes (`contents`, `issues`, `pull-requests`, `packages`, `id-token`, `actions`, etc.) and mapping them to the narrowest task is a recurring pattern across both this domain and the Security domain (Chapter 5).

---

## ⚠️ Common Mistakes in This Chapter

❌ Forgetting `pull_request` defaults to only 3 event types — expecting `labeled` to fire without adding it explicitly.
✅ Always list `types:` explicitly if you need anything beyond opened/synchronize/reopened.

❌ Comparing boolean inputs to string literals in `if:` expressions (`== "false"`).
✅ Compare booleans to booleans in expression context; only stringify when inside `run:` shell.

❌ Using deprecated `::set-output` syntax.
✅ Write to `$GITHUB_OUTPUT` instead.

❌ Forgetting that a failed upstream job **skips** downstream jobs by default.
✅ Use `if: always()` or `if: failure()` explicitly for cleanup/notification jobs.

❌ Missing health checks on service containers, causing flaky "connection refused" errors.
✅ Always add `options: --health-cmd ...` for databases/queues used as service containers.

---

## ✅ Chapter 1 Summary (TL;DR)

- Events (`push`, `pull_request`, `schedule`, `workflow_dispatch`, `repository_dispatch`) control **when** workflows run; `paths`/`branches` filters narrow **which changes** trigger them.
- `workflow_dispatch` supports 5 typed inputs (string/number/boolean/choice/environment), max 10 — remember booleans are real booleans in `if:` but strings in `run:`.
- Contexts (`github`, `needs`, `matrix`, `steps`, `vars`, `secrets`) are how data flows through a workflow; `$GITHUB_OUTPUT` is the modern way to produce step outputs.
- `needs:` controls job ordering; failed upstream jobs skip downstream ones by default — use `always()`/`failure()` to override.
- Matrix builds = Cartesian product, minus `exclude`, plus `include` (in that order); 256-job hard cap; `fail-fast` defaults true.
- Service containers give jobs sidecar dependencies (DBs, caches) — always add health checks; not supported the same way on Windows/macOS runners.
- Scope `permissions:` to least privilege at workflow or job level.

**Next:** Chapter 2 — Consume and Troubleshoot Workflows: reading other people's workflows, debugging failed runs, caching, artifacts, and reusable workflow consumption.
