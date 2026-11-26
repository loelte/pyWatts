import math

from pywatts.ui.widgets.spectrum import SpectrumWindow, period_text


def test_window_names_the_weekly_rhythm(qtbot):
    window = SpectrumWindow()
    qtbot.addWidget(window)
    window.set_series("Load", "load", [0, 0, 50, 0, 0, 150, 0] * 26)
    assert window.windowTitle() == "Rhythms in load"
    text = window.summary.text()
    assert text.startswith("Repeating pattern: every 7.0 d (±")
    assert "echoes at 3.5 d" in text
    window.set_series("Load", "load", [0, 60, 0, 80, 0, 120, 0] * 26)
    assert window.summary.text().startswith("Repeating pattern: every 7.0 d")
    assert window.readout_at(math.log10(7)).startswith("Every 7.0 d: ±")


def test_too_little_data_is_a_message(qtbot):
    window = SpectrumWindow()
    qtbot.addWidget(window)
    window.set_series("Hours", "h", [1.0] * 10)
    assert "Not enough data" in window.summary.text()
    assert window.spectrum is None
    window.set_series("Hours", "h", [1.0, 0.0] * 30)  # recovers
    assert window.spectrum is not None


def test_period_text():
    assert period_text(7) == "7.0 d"
    assert period_text(21) == "3.0 wk"
    assert period_text(365.25 / 4) == "3.0 mo"


def _imported_ride(repo, start, name):
    from datetime import timedelta

    from pywatts.storage.repo import ImportedRide, Sample

    samples = [
        Sample(t=i, power=150, cadence=None, hr=None, target_w=0, speed=28.0) for i in range(600)
    ]
    ride = ImportedRide(start, start + timedelta(seconds=600), name, samples)
    repo.import_session(ride, "file", name, f"sha-{name}")


def test_fft_button_opens_one_window(tmp_path, qtbot):
    from datetime import UTC, datetime, timedelta

    from pywatts.storage.db import open_db
    from pywatts.storage.repo import Repo
    from pywatts.ui.progress import ProgressPage

    conn = open_db(tmp_path / "t.db")
    repo = Repo(conn)
    page = ProgressPage(repo)
    qtbot.addWidget(page)
    with qtbot.waitExposed(page):
        page.show()
    _imported_ride(repo, datetime.now(UTC) - timedelta(days=3), "first")
    page.refresh()
    page.load.fft_button.click()
    window = page.spectrum
    assert window is not None and window.isVisible()
    assert page.message.text() == ""
    assert "Not enough data" in window.summary.text()

    for week in range(1, 20):
        _imported_ride(repo, datetime.now(UTC) - timedelta(weeks=week), f"w{week}")
    page.refresh()
    page.load.measure_buttons["hours"].click()
    page.load.fft_button.click()
    assert page.spectrum is window
    assert window.windowTitle() == "Rhythms in hours"
    assert window.summary.text().startswith("Repeating pattern: every 7.0 d")
    page.close()
    conn.close()
