---
description: Create or update the project constitution from interactive or provided principle inputs, ensuring all dependent templates stay in sync

---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty).

If the given `$ARGUMENTS` contains a link, you need to read the content of the link (use lark-docs mcp if it's a lark doc) and replace the link with content.

## Context

**Read context before Executing**:

1. **Language Setting**: Read `preferred_language` from `.ttadk/config.json` (default: 'en' if missing).
   - **IMPORTANT**: Use the configured language for ALL outputs: 'en' → English, 'zh' → 中文. This applies to: generated documents, interactive prompts, confirmations, status messages, and error descriptions.

2. **Initialize Constitution**: Run `node .ttadk/plugins/ttadk/core/resources/scripts/init-constitution.js --json` from repo root.
   - This script copies `constitution-template.md` to `.ttadk/memory/constitution.md` if it doesn't exist yet (first-time setup). If the file already exists, the script does nothing.

## Outline

You are updating the project constitution at `.ttadk/memory/constitution.md`. This file is a TEMPLATE containing placeholder tokens in square brackets (e.g. `[PROJECT_NAME]`, `[PRINCIPLE_1_NAME]`). Your job is to (a) collect/derive concrete values, (b) fill the template precisely, and (c) propagate any amendments across dependent artifacts.

Follow this execution flow:

1. Load the existing constitution template at `.ttadk/memory/constitution.md`.
   - Identify every placeholder token of the form `[ALL_CAPS_IDENTIFIER]`.
     **IMPORTANT**: The user might require less or more principles than the ones used in the template. If a number is specified, respect that - follow the general template. You will update the doc accordingly.

2. Collect/derive values for placeholders:
   - If user input (conversation) supplies a value, use it.
   - Otherwise infer from existing repo context (README, docs, prior constitution versions if embedded).
   - For governance dates: `RATIFICATION_DATE` is the original adoption date (if unknown ask or mark TODO), `LAST_AMENDED_DATE` is today if changes are made, otherwise keep previous.
   - `CONSTITUTION_VERSION` must increment according to semantic versioning rules:
     - MAJOR: Backward incompatible governance/principle removals or redefinitions.
     - MINOR: New principle/section added or materially expanded guidance.
     - PATCH: Clarifications, wording, typo fixes, non-semantic refinements.
   - If version bump type ambiguous, propose reasoning before finalizing.

3. Draft the updated constitution content:
   - Replace every placeholder with concrete text (no bracketed tokens left except intentionally retained template slots that the project has chosen not to define yet—explicitly justify any left).
   - Preserve heading hierarchy and comments can be removed once replaced unless they still add clarifying guidance.
   - Ensure each Principle section: succinct name line, paragraph (or bullet list) capturing non‑negotiable rules, explicit rationale if not obvious.
   - Ensure Governance section lists amendment procedure, versioning policy, and compliance review expectations.
   - **Fixed Rules Section**: This section contains default rules for AI-assisted development. Translate to match `preferred_language` while preserving the original meaning.

4. Consistency propagation checklist (convert prior checklist into active validations):

   **Standard Workflow Templates:**
   - Read `.ttadk/plugins/ttadk/core/resources/templates/plan-template.md` and ensure any "Constitution Check" or rules align with updated principles.
   - Read `.ttadk/plugins/ttadk/core/resources/templates/spec-template.md` for scope/requirements alignment—update if constitution adds/removes mandatory sections or constraints.
   - Read `.ttadk/plugins/ttadk/core/resources/templates/tasks-template.md` and ensure task categorization reflects new or removed principle-driven task types (e.g., observability, versioning, testing discipline).

   **Fast-forward Workflow Alignment:**
   - Do not assume fast-forward uses a separate template set. Treat `.ttadk/plugins/ttadk/core/resources/templates/spec-template.md` as the canonical specification template and ensure constitution changes remain reflected there for both standard and fast-forward flows.
   - If fast-forward also produces `plan.md` and `tasks.md`, verify those outputs remain aligned with constitution principles and the standard workflow expectations. Do not rely on `*-lite` templates as the source of truth when checking governance alignment.

   **Command Definitions and Documentation:**
   - Read each command file in `.ttadk/plugins/ttadk/core/commands/**/*.md` (including this one) to verify no outdated references (agent-specific names like CLAUDE only) remain when generic guidance is required.
   - Read any runtime guidance docs (e.g., `README.md`, `docs/quickstart.md`, or agent-specific guidance files if present). Update references to principles changed.

5. Produce a Sync Impact Report (prepend as an HTML comment at top of the constitution file after update):
   - Version change: old → new
   - List of modified principles (old title → new title if renamed)
   - Added sections
   - Removed sections
   - Templates requiring updates (✅ updated / ⚠ pending) with file paths
   - Follow-up TODOs if any placeholders intentionally deferred.

6. Validation before final output:
   - No remaining unexplained bracket tokens.
   - Version line matches report.
   - Dates ISO format YYYY-MM-DD.
   - Principles are declarative, testable, and free of vague language ("should" → replace with MUST/SHOULD rationale where appropriate).

7. Write the completed constitution back to `.ttadk/memory/constitution.md` (overwrite).

8. Output a final summary to the user with:
   - New version and bump rationale.
   - Any files flagged for manual follow-up.
   - Suggested commit message (e.g., `docs: amend constitution to vX.Y.Z (principle additions + governance update)`).

Formatting & Style Requirements:

- Use Markdown headings exactly as in the template (do not demote/promote levels).
- Wrap long rationale lines to keep readability (<100 chars ideally) but do not hard enforce with awkward breaks.
- Keep a single blank line between sections.
- Avoid trailing whitespace.

If the user supplies partial updates (e.g., only one principle revision), still perform validation and version decision steps.

If critical info missing (e.g., ratification date truly unknown), insert `TODO(<FIELD_NAME>): explanation` and include in the Sync Impact Report under deferred items.

Do not create a new template; always operate on the existing `.ttadk/memory/constitution.md` file.

## Next Step Guidance

After executing this command, provide next-step guidance to user:

### Step 1 - Confirmation
Guide user to verify the generated constitution is correct and aligns with project principles.

**If needs adjustment**: Re-run `/adk:sdd:constitution [feedback]` to refine.

### Step 2 - Next Step Recommendation
Once constitution is confirmed and satisfactory:

**Create Feature Specification**:
- **Standard workflow**: Execute `/adk:sdd:specify [input]` to create detailed feature specification with validation and clarification flow
- **Fast-forward workflow**: Execute `/adk:sdd:ff [input]` to quickly draft spec, plan, and tasks together
