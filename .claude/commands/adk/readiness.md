---
description: Scan the repository to assess AI Coding readiness, producing a structured maturity-level report with actionable improvement suggestions.

---

## User Input

```text
$ARGUMENTS
```
You **MUST** consider the user input before proceeding (if not empty).

Supported user input patterns:
- **No arguments**: Full 10-dimension scan
- **Specific dimensions**: e.g. `"only context and testing"` → scan only those dimensions
- **Output to file**: e.g. `"output to file"` → write report to `.ttadk/readiness-report.md`
- **Compare mode**: e.g. `"compare with last run"` → diff against previous report in `.ttadk/readiness-history/`
- **Target level**: e.g. `"target L3"` → only report improvements needed to reach L3

## Context

**Read context before Executing**:

1. **Language Setting**: Read `preferred_language` from `.ttadk/config.json` (default: 'en' if missing).
   - **IMPORTANT**: Use the configured language for ALL outputs: 'en' → English, 'zh' → 中文. This applies to: report text, section headings, improvement suggestions, status messages, and error descriptions.

## Operating Constraints

**STRICTLY READ-ONLY**: Do **not** create, modify, or delete any project files. This command only reads the repository and outputs a report. The sole exception is writing the report file when the user explicitly requests `"output to file"`.

## Evidence Model (avoid OSS-only false negatives)

The rubric measures **engineering capability**, not **GitHub layout**. A check is poorly designed if it can only pass via a single vendor path.

1. **Equivalence bundles (OR logic)** — For CI, ownership, MR/PR templates, dependency scanning, etc.: if **any** credible signal in the table below is present, treat the check as **PASS** (score accordingly). Do **not** FAIL solely because `.github/*` is missing.
2. **Repo vs platform** — Some controls (branch protection, required reviewers, org-level SCA) live on the **code platform**, not in the clone. If there is **no** file-based evidence, mark **`SKIP` (not verifiable from repo)** — **never FAIL** on absence of local files. List under report: "需在代码平台侧确认".
3. **Monorepo** — CI/owners/templates may live in a **parent** or **shared** config path (e.g. repo root vs package). Prefer the **nearest applicable** config; if clearly inherited from monorepo root, PASS with note `inherited from monorepo root`.
4. **Scoring** — For any row marked **SKIP**, exclude that row from **both** pass count and total for level pass-rate math (same as other SKIP rules).

### Common equivalence signals (non-exhaustive)

| Capability | Accept if any of these exist (or documented in README / `CONTRIBUTING.md` / AI instruction files) |
|------------|-----------------------------------------------------------------------------------------------------|
| CI / pipeline | `.codebase/pipelines/*` (ByteDance Codebase CI YAML), `.github/workflows/*`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/*`, `azure-pipelines.yml`, `build.sh` + CI doc, `ci.yml` / `*.pipeline.yml`, monorepo `**/ci/*`, Bazel/Gradle enterprise patterns with **documented** pipeline entry |
| Test in CI | Explicit test step in above, or `Makefile`/npm scripts used by documented pipeline |
| CODEOWNERS / ownership | `.github/CODEOWNERS`, `.gitlab/CODEOWNERS`, `CODEOWNERS` (root), `OWNERS` (e.g. Chromium-style), `.codeowners`, internal `**/OWNERS` or ownership doc **linking to platform** |
| MR/PR template | `.github/pull_request_template.md`, `.github/PULL_REQUEST_TEMPLATE/*`, `.gitlab/merge_request_templates/*`, `docs/*merge*request*template*`, template section in `CONTRIBUTING.md` |
| Issue template | `.github/ISSUE_TEMPLATE/*`, `.gitlab/issue_templates/*`, or documented "use internal issue tracker" → **SKIP** issue-template row with note |
| Dependency / supply-chain scan | `dependabot.yml`, Renovate, Snyk, `dependency-check`, `osv-scanner`, `npm audit` / `go mod` audit in CI, **internal SCA** job names in pipeline YAML, documented mandatory org pipeline |

## Execution Steps

### Step 1 — Identify Project Meta-Information

Scan the repository root to detect:

**1a. Tech Stack Detection** — Check for these files (batch into minimal tool calls):

| Signal Files | Tech Stack |
|---|---|
| `package.json` + `tsconfig.json` | Node.js / TypeScript |
| `package.json` + `next.config.*` | Next.js |
| `package.json` + `vite.config.*` | Vite |
| `go.mod` | Go |
| `requirements.txt` or `pyproject.toml` | Python |
| `Cargo.toml` | Rust |
| `pom.xml` or `build.gradle` | Java |

If multiple are present → Monorepo / multi-stack.

**1b. Repo Type** — Determine: single repo, monorepo, library/SDK, CLI tool, web app, backend service, etc.

**1c. Delivery context (heuristic)** — From repo signals only, set one tag for narrative (not a hard gate): `oss_github` (strong `.github/` usage), `gitlab_style` (`.gitlab-ci.yml` / `.gitlab/`), `enterprise_internal` (`.codebase/pipelines/` and/or internal CI docs, OWNERS, monorepo tooling, no OSS CI markers), `unknown`. Use this to phrase recommendations (e.g. avoid "enable Dependabot" when internal SCA is more accurate).

**1d. Load Previous Report** — Check `.ttadk/readiness-history/` for the most recent report. If found, load it for Grounding (delta comparison).

### Step 2 — Determine Skip Items

Based on detected tech stack, mark inapplicable checks as **SKIP**:

| Condition | Auto-SKIP |
|---|---|
| Go project | Formatter config (gofmt is built-in) |
| Rust project | Formatter config (rustfmt is built-in) |
| Go / Rust / Java | Type system check (language has built-in types) |
| Pure CLI tool | E2E tests |
| Library / SDK | One-click startup |
| Issue tracking fully external | Issue template row — if README/CONTRIBUTING states issues are **only** in an external system and no in-repo template applies → **SKIP** with note |
| Branch protection | **Always SKIP** (platform-side only; see Dimension 7 and Evidence Model) |

### Step 3 — Scan All 10 Dimensions

If `$ARGUMENTS` requests **only specific dimensions** (e.g. context and testing), run checks **only** for those dimensions; in Section A/E/F state clearly that the run is **partial** and overall **Level / Score** are **not** comparable to a full scan unless recalculated with full weights (or omit headline Level/Score and show per-dimension only).

For each **selected** dimension, execute the checks listed below. Use a **progressive scanning** strategy:
1. **Batch file-existence checks first** (low token cost) — list directories and check for specific files
2. **Then selectively read files** for quality/gradient checks (medium token cost)
3. **Early termination**: if a dimension is clearly 0/10 or 10/10 after existence checks, skip deep inspection

---

#### Dimension 1: Context Engineering (Weight 15%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| AI instruction files exist | PASS/FAIL | L2 | 3 | Check for: `CLAUDE.md`, `.cursorrules`, `.cursor/rules/`, `AGENTS.md`, `.windsurfrules`, `.github/copilot-instructions.md`, or **equivalent** AI context in `README.md` / `docs/` / `.ttadk/memory/constitution.md` (count as 1 file if substantial). Score: 1 file = 1pt, 2 = 2pt, 3+ = 3pt |
| AI instruction file quality | Gradient | L3 | 2 | If file(s) found: >50 lines = 1pt; contains architecture/conventions/prohibitions sections = 2pt |
| TTADK configuration | PASS/FAIL | L2 | 2 | `.ttadk/` exists = 1pt; has `config.json` + `memory/constitution.md` = 2pt |
| MCP configuration | PASS/FAIL | L3 | 2 | `.mcp.json` exists = 1pt; has project-specific MCP servers configured = 2pt |
| AI Skills configuration | PASS/FAIL | L4 | 1 | `.claude/skills/` or equivalent AI skill/knowledge config exists |

#### Dimension 2: Documentation (Weight 12%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| README exists | PASS/FAIL | L1 | 1 | `README.md` exists |
| README quality | Gradient | L2 | 2 | >100 lines = 1pt; has install/usage/architecture sections = 2pt |
| Architecture doc | PASS/FAIL | L3 | 2 | `ARCHITECTURE.md` or `docs/architecture.*` exists = 1pt; has component diagrams/data flow = 2pt |
| API documentation | PASS/FAIL | L3 | 2 | OpenAPI/Swagger/GraphQL schema exists = 1pt; covers major interfaces = 2pt |
| Contributing guide | PASS/FAIL | L2 | 1 | `CONTRIBUTING.md` exists |
| Changelog | PASS/FAIL | L3 | 1 | `CHANGELOG.md` or release notes exist |
| Doc freshness | PASS/FAIL | L4 | 1 | Core docs updated within 180 days (check git log) |

#### Dimension 3: Style & Validation (Weight 10%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| Type system | PASS/FAIL | L1 | 2 | Strong-typed lang (TS/Go/Rust/Java) or type checker (mypy/pyright) = 1pt; strict mode = 2pt. SKIP for Go/Rust/Java (built-in) |
| Lint config | PASS/FAIL | L1 | 2 | ESLint/golangci-lint/ruff config exists = 1pt; rules not all disabled = 2pt |
| Formatter config | PASS/FAIL | L2 | 1 | Prettier/Black/gofmt config exists. SKIP for Go/Rust (built-in) |
| Pre-commit hooks | PASS/FAIL | L3 | 2 | `.pre-commit-config.yaml` or Husky/lint-staged config exists |
| Schema definitions | PASS/FAIL | L3 | 2 | Protobuf/Thrift/GraphQL/JSON Schema exists = 1pt; covers major interfaces = 2pt |
| Naming consistency | Gradient | L2 | 1 | Sample 10–20 files: consistent naming conventions |

#### Dimension 4: Build System (Weight 10%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| Package manager | PASS/FAIL | L1 | 1 | `package.json`/`go.mod`/`pyproject.toml`/`Cargo.toml` exists |
| Lock file | PASS/FAIL | L1 | 1 | Lock file exists and is committed |
| Build commands documented | PASS/FAIL | L2 | 2 | dev/build/test commands exist = 1pt; documented in README or AI instruction file = 2pt |
| One-click startup | PASS/FAIL | L3 | 2 | Single-command dev setup (`make dev`, `docker-compose up`, etc.). SKIP for library/SDK |
| Environment template | PASS/FAIL | L2 | 1 | `.env.example` exists |
| CI/CD config | PASS/FAIL | L2 | 1 | Use **equivalence bundle** from Evidence Model (any recognized pipeline config **or** clearly documented pipeline entry in README/CONTRIBUTING/AI instructions). If only platform-side pipeline exists with **no** doc in repo → **SKIP** with note, do not FAIL |
| Fast CI feedback | Gradient | L4 | 1 | If CI config readable locally: estimate core job duration; if **SKIP** (no local CI) → **SKIP** this gradient, do not penalize |
| Dependency version constraints | PASS/FAIL | L2 | 1 | No `*` or `latest` in dependency versions |

#### Dimension 5: Testing (Weight 12%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| Test framework config | PASS/FAIL | L1 | 1 | Jest/Vitest/pytest/go test config exists |
| Unit tests exist | PASS/FAIL | L2 | 2 | Test files found = 1pt; test file ratio > 30% = 2pt |
| Tests runnable | PASS/FAIL | L2 | 1 | Test command documented and appears functional |
| Integration tests | PASS/FAIL | L3 | 2 | Integration test directory or patterns found |
| E2E tests | PASS/FAIL | L3 | 1 | Playwright/Cypress/Puppeteer config exists. SKIP for pure CLI/library |
| Coverage config | PASS/FAIL | L3 | 1 | Coverage tool configured (istanbul/c8/nyc/coverage.py) |
| CI test integration | PASS/FAIL | L2 | 1 | Test step present in any recognized CI config (equivalence bundle); if CI **SKIP** → **SKIP** this row |
| Flaky test detection | PASS/FAIL | L4 | 1 | Retry mechanism or flaky test detection configured |

#### Dimension 6: Code Organization (Weight 10%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| Semantic directory structure | Gradient | L1 | 3 | Top-level dirs have clear semantics = 1pt; clean hierarchy = 2pt; follows community conventions = 3pt |
| File granularity | Gradient | L2 | 3 | Avg file <300 lines = 1pt; large files (>500 lines) <10% = 2pt; no files >1000 lines = 3pt |
| Clear entry points | PASS/FAIL | L1 | 2 | Clear entry file(s) = 1pt; entry contains exports/route overview = 2pt |
| Separation of concerns | Gradient | L2 | 2 | Business logic, data layer, presentation clearly separated |

#### Dimension 7: Security & Governance (Weight 8%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| .gitignore completeness | PASS/FAIL | L1 | 2 | Exists and covers common patterns (node_modules, .env, build artifacts) |
| Branch protection | SKIP | L3 | 2 | **Always SKIP** for scoring: not verifiable from a clone (lives on code platform). In the narrative report, add one line: 请在代码平台确认默认分支保护与评审规则. **Never FAIL** this row due to missing repo files |
| CODEOWNERS | PASS/FAIL | L3 | 1 | Any path from Evidence Model ownership bundle; FAIL only if **no** ownership file and **no** documented alternative |
| Secrets management | PASS/FAIL | L2 | 2 | No hardcoded secrets = 1pt; `.env.example` or secret management solution = 2pt |
| Dependency security scanning | PASS/FAIL | L3 | 2 | Use **equivalence bundle** (Dependabot/Renovate/Snyk/internal SCA in CI / documented org mandate). If no local evidence → **SKIP**, do not FAIL |
| Sensitive data protection | PASS/FAIL | L3 | 1 | Log scrubbing or `.cursorignore` / AI access controls |

#### Dimension 8: Version Control & Collaboration (Weight 8%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| Commit conventions | Gradient | L2 | 3 | Sample last 20 commits: structured messages >50% = 2pt; >80% = 3pt |
| PR/MR template | PASS/FAIL | L3 | 2 | Any path from Evidence Model MR/PR template bundle; monorepo: accept root or service package if documented |
| Issue template | PASS/FAIL | L3 | 1 | Any path from bundle; if external-only issues per Step 2 → **SKIP** |
| AI collaboration traces | PASS/FAIL | L4 | 2 | Commits contain Co-authored-by AI/TTADK/Claude/Cursor markers |
| Branch strategy | PASS/FAIL | L2 | 1 | Clear branch naming convention (feat/, fix/, release/) in recent branches |
| Automated PR review | PASS/FAIL | L5 | 1 | Bot/review config in repo (e.g. CODEOWNERS + required review docs, `dangerfile`, review bot config); if **only** platform-side → **SKIP**, do not FAIL |

#### Dimension 9: Code Complexity (Weight 8%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| File size distribution | Gradient | L2 | 3 | Files >500 lines <5% = 3pt; <15% = 2pt; <30% = 1pt |
| Single responsibility | Gradient | L3 | 2 | Sample files: single-purpose, name reflects content |
| Nesting depth | Gradient | L3 | 2 | Sample functions: nesting depth ≤ 4 |
| Language uniformity | Gradient | L1 | 2 | 1–2 primary languages = 2pt; 3–4 = 1pt |
| Modularity | PASS/FAIL | L4 | 1 | No circular dependencies, clean module boundaries |

#### Dimension 10: SDD Readiness (Weight 7%, Max 10 pts)

| Check | Type | Level | Points | How to Verify |
|---|---|---|---|---|
| TTADK initialized | PASS/FAIL | L3 | 1 | `.ttadk/` directory with complete config |
| Constitution | PASS/FAIL | L3 | 2 | `constitution.md` exists = 1pt; comprehensive content (coding standards, quality gates) = 2pt |
| Specs directory | PASS/FAIL | L4 | 3 | `specs/` exists = 1pt; structured spec files = 2pt; covers major modules = 3pt |
| Requirements traceability | Gradient | L4 | 2 | Requirements docs/links exist = 1pt; code-to-requirement traceable = 2pt |
| Feature index | PASS/FAIL | L4 | 2 | Module/feature list exists = 1pt; documentation coverage complete = 2pt |

### Step 4 — Calculate Dual-Track Scores

**4a. Maturity Level (L1–L5)**

For each level L1 through L5, collect all check items tagged with that level:
- Let **applicable** = all items at that level that are **not** SKIP
- Pass rate = (count of **PASS** among applicable) / (count of **applicable**)
- Level N is unlocked if **all levels ≤ N** have pass rate ≥ **80%**
- Current Level = highest unlocked level

**4b. Weighted Percentage Score (0–100)**

```
Total Score = Σ (dimension_points / dimension_max_points × dimension_weight × 100)
```

For each dimension, `dimension_points` = sum of points from **non-SKIP** checks; `dimension_max_points` = sum of max points for those **same non-SKIP** checks (so platform-only SKIP rows do not deflate the score).

Where dimension weights are:
- Context Engineering: 15%
- Documentation: 12%
- Style & Validation: 10%
- Build System: 10%
- Testing: 12%
- Code Organization: 10%
- Security & Governance: 8%
- Version Control & Collaboration: 8%
- Code Complexity: 8%
- SDD Readiness: 7%

### Step 5 — Generate Report

Produce the report in the following structure. Use the language from `preferred_language`.

If `$ARGUMENTS` includes **target L*N*** (e.g. `target L3`): prioritize **Section D / G / H** on gaps that block reaching **L*N***; you may shorten lower-priority sections.

---

**Section A: Overview**

```
# AI Development Readiness Report

| Field      | Value                          |
|------------|--------------------------------|
| Project    | <project-name>                 |
| Path       | <repo-path>                    |
| Tech Stack | <detected-stack>               |
| Repo Type  | <repo-type>                    |
| Date       | <today>                        |
| Level      | **L<N> (<level-name>)**        |
| Score      | **<score>/100**                |
| Pass Rate  | **<pass>/<total> criteria (<pct>%)** |
```

**Section B: Maturity Level Progress**

Show a progress bar for each level (L1–L5) with percentage and pass/fail indicator. Mark the current level and next target:

```
L1 Functional     ████████████ 100%  ✅
L2 Documented     █████████░░░  82%  ✅  ← Current
L3 Standardized   ██████░░░░░░  55%     ← Target (need N more)
L4 Optimized      ███░░░░░░░░░  28%
L5 Autonomous     █░░░░░░░░░░░   8%
```

**Section C: Strengths**

List top 3 highest-scoring dimensions with percentage and key findings.

**Section D: Opportunities**

List the specific checks needed to unlock the next level.

**Section E: Score Summary Table**

| # | Dimension | Score | Pass Rate | Key Finding |
|---|-----------|-------|-----------|-------------|
| 1 | Context Engineering | X/10 | X/X | ... |
| ... | ... | ... | ... | ... |

**Section F: All Criteria (Detailed)**

For each dimension, list every check item with:
- ✓ / ✗ / — (pass / fail / skip) prefix
- Check name
- Points earned / max
- Brief explanation

Include **Quick Wins** under dimensions with easy improvements.

Example:
```
### Context Engineering 3/10 (2/5 pass)

✓ ttadk_configuration    1/2  .ttadk/ directory with config.json present
✗ ai_instruction_files   0/3  No CLAUDE.md, .cursorrules, or AGENTS.md found
✓ mcp_configuration      1/2  .mcp.json exists with basic config
✗ ai_file_quality        0/2  No AI instruction files to evaluate
✗ ai_skills              0/1  No AI skill configuration found

**Quick Wins:**
- Create `CLAUDE.md` with project overview → run `/adk:sdd:constitution` (+3 pts)
```

**Section G: Top Recommendations**

Provide a prioritized table:

| # | Priority | Action | Impact | Effort | Level |
|---|----------|--------|--------|--------|-------|
| 1 | CRITICAL | ... | +X pts | time | → LN |

Priority levels:
- **CRITICAL**: Blocks current level unlock, highest ROI (> 10 pts impact)
- **HIGH**: Required for next level (5–10 pts)
- **MEDIUM**: Score improvement (2–5 pts)
- **LOW**: Future-facing, low urgency (< 2 pts)

Link improvement suggestions to ADK commands where applicable (e.g., "Run `/adk:sdd:constitution`").

**Section H: Roadmap to Next Level**

```
Current: **LN** → Target: **L(N+1)** (<level-name>)

### Must-fix (X items to unlock L(N+1)):
1. ✗ check_name — concrete remediation command or action
...

### Estimated effort: ~X hours
### Expected result: L(N+1) unlocked, score X → Y+
```

### Step 6 — Output

- **Default**: Print the full report to the **console only**. Do NOT automatically write any files.
- **If user requested "compare"** and a previous report exists in `.ttadk/readiness-history/`: Append a **Change Since Last Report** section showing score deltas and criteria status changes.

### Step 7 — Ask Whether to Save Report

After printing the report, **ask the user** whether they want to save the report to a file. Present the following information:

1. **File path**: `.ttadk/readiness-report.md` (latest report) + `.ttadk/readiness-history/readiness-<YYYY-MM-DD>.md` (timestamped copy)
2. **Purpose**: Explain that saving the report enables:
   - Future runs with `"compare with last run"` to track improvement over time
   - Historical trend analysis across multiple assessments
   - Sharing the report with team members

**Do NOT write any file unless the user explicitly confirms.** If the user's original `$ARGUMENTS` already contained `"output to file"`, treat that as confirmation and proceed to write without asking again.

## Token Efficiency Strategy

Follow these principles to minimize token consumption:

1. **Batch file-existence checks**: Use directory listing to check multiple files in one tool call
2. **Progressive depth**: Start with existence checks (cheap), then selectively read files for quality assessment
3. **Sample, don't exhaust**: For large repos, sample 10–20 representative files for quality/complexity checks
4. **Early termination**: If a dimension is clearly 0 or max after basic checks, skip deep inspection
5. **Avoid reading large files entirely**: For file size checks, use line counts; for quality checks, read only the first 50–100 lines

## Tech Stack Adaptation Table

Adapt checks based on detected tech stack:

| Check | Node.js/TS | Go | Python | Java | Rust |
|---|---|---|---|---|---|
| Type system | tsconfig strict | Built-in (SKIP) | mypy/pyright | Built-in (SKIP) | Built-in (SKIP) |
| Package manager | package.json | go.mod | pyproject.toml | pom.xml | Cargo.toml |
| Lock file | package-lock.json / yarn.lock / pnpm-lock.yaml | go.sum | poetry.lock / uv.lock | — (SKIP) | Cargo.lock |
| Linter | ESLint | golangci-lint | ruff / flake8 | checkstyle / spotbugs | clippy |
| Test framework | Jest / Vitest | go test | pytest | JUnit | cargo test |
| Formatter | Prettier | gofmt (SKIP) | black / ruff format | google-java-format | rustfmt (SKIP) |
| Pre-commit | Husky / lint-staged | pre-commit | pre-commit | — | — |

## Next Step Guidance

After outputting the report, provide next-step guidance:

### If Level < L2:
- **Priority**: Run `/adk:sdd:constitution` to create AI instruction files and project context
- Ensure basic build/test infrastructure is in place

### If Level = L2, targeting L3:
- Focus on the **Must-fix** items in the Roadmap section
- Typically: pre-commit hooks, integration tests, **MR/PR 描述规范**（模板或 CONTRIBUTING 约定）, **依赖/供应链扫描**（仓库内配置或平台流水线，按 Evidence Model 判断）

### If Level = L3, targeting L4:
- **Priority**: Run `/adk:sdd:constitution` to refine project principles
- Initialize specs/ directory with `/adk:sdd:specify`, or use `/adk:sdd:ff` for the fast-forward flow
- Set up comprehensive CI/CD pipeline

### If Level ≥ L4:
- Focus on autonomous operation capabilities
- Enhance observability and monitoring
- Consider running readiness periodically to track trends

### Re-assessment:
- After making improvements, re-run `/adk:readiness` to verify progress
- Use `"compare with last run"` to see the delta
