# Admin Dashboard: One Tile per Notebook + Edit/Run Buttons

Narrow focus extracted from [`16_notebook_first_architecture.md`](./16_notebook_first_architecture.md) Phase 3. Validation gate has resolved (preprocessing tutorial runs both local + remote against the redeployed devbox). MCP-in-marimo work is parked — marimo has native AI features that need investigation first. Promotion and community-submission flows are punted; they'll follow the same UI pattern once the dashboard skeleton exists.

## Goal

Replace the current single-tile dashboard with a grid of tiles — **one tile per notebook**, **two buttons per tile** (Edit / Run). Clicking either button lazily provisions an `AppEnvironment` for the user with the right marimo mode and opens the resulting URL.

## Out of scope

- A reusable local-vs-remote toggle UI **inside** notebook cells. The dashboard's Edit/Run choice covers the coarse default; the in-cell toggle is its own Upcoming roadmap item.
- Per-notebook slim images. Right now every notebook still ships in the same `stargazer-note` image; only `args` (marimo path + mode) differ per tile. Splitting images comes later.
- Marimo AI cell prototype (parked pending investigation).
- Promotion / fork-to-pin flow (same UI pattern, future plan).
- Community submission flow (same UI pattern, future plan).

## Architecture

### Notebook registry

A hand-maintained list in `app/notebooks.py`:

```python
@dataclass(frozen=True)
class Notebook:
    slug: str          # url-safe id, e.g. "preprocessing"
    title: str         # human-readable, shown on the tile
    description: str   # one-line, shown under the title
    path_in_image: str # e.g. "src/stargazer/notebooks/tutorials/preprocessing_tutorial.py"

NOTEBOOKS: tuple[Notebook, ...] = (
    Notebook(
        slug="preprocessing",
        title="scRNA-seq Preprocessing",
        description="Asset → Task → Workflow → local vs remote, on one sample.",
        path_in_image="src/stargazer/notebooks/tutorials/preprocessing_tutorial.py",
    ),
    Notebook(
        slug="assets",
        title="Assets Tutorial",
        description="Stargazer's content-addressed I/O primitives.",
        path_in_image="src/stargazer/notebooks/tutorials/assets_tutorial.py",
    ),
    Notebook(
        slug="tasks",
        title="Tasks Tutorial",
        description="How tasks are defined, cached, and dispatched.",
        path_in_image="src/stargazer/notebooks/tutorials/tasks_tutorial.py",
    ),
)
```

Hand-maintained beats auto-discovery: a notebook only appears once a maintainer has actually written a description for it.

### AppEnvironment per (slug, mode)

Today: one shared `notebook_env` with hardcoded path + `marimo edit`. After:

```python
def notebook_env_for(nb: Notebook, mode: Literal["edit", "run"]) -> flyte.app.AppEnvironment:
    """Build the AppEnvironment for one (notebook, mode) pair."""
    return flyte.app.AppEnvironment(
        name=f"nb-{nb.slug}-{mode}",   # deterministic → Knative dedupes revisions
        description=f"{nb.title} ({mode})",
        image=_NOTEBOOK_IMAGE,
        args=["marimo", mode, nb.path_in_image, "--port", "8080", "--host", "0.0.0.0",
              "--headless", "--no-token"],
        port=8080,
        requires_auth=False,
        resources=flyte.Resources(memory=("2Gi", "6Gi")),
        env_vars={**STARGAZER_ENV_VARS, "FLYTE_DOMAIN": "development"},
    )
```

The shared base `notebook_env` constant in `app/notebook_app.py` can stay temporarily as the template that `notebook_env_for` clones, or we delete it once the factory replaces every consumer.

### Lazy launch endpoint

Provisioning moves OUT of `auth_callback`. Login now just ensures the user's project exists and renders the dashboard. The actual `serve.aio(...)` happens per click:

```
POST /notebooks/{slug}/launch?mode=edit   →   302 to the notebook URL
```

Handler logic:

1. Resolve `slug` to a `Notebook`; 404 if unknown.
2. Resolve `mode` to `"edit"` or `"run"`; 400 if neither.
3. Build the per-(slug, mode) AppEnvironment.
4. Inject `FLYTE_PROJECT=<user_project>` into its `env_vars` (existing pattern from `provision.py`).
5. `await flyte.with_servecontext(project=user_project, domain=DOMAIN).serve.aio(env)`.
6. Redirect to `app.endpoint`.

Step 5 is idempotent: re-clicking the same button just returns the existing Knative revision's URL, no rebuild cost.

### Session model changes

`SessionData.notebook_url: str | None` → drop the field entirely. There are now N possible URLs per user, and they're cheap enough to resolve at click time. The session only needs identity (`github_username`, `github_id`).

This also removes the awkward "provisioning..." page state — login renders the dashboard immediately; the first click on a tile shows a spinner while `serve.aio()` runs.

### Templates

`dashboard_html()` signature changes from `(username, notebook_url)` to `(username, notebooks)` where `notebooks` is `tuple[Notebook, ...]`. The template renders a grid of tiles; each tile contains two `<form method="POST">` elements (one per mode) so we don't need JS for the basic flow.

`provisioning_html()` can be deleted along with the session field — it's no longer reachable.

## Steps (in order)

1. **Define the registry.** Create `app/notebooks.py` with `Notebook` + `NOTEBOOKS` for the three existing tutorial files.
2. **Add the factory.** Move (or rewrite) `notebook_env` from `app/notebook_app.py` into a `notebook_env_for(nb, mode)` factory. Existing single-env code becomes unused once the dashboard switches over.
3. **Rebuild the dashboard template.** `dashboard_html(username, notebooks)` renders the tile grid; each tile has two POST forms for `/notebooks/<slug>/launch?mode=edit` and `?mode=run`.
4. **Add the launch endpoint.** New handler in `app/admin_app.py`: looks up the notebook, builds the env, calls `serve.aio()`, redirects.
5. **Strip eager provision from auth callback.** `auth_callback` only ensures the project exists now; no `provision_user` call.
6. **Update `SessionData`.** Drop `notebook_url`; delete `provisioning_html`; update `landing()` to skip the provisioning branch.
7. **Verify** end-to-end against the redeployed devbox: log in, see all three tiles, click Edit on preprocessing → marimo edit loads, click Run on preprocessing → marimo run loads, click Edit on a different notebook → different URL, repeated clicks return the same URL fast.

## Open questions

1. **`marimo run` behavior** — does it render cells read-only? Auto-execute on connect? Hide the editor chrome? Determine empirically with one notebook before declaring the Run path "done."
2. **Per-user Knative app names** — currently the AppEnvironment is named uniformly per (slug, mode), and isolation comes from the per-user *project*. Confirm that two users hitting the same `nb-preprocessing-edit` env in their own projects each get their own Knative deployment, not a shared one.
3. **Per-user notebook state** — `marimo edit` writes changes back to the notebook file in the pod. Are user edits persisted between sessions? Lost? Mounted on a per-user volume? Affects whether "Edit" is genuinely user-private or just a per-session scratchpad.

## Verification checklist

- [ ] `/` after login shows three tiles, each with Edit + Run buttons.
- [ ] Clicking Edit on preprocessing opens a marimo editor for that notebook.
- [ ] Clicking Run on preprocessing opens marimo's run-mode view.
- [ ] Clicking the same button twice (across reloads) returns to the same URL without a long wait.
- [ ] Two different users see independent notebook URLs (per-project isolation holds).
- [ ] Pre-commit (ruff, docstr-coverage) passes.
- [ ] Admin app image rebuilt + redeployed; `python -m app.admin_app` succeeds.

## Files touched

- `app/notebooks.py` — **new**, registry + factory.
- `app/notebook_app.py` — keep `_NOTEBOOK_IMAGE` constant; remove or rewrite `notebook_env`.
- `app/admin_app.py` — drop eager provision in `auth_callback`, add `/notebooks/{slug}/launch`, update `landing()`.
- `app/provision.py` — strip down to `ensure_project()`; the notebook-serving piece moves to the launch endpoint.
- `app/session.py` — drop `notebook_url`.
- `app/templates.py` — rebuild `dashboard_html`, delete `provisioning_html`.
