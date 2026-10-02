---
name: sites-building
description: Use Sites to build websites, including landing pages, portfolios, dashboards, portals, trackers, hubs, and internal tools. Always use Sites when the project contains `.openai/hosting.json`.
---

# Sites building

Build the complete requested site, validate it, then use `sites-hosting` unless the user explicitly asks to keep it local.

Read `references/environment.md` before setup for this environment's project, starter, and preview instructions.

## Site lifecycle ownership

The Site-owning agent is the agent responsible for the user's requested Site, including an independently started background or invisible task. Only the Site-owning agent may initialize or edit the Site checkout, update `.openai/hosting.json`, run `sites-building` or `sites-hosting`, call `create_site` or any other Sites tool, obtain source credentials, save versions, deploy, or perform the browser handoff. A spawned subagent is never the owner of its parent agent's Site: give it only its explicitly assigned image, asset, or research task, and require it to return its result without invoking either Sites skill, calling Sites tools, initializing a Site, editing the Site checkout, or spawning another agent.

## 1. Start with the project

### Choose the execution path

Use the **one-shot fast path** only when all of these are true:

- this is a new site in an empty or projectless workspace;
- one route can satisfy the request;
- the request does not require D1, R2, uploads, app-owned authentication, external connectors, or browser UI QA; and
- the normal deliverable is a private deployed URL.

Use the **capability path** otherwise. This includes existing-site changes, multi-route sites, persistent data, uploads, authentication, external data, and requested browser testing.

### Start new projects immediately

For a new site in an empty or projectless workspace, make setup the first task action using the Project setup section in `references/environment.md`. Infer the capabilities required by the user's request; do not ask users to choose technical add-ons.

Install the generated project's dependencies with its package manager using the environment's setup instructions, and retain the session until installation completes. As soon as the generated files exist, inspect the minimum required files and begin the bounded first product slice below while installation continues. Do not run a second initializer over the project.

After setup starts, follow **Start image work early** as soon as each required image brief is clear.

For a new Site that will be hosted, check `.openai/hosting.json` as soon as the scaffold or retained-template source is in place. Reuse any existing `project_id`, including in a resumed unpublished checkout; otherwise call `create_site` once while dependency installation and authoring continue. Skip registration entirely for local-only work.

Persist the returned `project_id` immediately in `.openai/hosting.json` and retain the source write credential for hosting. Keep credentials out of files, Git configuration, remote URLs, and user-facing output. Reuse the same Site after installation or build failures. Retry creation only when the error explicitly identifies a temporary failure or slug conflict; treat quota, permission, and access errors as terminal. Resolve an ambiguous creation outcome before retrying instead of guessing another slug.

Registration leaves the Site private and unpublished. Save a version, deploy, and hand off the deployed URL only through `sites-hosting` after the complete requested Site passes validation.

Follow the Development and first preview section in `references/environment.md` as soon as setup finishes. Where a user-facing local preview is supported, keep the browser closed until the **First meaningful preview** gate below passes. Any generated loading skeleton is a fail-safe only and must never be the intended browser handoff. Keep any development server alive through build and hosting.

A Site-owning agent running in an independently started background, delegated, or invisible task initializes normally but does not start a browser-only preview unless its task otherwise needs the server. A spawned subagent working for that Site-owning agent never initializes a Site checkout.

## 2. Design the experience

Keep this planning lightweight and internal. Make these decisions while project setup continues, and revise them together when implementation reveals a better direction. Do not turn design planning into a mandatory interview or approval gate, generate design options, or pause for visual selection unless the user explicitly asks to compare designs.

### Frame the product and scope

Determine:

- who the site is for and the primary task they need to complete;
- the essential content, functionality, and requested capabilities; and
- the smallest coherent scope that fully satisfies the request without speculative features.

For a new Site, implement only the requested content and capabilities, plus the minimum structure, accessibility, responsive behavior, and basic document metadata needed for that experience to work. “Polished” changes execution quality, not product scope. Do not add sections, calls to action, routes, forms, search, filters, sharing, persistence, authentication, uploads, data, or workflows merely because they are common or easy to add. Add optional capabilities only when requested. For an existing Site, preserve its capabilities unless the requested change requires altering them; do not add new ones without a request.

Choose the dominant presentation mode from the user's intent:

- **Working surface by default:** When the primary goal is to explore, compare, monitor, decide, or act—especially for personal or internal use. The first viewport must expose core controls and at least one useful result when relevant; keep framing brief and secondary.
- **Narrative surface when intended:** When the primary goal is to publish, persuade, teach, sell, or tell a story.
- A topic resembling a report, review, or article does not by itself imply narrative intent.

Infer these decisions from the request and existing product when possible. Ask one concise group of up to three discovery questions only when important context is missing and the unresolved details would materially affect functionality or force a risky assumption. Otherwise proceed immediately with best judgment.

**Build the requested experience itself, not a page advertising it.** Unless the user asks for a landing or marketing page, make the primary activity the visual and functional focus of the first screen. A game should open on the play area or a game-native start screen that leads directly into play; a calculator should show editable inputs and results; an editor or dashboard should open on its workspace or data. Don’t make users scroll past an oversized hero, slogan, feature list, or decorative mockup—or click a generic “Get started” button—just to reach what they asked for. Brief context or necessary setup is fine when it supports the task and stays secondary. Before finishing, check: can the user immediately begin the activity they asked for?

### Shape the experience

Make a lightweight implementation plan:

- Identify the primary flow and what the first viewport must show or enable.
- Add routes and navigation only when the request requires multiple views.
- Account for relevant loading, empty, error, and success states.
- Choose layout, density, and responsive behavior around the primary task; working surfaces must not put a marketing or editorial hero before it.

**Write all visible text for the people who will actually use or read the result**. Think about what they already know and what they need to understand, decide, or do next. Use plain, specific language grounded in the user’s context. Cut filler, hype, unexplained jargon, repeated headings, and copy that states the obvious. Don’t narrate the interface, describe its styling, announce what you built, or address an evaluator. Don’t add a tagline, subtitle, or explanatory block just to fill space. Keep useful labels, brief instructions, and enough detail for the task; use marketing language only when it fits the request. Before finishing, reread the text from the audience’s perspective and remove anything they wouldn’t miss.

### Choose the implementation stack

Use the inline **Reuse installed components** guidance for matching interface primitives; do not read a separate guide merely to select them. Consult [Library selection](references/library-selection.md) only when a requested capability needs a library choice beyond those primitives. Its other library choices are recommendations; preserve the product requirements and existing project.

Preserve existing dependencies and the lockfile unless the requested work requires a change. Reuse suitable declared versions, avoid pruning unused packages as routine cleanup, and update the lockfile for any required dependency changes.

### Establish the visual direction

Before the first product-source edit, choose one concise visual thesis from the request's subject, audience, and tone. Let it drive page silhouette, typography, surfaces, spacing rhythm, and imagery, with a coherent palette, borders, corners, icons, and motion. Decide quickly and internally without delaying editing. Different briefs should produce meaningfully different compositions, not the same structure with new copy and colors. For polished or strongly visual work, make at least one memorable, request-appropriate visual decision without inventing content, sections, capabilities, or actions. Carry the direction through routes, responsive and interaction states, and later edits.

**Keep text readable.** Use 16px or larger for main body text. Use 14px as the default minimum for labels and other text people use regularly. Reserve 12–13px for secondary metadata and avoid sizes below 12px. If a due date or status is essential to the task, treat it as regular text. Prefer `rem`, respect browser font settings, and keep content and controls usable at 200% text enlargement. These are Sites defaults, not WCAG-mandated font sizes. Check the actual typeface, weight, line height, contrast, writing system, and viewing conditions together.

**Choose tasteful fonts, colors, and image styles.** Never use the generated shadcn default theme as the finished theme of a new site. Choose an intentional theme based on the product. If the user provides no visual direction, infer one. For an existing site, preserve and extend its established brand and theme unless the user requests a redesign. Avoid defaulting to washed-out palettes of warm off-white, beige, sage, dusty coral, or pale lavender. Use them when they fit the user's references, requirements, or existing brand. If the user asks for a new design direction, change more than just the colors.

**For imagery, do:**

- Use HTML, CSS, and SVG for functional interface styling and geometry, simple non-representational accents, trusted icons, diagrams, and data visualizations.
- Choose **0–3 discretionary final-site images**: use 1–3 for visually led marketing, brand, editorial, portfolio, consumer, or storytelling Sites; use zero for technical, data-heavy, dashboard, admin, developer, or other utilitarian Sites when typography, layout, icons, or data visualization carry the design. For inherently visual consumer subjects such as pets, food, travel, fashion, and homes, include at least one relevant in-page image unless the user requests an image-free direction.
- This discretionary budget does not cap suitable user-provided assets, explicitly requested images, or the content of a requested gallery, catalog, portfolio, or similar experience. Social-preview images and deployment thumbnails are separate explicit-request-only workflows.
- Prefer suitable supplied assets, web image search for real or factually specific subjects, and `imagegen` for original or stylized artwork. Generate clean standalone assets rather than screenshots containing page text or interface chrome.
- Use asset-only subagents for web image search and `imagegen`; the Site-owning agent selects and integrates results.

**Do not:**

- Build representational images or decorative artwork, including illustrations, objects, or scenes, from styled HTML, CSS shapes, pseudo-elements, or hand-written SVG.
- Add imagery that does not support the site's purpose.

### Start image work early

Once setup starts and an image brief is clear, dispatch bounded image search or generation while continuing independent Site work. Use web image search for real or factually specific subjects and `imagegen` for original or stylized artwork; never invent URLs, replace requested factual imagery with generated art, or repeat work when a suitable asset already exists.

For the default 1–3 generated in-page assets, use exactly one image-generation subagent with one request per chosen asset, together as one parallel batch when supported. Do not generate variants or retry in-page generation. Explicit requests for additional generated images take precedence over this default. Have the subagent save outside the Site checkout and return assets to the Site-owning agent for inspection and integration. When delegation is unavailable, the owner makes the same bounded requests using the synchronous fallback below. Explicitly requested social cards follow their separate retry allowance in **Social previews**.

Start asset-only subagents with `fork_turns="none"` and only the subject, factual requirements, placement, dimensions, and visual direction. They return candidate assets and, for search, source-page and image URLs plus available reuse information; they must not edit the Site, call Sites tools, invoke Sites skills, initialize projects, or spawn agents. If concurrency is unavailable, finish the preview slice's independent work first. Request synchronously only the images needed to make that slice coherent, show the preview where supported, then request the remaining required images; omit optional generation that would delay delivery. Never invent asynchronous jobs.

Reserve stable image dimensions and continue useful work instead of waiting or polling; optional images must not delay the first product-source edit or a supported preview that already meets the **First meaningful preview** gate. When independent work finishes, collect required results rather than treating pending work as failed. If optional images are not ready and useful when the Site is otherwise ready, omit them and remove their placeholders instead of delaying delivery. Images explicitly requested by the user or required by the visual-consumer rule are not optional: integrate the selected assets or a permitted fallback, or report the Site as incomplete. Verify selected images, their sources, and loading, then integrate required assets and applicable metadata before the final build; never ship unresolved placeholders. Handle requested social-card failures under **Social previews**.

### Keep authoring on the delivery path

As soon as project files exist, decide the requested scope and visual thesis, then make the earliest coherent product-source edit while installation continues. Do not draft the full page twice, add a planning-only round, inspect speculative files, or create alternate candidate pages unless requested. Spend polish on execution inside the requested scope. Overlap useful image work with implementation and validation; do not wait on optional imagery or add unrequested features.

These shortcuts never skip Site registration for hosted work, dependency installation, the production build, packaging, deployment, or terminal deployment-status verification. Preserve the complete-site publication flow and any supported first meaningful local preview.

## 3. Build, preview, and deliver

### Apply the selected theme

Apply the selected theme through the shared tokens in `app/globals.css` before styling individual components. Update both light and dark theme values when both are present.

### Reuse installed components

The standard Vinext starter includes the supported Shadcn catalog. For a new Site from that starter, a requested control with a direct catalog match must use the matching primitive on its first implementation. Map side navigation to `sidebar`, tabbed views to `tabs`, modal flows to `dialog`, detail panels to `sheet`, destructive confirmations to `alert-dialog`, searchable pickers to `combobox`, command menus to `command`, boolean choices to `switch` or `checkbox`, constrained choices to `select` or `radio-group`, ranges to `slider`, contextual actions to `dropdown-menu`, hover help to `tooltip`, verification codes to `input-otp`, tables to `table`, progress to `progress`, page navigation to `pagination`, empty/loading states to `empty`/`skeleton`, and transient feedback to `sonner`. Import directly from `@/components/ui/<component>` and compose/restyle at the call site. For existing or template-derived projects, reuse matching primitives only when already present and preserve existing import paths.

Select by semantic match without catalog inventories, mandatory guide reads, or proactive component scans. If the selected API is unclear for the next edit, open only that implementation; unfamiliarity is not a reason to hand-build a substitute. Use semantic HTML and custom React/CSS for layout, art direction, substantial data visualization beyond the chart wrapper, and uncovered UI. Do not invent features or state to exercise a component or meet a quota.

Do not run the Shadcn CLI, install another component package, change dependency manifests or lockfiles, or edit vendored `components/ui` files merely to obtain, recreate, or restyle an already-installed primitive. Compose and style it at the call site while preserving accessibility and interaction behavior.

### First meaningful preview

Where the environment supports a user-facing local preview, treat it as an early milestone in both execution paths. Otherwise skip this handoff and continue implementation and any explicitly requested social previews; never deploy an incomplete slice as a substitute. In a visible foreground thread with local preview support, open it as soon as, but not before, all of these are true:

- the route contains the smallest coherent slice that lets a reasonable person recognize the requested site and its intended visual direction;
- for a new site, the shared theme tokens reflect the selected visual direction rather than the generated defaults;
- it includes the primary product surface or layout and representative, product-specific content rather than an untouched starter, generic skeleton, blank page, or loading-only state;
- the primary affordance is visible when the requested experience is interaction-led; and
- the development server has successfully compiled the slice and the route responds without a blocking runtime error.

The slice may be static or partially inert. Keep it intentionally bounded and defer secondary routes, complete data models, exhaustive interactions, responsive refinements, animation, polish, and advanced capabilities until after the handoff unless one is required for recognition, security, or a successful render. Work on the slice while installation runs when possible.

For a new site, replace the generated placeholder content and temporary preview metadata, if present, as part of the slice. Cleanup of unused starter-only files may happen after the handoff, but must finish before final validation.

For a supported local preview, once the bounded slice is applied, make no further planned product-source edits before the handoff. Fix only compilation or blocking runtime failures, then follow the Preview handoff section in `references/environment.md` to show the first working version without waiting for the complete Site or a deployment. Reuse that preview as the Site's single continuous user-facing view through HMR, publishing, and any later fixes.

For an existing site, use its current coherent experience immediately when it still represents the requested product and compiles. If the request changes the primary direction, apply only the smallest representative part first. Preserve the last working content while changes compile; never replace an existing site with the starter skeleton.

### WebMCP tools for new sites

Apply to new sites where the primary journey lets users modify data or meaningful page state, or where a structured tool materially helps an agent complete that journey. Skip static or presentation-only sites whose user journey is limited to reading content or navigating between pages.

After the first meaningful preview and before the final build, read and apply [WebMCP tools](references/webmcp.md) to add a small task-completing imperative WebMCP surface. Do not add speculative tools or delay the first preview to implement them.

### Social previews

Generate or refresh a social-preview image only when the user explicitly requests a social-preview or social-sharing image. Otherwise preserve any existing preview image and its metadata unchanged. If none exists, omit it; do not start image generation, add `public/og.png`, or add social-preview image metadata. A missing preview or branding change alone does not authorize generation. Apply this rule in both the **One-shot build** and **Capability path**.

1. **Start early and keep building.** When a new card is explicitly requested, choose its title or primary headline, concise supporting copy, and visual direction early, then follow **Start image work early**. Make one `imagegen` request for a cohesive branded landscape card with that exact title or headline and copy as legible typography, matching the Site's palette, typography, and distinctive motifs while excluding credentials and private data. When delegating, give one `fork_turns="none"` subagent only that brief; have it save the result outside the Site checkout and return the path. Check the card against final copy, branding, and metadata before the final build. Retry or replace at most once total for unusable or stale output; treat any still-invalid card as generation failure.
2. **Wire the site-wide preview.** When a new card is generated, the Site-owning agent saves it as `public/og.png` and sets site-specific Open Graph and X title, description, and image metadata in `app/layout.tsx` or the framework's equivalent metadata entry point. Use an absolute URL from a trusted request or deployment origin; never blindly trust forwarded host headers. If generation fails, preserve any existing valid preview; omit `og:image` only when neither an existing nor generated image is available. Never use a generic fallback. Run the final build after wiring the asset.
3. **Handle requested item-specific previews.** When the explicit social-preview request covers independently shareable detail pages, use `generateMetadata` or its equivalent to set page title and description and Open Graph/X title, description, and image from the rendered record. Reuse its existing primary image with an absolute trusted-origin URL; otherwise clear both inherited Open Graph and X images. Never reuse `public/og.png` or generate images per record. Validate the root and every detail page when there are at most two; otherwise check at least two representative detail pages. Before final validation, verify that each checked page's title, description, and Open Graph/X fields match its record.

### One-shot build

After setup and any necessary clarification, show the first meaningful preview if the environment supports a user-facing local preview, then build and deploy the complete site in one focused pass.

1. Start by inspecting the generated project's instructions, package scripts, primary page, layout, stylesheet, and `.openai/hosting.json`. For the Vinext scaffold, these include `app/page.tsx`, `app/layout.tsx`, and `app/globals.css`. Read other files only when the implementation needs them. Avoid broad scans and speculative research. Preserve the package manager and lockfile.
2. Start required image work under **Start image work early**, then continue the smallest coherent product slice while dispatched work runs. Where a user-facing local preview is supported, complete the **First meaningful preview** handoff above before broadening the implementation. For a genuinely trivial request, the complete implementation may itself be that slice; do not manufacture extra edits merely to demonstrate HMR.
3. Reuse the project setup and any retained development server or Site tab, then make one complete product patch. Prefer one page component and one stylesheet. Include all requested content, interactions, responsive behavior, keyboard and touch behavior when relevant, and accessible labels. Replace the generated placeholder content and metadata with the requested site's own values, remove unused starter-only files, and update starter icons when appropriate before the final build unless the user explicitly asked to work on the starter itself. Integrate any explicitly requested **Social previews** result before the final build.
4. As soon as implementation is complete, run the project's build script while any retained development server stays alive. Fix actual build failures, then rerun it. Run lint separately only if the build omits compilation or the user asks.
5. Follow the shared preview rules below.
6. Continue to `sites-hosting`. Avoid an unnecessary polish pass after the build succeeds.

### Capability path

#### Project setup

- For a new site, use the setup flow in **Start new projects immediately** and preserve the generated project's structure.
- For an existing site, preserve its package manager, lockfile, scripts, architecture, and `.openai/hosting.json`. Install only when dependencies are absent. Do not replace a working structure merely to use the starter.
- Keep site code within the selected project surface.

#### Expand the design consistently

- Where a user-facing local preview is supported, apply the bounded slice and complete the **First meaningful preview** handoff above before comprehensive implementation.
- Start required imagery and explicitly requested social cards under **Start image work early**. Integrate required results before final validation, and omit optional assets that would delay delivery.
- Build the first viewport around the requested product, not generic dashboard chrome.
- For a new site, replace the generated placeholder content and metadata and remove unused starter-only files. Set the finished site's title and description through its framework's metadata API before final validation. Preserve starter content only when the user explicitly asked to work on the starter itself.
- Use concrete, product-specific copy and realistic data.
- Apply the chosen UX, layout, and visual rules consistently without making every page mechanically identical.
- Avoid speculative features and unnecessary client state.
- For server-backed builds, follow the Starter capabilities section in `references/environment.md` and produce Cloudflare Worker-compatible ESM output. Require the Worker entrypoint (`dist/server/index.js` by default) to export a default object with callable `fetch(request, env, ctx)`; if Cloudflare reports no registered event handlers, fix source/build and create a new version instead of redeploying the same archive.

Static-only builds without runtime bindings, capabilities, or migrations may publish Cloudflare-compatible static output without a Worker. Examples include `dist/index.html`, Next.js exports (`out/`), and vinext exports (`dist/client/`). All static-only builds must set `static.directory` in `.openai/hosting.json` to a supported public output directory (`dist`, `dist/client`, `out`, `build`, or `.output/public`). For Next.js/vinext exports, use `output: 'export'`; select only public assets, excluding any server intermediates.

#### Add only requested capabilities

- For durable state, records, uploads, or other persistence, read [Persistence and storage](references/persistence-and-storage.md).
- For any SQLite schema or query work, also read [SQLite](references/sqlite.md).
- For identity-aware or sign-in-gated behavior, read [Authentication](references/authentication.md).
- Hosted Sites do not support raw TCP sockets (`connect()`); use HTTP-based clients or APIs for external databases and services.
- Use browser storage only for device-local preferences or explicitly local state.
- Keep logical D1 and R2 declarations in `.openai/hosting.json`; Sites owns the real Cloudflare resources and deployment wiring.
- Keep local `.env` and `.env.example` keys aligned. Manage hosted runtime values through Sites.

#### Validate capability work

- Run the deployment build once after the complete implementation. If a D1 schema changed, generate and inspect its migration. Fix real failures before hosting.

### Preview rules

- In a visible foreground thread with user-facing local preview support, the **First meaningful preview** gate is the only local opening point. If the gate has not passed, keep the browser closed; never open the skeleton as a fallback. If the handoff fails after the gate passes, report it and continue.
- For an existing site, preserve its normal package and development flow.
- In a delegated, background, or invisible thread, skip the user-facing browser handoff and say why.
- Perform no screenshots, DOM inspection, clicking, resizing, or visual QA unless the user explicitly requests browser testing. For requested QA, follow the Browser testing section in `references/environment.md`.
- Do not scan ports or repeatedly open the browser.

### Hosting handoff

Use `sites-hosting` after validation. Do not finish with only a local build unless the user requested local-only work. Return the deployed Sites URL as the primary deliverable. Do not include file paths, commands, or validation jargon unless the user asks. Keep any development server running until hosting finishes, then stop it during final teardown using the environment's preview controls.

## Communicate with the user

Assume the user is a nontechnical knowledge worker. Talk about their site, choices, progress, and results. Keep tools, commands, files, runtimes, browser software, permissions, dependencies, source control, credentials, IDs, builds, and deployment internals out of user-facing messages unless the user asks or must take action.

Use no more than one short update for each user-visible phase: preparing the site, building it, and publishing. If a phase takes longer than 60 seconds, give one plain-language update. Keep recoverable technical problems private; say only that you hit a problem and are trying another method.
