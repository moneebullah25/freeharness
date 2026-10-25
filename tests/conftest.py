import pytest


def _harness_installed() -> bool:
    from freeharness.client import rpc
    return rpc.DSH_BIN.exists()


def pytest_collection_modifyitems(config, items):
    """test_client.py drives the real DeepSeek Harness (against a stub model, no network)."""
    if _harness_installed():
        return
    skip = pytest.mark.skip(reason="DeepSeek Harness not installed (run `freeharness setup`)")
    for item in items:
        if item.fspath.basename == "test_client.py":
            item.add_marker(skip)


@pytest.fixture
def wheelhouse(tmp_path):
    """A local directory of tiny wheels (fhdep fhtest fhextra fhlint fhci fhdev fhtool + a docs one) for `pip --no-index`."""
    from fake_pypi import build_wheelhouse
    return build_wheelhouse(tmp_path / "wheelhouse")
