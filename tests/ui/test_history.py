from pathlib import Path

import pytest

from pywatts.core.workout import Steady, Workout
from pywatts.storage.db import open_db
from pywatts.storage.repo import Repo, Sample
from pywatts.ui.history import HistoryPage, sanitize_filename


@pytest.fixture
def repo(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield Repo(conn)
    conn.close()


@pytest.fixture
def page(repo, qtbot):
    p = HistoryPage(repo)
    p.errors = []
    p.show_error = p.errors.append
    qtbot.addWidget(p)
    return p


def add(repo, name="A/B: test", n=60, finish=True, power=200, hr=None):
    sid = repo.start_session(Workout(name, [Steady(60, 1.0)]), 200)
    repo.add_samples(sid, [Sample(float(t), power, 90.0, hr, 200, 30.0) for t in range(n)])
    if finish:
        repo.finish_session(sid, "completed")
    return sid


def test_list_and_detail(repo, page):
    add(repo)
    add(repo, "in progress", finish=False)
    page.refresh()
    assert page.table.rowCount() == 1
    cells = [page.table.item(0, c).text() for c in range(6)]
    assert cells[1] == "A/B: test" and cells[3:] == ["200", "12", "completed"]
    assert cells[2] in ("00:59", "01:00")
    assert not page.export_button.isEnabled()
    page.table.selectRow(0)
    text = page.summary_label.text()
    assert "NP 200 W" in text and "Avg 200 W" in text and "Max 200 W" in text
    assert len(page.power_curve.getData()[0]) == 60
    page.refresh()
    assert page.selected_session() is not None


def test_missing_data(repo, page):
    add(repo, power=None)
    page.refresh()
    page.table.selectRow(0)
    assert "NP --" in page.summary_label.text()


def test_delete(repo, page):
    add(repo)
    page.refresh()
    page.table.selectRow(0)
    page.confirm_delete = lambda _s: False
    page.delete_button.click()
    assert page.table.rowCount() == 1
    page.confirm_delete = lambda _s: True
    page.delete_button.click()
    assert page.table.rowCount() == 0 and repo.list_sessions() == []
    assert not page.delete_button.isEnabled()


def test_export(repo, page, tmp_path):
    add(repo)
    page.refresh()
    page.table.selectRow(0)
    seen = []

    def choose(name):
        seen.append(name)
        return tmp_path / name

    page.choose_export_path = choose
    page.export_button.click()
    assert page.errors == []
    assert seen[0].endswith("_A_B_ test.fit") and (tmp_path / seen[0]).stat().st_size > 0
    page.choose_export_path = lambda _n: Path(tmp_path / "missing" / "x.fit")
    page.export_button.click()
    assert len(page.errors) == 1
    page.choose_export_path = lambda _n: None
    page.export_button.click()
    assert len(page.errors) == 1


def test_sanitize():
    assert sanitize_filename('a<>:"/\\|?*\n b. ') == "a__________ b"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("...") == "ride"
    assert len(sanitize_filename("ü" * 500).encode()) <= 120


def _add_rich(repo, altitude=None, cadence=90.0, speed=30.0, hr=140):
    sid = repo.start_session(Workout("rich", [Steady(60, 1.0)]), 200)
    repo.add_samples(
        sid,
        [
            Sample(
                float(t),
                200,
                cadence,
                hr,
                200,
                speed,
                0,
                None if altitude is None else altitude + t,
            )
            for t in range(60)
        ],
    )
    repo.finish_session(sid, "completed")
    return sid


def test_chart_only_offers_channels_with_data(repo, page):
    _add_rich(repo, cadence=None, speed=None, hr=None)
    page.refresh()
    page.table.selectRow(0)
    assert page.chart.available() == ["target", "power"]
    assert page.chart._cells["hr"].isHidden()
    assert page.chart._cells["power"].isVisibleTo(page.chart)
    assert page.chart._cells["altitude"].isHidden()


def test_chart_channels_and_axes(repo, page):
    _add_rich(repo, altitude=-5.0)
    page.refresh()
    page.table.selectRow(0)
    chart = page.chart
    assert set(chart.available()) == {"target", "power", "hr", "cadence", "speed", "altitude"}
    assert chart.curves["altitude"].getData()[1][0] == -5.0
    chart.checkboxes["speed"].setChecked(False)
    assert "speed" not in chart.shown() and not chart.curves["speed"].isVisible()
    assert chart.side("power") == "left" and chart.side("hr") == "right"
    chart.set_side("hr", "left")
    chart.set_side("power", "right")
    assert chart.side("hr") == "left"
    assert chart.curves["hr"] in chart._left.addedItems
    assert chart.curves["power"] in chart._right.addedItems
    # choices survive the next ride
    page.refresh()
    assert chart.side("hr") == "left" and not chart.checkboxes["speed"].isChecked()


def test_best_power_panel(repo, page):
    _add_rich(repo)
    page.refresh()
    page.table.selectRow(0)
    table = page.peaks.table
    assert table.rowCount() > 0
    assert table.item(0, 0).text() == "3 s" and table.item(0, 1).text() == "200"
    assert table.item(0, 3).text() == "100%"
    assert len(page.peaks._ride.getData()[0]) == table.rowCount()
    page.table.clearSelection()
    assert page.peaks.table.rowCount() == 0


def test_width_does_not_depend_on_the_channels(repo, page, qtbot):
    from PyQt6.QtWidgets import QApplication, QSplitter

    _add_rich(repo, altitude=1234.0)  # all channels
    add(repo)
    page.refresh()
    page.resize(900, 700)
    with qtbot.waitExposed(page):
        page.show()
    charts = next(
        s
        for s in page.findChildren(QSplitter)
        if s.isAncestorOf(page.chart) and s.count() == 2 and not s.isAncestorOf(page.table)
    )
    seen = set()
    channels = set()
    for row in (0, 1, 0):
        page.table.selectRow(row)
        QApplication.processEvents()
        channels.add(len(page.chart.available()))
        seen.add((page.width(), page.minimumSizeHint().width() <= 900, *charts.sizes()))
    assert len(channels) == 2
    assert len(seen) == 1 and next(iter(seen))[1]


def test_flow_layout(qtbot):
    from PyQt6.QtWidgets import QLabel, QWidget

    from pywatts.ui.widgets.common import FlowLayout

    box = QWidget()
    qtbot.addWidget(box)
    flow = FlowLayout(spacing=10)
    box.setLayout(flow)
    labels = [QLabel() for _ in range(3)]
    for label in labels:
        label.setFixedSize(100, 20)
        flow.addWidget(label)
    box.resize(250, 100)
    box.show()
    assert [(w.x(), w.y()) for w in labels] == [(0, 0), (110, 0), (0, 30)]
    assert flow.heightForWidth(250) == 50 and flow.heightForWidth(320) == 20
    assert flow.minimumSize().width() == 100
    labels[0].hide()  # takes no room
    flow.activate()
    assert [(w.x(), w.y()) for w in labels[1:]] == [(0, 0), (110, 0)]
    assert flow.itemAt(5) is None and flow.takeAt(5) is None


def test_window_never_covers_the_taskbar():
    from PyQt6.QtCore import QMargins, QRect, QSize

    from pywatts.ui.main_window import SIZE, fitted

    frame = QMargins(7, 30, 7, 7)
    small = QRect(0, 0, 1280, 752)
    rect = fitted(SIZE, frame, small)
    assert small.contains(rect.marginsAdded(frame))
    assert rect.width() == SIZE.width() and rect.height() == 752 - 37
    beside = QRect(60, 0, 2500, 1400)  # taskbar on the left: full size
    rect = fitted(SIZE, frame, beside)
    assert rect.size() == SIZE and beside.contains(rect.marginsAdded(frame))
    assert fitted(QSize(400, 300), frame, small).size() == QSize(400, 300)
