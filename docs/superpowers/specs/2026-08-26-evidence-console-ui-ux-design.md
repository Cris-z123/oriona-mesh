# OrionaMesh Evidence Console UI/UX Design

## Goal

Make OrionaMesh feel like a quiet, precise knowledge-work tool: users can orient themselves in a knowledge scope, understand the state of their materials, and read source-grounded answers without navigating a generic AI chat or a crowded admin interface.

## Scope and invariants

- This redesign covers authentication, workspace navigation, knowledge bases, documents, conversations, citations, and shared feedback primitives.
- The existing REST/SSE contracts, server-authoritative authorization, document state machine, task queue behavior, and model egress boundary do not change.
- The document queue issue observed during audit is explicitly out of scope: it is already resolved by the user.
- No fictitious document counts, processing percentages, citations, or authorization conclusions may be rendered. UI derives facts from existing DTO fields or remains silent.
- Existing product colors remain the semantic source of truth: `#F7F6F1` warm background, `#FFFEFA` surface, `#202625` foreground, `#0C625D` primary/reliable signal, `#534CA5` evidence clue, and existing destructive tokens.

## Visual language

The product is an **Evidence Console**. Its technology feel comes from a stable spatial system, restrained monospaced metadata, exact source references, and a single evidence rail—not gradients, glass, neon, starfields, or dashboard metrics.

- Use the existing light theme as the default and its existing dark theme as the equivalent night-editing mode.
- Keep the display face restrained to page titles; use the existing system sans stack for reading and `ui-monospace` only for timestamps, source positions, small labels, and shortcut hints.
- Use iconography for compact repeated navigation and actions. Nav icons retain visible labels on expanded desktop and mobile sheets; icon-only buttons always expose accessible labels and tooltips.
- Replace numeric sidebar markers with Lucide library and conversation icons. Make “new conversation” an icon-only `SquarePen` action with an accessible name.
- The evidence rail is the one distinctive device: a narrow indigo vertical line and rank marker associates a grounded answer with its evidence rows. It never denotes errors or primary actions.

## Information architecture

### Application shell

Desktop has a 240px fixed navigation rail. Its order is product mark, primary navigation, a clearly labelled “最近对话” section only on conversation routes, and account controls at the bottom. The mobile sheet preserves exactly that grouping and labels the history rather than blending it into navigation.

The main content begins with a concise page header and a single local primary action. It must never render a second “context rail” or a permanent right sidebar. At 1024px and below, the shell becomes an accessible sheet; all actions remain reachable.

### Authentication

Authentication remains purposefully small. On wide screens, a quiet brand signal and the statement “私有资料，基于来源的答案” anchor the form without introducing marketing panels or unrelated navigation. Mobile reduces this to a compact mark and title.

Login and registration use identical field spacing, inline validation, submit-pending labels, and standard error panels that retain safe diagnostic trace IDs. Registration success lands on login with an explicit success confirmation. Password visibility controls have accessible labels.

### Knowledge bases

The knowledge-base page uses a header with an icon-only “新建知识库” control; creation happens in a dialog. The list is a calm set of full-width rows with a clickable title, description, and a trailing action menu. “编辑” and “删除” do not compete with “打开资料” as visible peer buttons. Danger actions remain confirmable.

Empty states contain the same new-library action. Error and invalid deep-link states use the shared error component and provide a safe return route.

### Documents

The document workspace header includes a back-to-library link, the knowledge base name, and an icon-only “上传资料” control that opens a sheet. The drop zone is displayed inline only as the empty-state equivalent, not permanently above every list.

Each document row presents filename, server-reported status, current task stage when supplied, and relevant updated time. Details and delete actions are trailing-menu actions; delete is never the most visually prominent control. The detail sheet contains a plain status summary first, then a clearly separated, collapsible “处理记录” for technical attempts and timestamps. Failed state handling remains driven solely by `allowed_actions` and service DTO errors.

### Conversations and citations

An empty conversation requires a knowledge-base picker and states why. Once a conversation exists, its bound knowledge base is a read-only context label; the creation picker is not rendered, preventing accidental scope clearing or a loading flash of “请选择知识库”.

The answer thread is capped at a 704px readable column, while the composer matches that width. User messages remain compact right-aligned bubbles. Assistant messages are unframed reading text with semantic paragraph, heading, list, and code rendering where the stored response format supports it.

For grounded answers, the assistant answer begins beside the indigo evidence rail. A small, text-based generation state appears during SSE streaming. Citation rows immediately follow the relevant answer in a single quiet list: rank, source name, quoted or locating summary, and page/section. They open the existing citation drawer. The rail and citations use `clue`, not `primary`.

The conversation history title, preview title, knowledge-base label, and recency metadata make blank or duplicate titles distinguishable. Existing global pagination and cross-knowledge-base restoration remain unchanged.

## Interaction and accessibility

- Use existing Radix/Shadcn primitives for dropdown menus, dialogs, sheets, tooltips, and destructive confirmations; remove hand-rolled menu behavior.
- Every icon-only control has an accessible name. Menus, drawers, dialogs, and citation sheets retain Escape, focus trap, and focus return behavior.
- Prefer state-specific wording: “已创建知识库”, “正在基于已完成资料生成”, “未找到相关证据”, “已取消生成”. Do not use generic “提交” or non-specific success messages.
- Respect reduced motion. Motion is limited to drawer/dialog transitions, a non-essential streaming cursor, and no continuous decorative animation.
- At desktop, layout density comes from rows and whitespace, not multiple bordered cards. At narrow widths, rows wrap predictable metadata before actions and preserve 44px touch targets.

## Component boundaries

- Add a focused `PageHeader`/action pattern rather than duplicating title/action layout in routes.
- Keep domain data fetching in existing Query hooks. New visual components consume frozen DTOs and do not copy server entities to Zustand.
- Extract shared document row metadata and conversation context presentation as focused domain components when they prevent repetitive conditional markup.
- Keep `AppShell` responsible only for layout and local navigation state; route pages decide which local action and content header to render.

## Verification requirements

Component tests must be written before behavior changes and cover at least:

1. Authentication success feedback, field errors, pending state, and accessible password toggle.
2. Knowledge-base creation dialog, row action menu, safe navigation, and invalid-detail fallback.
3. Upload-sheet trigger/empty-state equivalence, document row metadata, hidden destructive action, and technical-record disclosure.
4. Existing conversation’s read-only bound context, new-conversation picker, labelled history, icon accessibility, and narrow-screen shell equivalence.
5. Evidence rail/clue semantics, compact citation list/drawer invocation, streaming copy, readable message width, and keyboard paths for all new menus/dialogs.

Run affected Vitest suites, frontend lint, format check, typecheck, and live browser checks at the current narrow viewport plus 1280px desktop. No end-to-end framework is introduced.
