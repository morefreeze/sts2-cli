"""The catalog must warm in the background, not on the first page load.

Measured on this repo's logs: /api/cohorts takes ~163s cold and 0.32s warm.
The workbench disables its controls (including 「载入单个记录」) until that
first request returns, so a user opening the page sits for over two minutes
with a dead UI. Warming at startup moves that cost off the user's first click.
"""
import agent.run_progress_viewer as viewer


class _Catalog:
    def __init__(self, boom=False):
        self.calls = 0
        self._boom = boom

    def _build_cohorts(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("scan blew up")
        return []


def test_warm_catalog_builds_cohorts_once():
    cat = _Catalog()

    viewer._warm_catalog(cat)

    assert cat.calls == 1


def test_warm_catalog_swallows_errors():
    """A warm-up failure must never stop the server from serving."""
    cat = _Catalog(boom=True)

    viewer._warm_catalog(cat)  # must not raise

    assert cat.calls == 1
