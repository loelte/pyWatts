"""Settings page: units, navigation bar, ride layout, default trainer (stored at once)."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShowEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pywatts.storage.repo import (
    COACH_COMMAND,
    COACH_PATH,
    NAV_HIDDEN,
    RIDE_KEYS,
    RIDE_TILES,
    TRAINER_ADDR,
    TRAINER_NAME,
    UNITS,
    Repo,
)
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui import theme
from pywatts.ui.ride_keys import ACTIONS, parse_keys, serialize_keys
from pywatts.ui.ride_layout import COLUMNS, TILES, parse_layout, serialize_layout
from pywatts.ui.widgets.common import GAP, page_layout, text_label

ROLE = Qt.ItemDataRole.UserRole
COACH_NOTE = (
    "Another program keeps the tools it comes with, for example access to files and the "
    "command line. Only Claude Code is limited to pyWatts."
)
COACH_COMMAND_TIP = (
    "Command line of an AI program that supports MCP. {mcp_config} is replaced by the "
    "path of the server configuration, {prompt} by the coach instructions."
)
SCAN_S = 5.0
SCAN_GUARD_MS = 20_000  # scan neither finished nor failed


class SettingsPage(QWidget):
    settings_changed = pyqtSignal()

    def __init__(self, repo: Repo, bridge: TrainerBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._bridge = bridge
        self._scanning = False
        self._select_after: str | None = None
        self._scan_guard = QTimer(self, singleShot=True, interval=SCAN_GUARD_MS)
        self._scan_guard.timeout.connect(lambda: self._on_error("No answer to the scan request"))
        self._build()
        bridge.scan_finished.connect(self._on_scan_finished)
        bridge.error.connect(self._on_error)
        self.refresh()

    def _build(self) -> None:
        self.units = QComboBox()
        self.units.addItem("Metric (km)", "metric")
        self.units.addItem("Imperial (mi)", "imperial")
        self.units.currentIndexChanged.connect(
            lambda _i: self._store({UNITS: self.units.currentData()})
        )

        self.trainer = QLabel()
        self.trainer.setTextFormat(Qt.TextFormat.PlainText)
        self.forget_button = QPushButton("Forget")
        self.forget_button.clicked.connect(
            lambda: self._store({TRAINER_ADDR: "", TRAINER_NAME: ""})
        )
        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(lambda: self._start_scan())
        self.devices = QListWidget()
        self.devices.currentRowChanged.connect(lambda _r: self._update_buttons())
        self.devices.itemDoubleClicked.connect(lambda _i: self._select_device())
        self.select_button = QPushButton("Use selected")
        self.select_button.clicked.connect(lambda: self._select_device())
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)

        trainer_row = QHBoxLayout()
        trainer_row.addWidget(self.trainer, 1)
        trainer_row.addWidget(self.forget_button)
        general = QGroupBox("General")
        form = QFormLayout(general)
        form.addRow("Units", self.units)
        self.units.setMaximumWidth(260)
        theme.set_role(self.select_button, "primary")
        trainer = QGroupBox("Default trainer")
        scan_row = QHBoxLayout()
        scan_row.addWidget(self.scan_button)
        scan_row.addWidget(self.select_button)
        scan_row.addStretch(1)
        trainer_layout = QVBoxLayout(trainer)
        trainer_layout.addLayout(trainer_row)
        trainer_layout.addWidget(self.devices, 1)
        trainer_layout.addWidget(self.message)
        trainer_layout.addLayout(scan_row)
        self.nav_visible = QCheckBox("Show the navigation bar (Ctrl+B toggles it)")
        self.nav_visible.toggled.connect(lambda on: self._store({NAV_HIDDEN: "" if on else "1"}))
        form.addRow("Window", self.nav_visible)
        self.coach_path = QLineEdit()
        self.coach_path.setPlaceholderText("Found automatically")
        self.coach_path.editingFinished.connect(
            lambda: self._store_text(COACH_PATH, self.coach_path)
        )
        self.coach_command = QLineEdit()
        self.coach_command.setPlaceholderText("Claude Code, limited to pyWatts")
        self.coach_command.setToolTip(COACH_COMMAND_TIP)
        self.coach_command.editingFinished.connect(
            lambda: self._store_text(COACH_COMMAND, self.coach_command)
        )
        form.addRow("Claude Code program", self.coach_path)
        form.addRow("Other AI program", self.coach_command)
        note = text_label("muted", COACH_NOTE)
        note.setWordWrap(True)
        form.addRow("", note)

        layout = QVBoxLayout(self)
        page_layout(self, layout)
        layout.addWidget(text_label("title", "Settings"))
        layout.addWidget(general)
        columns = QHBoxLayout()
        columns.setSpacing(GAP)
        left = QVBoxLayout()
        left.addWidget(self._build_layout_group(), 1)
        left.addWidget(self._build_keys_group())
        columns.addLayout(left, 1)
        columns.addWidget(trainer, 1)
        layout.addLayout(columns, 1)

    def _build_keys_group(self) -> QGroupBox:
        group = QGroupBox("Ride shortcuts")
        form = QFormLayout(group)
        self.key_edits: dict[str, QKeySequenceEdit] = {}
        for action, (caption, _default) in ACTIONS.items():
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1)
            edit.setClearButtonEnabled(True)
            edit.editingFinished.connect(self._store_keys)
            self.key_edits[action] = edit
            form.addRow(caption, edit)
        self.reset_keys_button = QPushButton("Reset keys")
        self.reset_keys_button.clicked.connect(lambda: self._store({RIDE_KEYS: ""}))
        form.addRow(self.reset_keys_button)
        return group

    def _store_keys(self) -> None:
        keys = {
            action: edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            for action, edit in self.key_edits.items()
        }
        self._store({RIDE_KEYS: serialize_keys(keys)})

    def _build_layout_group(self) -> QGroupBox:
        self.shown_list = QListWidget()
        self.available_list = QListWidget()
        self.shown_list.currentRowChanged.connect(lambda _r: self._update_layout_buttons())
        self.available_list.currentRowChanged.connect(lambda _r: self._update_layout_buttons())
        self.remove_tile_button = QPushButton("Remove >")
        self.add_tile_button = QPushButton("< Add")
        self.up_tile_button = QPushButton("Up")
        self.down_tile_button = QPushButton("Down")
        self.reset_tiles_button = QPushButton("Reset")
        self.remove_tile_button.clicked.connect(lambda: self._move_tile(remove=True))
        self.add_tile_button.clicked.connect(lambda: self._move_tile(remove=False))
        self.up_tile_button.clicked.connect(lambda: self._shift_tile(-1))
        self.down_tile_button.clicked.connect(lambda: self._shift_tile(1))
        self.reset_tiles_button.clicked.connect(lambda: self._store({RIDE_TILES: ""}))

        buttons = QVBoxLayout()
        for button in (
            self.up_tile_button,
            self.down_tile_button,
            self.remove_tile_button,
            self.add_tile_button,
            self.reset_tiles_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        lists = QHBoxLayout()
        shown = QVBoxLayout()
        shown.addWidget(text_label("muted", "Shown, in order"))
        shown.addWidget(self.shown_list, 1)
        available = QVBoxLayout()
        available.addWidget(text_label("muted", "Available"))
        available.addWidget(self.available_list, 1)
        lists.addLayout(shown, 1)
        lists.addLayout(buttons)
        lists.addLayout(available, 1)
        group = QGroupBox("Ride layout")
        outer = QVBoxLayout(group)
        outer.addWidget(
            text_label(
                "muted", f"The first {COLUMNS} data panels are shown large, the rest smaller."
            )
        )
        outer.addLayout(lists, 1)
        return group

    # --- ride layout ------------------------------------------------------------------------

    def _current_tiles(self) -> list[str]:
        return [self.shown_list.item(i).data(ROLE) for i in range(self.shown_list.count())]

    def _store_tiles(self, ids: list[str], select: str | None) -> None:
        self._select_after = select
        self._store({RIDE_TILES: serialize_layout(ids)})

    def _move_tile(self, remove: bool) -> None:
        ids = self._current_tiles()
        source = self.shown_list if remove else self.available_list
        item = source.currentItem()
        if item is None:
            return
        key = item.data(ROLE)
        if remove:
            ids.remove(key)
            self._store_tiles(ids, None)
        else:
            ids.append(key)
            self._store_tiles(ids, key)

    def _shift_tile(self, step: int) -> None:
        ids = self._current_tiles()
        i = self.shown_list.currentRow()
        j = i + step
        if i < 0 or not 0 <= j < len(ids):
            return
        ids[i], ids[j] = ids[j], ids[i]
        self._store_tiles(ids, ids[j])

    def _fill_tiles(self) -> None:
        shown = parse_layout(self._repo.get_setting(RIDE_TILES))
        wanted, self._select_after = self._select_after, None
        for widget, keys in (
            (self.shown_list, shown),
            (self.available_list, [k for k in TILES if k not in shown]),
        ):
            widget.blockSignals(True)
            widget.clear()
            for key in keys:
                item = QListWidgetItem(TILES[key])
                item.setData(ROLE, key)
                widget.addItem(item)
            widget.blockSignals(False)
        if wanted in shown:
            self.shown_list.setCurrentRow(shown.index(wanted))
        elif self.available_list.count():
            self.available_list.setCurrentRow(0)
        self._update_layout_buttons()

    def _update_layout_buttons(self) -> None:
        row, count = self.shown_list.currentRow(), self.shown_list.count()
        self.up_tile_button.setEnabled(row > 0)
        self.down_tile_button.setEnabled(0 <= row < count - 1)
        self.remove_tile_button.setEnabled(row >= 0)
        self.add_tile_button.setEnabled(self.available_list.currentRow() >= 0)

    def _store_text(self, key: str, edit: QLineEdit) -> None:
        text = edit.text().strip()
        if text != self._shown_text(key):
            self._store({key: text})

    def _shown_text(self, key: str) -> str | None:
        try:
            return self._repo.get_setting(key)
        except Exception:  # _store reports it
            return None

    # --- settings ---------------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read stored values (the connect dialog may have changed the trainer)."""
        self._guard(self._load)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh()

    def _load(self) -> None:
        self.units.blockSignals(True)
        self.units.setCurrentIndex(max(0, self.units.findData(self._repo.get_setting(UNITS))))
        self.units.blockSignals(False)
        self.nav_visible.blockSignals(True)
        self.nav_visible.setChecked(self._repo.get_setting(NAV_HIDDEN) != "1")
        self.nav_visible.blockSignals(False)
        self.coach_path.setText(self._repo.get_setting(COACH_PATH))
        self.coach_command.setText(self._repo.get_setting(COACH_COMMAND))
        self._fill_tiles()
        for action, key in parse_keys(self._repo.get_setting(RIDE_KEYS)).items():
            edit = self.key_edits[action]
            edit.blockSignals(True)
            edit.setKeySequence(QKeySequence(key))
            edit.blockSignals(False)
        addr, name = self._repo.get_setting(TRAINER_ADDR), self._repo.get_setting(TRAINER_NAME)
        self.trainer.setText(f"{name or 'Unnamed'} ({addr})" if addr else "None")
        self.forget_button.setEnabled(bool(addr))
        self._update_buttons()

    def _store(self, values: dict[str, str]) -> bool:
        def write() -> None:
            for key, value in values.items():
                self._repo.set_setting(key, value)

        self.message.clear()
        ok = self._guard(write)
        error = self.message.text()
        self.refresh()  # on failure the widgets must show what is stored
        if not ok:
            self.message.setText(error)
        self.settings_changed.emit()  # even after a failure: part may have been written
        return ok

    def _guard(self, action: Callable[[], None]) -> bool:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        try:
            action()
        except Exception as e:
            self.message.setText(f"{type(e).__name__}: {e}")
            return False
        return True

    # --- trainer scan -----------------------------------------------------------------------

    def _start_scan(self) -> None:
        if self._scanning:
            return
        self.devices.clear()
        self.message.setText("Scanning…")
        self._set_scanning(True)
        if not self._guard(lambda: self._bridge.scan(SCAN_S)):
            self._set_scanning(False)

    def _set_scanning(self, on: bool) -> None:
        self._scanning = on
        if on:
            self._scan_guard.start()
        else:
            self._scan_guard.stop()
        self._update_buttons()

    def _on_scan_finished(self, devices: list) -> None:
        if not self._scanning:  # requested elsewhere (connect dialog)
            return
        self._set_scanning(False)
        for d in sorted(devices, key=lambda d: d.rssi, reverse=True):
            item = QListWidgetItem(f"{d.name or 'Unnamed'}  ({d.addr}, {d.rssi} dBm)")
            item.setData(ROLE, (d.name or "", d.addr))
            self.devices.addItem(item)
        if devices:
            self.devices.setCurrentRow(0)
        self.message.setText("" if devices else "No trainer found. Is it awake and in range?")

    def _on_error(self, text: str) -> None:
        """Errors outside our scan belong to other pages."""
        if self._scanning:
            self._set_scanning(False)
            self.message.setText(f"Scan failed: {text}")

    def _select_device(self) -> None:
        item = self.devices.currentItem()
        if item is not None:
            name, addr = item.data(ROLE)
            if self._store({TRAINER_ADDR: addr, TRAINER_NAME: name}):
                self.message.setText(f"Default trainer: {name or addr}")

    def _update_buttons(self) -> None:
        self.scan_button.setEnabled(not self._scanning)
        self.select_button.setEnabled(self.devices.currentItem() is not None)
