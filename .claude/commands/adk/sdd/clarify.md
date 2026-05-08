---
description: Identify underspecified areas in the current feature spec by asking up to 5 highly targeted clarification questions and encoding answers back into the spec.

---

## User Input

```text
$ARGUMENTS
```
You **MUST** consider the user input before proceeding (if not empty).

If the given `$ARGUMENTS` contains a link, you need to read the content of the link (use lark-docs mcp if it's a lark doc) and replace the link with content.

## Context
**Read context before Executing**:
1. Language Setting
   - Read `preferred_language` from `.ttadk/config.json` (default: 'en' if missing). **IMPORTANT** **Use the configured language for ALL outputs: 'en' → English, 'zh' → 中文. This applies to: generated documents (specs, plans, tasks), interactive prompts, confirmations, status messages, and error descriptions.**

## Outline

Goal: Detect and reduce ambiguity or missing decision points in the active feature specification and record the clarifications directly in the spec file.

Note: This clarification workflow can run at ANY point during the design stage. It will synchronously update ALL existing design artifacts based on the clarifications.

**Core Principle**: Check which design-stage documents exist, and update ALL of them to maintain consistency.

**Design Stage Commands & Artifacts**:
- `/adk:sdd:specify` → `spec.md`
- `/adk:sdd:plan` → `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`
- `/adk:sdd:erd` → `technical-design.md` (Technical Design Document)
- `/adk:sdd:tasks` → `tasks.md`

**Usage Examples**:
- Run after `/adk:sdd:specify` only → Updates only `spec.md`
- Run after `/adk:sdd:specify` + `/adk:sdd:plan` → Updates `spec.md` + all `/adk:sdd:plan` outputs
- Run after full design stage → Updates all design artifacts (spec, plan, erd, tasks)

Execution steps:

1. **Discover existing design artifacts:**
    - Run `node .ttadk/plugins/ttadk/core/resources/scripts/check-prerequisites.js --json --paths-only` from repo root **once**
    - Parse JSON payload fields: `FEATURE_DIR`, `FEATURE_SPEC`, `IMPL_PLAN`, `TASKS`
    - If JSON parsing fails, abort and instruct user to re-run `/adk:sdd:specify` or verify feature directory environment.
    - **Explicitly check which design-stage documents exist** using Read tool for each file:
      * `FEATURE_DIR/spec.md` (REQUIRED - from `/adk:sdd:specify`)
      * `FEATURE_DIR/plan.md` (from `/adk:sdd:plan`)
      * `FEATURE_DIR/research.md` (from `/adk:sdd:plan` Phase 0)
      * `FEATURE_DIR/data-model.md` (from `/adk:sdd:plan` Phase 1)
      * `FEATURE_DIR/contracts/` directory (from `/adk:sdd:plan` Phase 1)
      * `FEATURE_DIR/quickstart.md` (from `/adk:sdd:plan` Phase 1)
      * `FEATURE_DIR/tasks.md` (from `/adk:sdd:tasks`)
      * `FEATURE_DIR/technical-design.md` (from `/adk:sdd:erd`)
    - **Build and maintain a DISCOVERED_DOCS tracking list** with existence status for each document:
      ```
      DISCOVERED_DOCS = {
        "spec.md": { exists: true/false, path: "..." },
        "plan.md": { exists: true/false, path: "..." },
        "tasks.md": { exists: true/false, path: "..." },
        ...
      }
      ```
    - This list is **REQUIRED** for Step 6 and Step 9 - every existing document must be processed.

2. Load guiding principles and current spec:
   - Read `.ttadk/memory/constitution.md` - consider these principles when evaluating clarification impact
   - Load the current spec file
   - Perform a structured ambiguity & coverage scan using this taxonomy. For each category, mark status: Clear / Partial / Missing. Produce an internal coverage map used for prioritization (do not output raw map unless no questions will be asked).

    Functional Scope & Behavior:
    - Core user goals & success criteria
    - Explicit out-of-scope declarations
    - User roles / personas differentiation

    Domain & Data Model:
    - Entities, attributes, relationships
    - Identity & uniqueness rules
    - Lifecycle/state transitions
    - Data volume / scale assumptions

    Interaction & UX Flow:
    - Critical user journeys / sequences
    - Error/empty/loading states
    - Accessibility or localization notes

    Non-Functional Quality Attributes:
    - Performance (latency, throughput targets)
    - Scalability (horizontal/vertical, limits)
    - Reliability & availability (uptime, recovery expectations)
    - Observability (logging, metrics, tracing signals)
    - Security & privacy (authN/Z, data protection, threat assumptions)
    - Compliance / regulatory constraints (if any)

    Integration & External Dependencies:
    - External services/APIs and failure modes
    - Data import/export formats
    - Protocol/versioning assumptions

    Edge Cases & Failure Handling:
    - Negative scenarios
    - Rate limiting / throttling
    - Conflict resolution (e.g., concurrent edits)

    Constraints & Tradeoffs:
    - Technical constraints (language, storage, hosting)
    - Explicit tradeoffs or rejected alternatives

    Terminology & Consistency:
    - Canonical glossary terms
    - Avoided synonyms / deprecated terms

    Completion Signals:
    - Acceptance criteria testability
    - Measurable Definition of Done style indicators

    Misc / Placeholders:
    - TODO markers / unresolved decisions
    - Ambiguous adjectives ("robust", "intuitive") lacking quantification

    For each category with Partial or Missing status, add a candidate question opportunity unless:
    - Clarification would not materially change implementation or validation strategy
    - Information is better deferred to planning phase (note internally)

3. Generate (internally) a prioritized queue of candidate clarification questions (maximum 5). Do NOT output them all at once. Apply these constraints:
    - Maximum of 10 total questions across the whole session.
    - Each question must be answerable with EITHER:
        * A short multiple‑choice selection (2–5 distinct, mutually exclusive options), OR
        * A one-word / short‑phrase answer (explicitly constrain: "Answer in <=5 words").
    - Only include questions whose answers materially impact architecture, data modeling, task decomposition, test design, UX behavior, operational readiness, or compliance validation.
    - Ensure category coverage balance: attempt to cover the highest impact unresolved categories first; avoid asking two low-impact questions when a single high-impact area (e.g., security posture) is unresolved.
    - Exclude questions already answered, trivial stylistic preferences, or plan-level execution details (unless blocking correctness).
    - Favor clarifications that reduce downstream rework risk or prevent misaligned acceptance tests.
    - If more than 5 categories remain unresolved, select the top 5 by (Impact * Uncertainty) heuristic.

4. Sequential questioning loop (interactive):
    - Present EXACTLY ONE question at a time.
    - For multiple‑choice questions render options as a Markdown table:

        | Option | Description |
        |--------|-------------|
        | A | <Option A description> |
        | B | <Option B description> |
        | C | <Option C description> | (add D/E as needed up to 5)
        | Short | Provide a different short answer (<=5 words) | (Include only if free-form alternative is appropriate)

    - For short‑answer style (no meaningful discrete options), output a single line after the question: `Format: Short answer (<=5 words)`.
    - After the user answers:
        * Validate the answer maps to one option or fits the <=5 word constraint.
        * If ambiguous, ask for a quick disambiguation (count still belongs to same question; do not advance).
        * Once satisfactory, record it in working memory (do not yet write to disk) and move to the next queued question.
    - Stop asking further questions when:
        * All critical ambiguities resolved early (remaining queued items become unnecessary), OR
        * User signals completion ("done", "good", "no more"), OR
        * You reach 5 asked questions.
    - Never reveal future queued questions in advance.
    - If no valid questions exist at start, immediately report no critical ambiguities.

5. Integration after EACH accepted answer (incremental update approach):
    - Maintain in-memory representation of the spec (loaded once at start) plus the raw file contents.
    - For the first integrated answer in this session:
        * Ensure a `## Clarifications` section exists (create it just after the highest-level contextual/overview section per the spec template if missing).
        * Under it, create (if not present) a `### Session YYYY-MM-DD` subheading for today.
    - Append a bullet line immediately after acceptance: `- Q: <question> → A: <final answer>`.
    - Then immediately apply the clarification to the most appropriate section(s):
        * Functional ambiguity → Update or add a bullet in Functional Requirements.
        * User interaction / actor distinction → Update User Stories or Actors subsection (if present) with clarified role, constraint, or scenario.
        * Data shape / entities → Update Data Model (add fields, types, relationships) preserving ordering; note added constraints succinctly.
        * Non-functional constraint → Add/modify measurable criteria in Non-Functional / Quality Attributes section (convert vague adjective to metric or explicit target).
        * Edge case / negative flow → Add a new bullet under Edge Cases / Error Handling (or create such subsection if template provides placeholder for it).
        * Terminology conflict → Normalize term across spec; retain original only if necessary by adding `(formerly referred to as "X")` once.
    - If the clarification invalidates an earlier ambiguous statement, replace that statement instead of duplicating; leave no obsolete contradictory text.
    - Save the spec file AFTER each integration to minimize risk of context loss (atomic overwrite).
    - Preserve formatting: do not reorder unrelated sections; keep heading hierarchy intact.
    - Keep each inserted clarification minimal and testable (avoid narrative drift).

5.1 **MANDATORY: After questioning loop ends (before reporting):**
    - ⚠️ **DO NOT proceed to Step 9 (reporting) without executing Step 6 (document synchronization)**
    - When user signals completion ("done", "good", "no more") or question quota reached:
      * First complete any pending spec.md integration from Step 5
      * Then **IMMEDIATELY proceed to Step 6** - this is NOT optional
    - Skipping Step 6 will result in inconsistent design artifacts

6. **Synchronously update all existing design-stage documents:**

    ⚠️ **THIS STEP IS MANDATORY - DO NOT SKIP**

    - **BEFORE reporting completion, you MUST process ALL documents in DISCOVERED_DOCS from Step 1**
    - For each document in DISCOVERED_DOCS where `exists: true`:
      * **Analyze impact**: Determine if clarifications affect this document
      * **If affected**: Update the document with relevant changes
      * **If not affected**: Record "No changes needed" with specific reason (for Step 9 report)
    - **ONLY update documents that exist** - skip non-existent files.
    - Update documents in dependency order to maintain consistency:

    **A. Update `/adk:sdd:plan` outputs (if they exist):**

    **If `plan.md` exists:**
    - Read existing `plan.md` to understand current implementation approach.
    - Analyze spec.md clarifications that impact technical design (data model, entities, non-functional requirements, integrations).
    - Update affected sections:
      * Technical Context: Update tech stack if clarifications reveal new requirements
      * Architecture: Adjust structure if functional scope changed
      * User Stories: Ensure they still align with the clarified spec
      * Data Storage/Model: Update if data requirements clarified
    - Save updated plan.md.

    **If `research.md` exists:**
    - Read existing `research.md`.
    - If clarifications resolve NEEDS CLARIFICATION items that were deferred to research:
      * Add new decisions based on clarifications
      * Update rationale if clarifications change technical choices
      * Mark resolved items from spec clarification
    - Save updated research.md.

    **If `data-model.md` exists:**
    - Read existing `data-model.md`.
    - Analyze clarifications that impact data model (entities, attributes, relationships, constraints).
    - Update data-model.md:
      * Add/modify entities based on clarified requirements
      * Update field definitions and types
      * Adjust relationships if functional scope changed
      * Update validation rules from clarified constraints
    - Save updated data-model.md.

    **If `contracts/` directory exists:**
    - List files in contracts/ directory.
    - If clarifications affect API behavior (new endpoints, changed request/response, new error cases):
      * Update relevant OpenAPI/GraphQL schema files
      * Add new endpoints if functional requirements expanded
      * Modify existing endpoint definitions for clarified behavior
    - Save updated contract files.

    **If `quickstart.md` exists:**
    - Read existing `quickstart.md`.
    - If clarifications change user scenarios or core flows:
      * Update quickstart examples to reflect clarified behavior
      * Add new steps if functional requirements expanded
    - Save updated quickstart.md.

    **B. Update `/adk:sdd:erd` outputs (if they exist):**

    **If `technical-design.md` exists:**
    - Read existing `technical-design.md` to understand current technical design (architecture, data model, interfaces, etc.).
    - Check if clarifications impact technical design (architecture changes, data model, API definitions, schema changes).
    - If impacted:
      * Update architecture diagrams and descriptions
      * Update entity definitions and relationships in ER diagrams
      * Update interface definitions (API/RPC)
      * Update schema definitions (DB/MQ/Cache)
      * Ensure all Mermaid diagrams follow correct syntax
    - Save updated technical-design.md.

    **C. Update `/adk:sdd:tasks` outputs (if they exist):**

    **If `tasks.md` exists:** ← **THIS IS MANDATORY IF tasks.md EXISTS**
    - Read existing `tasks.md` to understand current task breakdown.
    - **ALWAYS perform impact analysis** - do not skip this step:
      * Review each clarification Q&A from this session
      * Map each clarification to potentially affected task categories
    - **Update triggers** (if ANY of these apply, tasks.md MUST be updated):
      * Functional requirement clarified → Review and update affected user story tasks
      * New edge case identified → Add validation/error handling tasks
      * Data model clarified → Update model/migration/schema tasks
      * Non-functional requirement added/changed → Add corresponding performance/security tasks
      * Scope changed (added/removed features) → Add/remove/reorder tasks accordingly
      * New integration point identified → Add integration tasks
    - Update tasks to align with clarified spec and updated plan.
    - **If no updates needed**: Document specific reason why each clarification does NOT affect tasks
      (e.g., "Clarification Q1 only affects UI text, no task changes needed")
    - Save updated tasks.md.

7. Validation (performed after EACH write plus final pass):
    - Clarifications session contains exactly one bullet per accepted answer (no duplicates).
    - Total asked (accepted) questions ≤ 5.
    - Updated sections contain no lingering vague placeholders the new answer was meant to resolve.
    - No contradictory earlier statement remains (scan for now-invalid alternative choices removed).
    - Markdown structure valid; only allowed new headings: `## Clarifications`, `### Session YYYY-MM-DD`.
    - Terminology consistency: same canonical term used across all updated sections.
    - If downstream documents were updated, ensure consistency across all documents.

    **Cross-document consistency checks (REQUIRED):**
    - For each document in DISCOVERED_DOCS where `exists: true`:
      * Verify it was either **updated** OR **explicitly marked as "no changes needed" with reason**
    - **If tasks.md exists AND any clarification touched these areas, tasks.md MUST reflect changes:**
      * Functional requirements → task breakdown must align
      * Edge cases or error handling → corresponding validation tasks must exist
      * Data model changes → model/migration tasks must be updated
      * Non-functional requirements → performance/security tasks must exist
    - **FAIL validation if:**
      * Any existing document was neither updated nor explicitly reviewed
      * tasks.md exists but was skipped without documented justification for each clarification

8. Write all updated documents back to their respective paths.

9. Report completion (after questioning loop ends or early termination):
    - Number of questions asked & answered.
    - Path to updated spec.

    **Document Update Summary (REQUIRED for each document in DISCOVERED_DOCS):**

    | Document | Exists | Updated | Sections Changed | Reason if Not Updated |
    |----------|--------|---------|------------------|----------------------|
    | spec.md  | ✓/✗    | ✓/✗     | (list sections)  | (reason or "-")      |
    | plan.md  | ✓/✗    | ✓/✗     | (list sections)  | (reason or "-")      |
    | research.md | ✓/✗ | ✓/✗     | (list sections)  | (reason or "-")      |
    | data-model.md | ✓/✗ | ✓/✗   | (list sections)  | (reason or "-")      |
    | contracts/ | ✓/✗   | ✓/✗     | (list files)     | (reason or "-")      |
    | quickstart.md | ✓/✗ | ✓/✗   | (list sections)  | (reason or "-")      |
    | technical-design.md | ✓/✗ | ✓/✗ | (list sections) | (reason or "-")    |
    | tasks.md | ✓/✗    | ✓/✗     | (list tasks/phases) | (reason or "-")   |

    - **If any document exists but was NOT updated, you MUST provide explicit reasoning in the table**
    - Coverage summary table listing each taxonomy category with Status: Resolved (was Partial/Missing and addressed), Deferred (exceeds question quota or better suited for planning), Clear (already sufficient), Outstanding (still Partial/Missing but low impact).
    - If any Outstanding or Deferred remain, recommend whether to proceed to `/adk:sdd:plan` or `/adk:sdd:erd` or `/adk:sdd:tasks` or run `/adk:sdd:clarify` again.
    - Suggested next command.

Behavior rules:
- If no meaningful ambiguities found (or all potential questions would be low-impact), respond: "No critical ambiguities detected worth formal clarification." and suggest proceeding.
- If spec file missing, instruct user to run `/adk:sdd:specify` first (do not create a new spec here).
- Never exceed 5 total asked questions (clarification retries for a single question do not count as new questions).
- Avoid speculative tech stack questions unless the absence blocks functional clarity.
- Respect user early termination signals ("stop", "done", "proceed") - **but ALWAYS execute Step 6 before reporting**.
- If no questions asked due to full coverage, output a compact coverage summary (all categories Clear) then suggest advancing.
- If quota reached with unresolved high-impact categories remaining, explicitly flag them under Deferred with rationale.

**CRITICAL - Cross-document update rules:**
- **If tasks.md exists, you MUST either:**
  1. Update it based on clarifications, OR
  2. Explicitly state in the Document Update Summary why no updates were needed (with specific analysis per clarification)
- **Never report completion without having processed ALL existing design documents**
- **Document skips are NOT allowed** - every document in DISCOVERED_DOCS where `exists: true` must be analyzed
- Early termination by user does NOT exempt you from Step 6 document synchronization

Context for prioritization: $ARGUMENTS

## Next Step Guidance

After executing this command, provide next-step guidance to user:

### Step 1 - Confirmation
Guide user to verify the updated documents are correct.

**If needs adjustment**: Run `/adk:sdd:clarify [feedback]` again to continue refining the documents.

### Step 2 - Next Step Recommendation
Once documents are confirmed and satisfactory, recommend the next command based on existing documents:

- If only `spec.md` exists → Execute `/adk:sdd:plan`
- If `spec.md` + `plan.md` exist but no `tasks.md` → Execute `/adk:sdd:tasks`
- If all documents exist → Execute `/adk:sdd:implement`
- If unclear which stage the user is in → Do not make explicit recommendation, let user decide
