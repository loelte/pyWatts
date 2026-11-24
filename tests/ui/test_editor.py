from copy import deepcopy
from uuid import uuid4

import pytest

from pywatts.core import zwo
from pywatts.core.workout import Block, Ramp, Repeat, Steady, Workout, flatten
from pywatts.storage.db import open_db
from pywatts.storage.repo import DEFAULTS, FTP_W, Repo
from pywatts.ui.editor import EditorPage
from pywatts.ui.widgets import editor_model as em
from pywatts.ui.widgets.editor_model import EditError, EditorModel


class FakeRepo:
    """Used only while the real storage is a stub."""

    def __init__(self):
        self.w, self.s = {}, {}

    def list_workouts(self):
        return sorted(deepcopy(list(self.w.values())), key=lambda w: w.name.lower())

    def get_workout(self, i):
        return deepcopy(self.w.get(i))

    def save_workout(self, w):
        self.w[w.id] = deepcopy(w)

    def delete_workout(self, i):
        self.w.pop(i, None)

    def get_setting(self, k):
        return self.s.get(k, DEFAULTS.get(k, ""))

    def set_setting(self, k, v):
        self.s[k] = v


def fake_flatten(w):
    out, t = [], 0
    for seg in w.segments:
        parts = seg.children * seg.count if isinstance(seg, Repeat) else [seg]
        for p in parts:
            a, b = (p.power, p.power) if isinstance(p, Steady) else (p.start, p.end)
            out.append(Block(t, t + p.duration_s, a, b))
            t += p.duration_s
    return out


@pytest.fixture(autouse=True)
def core(monkeypatch):
    try:
        flatten(Workout("x"))
    except NotImplementedError:
        monkeypatch.setattr(em, "flatten", fake_flatten)
        monkeypatch.setattr(
            em, "total_duration", lambda w: int(sum(b.t1 - b.t0 for b in fake_flatten(w)))
        )


@pytest.fixture
def repo(tmp_path):
    try:
        conn = open_db(tmp_path / "t.db")
        r = Repo(conn)
        r.list_workouts()
    except NotImplementedError:
        yield FakeRepo()
        return
    yield r
    conn.close()


class Page(EditorPage):
    answer = "cancel"
    confirm = True
    open_path = None
    save_path = None
    asked = 0

    def ask_unsaved(self):
        self.asked += 1
        return self.answer

    def ask_confirm(self, text):
        return self.confirm

    def pick_open_path(self):
        return self.open_path

    def pick_save_path(self, suggested):
        self.suggested = suggested
        return self.save_path


A = Workout("A", [Steady(60, 0.5), Repeat(2, [Steady(30, 1.0), Ramp(30, 0.5, 1.0)])])
B = Workout("B", [Steady(120, 0.6)])


@pytest.fixture
def page(qtbot, repo):
    repo.save_workout(A)
    repo.save_workout(B)
    p = Page(repo)
    qtbot.addWidget(p)
    return p


def click(page, key):
    page.buttons[key].click()


def type_(edit, text):
    edit.setText(text)
    edit.textEdited.emit(text)


def dirty_it(page):
    type_(page.name_edit, "changed")


# --- model ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,s", [("5:00", 300), ("5", 300), ("0:01", 1), ("90:00", 5400), ("1:00:05", 3605)]
)
def test_parse_duration(text, s):
    assert em.parse_duration(text) == s


@pytest.mark.parametrize("text", ["", "0", "0:00", "x", "1:60", "-1", "1.5", "²", "9999"])
def test_parse_duration_bad(text):
    with pytest.raises(EditError):
        em.parse_duration(text)


@pytest.mark.parametrize("text,p", [("88", 0.88), ("88,5 %", 0.885), ("0", 0), ("500", 5)])
def test_parse_percent(text, p):
    assert em.parse_percent(text) == p


@pytest.mark.parametrize("text", ["", "nan", "inf", "-5", "501", "1e2", "%", "²", "9" * 400])
def test_parse_percent_bad(text):
    with pytest.raises(EditError):
        em.parse_percent(text)


@pytest.mark.parametrize("text", ["", "0", "101", "x", "1.5", "²"])
def test_parse_count_bad(text):
    with pytest.raises(EditError):
        em.parse_count(text)


def test_formats():
    assert em.format_percent(0.885) == "88.5"
    assert em.format_percent(1.0) == "100"
    assert em.format_total(3725) == "1:02:05"
    assert em.watts(0.75, 200) == 150


def test_model_structure():
    m = EditorModel()
    p = m.add(em.default_steady())
    assert p == (0,) and m.dirty
    r = m.add(em.default_repeat(), p)
    assert r == (1,)
    c = m.add_child(r, em.default_ramp())
    assert c == (1, 2)
    assert m.add(em.default_steady(), (1, 0)) == (1, 1)
    assert m.add(em.default_repeat(), (1, 0)) == (2,)  # never nested
    assert m.duplicate((1, 0)) == (1, 1)
    assert m.move((1, 1), -1) == (1, 0)
    assert m.move((1, 0), -1) == (1, 0)
    assert m.move((0,), 1) == (1,)
    assert isinstance(m.get((0,)), Repeat)
    assert m.delete((2,)) == (1,)
    m.delete((1,))
    assert m.delete((0,)) is None and m.empty
    assert not m.dirty


def test_model_errors_leave_it_unchanged():
    m = EditorModel(A)
    with pytest.raises(EditError):
        m.add_child((0,), em.default_steady())
    with pytest.raises(EditError):
        m.add_child((1,), em.default_repeat())
    with pytest.raises(EditError):
        m.set_field((1,), "power", "50")
    with pytest.raises(EditError):
        m.get((9,))
    with pytest.raises(EditError):
        m.get((0, 0))
    m.delete((1, 0))
    with pytest.raises(EditError):
        m.delete((1, 0))
    m.revert()
    assert not m.dirty


def test_model_does_not_alias_its_source():
    src = deepcopy(A)
    m = EditorModel(src)
    m.set_field((1, 0), "duration", "9:00")
    m.set_field((1,), "count", "7")
    assert src == A
    out = m.workout()
    out.segments.clear()
    assert not m.empty
    dup = EditorModel.copy_of(src)
    assert dup.id != src.id and dup.name == "A (copy)"


def test_model_name_required_and_stripped():
    m = EditorModel(A)
    m.name = "  "
    with pytest.raises(EditError):
        m.prepare_save()
    m.name = " N "
    assert m.prepare_save().name == "N"


# --- page -------------------------------------------------------------------------------------


def test_initial_state(page):
    assert page.model.id == A.id
    assert page.library.currentItem().text() == "A"
    assert "3:00" in page.total.text()
    assert page.tree.topLevelItem(1).childCount() == 2
    assert page.tree.topLevelItem(0).text(3) == "100 W"
    assert not page.dirty


def test_empty_library(qtbot, repo):
    p = Page(repo)
    qtbot.addWidget(p)
    assert p.model.empty and not p.dirty
    with qtbot.assertNotEmitted(p.ride_requested):
        click(p, "ride")
    assert "empty" in p.message.text()


def test_edit_fields_live_and_save(page, repo):
    page.select_segment((0,))
    type_(page.fields["duration"], "2:00")
    type_(page.fields["power"], "80")
    assert page.field_watts["power"].text() == "160 W"
    assert "4:00" in page.total.text()
    block, zone = page.plot.pieces[0]
    assert block.t1 == 120 and block.p_start == 0.8 and zone == 3
    page.select_segment((1,))
    type_(page.fields["count"], "3")
    assert page.dirty
    click(page, "save")
    assert not page.dirty
    stored = repo.get_workout(A.id)
    assert stored.segments[0] == Steady(120, 0.8) and stored.segments[1].count == 3


@pytest.mark.parametrize("field,bad", [("duration", "abc"), ("power", "nan")])
def test_invalid_input_is_shown_and_blocks_save(page, repo, field, bad):
    page.select_segment((0,))
    type_(page.fields[field], bad)
    assert page.message.text() and page.dirty
    click(page, "save")
    assert "Fix" in page.message.text()
    assert repo.get_workout(A.id).segments[0] == Steady(60, 0.5)
    page.answer = "save"
    assert not page.confirm_discard()
    page.refresh()
    assert page.fields[field].text() == bad
    page.answer = "discard"
    assert page.confirm_discard() and not page.dirty
    assert page.fields[field].text() != bad


def test_invalid_text_dropped_on_row_change_is_reported(page):
    page.select_segment((0,))
    type_(page.fields["power"], "x")
    page.select_segment((1, 1))
    assert "not applied" in page.message.text()
    assert not page.dirty


def test_structure_buttons(page):
    page.select_segment((1,))
    click(page, "child_ramp")
    assert page._path == (1, 2)
    click(page, "up")
    assert page._path == (1, 1)
    click(page, "dup_segment")
    click(page, "del_segment")
    click(page, "down")
    page.select_segment((0,))
    click(page, "child_steady")
    assert "repeat" in page.message.text()
    for key in ("add_steady", "add_ramp", "add_repeat"):
        click(page, key)
    assert len(page.model.segments) == 5
    page.select_segment(None)
    click(page, "del_segment")
    assert "Select" in page.message.text()


def test_select_other_confirms(page, qtbot):
    dirty_it(page)
    page.library.setCurrentRow(1)
    qtbot.waitUntil(lambda: page.asked == 1)
    assert page.model.id == A.id
    assert page.library.currentRow() == 0
    page.answer = "discard"
    page.library.setCurrentRow(1)
    qtbot.waitUntil(lambda: page.model.id == B.id)
    assert page.library.currentRow() == 1


def test_select_other_saves(page, repo, qtbot):
    dirty_it(page)  # re-sorts the list while selecting
    type_(page.name_edit, "Z")
    page.answer = "save"
    page.library.setCurrentRow(1)
    qtbot.waitUntil(lambda: page.model.id == B.id)
    assert repo.get_workout(A.id).name == "Z"
    assert page.library.currentItem().text() == "B"


@pytest.mark.parametrize("key", ["new", "duplicate", "ride"])
def test_actions_cancelled_keep_edits(page, qtbot, key):
    dirty_it(page)
    with qtbot.assertNotEmitted(page.ride_requested):
        click(page, key)
    assert page.asked == 1 and page.model.id == A.id and page.dirty


def test_new_and_save(page, repo):
    click(page, "new")
    assert not page.dirty and page.library.currentRow() == -1
    assert not page.buttons["delete"].isEnabled()
    type_(page.name_edit, "Mine")
    page.desc_edit.setPlainText("d")
    click(page, "add_steady")
    click(page, "save")
    w = next(w for w in repo.list_workouts() if w.name == "Mine")
    assert w.description == "d" and len(w.segments) == 1
    assert page.library.currentItem().text() == "Mine"


def test_duplicate_and_delete(page, repo):
    click(page, "duplicate")
    assert page.model.name == "A (copy)" and len(repo.list_workouts()) == 3
    page.confirm = False
    click(page, "delete")
    assert len(repo.list_workouts()) == 3
    page.confirm = True
    for _ in range(3):
        click(page, "delete")
    assert repo.list_workouts() == [] and page.model.empty and not page.dirty


def test_ride_emits_independent_copy(page, qtbot):
    with qtbot.waitSignal(page.ride_requested) as sig:
        click(page, "ride")
    assert sig.args[0].id == A.id
    sig.args[0].segments.clear()
    assert not page.model.empty


def test_ride_discarding_to_empty_is_refused(page, qtbot):
    click(page, "new")
    click(page, "add_steady")
    page.answer = "discard"
    with qtbot.assertNotEmitted(page.ride_requested):
        click(page, "ride")
    assert "empty" in page.message.text()


def test_import(page, repo, tmp_path, monkeypatch):
    page.open_path = tmp_path / "x.zwo"
    monkeypatch.setattr(zwo, "load", lambda p: Workout("", [Steady(10, 1)], id=uuid4()))
    dirty_it(page)
    click(page, "import")
    assert len(repo.list_workouts()) == 2
    page.answer = "discard"
    click(page, "import")
    assert page.model.name == "x" and len(repo.list_workouts()) == 3


def test_import_error_shown_and_edits_kept(page, tmp_path, monkeypatch):
    def bad(_p):
        raise ValueError("bad xml")

    monkeypatch.setattr(zwo, "load", bad)
    page.open_path = tmp_path / "x.zwo"
    dirty_it(page)
    click(page, "import")
    assert "bad xml" in page.message.text() and page.asked == 0 and page.dirty


def test_export(page, tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(zwo, "save", lambda w, p: saved.append((w, p)))
    type_(page.name_edit, "a/b:c")
    page.save_path = tmp_path / "out"
    click(page, "export")
    assert page.suggested == "a_b_c.zwo"
    assert saved[0][1] == tmp_path / "out.zwo"
    (tmp_path / "out.zwo").touch()
    page.confirm = False
    click(page, "export")
    assert len(saved) == 1


def test_zwo_roundtrip(page, repo, tmp_path):
    page.save_path = page.open_path = tmp_path / "w.zwo"
    try:
        zwo.dumps(B)
    except NotImplementedError:
        pytest.skip("zwo is a stub")
    click(page, "export")
    click(page, "import")
    assert len(repo.list_workouts()) == 3 and page.model.id != A.id
    assert page.model.total_duration() == 180


def test_refresh_keeps_edits_and_reads_ftp(page, repo):
    page.select_segment((0,))
    type_(page.fields["power"], "60")
    repo.set_setting(FTP_W, "300")
    repo.save_workout(Workout("C"))
    page.refresh()
    assert page.dirty and page._path == (0,)
    assert page.field_watts["power"].text() == "180 W"
    assert page.library.count() == 3
    repo.set_setting(FTP_W, "junk")
    page.refresh()
    assert page._ftp == 200


def test_storage_failure_is_shown(page, repo, monkeypatch):
    def boom(*_):
        raise OSError("disk full")

    monkeypatch.setattr(repo, "save_workout", boom)
    dirty_it(page)
    click(page, "save")
    assert "disk full" in page.message.text() and page.dirty
    page.answer = "save"
    assert not page.confirm_discard()


def test_empty_name_not_saved(page, repo):
    type_(page.name_edit, " ")
    click(page, "save")
    assert "name" in page.message.text()
    assert repo.get_workout(A.id).name == "A"


def test_sprint_segment(page, repo):
    click(page, "new")
    click(page, "add_sprint")
    item = page.tree.currentItem()
    assert [item.text(c) for c in range(3)] == ["Sprint", "0:15", "open"]
    click(page, "add_repeat")
    click(page, "child_sprint")
    page.name_edit.setText("S")
    page.model.name = "S"
    assert page.save()
    stored = repo.get_workout(page.model.id)
    assert [type(s).__name__ for s in stored.segments] == ["Sprint", "Repeat"]
    assert type(stored.segments[1].children[-1]).__name__ == "Sprint"
