---
description: "Brainstorm based on user input to organize feature points, assess change scope, and explore technical solutions as a Lark document."

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

Execute the following workflow to brainstorm and analyze based on user input, ultimately producing a structured concise technical proposal.

<HARD-GATE>
Before the technical proposal is explicitly approved by the user, it is strictly forbidden to:
- Write any implementation code
- Create files or scaffolding
- Modify any configuration or feature flags
This rule applies to all projects, no matter how simple they may seem.
</HARD-GATE>

**Core Principles**:
1. **User Confirmation First**: When encountering ambiguous, unclear, or decision-requiring information, you must **immediately pause** and ask the user for confirmation. **Never assume or make decisions on your own**. All AI-inferred content must be labeled "[AI Inference]".
2. **One Question at a Time**: Do not ask multiple questions in a single message to avoid information overload. If clarification is needed across multiple dimensions, split them into separate questions.
3. **YAGNI Principle**: Proactively trim unnecessary features. "Simple" requirements are the most prone to overlooked details, but designs can be brief (a few sentences) — they just can't be skipped.
4. **Proposals Must Be Compared**: Present at least 2 viable proposals for trade-off analysis. Avoid letting the first idea that comes to mind become the final proposal.

---

### Step 1: Parse User Input

#### 1.1 Identify and Read Input Content

1. **Identify input type**:
   - **Lark link**: Detect if it contains `bytedance.larkoffice.com`, `feishu.cn`, or `larksuite.com`
   - **Local file**: Detect if it starts with `/`, or contains common file extensions (`.md`, `.txt`, `.go`, `.py`, `.java`, etc.)
   - **Plain text**: Default to plain text description for all other cases

2. **Read content**:
   ```
   IF Lark link:
     Use mcp__lark-docs__export_lark_doc_markdown to export
     Read the exported markdown content
   ELSE IF local file:
     Use the Read tool to read the file content
   ELSE:
     Use the input text content directly
   ```

3. **Error handling**:
   - Empty input: Prompt "Please provide input content (text, file path, or Lark link)"
   - File not found: Prompt "File not found: [path]"
   - Invalid Lark link: Prompt "Unable to access Lark document, please check the link or permissions"
   - Content too long: Auto-truncate and prompt "Content too long, truncated for processing"

#### 1.2 Summarize Content and Confirm Objective

Summarize the parsed content, extract key information (functional requirements, business rules, technical highlights), then use the AskUserQuestion tool to confirm:

**Question**: Please confirm whether the above summary is accurate and your objective
| Option | Description |
|--------|-------------|
| Correct, generate concise technical proposal | Summary is accurate, proceed to generate the technical proposal |
| Summary is inaccurate, let me supplement | Need to revise the summary content |

#### 1.3 Confirm Tech Stack

After the user confirms the summary, **ask about the tech stack separately**:

**Question**: Please select the tech stack involved in this requirement
| Option | Description |
|--------|-------------|
| Backend | Server-side development (Go/Java/Python, etc.) — focuses on IDL, DB, RPC interfaces, service code |
| Frontend | Web frontend development (React/Vue, etc.) — focuses on API interfaces, page components, state management |
| Client | Mobile development (iOS/Android/Flutter, etc.) — focuses on native interfaces, UI components, local storage |

**Tech stack determines subsequent template selection**:
- **Backend** → Use `brainstorm-output-template.md` (IDL/DB/code layering structure)
- **Frontend** → Use `brainstorm-output-template-frontend.md` (page/component/API/state management structure)
- **Client** → Use `brainstorm-output-template.md` (reuse backend template, adapted with mobile terminology)

---

### Step 2: Feature Breakdown and Focus Selection

#### 2.1 Analyze Input Content

Based on the user's requirement input, identify the functional modules and feature points it contains:

1. **Analysis dimensions**:
   - **Functional modules**: Grouped by business domain (e.g., orders, creator fulfillment, payments, etc.)
   - **Feature points**: Specific features under each module (e.g., new API endpoint, status validation, data query, etc.)

2. **Breakdown principles**:
   - Each feature point should be an independently deliverable minimum unit
   - Feature point granularity examples:
     - "New reverse-selection API" (specific)
     - "CPT payment status validation" (specific)
     - "Reverse-selection feature" (too coarse)
     - "Payment module" (too coarse)

#### 2.2 Display Feature Point List and User Selection

```markdown
## Feature Point Analysis

Based on your requirements, I identified the following functional modules and feature points:

### Module A: [Module Name]
| ID | Feature Point | Brief Description |
| --- | --- | --- |
| A1 | [Feature Point 1] | [Description] |
| A2 | [Feature Point 2] | [Description] |

### Module B: [Module Name]
| ID | Feature Point | Brief Description |
| --- | --- | --- |
| B1 | [Feature Point 1] | [Description] |
| B2 | [Feature Point 2] | [Description] |
```

Use the AskUserQuestion tool with multi-select (multiSelect: true):
- Dynamically generate options based on identified feature points
- Each option format: `[ID] [Feature Point Name] - [Brief Description]`

---

### Step 3: Explore Project Context

**Core Principle**: Make no assumptions — read the code repository directly for real information.

#### 3.1 Obtain System Architecture Information

1. **Attempt to retrieve architecture information from context**:
   - Search for `README.md`, `ARCHITECTURE.md`, `docs/` and other files in the current directory or user-specified directory
   - Read system architecture, service partitioning, tech stack, and other information from them

2. **Review Git history**:
   - Review the most recent 20 commits for the involved projects
   - Identify recent modification hotspots and understand code evolution trends

3. **If architecture information is not found, ask the user to provide it**:
   Use the AskUserQuestion tool:
   ```markdown
   No system architecture information found in the current context. Please provide the involved services/projects:
   ```

#### 3.2 Read Code to Obtain Core Entities

**Warning**: All information below must be read directly from the code repository — no guessing allowed.

**If no code repository path is available, you must first ask the user to provide one**.

**Backend projects**:
1. Read IDL files: `idl/**/*.thrift`, `idl/**/*.proto`
2. Read database table structures: `pkg/migration/mysql/**/*.sql`, `sql/**/*.sql`
3. Read core code structure: Handler/Service/DAO layered code

**Frontend projects**:
1. Read project configuration: `package.json`, `tsconfig.json`
2. Read route definitions: `src/router/**`, `src/pages/**`
3. Read API definitions: `src/api/**`, `src/services/**`
4. Read state management: `src/store/**`, `src/models/**`
5. Read component structure: `src/components/**`

#### 3.3 Present Structured Results and Confirm

Present the retrieved information in a structured format to the user, using the AskUserQuestion tool to confirm:
| Option | Description |
|--------|-------------|
| Information is correct, proceed | Analysis results are accurate |
| Information is incorrect or incomplete, let me supplement | Needs correction |
| Need to analyze more projects/files | Continue exploring |

---

### Step 4: Iterative Clarification Questions

Based on the information collected so far, **ask only one question at a time** to deeply understand the requirement details.

**Questioning strategy**:
- Prefer multiple-choice over open-ended questions
- Focus on: purpose, constraints, success criteria, edge cases
- Only move to the next question after the current one is fully answered
- If a topic needs further exploration, split it into multiple questions

**Questioning directions** (select as needed, not every one is required):
1. **Business constraints**: Are there performance requirements, data volume expectations, or concurrency scenarios?
2. **Edge cases**: How to handle exception scenarios? Is idempotency/retry needed?
3. **Dependencies**: Does this depend on changes from other services/teams?
4. **Compatibility**: Is backward compatibility with older versions needed? Is there a gradual rollout strategy?
5. **Testing criteria**: What constitutes passing acceptance?

**Termination condition**: When your understanding of the requirements is sufficient to propose a clear technical proposal, inform the user: "I have a thorough understanding of the requirements. Next, I will propose options for your selection," then proceed to Step 5.

---

### Step 5: Propose 2-3 Options and Compare

You **must** propose at least 2 viable options, even if one seems obvious.

#### 5.1 Option Presentation

```markdown
## Option Comparison Analysis

| Comparison Dimension | Option A: [Name] | Option B: [Name] |
| --- | --- | --- |
| **Core Approach** | [Description] | [Description] |
| **Scope of Changes** | [Scope] | [Scope] |
| **Development Cost** | [High/Medium/Low] | [High/Medium/Low] |
| **Risk Points** | [Risks] | [Risks] |
| **Extensibility** | [Assessment] | [Assessment] |

**Recommendation**: Option [X], Reason: [Specific reason]
```

#### 5.2 User Selection

Use the AskUserQuestion tool:
| Option | Description |
|--------|-------------|
| Option A | [Option A name] |
| Option B | [Option B name] |
| I have a different idea | User-defined option |

---

### Step 6: Present Design in Sections

Based on the user's selected option, **present the detailed design in sections**, obtaining user confirmation after each section.

#### 6.1 Present Expected Scope of Changes

```markdown
## Expected Scope of Changes

### Involved Services/Projects
| Service (PSM) | Project Path | Change Type |
| --- | --- | --- |
| [Service Name] | [Path] | New/Modified |
```

**Additional display for backend projects**:
```markdown
### IDL/Interface Changes
| Service | Method | Change Type | Description |
| --- | --- | --- | --- |
| [Service Name] | [Method Name] | New/Modified | [Brief description] |

### Database Changes
| Table Name | Change Type | Description |
| --- | --- | --- |
| [Table Name] | New table/New field/New index | [Brief description] |

### Code Changes
| Layer | File/Method | Change Type | Description |
| --- | --- | --- | --- |
| Handler | [File#Method] | New/Modified | [Brief description] |
| Service | [File#Method] | New/Modified | [Brief description] |
| DAO | [File#Method] | New/Modified | [Brief description] |
```

**Additional display for frontend projects**:
```markdown
### Page/Route Changes
| Page | Route | Change Type | Description |
| --- | --- | --- | --- |
| [Page Name] | [Route Path] | New/Modified | [Brief description] |

### Component Changes
| Component | File Path | Change Type | Description |
| --- | --- | --- | --- |
| [Component Name] | [File Path] | New/Modified | [Brief description] |

### API/Interface Changes
| API | Method | Change Type | Description |
| --- | --- | --- | --- |
| [API Path] | GET/POST | New/Modified | [Brief description] |

### State Management Changes
| Store/Model | Change Type | Description |
| --- | --- | --- |
| [Store Name] | New/Modified | [Brief description] |
```

Use AskUserQuestion to confirm:
| Option | Description |
|--------|-------------|
| Correct, proceed | Scope of changes is accurate |
| Incorrect, let me supplement | Needs adjustment |

#### 6.2 Core Change Analysis (Presented per Feature Point)

For each selected feature point, present detailed change analysis. After each feature point is presented, ask the user if it's accurate.

**Backend feature point template**:
```markdown
### Feature Point: [A1] [Feature Point Name]

#### IDL Changes
| Change Type | Object | Description |
| --- | --- | --- |
| [New/Modified] | [Service#Method or Struct] | [Description] |

#### DB Changes
| Change Type | Table/Field | Description |
| --- | --- | --- |
| [New/Modified] | [table_name.field] | [Description] |

#### Code Changes
| # | Change Type | File/Method | Change Content | Purpose |
| --- | --- | --- | --- | --- |
| 1 | New | `handler/xxx.go#NewHandler` | New API handler function | Receive request |

#### Call Chain
`Request → Handler → Service → DAO → DB`
```

**Frontend feature point template**:
```markdown
### Feature Point: [A1] [Feature Point Name]

#### Page/Component Changes
| Change Type | Component/Page | File Path | Description |
| --- | --- | --- | --- |
| [New/Modified] | [Component Name] | [File Path] | [Description] |

#### API Calls
| API Path | Method | Description |
| --- | --- | --- |
| [/api/xxx] | GET/POST | [Description] |

#### State Management
| Store/Model | Change Type | Description |
| --- | --- | --- |
| [Store Name] | [New/Modified] | [Description] |

#### Interaction Flow
`User Action → Component Event → API Call → State Update → UI Render`
```

Use AskUserQuestion to confirm after each feature point:
| Option | Description |
|--------|-------------|
| Correct, proceed to next | Analysis is accurate |
| Incorrect, let me supplement | Needs adjustment |

---

### Step 7: Proposal Confirmation

Consolidate the design of all feature points, present the complete proposal summary, and request the user's final confirmation.

```markdown
## Proposal Summary

**Selected Option**: [Option Name]
**Involved Projects**: [N]
**Feature Points**: [N]
**Estimated Changed Files**: [N]

### Risk Points
1. **[Risk Type]**: [Brief description]
2. **[Risk Type]**: [Brief description]

---

Does the above design pass review?
```

Use AskUserQuestion to confirm:
| Option | Description |
|--------|-------------|
| Approved, generate technical proposal | Proposal confirmed, generate final document |
| Needs modification | Return to modify (specify what needs to be changed) |

---

### Step 8: Generate Structured Technical Proposal

Integrate all information and generate the final technical proposal based on the template.

**Template selection** (based on the tech stack selected by the user in Step 1.3):
- **Backend/Client**: Read `plugins/ttadk/core/resources/templates/brainstorm-output-template.md`
- **Frontend**: Read `plugins/ttadk/core/resources/templates/brainstorm-output-template-frontend.md`

**Output principles**:
- **IMPORTANT**: Do not add a `# Title` at the beginning of the document — the Lark document title will serve as the main title to avoid duplicate titles
- The document starts with a TTADK quote (refer to the `> Generated by ...` line in the template)
- Only output sections with actual changes; omit sections with no changes
- Change content: Only show change summary tables, no complete code needed
- Code changes: Only list key change points
- Call chain/interaction flow: Simplify to a single-line expression

**Output file**: `/specs/brainstorm/[feature_name]_brainstorm.md`

#### 8.1 Validate Mermaid Diagrams

If the generated document contains Mermaid code blocks (```mermaid```), you **must** perform **static syntax validation** on ALL Mermaid code blocks before proceeding:
   - **DO NOT use mermaid-cli** - it may not be installed and has version compatibility issues
   - For each `mermaid` code block, manually verify against this checklist:

   **Validation Checklist** (check each item):

   **Flowchart Validation**:
   - Uses `flowchart LR` or `flowchart TD` (not `graph`)
   - Node text with special chars is quoted: `A["Text with spaces"]`
   - Only allowed arrows: `-->`, `-.->`, `==>` (NO `-,->`, `--x`, `--o`)
   - Arrow labels are simple: `-->|Label|` (no `[]`, `<>`, or special chars in labels)
   - Subgraph syntax: `subgraph Name["Title"]` or `subgraph Name`

   **Sequence Diagram Validation**:
   - All `alt`/`opt`/`loop` blocks have matching `end`
   - **NO `box` syntax** (not supported in Mermaid 9.x)
   - Participant aliases don't contain special chars

   **State Diagram Validation**:
   - Uses `stateDiagram-v2` keyword
   - State names have no spaces (use aliases: `state "Name" as Alias`)

   **If issues found**:
   1. Fix the syntax error in the Mermaid code
   2. Re-write the corrected content to the file using Write tool
   3. Re-validate until all checks pass

   **Common Fixes**:
   - Unquoted text -> Add quotes: `A[Text]` -> `A["Text"]`
   - Wrong arrow -> Replace: `-,->` -> `-.->`, `--x` -> `-->`
   - Complex labels -> Simplify: remove `[]`, `<>`, `<br/>`
   - `box` syntax -> Remove entirely (use comments for grouping instead)

---

### Step 9: Lark Export and Feedback Collection

1. **Export to Lark document** (mandatory):
   - Use `mcp__lark-docs__import_markdown_to_lark` to import the generated proposal into Lark
   - Parameter settings:
     - `filePath`: Absolute path of the generated `brainstorm-output.md`
     - `title`: **IMPORTANT** - Generate a concise descriptive title that summarizes the core purpose of the feature:
       - Use the language matching the `preferred_language` setting (zh → Chinese, en → English)
       - Keep it short (preferably no more than 30 characters)
       - Focus on what the feature does, not technical implementation details
       - Examples: "User Auth Module Design", "Payment Gateway Integration", "Order State Machine Refactor"
       - Do not use the folder name directly — summarize the actual content
   - Obtain the Lark document URL
   - **Directly use the `open` command to open the Lark document link**:
     ```bash
     open "https://feishu.cn/docx/xxxxx"
     ```

2. **Guide the user to read and provide feedback**:
   ```markdown
   ## Proposal Generated

   **Lark Document**: [Link] (opened in browser)
   **Local File**: `brainstorm/[feature-name]/brainstorm-output.md`

   ---

   Please read the technical proposal in the Lark document, then let me know:
   1. Which parts are inaccurately described?
   2. Which change points are missing?
   3. Which sections need more detail?

   I will revise and refine based on your feedback.
   ```

3. **Handle user feedback**: Support multiple rounds of feedback iteration until the user is satisfied.

---

## Next Step Guidance

After the command execution is complete, provide next step guidance:

### Proposal Review
Guide the user to check whether the generated technical proposal meets expectations.

**If adjustments are needed**:
- Tell me directly what needs to be modified, and I will update the proposal
- Or re-execute `/adk:sdd:brainstorm` with additional information

### Next Steps
After the proposal is confirmed, you can:
1. Share the proposal with the team for review
2. Execute `/adk:sdd:specify` to create a formal feature specification based on the brainstorm output
3. Execute `/adk:sdd:ff` to quickly draft `spec.md`, `plan.md`, and `tasks.md`
