# GH-200 Exam Prep

A self-contained study guide for the GitHub Actions certification exam (GH-200), built as six chapters, a quick-reference cheat sheet, and an interactive practice exam. Each core chapter mixes theory, real-world examples, and worked tasks; Chapter 7 condenses all of it into a code-free review sheet; Chapter 6 is a pure MCQ simulator for drilling exam-style recall.

## 🔗 Live site

This guide is hosted on GitHub Pages: **https://karthikreddy432.github.io/GH200/**

Jump straight to the two interactive tools (these need a real browser session — they won't do anything useful viewed as raw source on github.com):
- **[MCQ Practice Exam Simulator](https://karthikreddy432.github.io/GH200/06_chapter_mcq_simulator.html)** — 94 questions, domain-weighted, with "Review missed questions" for repeat runs.
- **[Interactive Companion](https://karthikreddy432.github.io/GH200/08_interactive_companion.html)** — five hands-on tools (permission scoping, matrix math, action-type picker, secrets tracer, PR trigger simulator).

Or start from the beginning: **[Chapter 0 — Foundations](https://karthikreddy432.github.io/GH200/00_chapter_foundations.html)**.

> **Sourcing note.** The GH-200 blueprint changed during 2026. This guide follows the current **5-domain structure** published on Microsoft Learn (last confirmed update: August 2026). If you find older prep material describing a retired 4-domain version or features like "required workflows" (retired — see Chapter 4), treat it as outdated. Reconfirm against `learn.microsoft.com/credentials/certifications/github-actions` before your exam date.

## Structure

| Chapter | Title | Exam weight | Est. time |
|---|---|---|---|
| 0 | [Foundations](00_chapter_foundations.md) | — (prerequisite) | 2–3 hrs |
| 1 | [Author and Manage Workflows](01_chapter_author_manage_workflows.md) | 20–25% | 4–5 hrs |
| 2 | [Consume and Troubleshoot Workflows](02_chapter_consume_troubleshoot_workflows.md) | 15–20% | 3–4 hrs |
| 3 | [Author and Maintain Actions](03_chapter_author_maintain_actions.md) | 15–20% | 3–4 hrs |
| 4 | [Manage GitHub Actions for the Enterprise](04_chapter_manage_enterprise.md) | 20–25% | 3–4 hrs |
| 5 | [Secure and Optimize Automation](05_chapter_secure_optimize.md) | 10–15% | 3–4 hrs |
| 6 | [MCQ Practice Exam Simulator](06_chapter_mcq_simulator.html) | all domains | 1–2 hrs |
| 7 | [Quick Reference — Exam Tips & Common Mistakes](07_chapter_quick_reference.md) | all domains | 20–30 min |
| — | [Interactive Companion](08_interactive_companion.html) | supplementary | 30–45 min |

**Total estimated study time:** ~20–25 hours for a full first pass, plus practice-exam time. Chapter 7 is a fast final review, not a substitute for 0–5.

## How to use this guide

1. **Read chapters 0–5 in order.** Each ends with a "Common Mistakes" recap and a TL;DR summary. Tasks are embedded inline (look for 📝) with collapsible solutions — attempt each one before revealing the answer.
2. **Run Chapter 6 whenever you want to check retention.** It's a standalone HTML file — open it directly in a browser, no server or build step required (or use the [live version](https://karthikreddy432.github.io/GH200/06_chapter_mcq_simulator.html)). It pulls 94 questions from chapters 0–5, weighted to match the real exam's domain split, with an explanation on every answer (each with a real, clickable link back to the source chapter section) and a per-domain score breakdown at the end. Re-run it as many times as you like; questions and options are shuffled each run — or use **"Review missed questions"** at the results screen to immediately retake just the ones you got wrong, instead of pulling from all 94 again.
3. **Watch for the ⏰ time-sensitive callouts** in Chapters 3 and 4 — a couple of facts in this space (runner Node.js version, the retirement of "required workflows") changed in 2026 and are easy to get wrong from older material.
4. **Treat 🔴 Exam tip callouts as high-yield.** These flag the facts and scenario patterns the chapters identify as most frequently tested.
5. **Use Chapter 7 the night before (or morning of) the exam.** It's every ⏰ time-sensitive fact, 🔴 exam tip, and ⚠️ common mistake from Chapters 0–5, pulled out into one code-free page per domain — for rapid recall, not first-pass learning. If a line in it doesn't ring a bell, that's your cue to jump back into the matching chapter, not to memorize it in isolation.
6. **Open the Interactive Companion for the five concepts that reward hands-on play over reading.** Permission scope-zeroing, matrix expansion, action-type selection, secrets propagation through nested `workflow_call`, and the `pull_request` vs `pull_request_target` trap — all five run live in the browser, client-side only, nothing sent anywhere. See [Diagrams and Interactive Tools](#diagrams-and-interactive-tools) below for what's in each tab. The relevant chapters (§1.5, §2.5, §5.2) now link straight to the matching tab inline, and the companion opens directly to that tab via a URL fragment (e.g. `08_interactive_companion.html#matrix`) — no manual tab-hunting needed.
7. **Check the "Last verified" line at the top of each chapter.** Every chapter carries a verification date; anything tied to a specific date (deprecations, defaults, retirements — the ⏰ callouts) can drift after that point, so a stamp older than a few months is your cue to spot-check against `learn.microsoft.com` or `github.blog/changelog` before trusting it on exam day.

## Diagrams and Interactive Tools

Every diagram in this guide is a Mermaid flowchart rendered inline in its chapter — nothing to click, just scroll to the linked section. The interactive companion is separate: five live, adjustable tools in one HTML file.

**Diagrams, by chapter:**

| Diagram | Where |
|---|---|
| Repository → Workflow → Event → Job → Step → Action hierarchy, with parallel jobs on isolated runners | [Ch. 0, §0.1 The Core Vocabulary](00_chapter_foundations.md#01-the-core-vocabulary) |
| Job dependency / conditional execution graph | [Ch. 1, §1.4 Job Dependencies, Conditionals, and Execution Control](01_chapter_author_manage_workflows.md#14-job-dependencies-conditionals-and-execution-control) |
| Matrix build expansion | [Ch. 1, §1.5 Matrix Builds](01_chapter_author_manage_workflows.md#15-matrix-builds) |
| Systematic debugging flow (red job → red step → root cause) | [Ch. 2, §2.1 A Systematic Debugging Workflow](02_chapter_consume_troubleshoot_workflows.md#21-a-systematic-debugging-workflow) |
| Cache vs artifact decision | [Ch. 2, §2.4 Caching vs Artifacts — Choose Correctly](02_chapter_consume_troubleshoot_workflows.md#24-caching-vs-artifacts--choose-correctly) |
| Composite / JavaScript / Docker action decision tree | [Ch. 3, §3.1 Three Action Types — Choosing Correctly](03_chapter_author_maintain_actions.md#31-three-action-types--choosing-correctly) |
| Enterprise → Org → Repo policy hierarchy (most restrictive wins) | [Ch. 4, §4.1 Allowed Actions Policy](04_chapter_manage_enterprise.md#41-allowed-actions-policy-repo-org-enterprise-levels) |
| Runner group structure for mixed public/private/GPU repos | [Ch. 4, §4.2 Self-Hosted Runner Groups](04_chapter_manage_enterprise.md#42-self-hosted-runner-groups) |
| Starter workflows vs reusable workflows vs rulesets | [Ch. 4, §4.5 Enforcing Required Checks](04_chapter_manage_enterprise.md#45-enforcing-required-checks--repository-rulesets-current-mechanism) |
| `GITHUB_TOKEN` scoping and zeroing | [Ch. 5, §5.1 GITHUB_TOKEN — Default Behavior and Scoping](05_chapter_secure_optimize.md#51-github_token--default-behavior-and-scoping) |
| The safe two-workflow split for `pull_request_target` | [Ch. 5, §5.2 The pull_request vs pull_request_target Trap](05_chapter_secure_optimize.md#52-the-pull_request-vs-pull_request_target-trap) |
| OIDC keyless auth sequence | [Ch. 5, §5.4 OIDC — Keyless Cloud Authentication](05_chapter_secure_optimize.md#54-oidc--keyless-cloud-authentication) |
| **Everything at a glance** — one color-coded map of all five domains | [Ch. 7, 🗺️ Everything at a Glance](07_chapter_quick_reference.md#-everything-at-a-glance) |

**Interactive Companion tabs** ([08_interactive_companion.html](08_interactive_companion.html)):

| Tab | Direct link | What it does |
|---|---|---|
| Permission Scoping | [`#perms`](08_interactive_companion.html#perms) | Set workflow- and job-level `permissions:`, toggle fork/`pull_request_target`, watch the effective `GITHUB_TOKEN` scope compute live — including scope-zeroing and the fork-PR floor. |
| Matrix Math | [`#matrix`](08_interactive_companion.html#matrix) | Enter matrix dimensions plus `exclude`/`include` lists and see the Cartesian product expand stage by stage, with a warning if you cross the 256-job cap. |
| Action Type Picker | [`#action`](08_interactive_companion.html#action) | Answer a few questions about what the action needs to do and get a composite/JavaScript/Docker recommendation, with the reasoning shown. |
| Secrets Tracer | [`#secrets`](08_interactive_companion.html#secrets) | Build a nested `workflow_call` chain (1–4 hops), set `inherit`/explicit/none at each hop, and see exactly what secrets the deepest workflow actually receives. |
| PR Trigger Simulator | [`#prtarget`](08_interactive_companion.html#prtarget) | Toggle `pull_request` vs `pull_request_target`, fork origin, explicit head-checkout, and code execution to see whether the result is the exam's #1 security anti-pattern — or one of the safe patterns. |

A progress strip at the top of the page tracks (in-memory only, resets on reload) which of the five tabs you've actually exercised, not just visited. All three status displays (Permission Scoping, Secrets Tracer, PR Trigger Simulator) pair color with a ✓/✗ symbol, so the tool doesn't rely on color alone to communicate a result.

## Repo layout

```
00_chapter_foundations.md
01_chapter_author_manage_workflows.md
02_chapter_consume_troubleshoot_workflows.md
03_chapter_author_maintain_actions.md
04_chapter_manage_enterprise.md
05_chapter_secure_optimize.md
06_chapter_mcq_simulator.html
07_chapter_quick_reference.md
08_interactive_companion.html
README.md
```

## Viewing the MCQ simulator and interactive companion on GitHub

GitHub renders `.md` files directly but not interactive `.html` files in-repo. To use Chapter 6 or the Interactive Companion:
- **Hosted (recommended):** this repo's GitHub Pages site is already live at **https://karthikreddy432.github.io/GH200/** — see the [Live site](#-live-site) links at the top of this file.
- **Locally:** clone the repo and open `06_chapter_mcq_simulator.html` or `08_interactive_companion.html` in any browser.

Note for anyone forking or re-hosting this guide under a different Pages URL: the MCQ simulator's "Related diagram" links and the README's diagram/companion tables use the `karthikreddy432.github.io/GH200` base path directly (not relative paths), since relative `.md` links inside raw `<a href>` tags in the two standalone `.html` files aren't rewritten by Jekyll the way markdown-syntax links in `.md` files are. If you fork this to a different Pages URL, find-and-replace that base path across `06_chapter_mcq_simulator.html` and `README.md`.
