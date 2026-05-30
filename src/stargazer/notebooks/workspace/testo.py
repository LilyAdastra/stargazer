# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "marimo",
#   "stargazer",
# ]
#
# [tool.uv.sources]
# stargazer = { path = "/stargazer", editable = true }
#
# [tool.stargazer]
# cpu = 2
# memory = "4Gi"
# ///
"""### Blank workspace notebook."""

import marimo

__generated_with = "0.21.1"
app = marimo.App(width="medium")


@app.cell
def _():
    """Imports."""
    import marimo as mo

    return (mo,)


if __name__ == "__main__":
    app.run()
