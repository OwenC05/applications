# Design

## Source of truth
Status: Active. Date: 2026-10-05. Surfaces: private local application dashboard and browser-based deep onboarding. Evidence: .omx/specs/deep-interview-application-copilot.md, .omx/plans/application-copilot-mvp.md, application-copilot/API.md. Greenfield: no prior UI/assets/screenshots/tokens existed. These are reversible design choices, not user-imposed branding.

## Brand
Application Copilot. Calm, capable, evidence-led; appropriate for both engineering and finance. Trust signals: explicit profile, verified/pending badges, source links, timestamps, real offline/error states. Avoid magic-brain claims, hiring-manager mind-reading, gamified pressure and mass-apply imagery.

## Product goals
Help a person turn real experience and real employer research into strong reviewable applications. Non-goals: forced hour-long gate, hosted accounts, automatic submission, unverified personal claims. Success: resume onboarding; review factual memory; research role/company; inspect/edit drafts with evidence and record reviewed/submitted separately.

## Personas and jobs
Primary: UK student applying in technology/finance. Friends use their own local installation/profile. Need fast job shortlist, deep evidence discovery, career-story reuse, employer-specific reasoning, privacy/control and reliable status tracking.

## Information architecture
Persistent sidebar: Overview, Opportunities, Interview, Brain, Applications, Settings. Main header: current profile, sector filter/context and provider/offline badge. Overview shows next action and truthful counters. Opportunities includes manual role entry with real questions and research-ready company URL/JD. Brain has verified evidence and pending inbox. Application detail combines employer research, evidence match, draft answers and review/history. Settings holds consent, provider configuration instructions, export/delete scope.

## Design principles
Evidence before persuasion. Progressive disclosure over huge forms. Hour-long target, not coercion: save/resume/skip visible. Per-profile ownership never ambiguous. Company research and personal brain are different knowledge domains. Loading/error/empty states remain honest. Historical prose is inspectable, never automatically factual evidence.

## Visual language
Warm off-white canvas (#f6f7f5), white panels, deep navy sidebar (#14212b), teal accent (#127b70), muted graphite text (#28343d), amber pending, red error. CSS variable tokens own palette/spacing. System sans-serif stack; 14–16px body and 30–36px principal title. Eight-pixel spacing rhythm, 12px panel radii, subtle borders/shadows. Text-first cards and lightweight inline SVG icons; no raster assets/external fonts. Motion minimal 120–180ms and optional.

## Components
Sidebar/navigation with active state; profile select; provider/sector/status badges; statistic cards; next-action card; labelled inputs/textareas; primary/secondary/danger buttons; modal or inline forms; memory card with provenance/actions; source list; claim/inference tags; question progress and answer composer; application cards/detail; warning banners; copy feedback. Each handles disabled/busy/error/empty states. Single stylesheet, no parallel design system.

## Accessibility
Target WCAG 2.2 AA behavior; semantic headings/landmarks, actual buttons/labels, keyboard navigation, visible focus, status aria-live, adequate contrast, minimum 44px touch controls where practical. Never use color alone for verification. Focus moves appropriately after dialogs/actions; errors linked to affected fields. Respect prefers-reduced-motion.

## Responsive behavior
Desktop sidebar ~230px, main max-width ~1250px; 2–3 card columns. Under 900px use compact top navigation and 1–2 columns; under 600px stack forms/cards/detail, maintain readable 16px inputs and no horizontal scroll. Touch does not depend on hover.

## Interaction states
Initial empty profile prompts create profile or clearly synthetic demo. No provider is an offline state with setup instructions, not fake cloud success. Research button reports source retrieval/loading/error and missing official URL. Draft blocked/error if cloud research incomplete; offline templates explicitly labelled. Pending memory needs confirmation; stale draft warns and blocks review. Record reviewed and mark submitted are distinct actions. Every save persists server-side and shows failure if persistence fails.

## Content voice
Plain, encouraging and specific. Use Brain, Verified evidence, Pending confirmation, Company research, Inferred priority, Reviewed, Submitted. Say 'What the employer publicly emphasizes', not 'What the hiring manager secretly wants'. Describe gaps as questions, never confident invented answers. Explain raw local plaintext and cloud retention accurately.

## Editorial quality
Application drafts display an advisory assessment, actionable issues and expandable per-question example/angle plans. No numeric quality/hiring score. Distinguish Editorially assessed, Needs work, and Stale assessment from human Reviewed. Absent/offline assessments are explicitly unassessed. Unsaved prose edits immediately stale the visible assessment; reverting to the exact saved wording restores it. Expired/mismatched company research suppresses a fresh quality badge. Explain 3–5 cloud calls, latency and usage before generation. Final rewritten prose is critiqued anew; unsupported experience remains a question for the applicant.

## Implementation constraints
Dependency-free Node24 + vanilla HTML/CSS/browser ES module. Local API contract: API.md. All untrusted content escaped/textContent; no inline script/CSS needed; CSP self-hosted. LocalStorage may hold selected profile ID only, not brain/secrets. API key is server .env only; provider badge exposes configured/model, never key. Browser smoke/synthetic fixtures and responsive screenshots required if tooling permits.

## Open questions
- [ ] Later browser autofill platform coverage; owner product; out of current milestone.
- [ ] Later authenticated hosted product; owner product; no impact on local-first implementation.
- [ ] Real user's provider credentials/model access; owner user at local setup; no effect on offline/test implementation, live verification remains conditional.
