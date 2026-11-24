"""Workout library + editor page. Editing rules live in widgets/editor_model.py."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from uuid import UUID

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pywatts.core import zwo
from pywatts.core.workout import Ramp, Repeat, Segment, Sprint, Steady
from pywatts.core.zones import ZONE_COUNT, power_zones
from pywatts.storage.profile import load_profile
from pywatts.storage.repo import DEFAULTS, FTP_W, Repo
from pywatts.ui import theme
from pywatts.ui.history import sanitize_filename
from pywatts.ui.widgets import editor_model as em
from pywatts.ui.widgets.common import card, page_layout, text_label
from pywatts.ui.widgets.editor_model import EditError, EditorModel
from pywatts.ui.widgets.profile_plot import ProfilePlot

ROLE = Qt.ItemDataRole.UserRole
ZWO_FILTER = "Zwift workout (*.zwo)"
FIELD_LABELS = {
    "duration": "Duration (mm:ss)",
    "power": "Power (% FTP)",
    "start": "Start (% FTP)",
    "end": "End (% FTP)",
    "cadence": "Target cadence (rpm, optional)",
    "count": "Repeat count",
}


class EditorPage(QWidget):
    ride_requested = pyqtSignal(object)  # Workout

    def __init__(self, repo: Repo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self.model = EditorModel()
        self._ftp = int(DEFAULTS[FTP_W])
        self._zones = power_zones(self._ftp)
        self._library: dict[UUID, str] = {}
        self._invalid: dict[str, str] = {}  # field -> message, text not applied to the model
        self._path: em.Path | None = None
        # Deferred: the list must not be rebuilt, nor a dialog opened, inside its own mouse event.
        self._select_timer = QTimer(self, singleShot=True, interval=0)
        self._select_timer.timeout.connect(partial(self._guard, self._on_library_selection))
        self._build()
        self._show_document()
        self._guard(self._initial_load)

    # --- overridable dialogs ----------------------------------------------------------------

    def ask_unsaved(self) -> str:
        """'save', 'discard' or 'cancel'."""
        b = QMessageBox.StandardButton
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f"Save changes to “{self.model.name}”?",
            b.Save | b.Discard | b.Cancel,
            b.Save,
        )
        return {b.Save: "save", b.Discard: "discard"}.get(answer, "cancel")

    def ask_confirm(self, text: str) -> bool:
        return QMessageBox.question(self, "Please confirm", text) == QMessageBox.StandardButton.Yes

    def pick_open_path(self) -> Path | None:
        name, _ = QFileDialog.getOpenFileName(self, "Import workout", "", ZWO_FILTER)
        return Path(name) if name else None

    def pick_save_path(self, suggested: str) -> Path | None:
        name, _ = QFileDialog.getSaveFileName(self, "Export workout", suggested, ZWO_FILTER)
        return Path(name) if name else None

    # --- public -----------------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self.model.dirty or bool(self._invalid)

    def confirm_discard(self) -> bool:
        """True if nothing is left unsaved (clean, saved, or discarded by the user)."""
        if not self.dirty:
            return True
        answer = self.ask_unsaved()
        if answer == "save":
            return self.save()
        if answer == "discard":
            self.model.revert()
            self._show_document()
            return True
        return False

    def refresh(self) -> None:
        """Re-read FTP and library; unsaved edits (also invalid input) are kept."""
        self._guard(self._refresh)

    def open_workout(self, workout_id: UUID) -> bool:
        """Show a library workout; False if the user keeps unsaved changes."""
        return self._guard(lambda: self._open_if_clean(workout_id))

    def _open_if_clean(self, workout_id: UUID) -> bool:
        if workout_id == self.model.id:
            return True
        if not self.confirm_discard():
            return False
        self._open(workout_id)
        return True

    def save(self) -> bool:
        return self._guard(self._save)

    # --- build ------------------------------------------------------------------------------

    def _build(self) -> None:
        self.library = QListWidget()
        self.library.currentItemChanged.connect(lambda *_: self._select_timer.start())
        self.buttons: dict[str, QPushButton] = {}

        left = QVBoxLayout()
        head = QHBoxLayout()
        head.addWidget(text_label("title", "Workouts"), 1)
        head.addLayout(self._button_row((("new", "New", self._new),)))
        left.addLayout(head)
        left.addWidget(self.library, 1)
        for row in (
            (("import", "Import .zwo…", self._import), ("export", "Export .zwo…", self._export)),
            (("duplicate", "Duplicate", self._duplicate), ("delete", "Delete", self._delete)),
        ):
            left.addLayout(self._button_row(row))

        self.title = text_label("heading")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Name")
        self.name_edit.textEdited.connect(self._on_name)
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText("Description")
        self.desc_edit.setTabChangesFocus(True)
        self.desc_edit.setMaximumHeight(50)
        self.desc_edit.textChanged.connect(self._on_description)

        self.plot = ProfilePlot()
        self.plot.setMinimumHeight(100)
        self.total = text_label("muted")

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Segment", "Duration", "% FTP", "Watts", "Zone"])
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        self.tree.currentItemChanged.connect(lambda cur, _prev: self._on_segment(cur))

        add = partial(partial, self._structure)
        segment_row = (
            ("add_steady", "+ Steady", add(lambda: self._add(em.default_steady()))),
            ("add_ramp", "+ Ramp", add(lambda: self._add(em.default_ramp()))),
            ("add_sprint", "+ Sprint", add(lambda: self._add(em.default_sprint()))),
            ("add_repeat", "+ Repeat", add(lambda: self._add(em.default_repeat()))),
            ("child_steady", "+ Steady in repeat", add(lambda: self._child(em.default_steady()))),
            ("child_ramp", "+ Ramp in repeat", add(lambda: self._child(em.default_ramp()))),
            ("child_sprint", "+ Sprint in repeat", add(lambda: self._child(em.default_sprint()))),
        )
        order_row = (
            ("up", "Up", add(lambda: self.model.move(self._require_path(), -1))),
            ("down", "Down", add(lambda: self.model.move(self._require_path(), 1))),
        )
        edit_row = (
            (
                "dup_segment",
                "Duplicate",
                add(lambda: self.model.duplicate(self._require_path())),
            ),
            ("del_segment", "Delete", add(lambda: self.model.delete(self._require_path()))),
        )

        self._form = QFormLayout()
        self._form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.fields: dict[str, QLineEdit] = {}
        self.field_watts: dict[str, QLabel] = {}
        self._field_rows: dict[str, QWidget] = {}
        for name, label in FIELD_LABELS.items():
            edit, info, row = QLineEdit(), text_label("muted"), QWidget()
            edit.textEdited.connect(partial(self._on_field, name))
            box = QHBoxLayout(row)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(edit)
            box.addWidget(info)
            self.fields[name], self.field_watts[name], self._field_rows[name] = edit, info, row
            self._form.addRow(label, row)

        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)

        top = QHBoxLayout()
        top.addWidget(self.title)
        top.addWidget(self.message, 1)
        top.addLayout(
            self._button_row((("save", "Save", self._save), ("ride", "Ride", self._ride)))
        )
        theme.set_role(self.buttons["ride"], "primary")
        theme.set_role(self.buttons["delete"], "danger")

        inspector = QGroupBox("Segment")
        inspector.setFixedWidth(232)
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.addLayout(self._form)
        inspector_layout.addStretch(1)
        inspector_layout.addLayout(self._button_row(order_row))
        inspector_layout.addLayout(self._button_row(edit_row))
        theme.set_role(self.buttons["del_segment"], "danger")
        segments = QVBoxLayout()
        segments.addWidget(self.tree, 1)
        add_row = QHBoxLayout()
        add_row.addLayout(self._button_row(segment_row))
        add_row.addStretch(1)
        body = QHBoxLayout()
        body.addLayout(segments, 1)
        body.addWidget(inspector)

        right = QVBoxLayout()
        right.setSpacing(10)
        top.setSpacing(12)
        body.setSpacing(10)
        right.addLayout(top)
        right.addWidget(self.name_edit)
        right.addWidget(self.desc_edit)
        right.addWidget(card(self.plot, self.total), 2)
        right.addLayout(add_row)
        right.addLayout(body, 3)

        sidebar = QWidget()
        sidebar.setFixedWidth(244)
        left.setContentsMargins(0, 0, 0, 0)
        sidebar.setLayout(left)
        layout = QHBoxLayout(self)
        page_layout(self, layout)
        layout.setSpacing(20)
        layout.addWidget(sidebar)
        layout.addLayout(right, 1)

    def _button_row(self, specs: tuple) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        for key, text, action in specs:
            button = QPushButton(text)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, a=action: self._guard(a))
            self.buttons[key] = button
            row.addWidget(button)
        return row

    # --- messages / guard -------------------------------------------------------------------

    def _guard(self, action: Callable[[], object]) -> bool:
        """Run a user action; show any failure instead of raising out of a slot.

        True unless it failed or returned False.
        """
        self._info("")
        try:
            return action() is not False
        except EditError as e:
            self._error(str(e))
        except Exception as e:  # repo / file / parser failures: an escape would abort the app
            self._error(f"{type(e).__name__}: {e}")
        return False

    def _error(self, text: str) -> None:
        self.message.setStyleSheet(f"color: {theme.ERROR}")
        self.message.setText(text)

    def _info(self, text: str) -> None:
        self.message.setStyleSheet("")
        self.message.setText(text)

    # --- library ----------------------------------------------------------------------------

    def _initial_load(self) -> None:
        self._refresh()
        if self._library:
            self._open(next(iter(self._library)))

    def _refresh(self) -> None:
        profile = load_profile(self._repo)
        self._ftp, self._zones = profile.ftp_w, profile.power_zones
        self._reload_library()
        if not self.dirty and self.model.id in self._library:
            stored = self._repo.get_workout(self.model.id)
            if stored is not None and stored != self.model.workout():
                self.model.load(stored)
                self._show_document()
                return
        self._update_views()

    def _reload_library(self) -> None:
        workouts = self._repo.list_workouts()
        self._library = {w.id: w.name for w in workouts}
        self.library.blockSignals(True)
        self.library.clear()
        for w in workouts:
            item = QListWidgetItem(w.name)
            item.setData(ROLE, str(w.id))
            self.library.addItem(item)
        self.library.blockSignals(False)
        self._sync_selection()

    def _sync_selection(self) -> None:
        """Make the list highlight what is being edited (nothing for an unsaved draft)."""
        ids = list(self._library)
        row = ids.index(self.model.id) if self.model.id in self._library else -1
        self.library.blockSignals(True)
        self.library.setCurrentRow(row)
        if row < 0:
            self.library.clearSelection()
        self.library.blockSignals(False)

    def _on_library_selection(self) -> None:
        item = self.library.currentItem()
        if item is None:
            return
        target = UUID(item.data(ROLE))
        if target == self.model.id:
            return
        try:
            if self.confirm_discard():
                self._open(target)
        finally:
            self._sync_selection()

    def _open(self, workout_id: UUID) -> None:
        workout = self._repo.get_workout(workout_id)
        if workout is None:
            self._reload_library()
            raise EditError("This workout no longer exists")
        self.model.load(workout)
        self._show_document()
        self._sync_selection()

    def _new(self) -> None:
        if self.confirm_discard():
            self.model.new()
            self._show_document()
            self._sync_selection()
            self.name_edit.setFocus()
            self.name_edit.selectAll()

    def _duplicate(self) -> None:
        if not self.confirm_discard():
            return
        if self.model.id not in self._library:
            raise EditError("Save the workout before duplicating it")
        self._store_and_open(EditorModel.copy_of(self.model.workout()))

    def _store_and_open(self, workout) -> None:
        self._repo.save_workout(workout)
        self.model.load(workout)
        self._reload_library()
        self._show_document()

    def _delete(self) -> None:
        if self.model.id not in self._library:
            raise EditError("This workout is not in the library yet")
        if not self.ask_confirm(f"Delete “{self._library[self.model.id]}” permanently?"):
            return
        row = list(self._library).index(self.model.id)
        self._repo.delete_workout(self.model.id)
        self.model.new()
        self._reload_library()
        self._show_document()
        ids = list(self._library)
        if ids:
            self._open(ids[min(row, len(ids) - 1)])

    def _import(self) -> None:
        path = self.pick_open_path()
        if path is None:
            return
        try:
            workout = zwo.load(path)
        except (OSError, ValueError) as e:
            raise EditError(f"Cannot import {path.name}: {e}") from e
        if not workout.name.strip():
            workout.name = path.stem
        if self.confirm_discard():
            self._store_and_open(workout)
            self._info(f"Imported “{workout.name}”")

    def _export(self) -> None:
        self._require_valid()
        workout = self.model.workout()
        path = self.pick_save_path(f"{sanitize_filename(workout.name)}.zwo")
        if path is None:
            return
        if path.suffix.lower() != ".zwo":
            path = path.with_name(path.name + ".zwo")
            # the file dialog only asked about the name without the suffix
            if path.exists() and not self.ask_confirm(f"Replace existing {path.name}?"):
                return
        try:
            zwo.save(workout, path)
        except (OSError, ValueError) as e:
            raise EditError(f"Cannot export {path.name}: {e}") from e
        self._info(f"Exported {path.name}")

    # --- document ---------------------------------------------------------------------------

    def _require_valid(self) -> None:
        if self._invalid:
            raise EditError("Fix the invalid input first: " + next(iter(self._invalid.values())))

    def _save(self) -> None:
        self._require_valid()
        self._repo.save_workout(self.model.prepare_save())
        self.model.mark_saved()
        if self.name_edit.text() != self.model.name:
            self.name_edit.setText(self.model.name)
        self._reload_library()
        self._update_views()
        self._info("Saved")

    def _ride(self) -> None:
        self._require_valid()
        self._require_ridable()
        if self.confirm_discard():
            self._require_ridable()  # discarding may have reverted to an empty workout
            self.ride_requested.emit(self.model.workout())

    def _require_ridable(self) -> None:
        if self.model.empty:
            raise EditError("An empty workout cannot be ridden; add a segment first")

    def _on_name(self, text: str) -> None:
        self.model.name = text
        self._update_title()

    def _on_description(self) -> None:
        self.model.description = self.desc_edit.toPlainText()
        self._update_title()

    def _show_document(self) -> None:
        """Full reload of all widgets from the model."""
        self._invalid.clear()
        self.name_edit.setText(self.model.name)
        self.desc_edit.blockSignals(True)
        self.desc_edit.setPlainText(self.model.description)
        self.desc_edit.blockSignals(False)
        self._rebuild_tree(None if self.model.empty else (0,))

    # --- segments ---------------------------------------------------------------------------

    def _require_path(self) -> em.Path:
        if self._path is None:
            raise EditError("Select a segment first")
        return self._path

    def _add(self, seg: Segment) -> em.Path:
        return self.model.add(seg, self._path)

    def _child(self, seg: Steady | Ramp | Sprint) -> em.Path:
        return self.model.add_child(self._require_path(), seg)

    def _structure(self, change: Callable[[], em.Path | None]) -> None:
        self._rebuild_tree(change())

    def _rebuild_tree(self, select: em.Path | None) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        for i, seg in enumerate(self.model.segments):
            top = self._tree_item((i,))
            self.tree.addTopLevelItem(top)
            if isinstance(seg, Repeat):
                for j in range(len(seg.children)):
                    top.addChild(self._tree_item((i, j)))
        self.tree.expandAll()
        self.tree.blockSignals(False)
        self.select_segment(select)
        self._update_views()

    @staticmethod
    def _tree_item(path: em.Path) -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        item.setData(0, ROLE, path)
        return item

    def _items(self) -> list[QTreeWidgetItem]:
        items = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            items.append(top)
            items += [top.child(j) for j in range(top.childCount())]
        return items

    def select_segment(self, path: em.Path | None) -> None:
        item = next((it for it in self._items() if tuple(it.data(0, ROLE)) == path), None)
        self.tree.blockSignals(True)
        self.tree.setCurrentItem(item)
        self.tree.blockSignals(False)
        self._on_segment(item)

    def _on_segment(self, item: QTreeWidgetItem | None) -> None:
        dropped = next(iter(self._invalid.values()), None)
        self._path = tuple(item.data(0, ROLE)) if item is not None else None
        self._load_form()
        if dropped:
            self._error(f"Invalid input was not applied. {dropped}")

    def _load_form(self) -> None:
        self._invalid.clear()
        seg = self.model.get(self._path) if self._path is not None else None
        names = em.fields_of(seg) if seg is not None else ()
        for name, edit in self.fields.items():
            self._form.setRowVisible(self._field_rows[name], name in names)
            edit.setText(em.field_text(seg, name) if name in names else "")
            edit.setStyleSheet("")
        self._update_watts()
        self._update_title()

    def _on_field(self, name: str, text: str) -> None:
        self._guard(partial(self._apply_field, name, text))

    def _apply_field(self, name: str, text: str) -> None:
        if self._path is None:
            return
        try:
            self.model.set_field(self._path, name, text)
        except EditError as e:
            self._invalid[name] = str(e)
            self.fields[name].setStyleSheet(f"border: 1px solid {theme.ERROR}")
            self._update_title()
            raise
        self._invalid.pop(name, None)
        self.fields[name].setStyleSheet("")
        self._update_views()
        if self._invalid:
            raise EditError(next(iter(self._invalid.values())))

    # --- views derived from the model -------------------------------------------------------

    def _update_views(self) -> None:
        """Everything except the input widgets."""
        for item in self._items():
            seg = self.model.get(tuple(item.data(0, ROLE)))
            for col, text in enumerate(self._row_texts(seg)):
                item.setText(col, text)
            if not isinstance(seg, Repeat):
                item.setIcon(0, theme.zone_icon(max(self._segment_zones(seg))))
        self.plot.set_profile(self.model.blocks(), self._ftp, self._zones)
        self.total.setText(
            f"Total {em.format_total(self.model.total_duration())}  ·  FTP {self._ftp} W"
        )
        self._update_watts()
        self._update_title()

    def _row_texts(self, seg: Segment) -> list[str]:
        duration = em.format_duration(em.segment_duration(seg))
        if isinstance(seg, Repeat):
            return [f"{seg.count} × repeat", duration, "", "", ""]
        if isinstance(seg, Sprint):
            return ["Sprint", duration, "open", "", ""]
        powers = [seg.power] if isinstance(seg, Steady) else [seg.start, seg.end]
        zones = dict.fromkeys(self._segment_zones(seg))  # each once, in order
        return [
            type(seg).__name__,
            duration,
            " → ".join(em.format_percent(p) for p in powers) + " %",
            " → ".join(str(em.watts(p, self._ftp)) for p in powers) + " W",
            " → ".join(f"Z{z}" for z in zones),
        ]

    def _segment_zones(self, seg: Steady | Ramp | Sprint) -> list[int]:
        if isinstance(seg, Sprint):
            return [ZONE_COUNT]  # shown as all out
        powers = [seg.power] if isinstance(seg, Steady) else [seg.start, seg.end]
        return [self._zones.zone_of(p * self._ftp) for p in powers]

    def _update_watts(self) -> None:
        seg = self.model.get(self._path) if self._path is not None else None
        for name, label in self.field_watts.items():
            shown = seg is not None and name in ("power", "start", "end")
            power = getattr(seg, name, None) if shown else None
            label.setText("" if power is None else f"{em.watts(power, self._ftp)} W")

    def _update_title(self) -> None:
        stored = self.model.id in self._library
        self.title.setText(
            ("Edit workout" if stored else "New workout (not in library)")
            + (" — unsaved changes" if self.dirty else "")
        )
        self.buttons["delete"].setEnabled(stored)
        self.buttons["duplicate"].setEnabled(stored)
