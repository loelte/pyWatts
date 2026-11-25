from datetime import date

import pytest
from PyQt6.QtCore import QDate

from pywatts.core.workout import Steady, Workout
from pywatts.storage.db import open_db
from pywatts.storage.profile import load_profile
from pywatts.storage.repo import FTP_W, UNITS, WEIGHT_KG, Repo, Sample
from pywatts.ui.profile import ProfilePage
from pywatts.ui.progress import ProgressPage


@pytest.fixture
def repo(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield Repo(conn)
    conn.close()


@pytest.fixture
def page(repo, qtbot):
    p = ProfilePage(repo)
    qtbot.addWidget(p)
    return p


@pytest.fixture
def progress(repo, qtbot):
    p = ProgressPage(repo)
    qtbot.addWidget(p)
    with qtbot.waitExposed(p):  # loads only while shown
        p.show()
    return p


def test_save_profile(page, repo, qtbot):
    assert not page.dirty and not page.save_button.isEnabled()
    page.ftp.setValue(260)
    page.weight.setValue(65)
    assert page.w_per_kg.text() == "4.00 W/kg"
    page.has_birth.setChecked(True)
    page.birth.setDate(QDate(1990, 1, 1))
    page.estimate_button.click()
    assert 150 < page.max_hr.value() < 190
    with qtbot.waitSignal(page.profile_changed):
        page.save_button.click()
    p = load_profile(repo)
    assert (p.ftp_w, p.weight_kg, p.birth_date) == (260, 65, date(1990, 1, 1))
    assert p.hr_zones is not None and not page.dirty and page.message.text() == "Saved"


def test_power_zones_follow_ftp_until_custom(page, repo):
    page.ftp.setValue(100)
    assert [b.value() for b in page.power.limits] == [55, 75, 90, 105]
    page.power.method.setCurrentIndex(1)
    page.ftp.setValue(300)
    assert page.power.limits[0].value() == 55 and page.power.limits[0].isEnabled()
    page.power.limits[0].setValue(80)  # above Z2's limit
    assert page.power.ranges[0].text() == "--"
    assert not page.save() and "Power zones" in page.message.text() and page.dirty
    page.power.limits[0].setValue(60)
    assert page.save()
    assert load_profile(repo).power_zones.limits == (60, 75, 90, 105)
    page.power.method.setCurrentIndex(0)
    assert page.save() and load_profile(repo).custom_power is None


def test_hr_zone_methods(page, repo):
    assert "max heart rate" in page.message.text()
    page.max_hr.setValue(190)
    page.hr.method.setCurrentIndex(1)
    assert "resting" in page.message.text()
    page.resting_hr.setValue(50)
    assert page.hr.limits[0].value() == 134
    page.hr.method.setCurrentIndex(2)
    page.hr.limits[0].setValue(120)
    assert page.save() and load_profile(repo).hr_zones.limits[0] == 120
    page.resting_hr.setValue(0)
    page.threshold_hr.setValue(195)
    assert not page.save() and "below" in page.message.text()


def test_imperial_keeps_stored_precision(page, repo):
    repo.record_metric(WEIGHT_KG, 72.25, date(2026, 1, 1))
    repo.set_setting(UNITS, "imperial")
    page.refresh(force=True)
    assert page.weight.value() == pytest.approx(159.3)
    page.name.setText("x")
    page._edited()
    assert page.save() and load_profile(repo).weight_kg == 72.25
    assert len(repo.list_metrics()) == 1
    page.weight.setValue(220.5)
    assert page.save() and load_profile(repo).weight_kg == pytest.approx(100.02)


def test_unsaved_edits(page, repo):
    page.ftp.setValue(300)
    page.refresh()
    assert page.ftp.value() == 300
    page.ask_unsaved = lambda: "cancel"
    assert not page.confirm_discard()
    page.ask_unsaved = lambda: "discard"
    assert page.confirm_discard()
    page.revert_button.click()
    assert page.ftp.value() == 200 and not page.dirty
    page.ftp.setValue(310)
    page.ask_unsaved = lambda: "save"
    assert page.confirm_discard() and repo.get_setting(FTP_W) == "310"


def test_save_failure_is_shown(page, repo, monkeypatch):
    def boom(*_):
        raise OSError("locked")

    page.ftp.setValue(300)
    monkeypatch.setattr(repo, "save_profile_values", boom)
    assert not page.save() and "locked" in page.message.text() and page.dirty


def test_progress_table_and_log(progress, repo, qtbot):
    repo.record_metric(WEIGHT_KG, 80, date(2026, 1, 1))
    sid = repo.start_session(Workout("W", [Steady(60, 1)]), 200)
    repo.add_samples(sid, [Sample(float(t), 240, None, None, 0, None) for t in range(60)])
    repo.finish_session(sid, "completed")
    progress.refresh()
    cells = [progress.peaks.item(0, c).text() for c in range(5)]
    assert cells[:3] == ["3 s", "240 W", "3.00"] and cells[4] == "240 W"
    assert progress.peaks.item(12, 0).text() == "60 min"
    assert progress.peaks.item(12, 1).text() == "--"
    assert "1 rides" in progress.volume.text()

    assert progress.value.value() == 80
    progress.value.setValue(79.5)
    with qtbot.waitSignal(progress.metrics_changed):
        progress.add_button.click()
    assert load_profile(repo).weight_kg == 79.5 and progress.log.rowCount() == 2
    progress.log.selectRow(0)
    progress.confirm_delete = lambda: True
    progress.delete_button.click()
    assert progress.log.rowCount() == 1 and load_profile(repo).weight_kg == 80


def test_progress_metric_switch_and_imperial(progress, repo):
    repo.set_setting(UNITS, "imperial")
    progress.refresh()
    progress.value.setValue(220.5)
    progress.add_button.click()
    assert load_profile(repo).weight_kg == pytest.approx(100.02)
    assert progress.log.item(0, 2).text() == "220.5 lb"
    progress.metric.setCurrentIndex(1)
    assert progress.value.suffix() == " bpm" and progress.value.maximum() == 150
    progress.value.setValue(52)
    progress.add_button.click()
    assert load_profile(repo).resting_hr == 52


def test_power_curve_and_form(progress, repo):
    sid = repo.start_session(Workout("W", [Steady(1500, 1)]), 200)
    repo.add_samples(sid, [Sample(float(t), 240, None, None, 0, None) for t in range(1500)])
    repo.finish_session(sid, "completed")
    progress.refresh()
    curve = progress.curve
    xs, ys = curve._all_line.getData()
    assert len(xs) == 11 and set(ys) == {240.0}
    assert len(curve._recent_line.getData()[0]) == 11
    assert "95% of your best 20 min is 228 W" in curve.summary.text()
    curve.show_duration(1200)
    assert "20 min  ·  all time 240 W  ·  90 days 240 W  ·  100% of best" in curve.readout.text()
    curve.set_per_kg(True)
    assert curve.readout_at(1200).count("--") == 2  # no weight logged on the ride
    assert "Today: fitness" in progress.form.summary.text()


def test_weekly_trend_and_export(progress, repo, tmp_path):
    sid = repo.start_session(Workout("W", [Steady(600, 1)]), 200)
    repo.add_samples(sid, [Sample(float(t), 200, None, None, 0, None) for t in range(600)])
    repo.finish_session(sid, "completed")
    repo.record_metric(FTP_W, 250, date(2026, 1, 1))
    repo.record_metric(FTP_W, 260, date(2026, 6, 1))
    progress.refresh()
    assert "This week: load" in progress.weekly.summary.text()
    assert "260 W on 2026-06-01" in progress.trend.summary.text()
    out = tmp_path / "plot.png"
    progress.ask_export_path = lambda _s: out
    progress.charts.setCurrentIndex(2)
    progress.export_button.click()
    assert out.stat().st_size > 0 and progress.message.text() == "Saved plot.png"


def test_fitness_form():
    from pywatts.core.metrics import fitness_form

    out = fitness_form([100.0] + [0.0] * 20)
    assert out[0][0] < out[0][1]  # fatigue rises faster
    assert out[-1][2] > 0  # rested after three weeks off
    assert fitness_form([]) == []


def test_smooth_and_estimated_load():
    from pywatts.core.metrics import estimated_load, smooth

    assert smooth([0.0, 0.0, 9.0, 0.0, 0.0], 1) == [0.0, 3.0, 3.0, 3.0, 0.0]
    assert estimated_load([None] * 10, [None] * 10, 70, 200) is None
    flat = estimated_load([30.0] * 3600, [0.0] * 3600, 70, 200)
    climb = estimated_load([30.0] * 3600, [i * 0.05 for i in range(3600)], 70, 200)
    assert flat and climb and climb > flat > 0


def test_estimated_power_matches_the_plain_sum():
    from random import Random

    from pywatts.core import metrics as m

    rng = Random(1)
    speed = [rng.choice([None, 0.0, 12.0, 28.5, 55.0]) if i % 7 else 20.0 for i in range(900)]
    altitude = [100 + 20 * (i / 200) ** 2 + rng.uniform(-1, 1) for i in range(900)]

    def plain():
        mass = 70 + m._BIKE_KG
        alt = m.smooth(altitude, m._SMOOTH_S)
        for i, kmh in enumerate(speed):
            v = (kmh or 0.0) / 3.6
            if v <= 0:
                yield 0.0
                continue
            lo, hi = max(0, i - m._SMOOTH_S), min(len(alt) - 1, i + m._SMOOTH_S)
            run = sum((speed[j] or 0.0) / 3.6 for j in range(lo, hi))
            grade = (alt[hi] - alt[lo]) / run if run > 1 else 0.0
            grade = max(-m._MAX_GRADE, min(m._MAX_GRADE, grade))
            force = mass * m._G * (m._CRR + grade) + 0.5 * m._RHO * m._CDA * v * v
            yield max(0.0, force * v / m._DRIVETRAIN)

    out = m.estimated_power(speed, altitude, 70)
    assert out == pytest.approx(list(plain()), abs=1e-9)
    assert max(out) > 0
    flat = m.estimated_power(speed, [None, *altitude[1:]], 70)  # incomplete: not used
    assert flat == pytest.approx(m.estimated_power(speed, [None] * 900, 70))


def test_nothing_is_read_while_hidden(repo, qtbot, monkeypatch):
    from datetime import UTC, datetime

    from pywatts.storage.repo import ImportedRide

    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    samples = [Sample(float(i), None, None, None, 0, 28.0, altitude=float(i)) for i in range(600)]
    ride = ImportedRide(start, start.replace(hour=9), "Spin", samples)
    session = repo.import_session(ride, "file", "x", "sha").session_id
    assert repo.speed_altitude(session) == [
        (s.speed, s.altitude) for s in repo.get_samples(session)
    ]
    reads = []
    real = repo.speed_altitude
    monkeypatch.setattr(repo, "speed_altitude", lambda i: reads.append(i) or real(i))
    monkeypatch.setattr(repo, "get_samples", lambda i: pytest.fail("not needed"))
    page = ProgressPage(repo)
    qtbot.addWidget(page)
    page.refresh()
    assert reads == []
    with qtbot.waitExposed(page):
        page.show()
    page.refresh()
    assert page.message.text() == "" and reads == [session]  # once
    assert page.form._values[-1][0] > 0


def test_form_range_and_ride_without_power(progress, repo):
    from datetime import UTC, datetime

    from pywatts.storage.repo import ImportedRide

    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    samples = [
        Sample(t=i, power=None, cadence=None, hr=None, target_w=0, speed=28.0) for i in range(1800)
    ]
    ride = ImportedRide(start, start.replace(hour=9), "Spin", samples)
    repo.import_session(ride, "file", "x", "sha")
    progress.refresh()
    assert progress.message.text() == ""
    assert progress.form._values[-1][0] > 0
    progress.form.set_range(30)
    assert progress.form.range_days == 30


def test_gaussian_smooth():
    from pywatts.core.metrics import gaussian_smooth, smooth

    assert gaussian_smooth([], 2) == []
    assert gaussian_smooth([5.0] * 20, 2) == pytest.approx([5.0] * 20)  # also at the ends
    out = gaussian_smooth([0.0] * 20 + [100.0] + [0.0] * 20, 2)
    assert sum(out) == pytest.approx(100) and out[19] == pytest.approx(out[21])
    assert out[20] == max(out) == pytest.approx(20, abs=0.5)

    # a jump leaves no corner: the slope changes much less than with the plain average
    def corner(ys):
        return max(abs(a - 2 * b + c) for a, b, c in zip(ys, ys[1:], ys[2:], strict=False))

    step = [0.0] * 20 + [100.0] * 20
    assert corner(gaussian_smooth(step, 2)) < 0.5 * corner(smooth(step, 3))


def test_weekly_range(qtbot):
    from datetime import timedelta

    from pywatts.ui.widgets.power_curve import WeeklyPlot

    plot = WeeklyPlot()
    qtbot.addWidget(plot)
    plot.set_range(30)  # no data yet
    weeks = [date(2025, 1, 6) + timedelta(weeks=i) for i in range(60)]
    plot.set_weeks(weeks, [500.0] + [100.0] * 59, [1.0] * 60)

    def view():
        (x0, x1), (_y0, y1) = plot.plot.getPlotItem().vb.viewRange()
        return round((x1 - x0) / 86400 / 7 - 1 / 7), y1

    for days, shown in ((30, 5), (90, 13), (180, 26), (365, 53)):
        plot.set_range(days)
        assert view() == (shown, pytest.approx(115))  # the high week is out of sight
    plot.set_range(None)
    assert view() == (60, pytest.approx(575))
    assert "Right click" in plot.summary.text()


def test_weekly_reaches_back_to_the_first_ride(progress, repo):
    from datetime import UTC, datetime, timedelta

    from pywatts.storage.repo import ImportedRide

    start = datetime.now(UTC) - timedelta(weeks=60)
    samples = [
        Sample(t=i, power=150, cadence=None, hr=None, target_w=0, speed=28.0) for i in range(600)
    ]
    ride = ImportedRide(start, start + timedelta(seconds=600), "Spin", samples)
    repo.import_session(ride, "file", "x", "sha")
    progress.refresh()
    assert progress.message.text() == ""
    assert len(progress.weekly._weeks) == 61 and progress.weekly._loads[0] > 0


def test_trend_range(qtbot):
    from datetime import timedelta

    from pywatts.ui.widgets.power_curve import TrendPlot

    plot = TrendPlot()
    qtbot.addWidget(plot)
    plot.set_range(30)  # no data yet
    today = date.today()
    days = [today - timedelta(days=d) for d in (400, 100, 10)]
    plot.set_series("FTP", "W", days, [200.0, 250.0, 260.0])
    assert "+0.0 W since" in plot.summary.text()
    (x0, x1), (y0, _y1) = plot.plot.getPlotItem().vb.viewRange()
    assert 35 < (x1 - x0) / 86400 < 37.5
    assert 250 < y0 < 260  # the entry 400 days ago does not stretch the axis
    plot.set_range(180)
    assert "+10.0 W since" in plot.summary.text()
    plot.set_range(None)
    assert "+60.0 W since" in plot.summary.text()
    plot.set_series("FTP", "W", days[:2], [200.0, 250.0])
    plot.set_range(30)
    assert "nothing logged in this time span" in plot.summary.text()
    plot.set_range(365)
    assert "+0.0 W since" in plot.summary.text()


def test_right_click_opens_the_range_menu(progress, monkeypatch):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtWidgets import QMenu

    class Click:
        def __init__(self, pos):
            self._pos = pos

        def button(self):
            return Qt.MouseButton.RightButton

        def scenePos(self):
            return self._pos

        def screenPos(self):
            return QPointF(0, 0)

        def accept(self):
            pass

    def choose(menu, _pos):
        next(a for a in menu.actions() if a.text() == "1 year").trigger()

    monkeypatch.setattr(QMenu, "exec", choose)
    for plot in (progress.form, progress.weekly, progress.trend):
        rect = plot.plot.getPlotItem().vb.sceneBoundingRect()
        plot.plot.scene().sigMouseClicked.emit(Click(rect.center()))
        assert plot.range_days == 365
