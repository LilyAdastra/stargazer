# Tighten GitHub Token Scope (OAuth App + GitHub App hybrid)

Security hardening for the credential the admin app holds on behalf of each user. Today the app requests a broad, long-lived OAuth token and hands it to the notebook runtime; this plan narrows it to a fork-scoped, short-lived credential and keeps it out of the pod where user code runs.

## Problem (current state)

Two independent weaknesses, the second worse than the first:

1. **Scope is too broad.** `app/oauth.py:30` requests `read:user public_repo`. `public_repo` grants **write to every public repo the user owns**, not just the fork — classic OAuth has no per-repo granularity — and the token does not expire.
2. **Exposure: the broad token reaches code the user controls.**
   - `app/per_notebook.py:205` injects it as `GITHUB_TOKEN` into the notebook pod `env_vars` → any marimo cell can read `os.environ["GITHUB_TOKEN"]`.
   - `app/launch-notebook.sh` embeds it in the clone URL → it persists in `/workspace/.git/config` (readable via `git remote -v` / `open()` in a cell).
   - `app/session.py` signs but does **not** encrypt the cookie → the token is base64-decodable client-side.

Net: a single malicious or careless notebook can exfiltrate a non-expiring token that rewrites all of the user's public repos.

## Goal

- The only credential that ever touches a notebook pod is **fork-scoped and short-lived (~1h, auto-expiring)**.
- The broad OAuth token is used **once** (login + initial fork) and then discarded — never stored in the session, never sent to a pod.
- The one high-value long-lived secret becomes the **GitHub App private key**, held only by the admin app (the trust anchor), used to mint per-fork installation tokens on demand.

## Token model (target)

| Step | Credential | Scope | Lifetime | Where it lives |
|---|---|---|---|---|
| Login + initial fork | OAuth user token (`read:user public_repo`) | all public repos | request-lifetime only | admin process memory; **discarded after fork** |
| List / get / create notebook (admin-side GitHub reads/writes) | GitHub App **installation token** | the fork only | ~1h, minted on demand | admin process memory |
| Clone / push in the notebook pod | GitHub App **installation token** via `GIT_ASKPASS` | the fork only | ~1h | pod env at boot, never written to `.git/config` |

The admin mints installation tokens from the GitHub App private key + the user's installation id (installation id is **not** a secret — it can sit in the session).

## Architecture

```
  Login (OAuth App)                     Ongoing ops (GitHub App)
  ─────────────────                     ────────────────────────
  user authorizes                       admin holds App private key
  read:user public_repo                          │
        │                                JWT ─► installation token
        ▼                                (scoped to fork, ~1h)
  fork upstream  ──────────────────────►         │
  (one-time, broad token)                        ├─► admin: list/get/create via API
  DISCARD token                                  │
                                                 └─► pod: GIT_ASKPASS clone/push
                                                     (token never in .git/config,
                                                      expires in ~1h)
```

---

## Phase 0 — Register the GitHub App (manual, external)

- [ ] Create a GitHub App (org-owned). Permissions: **`contents: write`**, **`metadata: read`** — nothing else.
- [ ] Generate and download a private key (PEM). This becomes `GITHUB_APP_PRIVATE_KEY`.
- [ ] Record `GITHUB_APP_ID` and (if user-to-server login is used later) the App's client id/secret.
- [ ] Set the install flow: "Only select repositories" so users can scope the install to their fork.
- [ ] Decide the install trigger UX (see Phase 3 choreography).

**Verify against current GitHub docs** (APIs move; do not hardcode from memory):
- exact permission required to **create a fork** via a token,
- the **add-repo-to-installation** API + which token type it needs,
- whether an installation token can be restricted to a single repo within a multi-repo install.

## Phase 1 — Admin-side GitHub App client (no behavior change yet)

- [ ] Add `app/github_app.py`:
  - `_app_jwt()` — sign a short JWT with `GITHUB_APP_PRIVATE_KEY` + `GITHUB_APP_ID` (PyJWT).
  - `get_installation_id(owner)` — look up the user's installation (`GET /users/{owner}/installation` with the app JWT).
  - `mint_installation_token(installation_id, repo_ids=[...])` — `POST /app/installations/{id}/access_tokens` scoped to the fork; returns `(token, expires_at)`.
  - A tiny in-memory cache keyed by installation id (tokens are ~1h; refresh before expiry).
- [ ] Add the App key + id to admin config (`STARGAZER_*` env). On devbox these go through the same `env_vars` baking as the OAuth secrets — see [`.opencode/reference/devbox_workarounds.md`](../reference/devbox_workarounds.md) (`secrets=[...]` is dropped on App pods).
- [ ] Unit-test JWT signing and token-response parsing with a mocked GitHub.

## Phase 2 — Route ongoing GitHub ops through installation tokens

Replace the user OAuth token in every **post-fork** call with a freshly-minted installation token.

- [ ] `app/github.py` — `list_workspace`, `get_workspace_notebook`, `create_workspace_notebook`, `_auth_headers`: take an installation token (or a `token_provider` callable) instead of the user `access_token`.
- [ ] `app/admin_app.py` — `/workspace/list`, `/workspace/create`: mint an installation token for `session.fork_full_name` and pass it down (no longer `session.access_token`).
- [ ] Keep `fork_upstream` on the OAuth token (Phase 3 owns the fork step).
- [ ] Tests: handlers call the GitHub-App path; assert no `session.access_token` use post-fork.

## Phase 3 — Drop the OAuth token from the session and the pod (the exposure fix)

This is the core trust win. The OAuth token becomes request-scoped; pods get a short-lived fork-only token.

### 3a. OAuth token is ephemeral
- [ ] `app/admin_app.py` callback: after `fork_upstream`, do **not** put `access_token` into `SessionData`. Trigger/record the GitHub App install + the fork's installation id.
- [ ] `app/session.py`: remove `access_token` from `SessionData`; add `installation_id: int | None` (non-secret). Re-sign cookie.
- [ ] Install choreography (one extra consent): after fork, redirect the user to install the App on the fork, then (with a user-to-server token) add the fresh fork to the installation (`PUT /user/installations/{id}/repositories/{repo_id}`) so they don't hand-pick it. Record `installation_id`.

### 3b. Pod never gets the broad token
- [ ] `app/per_notebook.py`: drop `GITHUB_TOKEN` from `env_vars`. Inject only `FORK_FULL_NAME` / `FORK_OWNER` and a **token endpoint** the pod can call back to.
- [ ] At boot, the pod fetches a short-lived installation token from the admin (mTLS/session-authenticated callback), uses it via `GIT_ASKPASS` for clone — **never** embed in the remote URL / `.git/config`.
- [ ] `app/launch-notebook.sh`: clone with `GIT_ASKPASS` (or `-c credential.helper`) instead of `https://x-access-token:${TOKEN}@…`; set remote URL token-free.
- [ ] `app/proxy.py` `_sync_workspace`: fetch a fresh installation token (it may have expired since boot) and push via `GIT_ASKPASS`.

> **Stronger variant (optional, "token never in pod at all"):** the pod sends its workspace diff to the admin, and the admin commits+pushes via the GitHub Contents API. Bigger change; defer unless required.

## Phase 4 — Hardening

- [ ] Encrypt the session cookie (or move to a server-side session store; cookie holds only an opaque id), so even non-credential identity data isn't readable client-side.
- [ ] Confirm the GitHub App private key is the **only** long-lived GitHub secret, held solely by the admin.
- [ ] Scrub any token from pod `.git/config` and process args; confirm `os.environ` in a notebook cell exposes no GitHub credential.
- [ ] Update `docs/architecture/app.md`: the token model table + the login/fork-vs-ongoing split.
- [ ] Add a security note to the README spec section **only if out of spec** (README is human-owned — do not edit; flag instead).

## Acceptance

- [ ] A notebook cell running `os.environ.get("GITHUB_TOKEN")` and reading `.git/config` yields no usable long-lived credential.
- [ ] Any token reachable from a pod is fork-scoped and expires within ~1h (verify by waiting out expiry).
- [ ] The session cookie carries no GitHub token.
- [ ] Revoking the GitHub App install on the fork immediately cuts off all access (no lingering OAuth token).

## Open questions / decisions

- **One app or two?** This plan keeps the OAuth App for login+fork and adds a GitHub App for everything else (forking through a GitHub App is fiddly). A later simplification: GitHub App does user-to-server login too, dropping the OAuth App entirely. Decide after Phase 3.
- **Pod token delivery** (3b): callback-fetch vs. short-TTL token injected at launch. Callback is tighter (token minted at use, not at deploy) but adds a pod→admin auth path.
- **Devbox secret injection** for the App private key rides on the existing `env_vars` baking gap; revisit if/when Union supports App-pod `secrets=[...]`.
