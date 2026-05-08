---
description: Answer questions about TTADK and SDD workflow, detect current project state, and guide users on what to do next.

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
   - Read `preferred_language` from `.ttadk/config.json` (default: 'en' if missing). **IMPORTANT** **Use the configured language for ALL outputs: 'en' → English, 'zh' → 中文. This applies to: interactive prompts, status messages, explanations, and recommendations.**

## Goal

Serve as the TTADK intelligent assistant: answer user questions about SDD workflow, TTADK CLI, commands, and best practices; optionally detect the current project state when needed; and recommend appropriate next actions.

## Execution Steps

### 1. Analyze User Intent

Before gathering any context, classify the user's question into one or more of the following categories:

| Category | Examples | Requires SDD State? |
|----------|---------|---------------------|
| **Knowledge Query** | "什么是 SDD？", "`/adk:sdd:plan` 怎么用？", "ttadk init 有哪些参数？" | No |
| **State Analysis** | "我当前进度如何？", "下一步该做什么？", "帮我分析当前状态" | Yes |
| **Troubleshooting** | "报错了怎么办？", "为什么 check-prerequisites 失败？" | Maybe (depends on context) |
| **Project Guidance** | "这个功能应该怎么拆分？", "我的 spec 写得好不好？" | Yes |

Use this classification to decide which of the following steps to execute. **Skip unnecessary steps** to keep the response fast and focused.

### 2. Load Knowledge Base

Use the `ttadk-knowledge` skill to answer user questions:

1. Read the skill via `.ttadk/plugins/ttadk/core/skills/ttadk-knowledge/SKILL.md`
2. The SKILL.md contains an overview of SDD workflow, command quick reference, CLI reference, and an **index to detailed sub-files**
3. Based on the user's question, follow the index in SKILL.md to read the relevant sub-files on demand

Do NOT read all sub-files blindly — only load what is needed based on the user's question topic.

### 3. Detect Current SDD State (Only When Needed)

**Skip this step** if the user is only asking knowledge-based questions (e.g., "什么是 SDD？", "`/adk:sdd:plan` 命令怎么用？").

**Execute this step** when the user's question involves their current project state, progress, or next steps.

Run `node .ttadk/plugins/ttadk/core/resources/scripts/check-prerequisites.js --paths-only` from repo root.

- If the script **succeeds**, parse the JSON output for: `REPO_ROOT`, `FEATURE_NAME`, `FEATURE_DIR`, `FEATURE_SPEC`, `IMPL_PLAN`, `TASKS`
- If the script **fails** (no feature directory), note that the user has not started any feature yet.

Then check which SDD artifacts exist by attempting to read each file:

| Artifact | Path | Produced By |
|----------|------|-------------|
| spec.md | `FEATURE_DIR/spec.md` | `/adk:sdd:specify` or `/adk:sdd:ff` |
| plan.md | `FEATURE_DIR/plan.md` | `/adk:sdd:plan` or `/adk:sdd:ff` |
| research.md | `FEATURE_DIR/research.md` | `/adk:sdd:plan` |
| data-model.md | `FEATURE_DIR/data-model.md` | `/adk:sdd:plan` |
| contracts/ | `FEATURE_DIR/contracts/` | `/adk:sdd:plan` |
| technical-design.md | `FEATURE_DIR/technical-design.md` | `/adk:sdd:erd` |
| tasks.md | `FEATURE_DIR/tasks.md` | `/adk:sdd:tasks` or `/adk:sdd:ff` |
| checklists/ | `FEATURE_DIR/checklists/` | Supporting validation artifacts created by workflow commands when needed |

Build a **state summary**:

```
SDD_STATE = {
  feature_name: "...",
  feature_dir: "...",
  artifacts: {
    "spec.md": true/false,
    "plan.md": true/false,
    "tasks.md": true/false,
    ...
  },
  current_stage: "not-started" | "specified" | "planned" | "designed" | "tasked" | "implementing" | "committed"
}
```

Stage detection logic:
- No feature dir → `not-started`
- Archive or commit artifacts found → `committed`
- tasks.md exists and some tasks marked done → `implementing`
- spec.md + plan.md + tasks.md → `tasked`
- spec.md + plan.md + technical-design.md → `designed`
- spec.md only → `specified`
- spec.md + plan.md → `planned`

### 4. Load Project Context (Optional)

If the user's question relates to their specific project context:

- Read `.ttadk/memory/constitution.md` for project principles
- Skim existing artifacts (spec.md, plan.md, etc.) for relevant context
- Do NOT load full artifact contents unless directly needed to answer the question

### 5. Compose Response

Structure your response based on the user's intent:

**A. Current State Summary** (only when SDD state was detected in Step 3):

Display a compact status overview:

```
📍 当前状态: [stage name]
📁 Feature: [feature_name]

已完成的制品:
✅ spec.md (规格说明)
✅ plan.md (实施计划)
⬜ tasks.md (任务分解)
⬜ technical-design.md (技术设计)
```

**B. Answer the Question**:

- If the user asked a specific question, answer it using the knowledge base and project context.
- Be concise and actionable. Prefer bullet points over long paragraphs.
- Reference specific commands with `/adk:command-name` format.
- **Include documentation links**: If your answer references information from the knowledge base that has associated documentation links (e.g., Lark doc links, external references), include those links in your answer and suggest the user read them for more details.
- If the knowledge base lacks information, acknowledge it and suggest using `tiksearch` MCP for internal docs or `lark-docs` MCP for Lark documents.

**C. Next Step Recommendation** (include when SDD state was detected or the user seems unsure about what to do):

Based on the detected SDD state and user's question, recommend the most logical next action:

| Current Stage | Recommended Next Step |
|---------------|----------------------|
| not-started | Run `/adk:readiness` to assess the repo, then `/adk:sdd:specify` for standard flow or `/adk:sdd:ff` for fast-forward flow |
| specified | Run `/adk:sdd:plan` to create implementation plan, or `/adk:sdd:clarify` to refine spec |
| planned | Run `/adk:sdd:erd` for technical design if needed, or `/adk:sdd:tasks` for task breakdown |
| designed | Run `/adk:sdd:tasks` for task breakdown |
| tasked | Run `/adk:sdd:analyze` for read-only artifact quality check, or `/adk:sdd:implement` to start coding |
| implementing | Continue with `/adk:sdd:implement`, or `/adk:commit` when ready |
| committed | Run `/adk:sdd:archive` to archive the feature |

If the user's question implies a specific need, tailor the recommendation accordingly.

**D. Further Help** (always include at the end):

Always append the following guidance at the end of your response:

> 如果以上内容未能解决你的问题，你可以：
> - 📖 查阅 [TTADK FAQ 文档](https://bytedance.larkoffice.com/wiki/RJsSwFVvhiD0DPkLgXicfoKknbh) 获取更多常见问题解答
> - 💬 加入 [TTADK Oncall 群](https://bytedance.larkoffice.com/wiki/RJsSwFVvhiD0DPkLgXicfoKknbh#share-ILFMdf42IoRN21x1jr3lxexkgcB) 向团队成员提问

Adapt the language based on `preferred_language` setting (English or Chinese).

## Behavior Rules

- **Read-only**: This command MUST NOT modify any files. It is purely informational.
- **Lazy loading**: Only gather information (SDD state, project context, knowledge sub-files) when the user's question requires it. Do not run all steps unconditionally.
- **Graceful degradation**: If `check-prerequisites.js` fails or no feature exists, still answer knowledge-based questions. Simply note that no active feature was detected.
- **No hallucination**: If you don't know the answer, say so. Do not invent command options or workflow steps that don't exist.
- **Concise**: Keep responses focused. Avoid dumping entire knowledge base contents.
- **Contextual**: Adapt the level of detail to the user's apparent expertise. If they ask a basic question, give a simple answer. If they ask an advanced question, provide deeper context.
- **Link-rich**: When knowledge sources contain documentation links, always surface them in the response to help users find authoritative references.
- **Fallback search**: When local knowledge is insufficient, suggest the user try `tiksearch` or `lark-docs` MCP tools for more information.
