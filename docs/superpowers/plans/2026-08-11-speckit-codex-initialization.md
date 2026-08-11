# Speckit Codex Initialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Initialize this Git repository with GitHub Spec Kit's Codex skills integration for specification, planning, and task breakdown workflows.

**Architecture:** The `specify` CLI owns all generated Spec Kit files. It will merge its bundled templates and PowerShell helper scripts into `.specify` and install Codex-native skills under `.agents/skills`; no application source or Python/uv project configuration will be created.

**Tech Stack:** `specify-cli` installed through uv; GitHub Spec Kit; Codex skills integration; PowerShell; Git.

---

### Task 1: Verify the installed Specify CLI can execute

**Files:**
- Modify: none
- Test: `specify --help`

- [ ] **Step 1: Run the installed CLI help command**

Run: `specify --help`

Expected: exit code 0 and a command list containing `init`.

- [ ] **Step 2: Inspect the installed CLI version**

Run: `specify version`

Expected: exit code 0 and a version string.

- [ ] **Step 3: If either command fails, stop before changing project files**

Run: `Get-Command specify | Format-List Name,Path`

Expected: reports the executable path used by the current shell; retain the complete failing output for remediation rather than manually creating Spec Kit files.

### Task 2: Initialize the Codex skills integration

**Files:**
- Create: `.agents/skills/` (Codex Speckit skills managed by `specify`)
- Create: `.specify/` (Spec Kit templates, scripts, memory, and configuration managed by `specify`)
- Modify: none
- Test: `.agents/skills/` and `.specify/` exist after initialization

- [ ] **Step 1: Capture the clean pre-initialization change scope**

Run: `git status --short`

Expected: only the reviewed planning documents are present or the working tree is clean.

- [ ] **Step 2: Run the official non-interactive initialization command**

Run: `specify init --here --force --integration codex --integration-options="--skills"`

Expected: exit code 0; it reports Codex skills installed under `.agents/skills` and Spec Kit shared infrastructure installed under `.specify`.

- [ ] **Step 3: Confirm the generated top-level directories**

Run: `Get-ChildItem -Force .agents, .specify`

Expected: `.agents/skills` and `.specify` are present.

### Task 3: Verify the initial workflow surface

**Files:**
- Test: `.agents/skills/`
- Test: `.specify/memory/constitution.md`

- [ ] **Step 1: List the installed Codex Speckit skills**

Run: `Get-ChildItem -Directory .agents/skills | Where-Object Name -Like 'speckit-*' | Select-Object -ExpandProperty Name`

Expected: skills whose names include `speckit-constitution`, `speckit-specify`, `speckit-plan`, and `speckit-tasks`.

- [ ] **Step 2: Verify the project constitution template is available**

Run: `Test-Path .specify/memory/constitution.md`

Expected: `True`.

- [ ] **Step 3: Inspect the final change scope**

Run: `git status --short`

Expected: only Spec Kit-generated files plus the reviewed design and plan documents are shown; `LICENSE` remains unmodified.

- [ ] **Step 4: Commit the initialization**

Run: `git add .agents .specify docs/superpowers/plans/2026-08-11-speckit-codex-initialization.md && git commit -m "chore: initialize speckit codex workflow"`

Expected: one commit records the generated Speckit assets and plan without application code.
