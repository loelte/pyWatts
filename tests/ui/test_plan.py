from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from PyQt6.QtCore import QDate, QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import QApplication, QLabel

from pywatts.core.plan import Plan, PlanUnit, Ride, Status
from pywatts.core.workout import Ramp, Repeat, Sprint, Steady, Workout
from pywatts.core.zones import power_zones
from pywatts.storage.db import open_db
from pywatts.storage.plan_repo import PlanRepo
from pywatts.storage.repo import COACH_SPLIT, ImportedRide, Repo, Sample
from pywatts.ui import plan as plan_page
from pywatts.ui import theme
from pywatts.ui.plan import PlanPage, ms_to_midnight, parse_split
from pywatts.ui.widgets import calendar_model as cm
from pywatts.ui.widgets.calendar_views import workout_shape
from pywatts.ui.widgets.plan_dialogs import DateDialog, PlanDialog, UnitDialog

TODAY = date(2026, 3, 18)  # a Wednesday
LEFT = Qt.MouseButton.LeftButton
DAY = timedelta(days=1)


class Clock:
    def __init__(self):
        self.day = TODAY

    def __call__(self):
        return self.day


@pytest.fixture
def conn(tmp_path):
    conn = open_db(tmp_path / "t.db")
    yield conn
    conn.close()


@pytest.fixture
def repo(conn):
    return Repo(conn)


@pytest.fixture
def plans(conn):
    return PlanRepo(conn)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def page(repo, plans, clock, qtbot):
    page = PlanPage(repo, plans, today=clock)
    page.ask_confirm = lambda _text: True
    qtbot.addWidget(page)
    page.resize(1070, 800)
    page.show()
    return page


def unit(uid="u", **kw):
    return PlanUnit(**{"id": uid, "day": TODAY, "kind": "indoor", "title": f"T {uid}"} | kw)


def add(plans, page, *units):
    plans.apply("user", "x", units)
    page.refresh()


def ride(repo, seconds=600, day=TODAY, hour=12, name="R", key=None):
    start = datetime(day.year, day.month, day.day, hour).astimezone()
    samples = [Sample(float(t), 200, None, None, 0, None) for t in range(seconds)]
    found = ImportedRide(start, start + timedelta(seconds=seconds), name, samples)
    key = key or f"{day}{hour}"
    return repo.import_session(found, "file", key, key).session_id


def entry(page, uid="u"):
    return page._data.find(("unit", uid))


def click(page, qtbot, uid="u"):
    rect = page.calendar.rect_of(("unit", uid))
    qtbot.mouseClick(page.calendar, LEFT, pos=rect.center().toPoint())


def drag(page, qtbot, uid, day):
    view = page.calendar
    start = view.rect_of(("unit", uid)).center().toPoint()
    end = view.rect_of_day(day).center().toPoint()
    qtbot.mousePress(view, LEFT, pos=start)
    move(view, start + QPoint(QApplication.startDragDistance() + 5, 0))
    move(view, end)
    qtbot.mouseRelease(view, LEFT, pos=end)


def move(view, pos):
    # qtbot.mouseMove reports no pressed button
    at = QPointF(pos)
    none = Qt.KeyboardModifier.NoModifier
    event = QMouseEvent(QEvent.Type.MouseMove, at, at, Qt.MouseButton.NoButton, LEFT, none)
    view.mouseMoveEvent(event)


def color_at(page, pos):
    image = page.calendar.grab().toImage()
    ratio = image.devicePixelRatio()
    return QColor(image.pixel(round(pos.x() * ratio), round(pos.y() * ratio))).name()


# --- model ---


def test_contrast_and_text_colors():
    assert cm.contrast("#000000", "#ffffff") == pytest.approx(21)
    for color in theme.PLAN_COLORS.values():
        assert cm.contrast(cm.text_on(color), color) >= 4.5
    for look in cm.LOOKS.values():
        assert cm.contrast(look.text, look.fill or theme.SURFACE) >= 4.5


def state(u, r=None, today=TODAY):
    return cm.make_entry(u, r, r is not None, today, cm.Loads()).state


def test_states():
    full, half = Ride(1, TODAY, 600.0, 10.0), Ride(1, TODAY, 200.0, 3.0)
    u = unit(target_s=600)
    assert state(u) == "indoor" and state(unit(kind="outdoor")) == "outdoor"
    assert state(u, full) == "done" and state(u, half) == "partial"
    assert state(u, today=TODAY + DAY) == "missed"
    assert state(unit(skipped=True), today=TODAY + DAY) == "skipped"
    for kind in ("rest", "note"):
        assert state(unit(kind=kind), today=TODAY + DAY) == kind
    assert cm.Entry(TODAY, "R", ride=full).state == "ride"
    colors = {k: (v.fill, v.outline) for k, v in cm.LOOKS.items()}
    c = theme.PLAN_COLORS
    assert colors["indoor"] == (c["indoor"], None) and colors["outdoor"] == (c["outdoor"], None)
    assert colors["done"] == (c["done"], None) and colors["missed"] == (c["missed"], None)
    assert colors["partial"] == (None, c["done"]) and colors["ride"] == (None, c["neutral"])
    assert all(colors[k] == (c["neutral"], None) for k in ("skipped", "rest", "note"))
    symbols = [cm.LOOKS[k].symbol for k in ("done", "missed", "partial", "skipped")]
    assert symbols == ["✓", "✗", "½", "–"]


def test_periods():
    assert cm.visible_range("month", date(2026, 2, 10)) == (date(2026, 1, 26), date(2026, 3, 1))
    assert cm.period("week", date(2027, 1, 1)) == (date(2026, 12, 28), date(2027, 1, 3))
    assert cm.period_label("week", date(2027, 1, 1)) == "28 Dec 2026 – 3 Jan 2027"
    assert cm.period_label("week", date(2026, 4, 1)) == "30 Mar – 5 Apr 2026"
    assert cm.period_label("month", TODAY) == "March 2026"
    assert cm.shift("month", date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert cm.shift("month", date(2026, 1, 31), -1) == date(2025, 12, 31)
    assert cm.shift("year", date(2028, 2, 29), 1) == date(2029, 2, 28)
    # the clocks change on 29 March and 25 October in Europe
    assert cm.shift("week", date(2026, 3, 25), 1) == date(2026, 4, 1)
    assert cm.shift("week", date(2026, 10, 21), 1) == date(2026, 10, 28)
    for view in cm.VIEWS:
        assert cm.shift(view, cm.LAST_DAY, 1) == cm.LAST_DAY
        assert cm.shift(view, cm.FIRST_DAY, -1) == cm.FIRST_DAY


def test_midnight_timer():
    late = datetime(2026, 3, 18, 23, 59, 30).astimezone()
    assert ms_to_midnight(late) == 31_000
    start = datetime(2026, 3, 29, 0, 0).astimezone()  # 23 hours long in Europe
    assert 22 * 3_600_000 < ms_to_midnight(start) <= 25 * 3_600_000 + 1000


def test_parse_split():
    assert parse_split("600,400") == (600, 400) and parse_split("600,0") == (600, 0)
    for bad in ("", "1", "a,b", "1,2,3", "0,5", "-5,5", "5,-1", "99999999999,1"):
        assert parse_split(bad) is None


def test_loads_are_computed_once(monkeypatch):
    calls = []
    real = cm.planned_load
    monkeypatch.setattr(cm, "planned_load", lambda w: calls.append(1) or real(w))
    loads = cm.Loads()
    for _ in range(3):
        assert loads.of_workout(Workout("W", [Steady(3600, 1.0)])) == pytest.approx(100)
    assert loads.of(unit(kind="outdoor", target_load=5.0)) == 5.0
    assert len(calls) == 1


def test_workout_shape():
    w = Workout("W", [Ramp(60, 0.4, 1.2), Repeat(2, [Sprint(10), Steady(20, 0.5)])])
    shape = workout_shape(w, 200, power_zones(200))
    assert {z for _, z, _ in shape} == {1, 2, 3, 4, 5} and any(s for _, _, s in shape)
    for path, _, _ in shape:
        box = path.boundingRect()
        assert box.left() >= 0 and box.right() <= 1.0001 and box.top() >= -0.0001
    assert workout_shape(Workout("E"), 200, power_zones(200)) == []


def test_totals():
    units = [
        unit("a", target_s=3600, target_load=50.0),
        unit("b", target_s=1800, skipped=True),
        unit("c", kind="rest"),
    ]
    rides = {
        1: Ride(1, TODAY, 3000.0, 40.0),
        2: Ride(2, TODAY + DAY, 600.0, None),
        3: Ride(3, TODAY, 60.0, 1.0),
    }

    def data(filtered):
        links = {"a": 1, "elsewhere": 3}
        return cm.build(TODAY, TODAY + DAY, TODAY, units, links, rides, cm.Loads(), str, filtered)

    sums = data(False).totals(TODAY, TODAY + DAY)
    assert (sums.planned_s, sums.planned_load, sums.planned) == (3600, 50, 1)
    assert (sums.done_s, sums.done_load, sums.done) == (3660, 41, 3)
    assert data(True).totals(TODAY, TODAY + DAY).done == 2
    assert data(False).totals(TODAY, TODAY).done == 2
    assert [e.state for e in data(False).entries(TODAY + DAY)] == ["ride"]


def test_year_fill_is_shaded_by_load():
    def fill(load):
        u = unit(target_load=load)
        return cm.year_fill([cm.make_entry(u, None, False, TODAY, cm.Loads())])

    assert fill(100.0) == fill(300.0) == theme.PLAN_COLORS["indoor"]
    levels = [cm._luminance(fill(v)) for v in (10.0, 50.0, 100.0)]
    assert levels == sorted(levels) and len(set(levels)) == 3
    assert cm.year_fill([]) is None
    rest = cm.make_entry(unit(kind="rest"), None, False, TODAY, cm.Loads())
    assert cm.year_fill([rest]) == theme.PLAN_COLORS["neutral"]


# --- views ---


@pytest.mark.parametrize("view", ["week", "month"])
def test_colors_in_views(page, plans, repo, qtbot, view):
    monday = TODAY - 2 * DAY
    sid = ride(repo, 600, TODAY - DAY)
    add(
        plans,
        page,
        unit("missed", day=monday),
        unit("done", day=TODAY - DAY, target_s=600),
        unit("today"),
        unit("out", day=TODAY + DAY, kind="outdoor"),
        unit("rest", day=TODAY + 2 * DAY, kind="rest"),
        unit("part", day=TODAY + 3 * DAY, target_s=3600),
    )
    plans.link("done", sid)
    page.show_period(view, TODAY)
    wanted = {
        "missed": "missed",
        "done": "done",
        "today": "indoor",
        "out": "outdoor",
        "rest": "neutral",
    }
    for uid, color in wanted.items():
        rect = page.calendar.rect_of(("unit", uid))
        inside = rect.topRight() + QPointF(-6, 2.5)  # beside the text
        assert color_at(page, inside) == theme.PLAN_COLORS[color], uid
    assert not page.message.text()


def test_year_view(page, plans, qtbot):
    add(plans, page, unit("a", day=date(2026, 7, 5), target_load=200.0), unit("b", day=TODAY - DAY))
    page.view_buttons["year"].click()
    view = page.calendar
    assert page.view == "year" and len(view.day_rects) == 365
    rect = view.rect_of_day(date(2026, 7, 5))
    assert color_at(page, rect.topLeft() + QPointF(3, 3)) == theme.PLAN_COLORS["indoor"]
    center = view.rect_of_day(TODAY - DAY).center()
    assert "T b" in view.tooltip_at(center) and "✗ Missed" in view.tooltip_at(center)
    qtbot.mouseClick(view, LEFT, pos=rect.center().toPoint())
    assert page.view == "month" and page.anchor == date(2026, 7, 5)
    assert page.period_label.text() == "July 2026" and page.view_buttons["month"].isChecked()
    page.show_period("year", date(2028, 1, 1))
    assert len(page.calendar.day_rects) == 366


def test_navigation(page, qtbot):
    assert page.view == "month" and page.period_label.text() == "March 2026"
    page.next_button.click()
    assert page.period_label.text() == "April 2026"
    page.previous_button.click()
    page.previous_button.click()
    assert page.period_label.text() == "February 2026"
    page.view_buttons["week"].click()
    assert page.period_label.text() == "16 – 22 Feb 2026"
    page.today_button.click()
    assert page.period_label.text() == "16 – 22 Mar 2026"
    page.view_buttons["month"].click()
    qtbot.mouseClick(
        page.calendar, LEFT, pos=page.calendar.rect_of_day(date(2026, 3, 30)).center().toPoint()
    )
    page.view_buttons["week"].click()  # the week that was clicked
    assert page.period_label.text() == "30 Mar – 5 Apr 2026"


def test_month_boundaries(page, plans):
    page.show_period("month", date(2026, 2, 1))
    days = [d for _, d in page.calendar.day_rects]
    assert days[0] == date(2026, 1, 26) and days[-1] == date(2026, 3, 1) and len(days) == 35
    add(plans, page, unit("a", day=date(2026, 1, 27), target_s=600))
    add(plans, page, unit("b", day=date(2026, 2, 28), target_s=1200))
    assert page.calendar.rect_of(("unit", "a")) is not None
    # only the month itself is summed up
    assert page.summary_label.text().startswith("Planned 20 min")
    page.show_period("week", date(2026, 12, 31))
    assert [d for _, d in page.calendar.day_rects][-1] == date(2027, 1, 3)
    page.show_period("month", date(2021, 2, 1))  # exactly four weeks
    assert len(page.calendar.day_rects) == 28


def test_dst_weeks(page, plans, repo, qtbot):
    for sunday in (date(2026, 3, 29), date(2026, 10, 25)):
        page.show_period("week", sunday)
        days = [d for _, d in page.calendar.day_rects]
        assert days == [sunday - (6 - i) * DAY for i in range(7)]
        late = ride(repo, 600, sunday, hour=23)
        page.refresh()
        assert [e.ride.session_id for e in page._data.entries(sunday)] == [late]
        uid = f"u{sunday.month}"
        add(plans, page, unit(uid, day=sunday))
        drag(page, qtbot, uid, sunday - DAY)
        assert plans.get_unit(uid).day == sunday - DAY


def test_overflow_opens_the_week(page, plans, qtbot):
    add(plans, page, *(unit(f"u{i}", position=i) for i in range(12)))
    view = page.calendar
    shown = [e for _, e in view.entry_rects if e.day == TODAY]
    [(rect, day, hidden)] = view.more_rects
    assert day == TODAY and len(shown) + hidden == 12
    assert "T u11" in view.tooltip_at(rect.center())
    qtbot.mouseClick(view, LEFT, pos=rect.center().toPoint())
    assert page.view == "week" and len(page.calendar.entry_rects) == 12
    scroll = page.stack.currentWidget()
    assert page.calendar.height() > scroll.viewport().height()


def test_tiny_month_cells(page, plans):
    add(plans, page, *(unit(f"u{i}", position=i) for i in range(3)))
    page.resize(660, 400)
    QApplication.processEvents()
    view = page.calendar
    for rect, e in view.entry_rects:
        assert view.rect_of_day(e.day).contains(rect)
    for rect, day, _ in view.more_rects:
        assert view.rect_of_day(day).contains(rect)
    for v in cm.VIEWS:
        page.show_period(v, TODAY)
        page.calendar.grab()
    assert not page.message.text()


def test_missed_after_midnight(page, plans, clock):
    add(plans, page, unit())
    assert entry(page).status is Status.PLANNED
    page.check_day()
    assert entry(page).state == "indoor"
    clock.day = TODAY + DAY
    page.check_day()
    assert entry(page).state == "missed" and page._day_timer.isActive()
    assert page.anchor == TODAY  # the period stays
    clock.day = TODAY
    page.hide()
    page.check_day()
    assert entry(page).state == "missed"
    page.show()
    assert entry(page).state == "indoor"


def test_refresh_waits_until_shown(page, plans, monkeypatch):
    page.hide()
    plans.apply("user", "x", [unit()])
    page.refresh()
    assert entry(page) is None
    page.show()
    assert entry(page) is not None


def test_refresh_is_cheap(page, plans, repo, monkeypatch):
    add(plans, page, *(unit(f"u{i}", day=TODAY + i * DAY) for i in range(5)))
    calls = []
    for name in ("units_between", "links", "rides_between"):
        real = getattr(plans, name)
        monkeypatch.setattr(
            plans, name, lambda *a, n=name, r=real, **k: calls.append(n) or r(*a, **k)
        )
    page.refresh()
    assert sorted(calls) == ["links", "rides_between", "units_between"]


def test_rides_without_unit(page, repo, qtbot):
    sid = ride(repo, 600, name="<b>Zwift</b>")
    page.refresh()
    [found] = page._data.entries(TODAY)
    assert found.title == "<b>Zwift</b>" and found.state == "ride"
    rect = page.calendar.rect_of(("ride", str(sid)))
    qtbot.mouseClick(page.calendar, LEFT, pos=rect.center().toPoint())
    assert page.detail.isVisible() and page.detail.title.text() == "<b>Zwift</b>"
    assert "Ridden: 10 min" in page.detail.facts.text()
    assert not page.detail.buttons["edit"].isVisible()
    repo.delete_session(sid)
    page.refresh()
    assert not page.detail.isVisible()


def test_ride_linked_to_another_day(page, plans, repo):
    sid = ride(repo, 600, TODAY + 60 * DAY)
    add(plans, page, unit(target_s=600, day=TODAY - DAY))
    plans.link("u", sid, "started")
    page.refresh()
    assert entry(page).state == "done"
    page.show_period("month", TODAY + 60 * DAY)
    assert page._data.entries(TODAY + 60 * DAY) == []  # it belongs to the unit


def test_unfinished_ride(page, plans, repo):
    plans.apply("user", "x", [unit()])
    repo.start_session(Workout("W", [Steady(60, 1.0)]), 200, "u")
    page.refresh()
    assert entry(page).state == "indoor" and entry(page).linked


# --- detail and actions ---


def test_detail(page, plans, repo, qtbot):
    w = Workout("W", [Steady(600, 0.8)])
    repo.save_workout(w)
    plan = Plan("p", "Base", TODAY, TODAY + DAY)
    plans.apply("user", "x", [plan])
    add(plans, page, unit(workout=w, notes="Easy!", plan_id="p", origin="ai"))
    click(page, qtbot)
    d = page.detail
    assert d.isVisible() and d.plot.isVisible() and d.plot.pieces
    assert d.status.text() == "○ Planned" and d.notes.toPlainText() == "Easy!"
    assert "Base" in d.subtitle.text() and "AI coach" in d.subtitle.text()
    assert d.facts.text().startswith("Planned: 10 min, load 11")
    assert d.buttons["ride"].isEnabled() and d.buttons["library"].isEnabled()
    page.next_button.click()  # stays open
    assert d.isVisible() and d.title.text() == "T u"
    page.refresh()
    assert d.isVisible()
    d.close_button.click()
    assert not d.isVisible()


def test_detail_place(page, plans, qtbot):
    add(plans, page, unit())
    click(page, qtbot)
    QApplication.processEvents()
    assert page.detail.x() > page.stack.x()
    page.resize(660, 760)
    QApplication.processEvents()
    assert page.detail.y() > page.stack.y() and page.detail.width() > 500


def test_double_click_does_not_hit_new_buttons(page, plans, qtbot):
    add(plans, page, unit())
    click(page, qtbot)
    qtbot.mouseClick(page.detail.buttons["skip"], LEFT)
    assert not plans.get_unit("u").skipped
    qtbot.wait(QApplication.doubleClickInterval() + 20)
    qtbot.mouseClick(page.detail.buttons["skip"], LEFT)
    assert plans.get_unit("u").skipped


def test_ride_signal(page, plans, repo, qtbot):
    w = Workout("W", [Steady(600, 0.8)])
    add(plans, page, unit(workout=w), unit("out", kind="outdoor"), unit("empty"))
    page._select(entry(page))
    with qtbot.waitSignal(page.ride_requested) as signal:
        page.detail.buttons["ride"].click()
    assert signal.args == [w, "u"]
    for uid in ("out", "empty"):
        page._select(entry(page, uid))
        assert not page.detail.buttons["ride"].isEnabled()


def test_open_workout(page, plans, repo, qtbot):
    w = Workout("W", [Steady(600, 0.8)])
    add(plans, page, unit(workout=w))
    click(page, qtbot)
    assert not page.detail.buttons["library"].isEnabled()
    repo.save_workout(w)
    page.refresh()
    with qtbot.waitSignal(page.workout_requested) as signal:
        page.detail.buttons["library"].click()
    assert signal.args == [w.id]
    repo.delete_workout(w.id)
    page.detail.buttons["library"].click()
    assert "no longer in the library" in page.message.text()


def test_skip_and_delete(page, plans, qtbot):
    add(plans, page, unit())
    click(page, qtbot)
    b = page.detail.buttons
    with qtbot.waitSignal(page.plan_changed):
        b["skip"].click()
    assert entry(page).state == "skipped" and b["skip"].text() == "Unskip"
    b["skip"].click()
    assert entry(page).state == "indoor" and b["skip"].text() == "Skip"
    page.ask_confirm = lambda _text: False
    b["delete"].click()
    assert plans.get_unit("u") is not None
    page.ask_confirm = lambda _text: True
    b["delete"].click()
    assert plans.get_unit("u") is None and not page.detail.isVisible()
    [change, *_] = plans.changes()
    assert change.actor == "user" and change.summary.startswith("Deleted “T u”")


def test_move(page, plans, repo, qtbot):
    add(plans, page, unit(), unit("other", day=TODAY + DAY, position=4))
    click(page, qtbot)
    page.ask_date = lambda _title, _day: None
    page.detail.buttons["move"].click()
    assert plans.get_unit("u").day == TODAY
    page.ask_date = lambda _title, day: day + DAY
    page.detail.buttons["move"].click()
    moved = plans.get_unit("u")
    assert moved.day == TODAY + DAY and moved.position == 5
    assert page.detail.entry.day == TODAY + DAY


def test_link_and_unlink(page, plans, repo, qtbot):
    add(plans, page, unit(target_s=600))
    click(page, qtbot)
    b = page.detail.buttons
    b["link"].click()
    assert "no free ride" in page.message.text()
    ride(repo, 60, TODAY - DAY, name="near")
    ride(repo, 60, TODAY - 2 * DAY, name="far")
    sid = ride(repo, 400, TODAY + DAY, name="chosen")
    offered = []
    page.pick = lambda _t, _l, texts: offered.extend(texts) or 1
    with qtbot.waitSignal(page.plan_changed):
        b["link"].click()
    assert len(offered) == 2 and "chosen" in offered[1]
    assert plans.links()["u"].session_id == sid and plans.links()["u"].how == "manual"
    assert entry(page).state == "partial" and "(67%)" in page.detail.facts.text()
    assert "linked by hand" in page.detail.facts.text()
    assert not b["move"].isEnabled() and not b["skip"].isEnabled() and b["link"].text() == "Unlink"
    b["link"].click()
    assert plans.links() == {} and entry(page).state == "indoor"
    page.pick = lambda *_a: None
    b["link"].click()
    assert plans.links() == {}


def test_added_past_unit_is_matched(page, plans, repo):
    ride(repo, 600, TODAY - DAY)
    page.ask_unit = lambda day, unit_=None: unit(day=day, target_s=600)
    page._add(TODAY - DAY)
    assert plans.links()["u"].how == "auto" and entry(page).state == "done"


def test_add(page, plans, qtbot):
    asked = []
    page.ask_unit = lambda day, unit_=None: asked.append(day) or None
    page.add_button.click()
    assert asked == [TODAY] and plans.changes() == []
    page.ask_unit = lambda day, unit_=None: asked.append(day) or unit("n", day=day)
    day = date(2026, 3, 5)
    qtbot.mouseClick(page.calendar, LEFT, pos=page.calendar.rect_of_day(day).center().toPoint())
    qtbot.mouseDClick(page.calendar, LEFT, pos=page.calendar.rect_of_day(day).center().toPoint())
    assert plans.get_unit("n").day == day and page.detail.title.text() == "T n"
    page.calendar.set_cursor_day(date(2026, 3, 9))
    page.add_button.click()
    assert asked[-1] == date(2026, 3, 9)


def test_double_click_edits(page, plans, qtbot):
    add(plans, page, unit())
    page.ask_unit = lambda day, old=None: replace(old, title="New")
    click(page, qtbot)
    pos = page.calendar.rect_of(("unit", "u")).center().toPoint()
    qtbot.mouseDClick(page.calendar, LEFT, pos=pos)
    assert plans.get_unit("u").title == "New"
    page.ask_unit = lambda day, old=None: replace(old, day=day + DAY)
    page.detail.buttons["edit"].click()
    assert plans.get_unit("u").day == TODAY + DAY


def test_changed_by_the_ai_meanwhile(page, plans, qtbot):
    add(plans, page, unit())
    click(page, qtbot)

    def ask(day, old=None):
        plans.apply("ai", "y", [replace(old, title="AI")])
        page.refresh()
        return replace(old, title="Mine")

    page.ask_unit = ask
    page.detail.buttons["edit"].click()
    assert plans.get_unit("u").title == "AI" and "in the meantime" in page.message.text()

    def confirm(_text):
        plans.apply("ai", "y", deletes=[plans.get_unit("u")])
        return True

    page.ask_confirm = confirm
    page.detail.buttons["delete"].click()
    assert "in the meantime" in page.message.text()
    page.detail.buttons["edit"].click()
    assert "no longer exists" in page.message.text()


# --- drag ---


@pytest.mark.parametrize("view", ["week", "month"])
def test_drag(page, plans, qtbot, view):
    add(plans, page, unit())
    page.show_period(view, TODAY)
    with qtbot.waitSignal(page.plan_changed):
        drag(page, qtbot, "u", TODAY + 2 * DAY)
    assert plans.get_unit("u").day == TODAY + 2 * DAY and not page.detail.isVisible()
    drag(page, qtbot, "u", TODAY + 2 * DAY)  # same day
    assert len(plans.changes()) == 2


def test_drag_cancelled(page, plans, qtbot):
    add(plans, page, unit())
    view = page.calendar
    start = view.rect_of(("unit", "u")).center().toPoint()
    end = view.rect_of_day(TODAY + DAY).center().toPoint()
    qtbot.mousePress(view, LEFT, pos=start)
    move(view, end)
    assert view._drag is not None
    view.grab()
    qtbot.keyClick(view, Qt.Key.Key_Escape)
    qtbot.mouseRelease(view, LEFT, pos=end)
    assert plans.get_unit("u").day == TODAY
    qtbot.mousePress(view, LEFT, pos=start)
    move(view, end)
    page.refresh()  # the AI changed something
    qtbot.mouseRelease(view, LEFT, pos=end)
    assert plans.get_unit("u").day == TODAY
    qtbot.mousePress(view, LEFT, pos=start)
    move(view, end)
    qtbot.mouseRelease(view, LEFT, pos=QPoint(-50, -50))
    assert plans.get_unit("u").day == TODAY


def test_linked_units_and_rides_stay(page, plans, repo, qtbot):
    sid = ride(repo)
    other = ride(repo, hour=18)
    add(plans, page, unit())
    plans.link("u", sid)
    page.refresh()
    drag(page, qtbot, "u", TODAY + DAY)
    assert plans.get_unit("u").day == TODAY and "cannot be moved" in page.message.text()
    view = page.calendar
    start = view.rect_of(("ride", str(other))).center().toPoint()
    qtbot.mousePress(view, LEFT, pos=start)
    move(view, start + QPoint(50, 50))
    assert view._drag is None
    with pytest.raises(ValueError):  # e.g. linked while it was dragged
        page._move_to(plans.get_unit("u"), TODAY + DAY)


def test_keyboard(page, plans, qtbot):
    add(plans, page, unit("a"), unit("b", position=1))
    view = page.calendar
    qtbot.keyClick(view, Qt.Key.Key_Right)
    assert view.cursor_day == TODAY + DAY
    qtbot.keyClick(view, Qt.Key.Key_Left)
    assert "T a" in view.accessibleDescription() and view.accessibleName() == "Month calendar"
    for title in ("T a", "T b", "T a"):
        qtbot.keyClick(view, Qt.Key.Key_Return)
        assert page.detail.title.text() == title
    for _ in range(5):
        qtbot.keyClick(view, Qt.Key.Key_Down)
    assert page.period_label.text() == "April 2026" and view.cursor_day == TODAY + 35 * DAY
    asked = []
    page.ask_unit = lambda day, _unit=None: asked.append(day)
    qtbot.keyClick(view, Qt.Key.Key_Insert)
    assert asked == [TODAY + 35 * DAY]


# --- plans ---


def test_plans(page, plans, qtbot):
    plan = Plan("p", "Base", TODAY, TODAY + 27 * DAY)
    assert not page.actions["edit"].isEnabled()
    page.ask_plan = lambda old=None: plan
    with qtbot.waitSignal(page.plan_changed):
        page.actions["new"].trigger()
    assert plans.list_plans() == [plan] and page.plan_combo.currentData() == "p"
    add(plans, page, unit("in", plan_id="p"), unit("out"))
    assert entry(page, "in") is not None and entry(page, "out") is None
    page.plan_combo.setCurrentIndex(0)
    page.plan_combo.activated.emit(0)
    assert entry(page, "out") is not None
    page.ask_plan = lambda old=None: replace(old, name="Build")
    page.actions["edit"].trigger()
    assert page.plan_combo.itemText(1) == "Build"
    page.actions["archive"].trigger()
    assert plans.list_plans() == [] and entry(page, "in") is None
    assert page.plan_combo.count() == 1
    page.pick = lambda *_a: 0
    page.actions["restore"].trigger()
    assert entry(page, "in") is not None
    page.actions["restore"].trigger()
    assert "no archived plan" in page.message.text()


def test_choosing_between_plans(page, plans):
    a, b = (Plan(i, i, TODAY, TODAY) for i in "ab")
    add(plans, page, a, b)
    page.pick = lambda *_a: 1
    page.ask_plan = lambda old=None: replace(old, goal="G")
    page.actions["edit"].trigger()
    assert plans.get_plan("b").goal == "G" and plans.get_plan("a").goal == ""
    page.pick = lambda *_a: None
    page.actions["archive"].trigger()
    assert len(plans.list_plans()) == 2


# --- dialogs ---


def test_unit_dialog_new(qtbot):
    w = Workout("Intervals", [Steady(600, 0.8)])
    dialog = UnitDialog(TODAY, None, [w], [Plan("p", "Base", TODAY, TODAY)], "p")
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.accept()
    assert dialog.isVisible() and "needs a title" in dialog.message.text()
    dialog.workout.setCurrentIndex(1)
    assert dialog.title.text() == "Intervals" and not dialog.duration.isVisible()
    assert dialog.info.text() == "10 min, load 11"
    u = dialog.value()
    assert u.workout == w and u.plan_id == "p" and u.day == TODAY and u.target_s is None
    dialog.kind.setCurrentIndex(1)  # outdoor
    assert not dialog.workout.isVisible() and dialog.duration.isVisible()
    dialog.title.setText("Long")
    dialog.title.textEdited.emit("Long")
    dialog.duration.setValue(90)
    dialog.load.setValue(80)
    dialog.day.setDate(QDate(2026, 5, 1))
    dialog.notes.setPlainText("N")
    u = dialog.value()
    assert (u.kind, u.title, u.target_s, u.target_load) == ("outdoor", "Long", 5400, 80)
    assert u.workout is None and u.day == date(2026, 5, 1) and u.notes == "N"
    dialog.kind.setCurrentIndex(2)  # rest
    assert dialog.value().target_s is None
    dialog.accept()
    assert dialog.result() == 1


def test_unit_dialog_keeps_what_it_cannot_show(qtbot):
    old = Workout("Old", [Steady(600, 0.8)])
    newer = Workout("Newer", [Steady(900, 0.8)], id=old.id)
    u = unit(workout=old, target_s=95, target_load=12.3, plan_id="gone", skipped=True, position=3)
    for library in ([], [newer]):
        dialog = UnitDialog(TODAY, u, library, [], linked=True)
        qtbot.addWidget(dialog)
        assert dialog.value() == u
        assert not dialog.day.isEnabled() and not dialog.kind.isEnabled()
    dialog.workout.setCurrentIndex(0)
    assert (dialog.value().workout, dialog.value().target_s) == (None, 95)
    dialog.duration.setValue(10)
    assert dialog.value().target_s == 600


def test_other_dialogs(qtbot):
    dialog = PlanDialog(None, TODAY)
    qtbot.addWidget(dialog)
    dialog.accept()
    assert "needs a name" in dialog.message.text()
    dialog.name.setText("Base")
    dialog.end.setDate(QDate(TODAY - DAY))
    dialog.accept()
    assert "cannot end before" in dialog.message.text()
    dialog.end.setDate(QDate(TODAY))
    assert dialog.value().name == "Base" and dialog.value().end_day == TODAY
    old = Plan("p", "P", TODAY, TODAY, origin="ai")
    edit = PlanDialog(old, TODAY)
    qtbot.addWidget(edit)
    assert edit.value() == old
    day = DateDialog("Move", TODAY)
    qtbot.addWidget(day)
    assert day.value() == TODAY


def test_dialogs_are_wired(page, plans, monkeypatch):
    monkeypatch.setattr(plan_page.UnitDialog, "exec", lambda self: 0)
    monkeypatch.setattr(plan_page.PlanDialog, "exec", lambda self: self.name.setText("N") or 1)
    monkeypatch.setattr(plan_page.DateDialog, "exec", lambda self: 1)
    assert page.ask_unit(TODAY) is None
    assert page.ask_plan().name == "N"
    assert page.ask_date("Move", TODAY) == TODAY


# --- errors ---


def broken(*_args, **_kwargs):
    raise OSError("disk full")


def test_storage_errors_are_shown(page, plans, repo, qtbot, monkeypatch):
    add(plans, page, unit())
    click(page, qtbot)
    monkeypatch.setattr(plans, "apply", broken)
    page.detail.buttons["skip"].click()
    assert "disk full" in page.message.text() and page.message.isVisible()
    page.detail.buttons["delete"].click()
    assert "disk full" in page.message.text()
    drag(page, qtbot, "u", TODAY + DAY)
    assert "disk full" in page.message.text() and entry(page).day == TODAY
    monkeypatch.setattr(plans, "links", broken)
    page.next_button.click()
    assert "disk full" in page.message.text()
    monkeypatch.undo()
    page.ask_confirm = lambda _text: False
    page.next_button.click()
    assert page.message.text() == "" and not page.message.isVisible()
    monkeypatch.setattr(plans, "units_between", broken)
    page.refresh()
    page.hide()
    page.show()
    assert "disk full" in page.message.text()


# --- coach panel and splitter ---


def coach_page(repo, plans, qtbot):
    page = PlanPage(repo, plans, QLabel("coach"), today=lambda: TODAY)
    qtbot.addWidget(page)
    page.resize(1070, 800)
    page.show()
    return page


def test_splitter(repo, plans, qtbot):
    page = coach_page(repo, plans, qtbot)
    assert all(page.splitter.sizes()) and page.coach_button.isVisible()
    assert page.splitter.isCollapsible(1) and not page.splitter.isCollapsible(0)
    page.splitter.moveSplitter(700, 1)
    assert repo.get_setting(COACH_SPLIT) == ""  # debounced
    qtbot.waitUntil(lambda: repo.get_setting(COACH_SPLIT) != "", timeout=3000)
    left, right = parse_split(repo.get_setting(COACH_SPLIT))
    assert left == 700
    again = coach_page(repo, plans, qtbot)
    assert again.splitter.sizes() == [left, right]
    again.coach_button.click()
    assert again.splitter.sizes()[1] == 0
    again.hide()  # saved at once
    assert repo.get_setting(COACH_SPLIT).endswith(",0")
    third = coach_page(repo, plans, qtbot)
    assert third.splitter.sizes()[1] == 0
    third.coach_button.click()
    assert third.splitter.sizes()[1] > 100


@pytest.mark.parametrize("stored", ["x", "0,0", "-1,5", "9,9,9"])
def test_bad_splitter_setting(repo, plans, qtbot, stored):
    repo.set_setting(COACH_SPLIT, stored)
    page = coach_page(repo, plans, qtbot)
    assert all(page.splitter.sizes()) and page.message.text() == ""


def test_without_coach(page):
    assert page.splitter.count() == 1 and not page.coach_button.isVisible()


def test_first_view_is_the_selected_one(page):
    assert page.view == "month" and page.calendar.isVisible()
