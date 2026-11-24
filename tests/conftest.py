import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from pywatts import platform


@pytest.fixture(autouse=True, scope="session")
def _no_real_data(tmp_path_factory):
    """Tests never touch the data of the rider."""
    with pytest.MonkeyPatch.context() as m:
        for name in ("data_dir", "config_dir"):
            path = tmp_path_factory.mktemp(name)
            m.setattr(platform, name, lambda path=path: path)
        yield
