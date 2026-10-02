---
name: sites-hosting
description: Host websites with Sites. Always use after `sites-building`, and use for website publishing, deployment, hosting management, or projects containing `.openai/hosting.json`.
---

# Sites hosting

Publish the exact validated source with the shortest safe sequence. Use native Sites connector calls directly, following their descriptions for arguments and archive requirements. Treat IDs and cursors as opaque and copy them unchanged from the selected Site's manifest or tool responses.

Read `references/environment.md` for this environment's native tool calls, archive delivery, and final handoff.

## Site lifecycle ownership

Only the Site-owning agent responsible for the user's requested Site may run `sites-hosting`, call `create_site` or any other Sites tool, edit the Site checkout or `.openai/hosting.json`, obtain source credentials, commit or push, save a version, deploy, or perform the final browser handoff. A spawned subagent must return its assigned image, asset, or research result without invoking this skill or any Sites tool. An independently started background or invisible task that owns the requested Site remains its Site-owning agent.

## Communicate clearly

Assume the user is a nontechnical knowledge worker. Keep source control, credentials, IDs, commits, branches, archives, versions, packaging, connector calls, and deployment polling out of user-facing messages. Usually send one update when publishing begins, then the final URL or a plain-language blocker.
For example: `Your site is ready. I’m publishing it privately now.`

## Rules

- Publish after a successful build unless the user requested local-only work.
- Publishing does not require additional browser testing or visual QA. Preserve the existing Site tab as its single user-facing view; a failed browser handoff does not block publishing.
- Treat `public/screenshot.jpeg` as an optional deployment thumbnail. Preserve an existing file. Create or refresh it only when the user explicitly requests a Sites deployment thumbnail; a generic screenshot request does not count. Missing or failed capture never blocks validation, version saving, or deployment.
- Store only `project_id`, optional `static` configuration, logical `d1` and `r2` bindings, and requested supported `capabilities` in `.openai/hosting.json`. Manage runtime values through Sites.

## Fast publish sequence

1. Reuse the successful build from `sites-building` when the source has not changed. Rebuild only when needed.
2. Reuse the `project_id` and source write credential from early registration in `sites-building`; obtain a fresh credential for the same Site if it is absent or expired. For a new Site being hosted directly without prior registration, call `create_site` once, persist its `project_id` in `.openai/hosting.json`, and retain its source write credential. Retry creation only when the error explicitly identifies a temporary failure or slug conflict; treat quota, permission, and access errors as terminal. Resolve an ambiguous creation outcome before retrying instead of guessing another slug.
3. Use a Git repository rooted at the selected Site project, initializing one there if needed; do not commit or push an unrelated parent repository. Commit the exact validated source. Push it with the returned credential as a per-command HTTP authorization header. Keep the credential out of remote URLs, Git configuration, files, and user-facing output. Use the pushed branch-head SHA as `commit_sha`.
4. Package with this plugin's root-level `scripts/package-site.sh` helper, passing the project directory and archive path. It stages the Worker build or explicitly configured static public output into `dist/`, includes hosting metadata and any Worker migrations, validates required files, and creates the archive.
5. Save one version with the connector using that `commit_sha` and archive.
6. Choose deployment from the site's current access, not tool availability. A site created in this flow remains owner-only until its access changes, so use `deploy_private_site_version` for that case. For an existing site, call `get_site` before deployment and use `deploy_private_site_version` only when `current_user_role` is `owner` and `access_policy` verifies `access_mode: "custom"`, exactly one `allowed_account_user_ids` entry, zero `external_visitor_count`, and no workspace or tenant group IDs. Treat missing or ambiguous access as not verifiably owner-only. For a shared, public, or not verifiably owner-only site, ask for approval naming the resolved access level, such as `Publish publicly` or `Publish to existing shared access`, plus `Not now`. Use `request_user_input` only when available and permitted for approvals; otherwise ask in the conversation. Wait for the response, and call `deploy_site_version` only after approval. If a private deployment returns `site_not_owner_only`, do not retry it; follow this approval path.
7. Poll `get_deployment_status` directly until deployment succeeds or fails. Use discovery calls only when an error requires them.

## Existing sites and advanced capabilities

- Reuse an existing `project_id` and valid source credential when available.
- If a credential is absent or expired, obtain one with `create_source_repository_write_credential` and reuse it until expiry.
- If the D1 schema changed, ensure generated migrations are present before packaging.
- For server-backed builds, require `dist/server/index.js`, static assets when emitted, `dist/.openai/hosting.json`, and `dist/.openai/drizzle/**` when migrations exist.
- For static-only builds, require an `index.html` in the public output directory selected by `static.directory` in `.openai/hosting.json`. The helper normalizes that output to `dist/` and rewrites the archived `static.directory` to `dist`. Static builds cannot use runtime bindings, capabilities, or migrations.
- For non-vinext server-backed projects, use the established Cloudflare Workers-compatible build output and adapt staging only as required by the connector contract.

## Handoff

After `get_deployment_status` reports `status: "succeeded"`, follow the Handoff section in `references/environment.md` to show the exact deployed URL. Preserve the existing Site view after subsequent fixes and redeployments.

Then return the deployed Sites URL and a concise description of what the user can do. If the deployment is unsuccessful, do not perform the success handoff; explain the user-visible reason and next step. Keep source credentials and temporary archives private. Do not include file paths, commands, build details, IDs, commits, or version information unless the user asks.
