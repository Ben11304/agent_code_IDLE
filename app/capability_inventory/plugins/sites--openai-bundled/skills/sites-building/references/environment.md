# Codex environment

## Hosted runtime

Hosted server code runs in Cloudflare Workers, with **128 MB of memory per isolate**, shared across concurrent requests and including JavaScript and WebAssembly allocations.

## Project setup

For a new site in an empty or projectless workspace, scaffold directly with `@openai/create-sites@0.3.0` through the environment's package manager and always include the `shadcn` add-on; the npm form is `npm create --yes @openai/sites@0.3.0 . -- --yes --add-ons shadcn --install`. Respect the environment's existing dependency-security and minimum-release-age policy. If it blocks the pinned release, report the blocker instead of changing versions or bypassing the policy. Do not force a different package manager. Use the current directory when it is a valid destination; otherwise choose an empty project directory without moving, deleting, or overwriting existing workspace files.

Always keep `shadcn` in the `--add-ons` value. Infer the other capabilities required by the user's request and append them in the same comma-separated value, such as `--add-ons shadcn,d1`. Consult the CLI's `--help` or `--list-add-ons --json` when needed instead of maintaining a separate template or capability catalog.

Install the generated project's dependencies with its package manager, either with the CLI's `--install` option or as a follow-up command. Preserve an existing project's package manager, lockfile, scripts, and architecture; install only when dependencies are absent. A retained source template is already the project scaffold: copy its sanitized source, including dotfiles, into an empty project directory and do not run another initializer over it.

## Starter capabilities

For server-backed builds, use `sites()` from `@openai/sites-vite-plugin`. For new sites with sign-in-gated routes, include the `auth` add-on and import the generated helpers from `app/chatgpt-auth.ts`.

## Development and first preview

In a visible foreground thread, start the project's development script in a retained session as soon as setup finishes. Stop the retained session during final teardown.

A Site-owning agent running in an independently started background, delegated, or invisible task initializes normally but does not start a browser-only preview unless its task otherwise needs the server. Skip `open_in_codex` in that case.

## Preview handoff

Make one lightweight non-browser request to the exact Local URL printed by the development server to force the current route to render. Require a successful compile and non-error response, but do not inspect the response body as visual QA. Then use `open_in_codex` to show that Local URL. Establish a stable browser-tab ID from the first preview and reuse it through HMR, publishing, and any later fixes.

## Browser testing

Use the environment's browser tools only when the user explicitly requests browser testing. Reuse the existing Site tab and development server; opening the user-facing preview does not itself require screenshots, DOM inspection, or interaction testing.
