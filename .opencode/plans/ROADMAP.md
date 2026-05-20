# Stargazer Roadmap

Upcoming work is ordered — the **next feature is at the top**. Move items into Complete (with a ✅) as they ship.

## Upcoming

1. **Per-user notebook app + GitHub fork persistence + three-section tile dashboard.** Detailed plan: [`16_user_notebook_app.md`](./16_user_notebook_app.md).
2. **In-notebook local-vs-remote toggle UI.** Formalize the dispatch choice as a reusable `mo.ui` element (radio / segmented control) so individual cells don't need to hardcode `flyte.with_runcontext(mode="local").run` vs `flyte.run`.
3. **Per-notebook slim AppEnvironment images.** Each tile gets its own `flyte.app.AppEnvironment` with only the deps to run its tasks locally in-pod; remote dispatch only needs the TaskEnvironment reference.
4. **Marimo AI features investigation.** Determine what marimo's native AI surface offers (`mo.ai.chat` / similar), whether tool-calling is supported, and how to wire the registry catalog in.
5. **Upload public assets for quickstart workflow to Pinata.**
6. **Update README with CLI quickstart and bump to alpha status.**
7. **Interactive workflow for generating a DB from existing data in `STARGAZER_LOCAL`.**
8. **Condensed context files for production use (separate from dev).**
9. **Recurring docs-sync job** so architecture docs never go stale against the code.
10. **Agentic PR process** for end-to-end automated review/merge of trusted contributors.
11. **More robust logging.**
    - Per-task tags so logs can be demultiplexed.
    - One logfile per workflow execution.
    - Stop flushing to stdout/err to keep context windows clean.
    - Env vars for log level and a bool to include actual tool-call output.
12. **Data-aware caching.** Flyte's input-hash caching is solid but breaks down for keyword/metadata-based workflows — need a higher-level cache keyed on semantic inputs.

## Complete

- ✅ scRNA preprocessing tutorial rebuild (Asset → Task → Workflow → local → remote). [`archive/15_scrna_tutorial_rebuild.md`](./archive/15_scrna_tutorial_rebuild.md)
- ✅ Integrate marimo as the notebook experience (basic plumbing — per-user provisioning, in-pod execution, tutorial scaffold).
- ✅ Create Stargazer org.
- ✅ Set up GitHub Pages.
- ✅ Exhaustively link docs to code for agent traversal.
