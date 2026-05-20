# Per-User Notebook App + GitHub Fork Persistence

Consolidation of the prior `16_notebook_first_architecture.md` and `archive/17_admin_dashboard.md` plans, narrowed to the active scope. MCP/LLM work and task/workflow promotion are explicitly out of scope (the in-notebook AI angle stays parked until marimo's native AI primitives are evaluated; promotion happens naturally via the GitHub PR system once forks are in place — no separate "fork-to-pin" mechanism is needed).

## Goal

When a user logs in via GitHub OAuth, the admin app:

1. Ensures their Flyte project exists (already done).
2. **Forks the upstream `stargazer` repo to their GitHub account** (new).
3. **Deploys a single secondary "notebook app" scoped to that user** (new) — one app per user, hosting the user's own notebooks plus the curated community set.

The user's dashboard then renders three sections of notebook tiles — **Tutorials**, **Community**, **Workspace** — each with Edit / Run buttons that route into that user's notebook app. Persistence is the user's GitHub fork: workspace edits are written into `notebooks/workspace/` on a branch in their fork, and the natural promotion path is a PR from there into upstream — no bespoke "promote" UI needed.

## Already done in this plan's scope

- `src/stargazer/notebooks/community/scrna_pipeline.py` — full 452-line production scRNA notebook restored from `HEAD:src/stargazer/notebooks/tutorials/scrna_tutorial.py` (the version with the two-workflow design, multi-sample fan-out, side-by-side UMAPs).
- `src/stargazer/notebooks/workspace/.gitkeep` — empty workspace dir committed; users add notebooks here in their forks.

## Architecture

```
┌──────────────────────────────┐
│  Admin app (existing)         │
│  - GitHub OAuth                │
│  - Project create / ensure     │
│  - Fork stargazer to user (+)  │
│  - Deploy per-user notebook app│
│  - Dashboard (tiles)            │
└────────────┬─────────────────┘
             │
             ▼  one per user
┌──────────────────────────────────────┐
│ Notebook app (new, per-user)         │
│  - Clones <user>/stargazer on boot   │
│  - Surfaces:                          │
│      tutorials/  (read, ships in image)│
│      community/  (read, ships in image)│
│      workspace/  (read+write, from fork) │
│  - marimo edit / run                  │
│  - Pushes workspace edits back to fork│
└──────────────────────────────────────┘
```

### Two-app model

- **Admin app** is shared infrastructure (one deployment for the whole org). Continues to live in `app/admin_app.py`.
- **Notebook app** is per-user (one deployment per logged-in GitHub user, in their `sg-<username>` Flyte project). Defined once as a `flyte.app.AppEnvironment` and `serve.aio()`'d per user with their context baked in.

### Repository layout

```
src/stargazer/notebooks/
├── tutorials/             # existing — small teaching notebooks (in image)
│   ├── assets_tutorial.py
│   ├── preprocessing_tutorial.py
│   └── tasks_tutorial.py
├── community/             # NEW — curated production notebooks (in image, committed to main)
│   └── scrna_pipeline.py
├── workspace/             # NEW — fork-only user scratch (empty on main, just .gitkeep)
│   └── .gitkeep
└── byod.py                # existing
```

- `tutorials/` and `community/` ship in the notebook image and the user sees them as read-only.
- `workspace/` is empty on `main`. Each user's fork is where their personal notebooks accumulate. PR back to upstream is how a workspace notebook gets promoted to `community/`.

### GitHub as persistence

On first login:

1. Use the GitHub OAuth token to call `POST /repos/<upstream-owner>/stargazer/forks`. Idempotent — calling it on an existing fork is a no-op.
2. Store `fork_owner` (their GitHub login, usually) and `fork_url` on the session.

When the user's notebook app boots:

1. Clone `https://<token>@github.com/<fork_owner>/stargazer.git` into a workspace dir inside the pod.
2. Mount that workspace dir as the marimo working dir, so `notebooks/workspace/` is editable in place.
3. Run a periodic / on-save hook that commits + pushes workspace changes back to the user's fork on a per-user branch (e.g. `workspace`).

Promotion is then "open a PR from `workspace` into upstream `main`" — the natural GitHub flow. **Not in scope for this plan.**

### Dashboard tile UI

`dashboard_html()` (in `app/templates.py`) renders three sections:

- **Tutorials** — fixed list from `notebooks/tutorials/`.
- **Community** — fixed list from `notebooks/community/` (curated; ships in image).
- **Workspace** — dynamic list, read from the user's fork checkout via the notebook app's HTTP API on dashboard render.

Each tile has Edit and Run buttons that route to the user's notebook-app endpoint with the notebook path as a query argument (`/edit?file=...` or `/run?file=...`). The notebook app itself decides how to surface that to marimo (likely just launching `marimo edit <path>` / `marimo run <path>` against the file).

## Open questions

1. **OAuth scope.** Current admin app likely requests `read:user` only. Forking requires `public_repo` (or `repo` for private forks). Existing logged-in users will need to re-consent on the upgraded scope.
2. **Fork strategy.** Always fork from the canonical upstream, or allow users who already have an unrelated fork to use that one? Recommend: always fork from a single canonical upstream URL (configured via env var); refuse to operate on arbitrary fork names.
3. **Push frequency.** Every save (chatty), on a timer (lossy on pod restart), or manual "Commit" button (explicit but adds UI)? Recommend timer + on-shutdown push, with an explicit "Push now" button surfaced in marimo as a stretch.
4. **Per-user app lifecycle.** Long-lived Knative deployment per user, or spin down on inactivity? Knative supports scale-to-zero, so the simpler answer is "always have an app spec, let Knative idle it." Costs near-zero when idle.
5. **Auth on the notebook app.** Currently `requires_auth=False` for the admin app and the existing notebook env. For per-user notebooks we need either Flyte's app auth or a session-cookie check at the notebook app entrypoint. Open question — left for the implementation phase.
6. **Workspace listing API.** Where does the workspace tile list come from? Options: (a) admin app shells out to `git ls-tree` on a server-side checkout of the user's fork; (b) the notebook app exposes an HTTP `/workspace/list` endpoint and the dashboard fetches it. (b) keeps git auth scoped to the notebook app's pod.

## Steps (in order)

1. ✅ Restore `community/scrna_pipeline.py` from HEAD.
2. ✅ Create `workspace/.gitkeep`.
3. **Bump GitHub OAuth scope** to `public_repo` in `app/oauth.py`. Document the re-consent flow.
4. **Add `fork_user_repo(...)` helper** in `app/oauth.py` (or a new `app/github.py`) that idempotently calls `POST /repos/.../forks`.
5. **Persist fork info on the session** — extend `SessionData` with `fork_owner: str` (the user's GitHub login).
6. **Define the per-user notebook AppEnvironment** in `app/notebook_app.py`:
   - Image: existing `stargazer-note`, no per-notebook split yet.
   - Args: a wrapper script that (a) clones the user's fork, (b) launches marimo against the working dir.
   - `env_vars`: include the user's GitHub token (short-lived OAuth access token) and fork URL.
7. **Deploy the notebook app per user** in `provision.py` — one `serve.aio()` per (user, project).
8. **Rebuild the dashboard template** to render three tile sections. Each tile has Edit / Run links pointing into the user's notebook-app URL.
9. **Wire the workspace listing** — minimal HTTP endpoint inside the notebook app that returns the current set of `notebooks/workspace/*.py` files; dashboard fetches it server-side on render.
10. **Verify end-to-end:** new user logs in → fork is created → notebook app is provisioned → dashboard shows tiles → clicking a community notebook opens it read-only in marimo → clicking a workspace notebook (after the user has added one to their fork) opens it editable.

## Verification checklist

- [ ] First login for a fresh GitHub user creates a fork at `https://github.com/<user>/stargazer`.
- [ ] Subsequent logins by the same user do not create duplicate forks (idempotent).
- [ ] User's notebook app deploys after login and reaches Ready.
- [ ] Dashboard shows three sections with the expected tiles (Tutorials: 3, Community: 1, Workspace: 0 initially).
- [ ] Clicking Edit on `community/scrna_pipeline.py` opens it in marimo.
- [ ] Adding a notebook to `notebooks/workspace/` in the user's fork makes it show up in the Workspace section on next dashboard load.
- [ ] Edits to a workspace notebook are pushed back to the user's fork.

## Out of scope (explicit)

- MCP server integration / in-notebook LLM (parked, will revisit after marimo's native AI features are investigated).
- Promotion workflow — handled implicitly by the GitHub PR system once forks are in place; no bespoke UI needed.
- Per-notebook slim images. The user notebook app continues to use the shared `stargazer-note` image.
- In-notebook local-vs-remote dispatch toggle UI. Tracked separately in the roadmap.
- Authentication / authorization on the notebook app itself (open question, left for implementation).

## Files that will be touched

- `app/oauth.py` — scope bump, fork helper.
- `app/session.py` — add `fork_owner`.
- `app/notebook_app.py` — replace the existing single-tutorial AppEnvironment with the per-user fork-clone one.
- `app/provision.py` — call the fork helper, deploy the per-user notebook app.
- `app/admin_app.py` — feed fork info / notebook lists into `dashboard_html`.
- `app/templates.py` — three-section tile grid.
- `Dockerfile` — `note` target adds a clone-and-launch entrypoint (and `git` if not already present).
- `src/stargazer/notebooks/community/scrna_pipeline.py` — already restored.
- `src/stargazer/notebooks/workspace/.gitkeep` — already created.
