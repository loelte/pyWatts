"""Ride page and trainer connection dialog."""

from __future__ import annotations

import html
import math

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut, QShowEvent
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from pywatts.core.runner import RunnerState
from pywatts.core.workout import Block, Workout
from pywatts.core.zones import ZONE_COUNT, Zones, power_zones
from pywatts.storage.profile import load_profile
from pywatts.storage.repo import (
    DEFAULTS,
    FTP_W,
    HR_ADDR,
    HR_NAME,
    RIDE_KEYS,
    RIDE_TILES,
    TRAINER_ADDR,
    TRAINER_NAME,
    UNITS,
    Repo,
)
from pywatts.trainer.base import ConnectionState, DeviceInfo, HeartRate, Telemetry
from pywatts.trainer.thread import TrainerBridge
from pywatts.ui import theme
from pywatts.ui.ride_controller import RideController
from pywatts.ui.ride_keys import parse_keys
from pywatts.ui.ride_layout import COLUMNS, TILES, parse_layout
from pywatts.ui.widgets.common import GAP, ValueLabel, card, page_layout, text_label
from pywatts.ui.widgets.profile_plot import ProfilePlot

KM_PER_MILE = 1.609344
NO_VALUE = "--"
POWER_STEP_W = 10
COUNTDOWN_S = 10
COUNTDOWN_MIN_ZONE = 3


def mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _countdown(seconds: float) -> str:
    return mmss(math.ceil(seconds - 1e-6))


def _message_label() -> QLabel:
    label = text_label("error")
    label.setWordWrap(True)
    label.hide()
    return label


def _show_message(label: QLabel, text: str) -> None:
    label.setText(text)
    label.setVisible(bool(text))


def _tone(link: ConnectionState) -> str:
    if link is ConnectionState.CONNECTED:
        return "ok"
    return "" if link is ConnectionState.DISCONNECTED else "warn"


def announcement(block: Block, ftp_w: int, zones: Zones) -> tuple[str, int] | None:
    """(text, zone) if this block is counted down: sprints and everything reaching Z3."""
    if block.open:
        return "SPRINT", ZONE_COUNT
    watts = round(block.p_start * ftp_w), round(block.p_end * ftp_w)
    zone = zones.zone_of(max(watts))
    if zone < COUNTDOWN_MIN_ZONE:
        return None
    return f"Z{zone}  ·  " + " → ".join(str(w) for w in dict.fromkeys(watts)) + " W", zone


class ConnectDialog(QDialog):
    """Scan and connect; HrDialog does the same for heart rate straps."""

    NOUN = "trainer"
    ADDR_KEY, NAME_KEY = TRAINER_ADDR, TRAINER_NAME

    def __init__(self, repo: Repo, bridge: TrainerBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Connect {self.NOUN}")
        self.resize(420, 360)
        self._repo = repo
        self._bridge = bridge
        self._last_addr = repo.get_setting(self.ADDR_KEY)
        self._last_name = repo.get_setting(self.NAME_KEY)

        self.reconnect_button = QPushButton(f"Reconnect to {self._last_name or self._last_addr}")
        self.reconnect_button.setVisible(bool(self._last_addr))
        self.device_list = QListWidget()
        self.scan_button = QPushButton("Scan")
        self.connect_button = QPushButton("Connect")
        self.status_label = text_label("muted")
        self.error_label = _message_label()
        theme.set_role(self.connect_button, "primary")
        close = QPushButton("Close")

        buttons = QHBoxLayout()
        for button in (self.scan_button, self.connect_button, close):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        page_layout(self, layout)
        layout.addWidget(self.reconnect_button)
        layout.addWidget(self.device_list, 1)
        layout.addWidget(self.status_label)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)

        self.scan_button.clicked.connect(lambda: self.scan())
        self.connect_button.clicked.connect(lambda: self.connect_selected())
        self.reconnect_button.clicked.connect(lambda: self.reconnect())
        self.device_list.itemSelectionChanged.connect(self._update_buttons)
        self.device_list.itemDoubleClicked.connect(lambda _item: self.connect_selected())
        close.clicked.connect(self.reject)
        self._scanning = False
        self._subscribe()
        self._update_buttons()

    # --- device specifics ---
    def _subscribe(self) -> None:
        self._bridge.scan_finished.connect(self._on_scan)
        self._bridge.connection_changed.connect(self._on_connection)
        self._bridge.error.connect(self._on_error)

    def _request_scan(self) -> None:
        self._bridge.scan()

    def _request_connect(self, addr: str) -> None:
        self._bridge.connect_trainer(addr)

    def _link_state(self) -> ConnectionState:
        return self._bridge.state

    def _ordered(self, devices: list[DeviceInfo]) -> list[DeviceInfo]:
        return sorted(devices, key=lambda d: d.rssi, reverse=True)

    # --- actions ---
    def scan(self) -> None:
        self._scanning = True
        _show_message(self.error_label, "")
        self.status_label.setText("Scanning...")
        self._update_buttons()
        self._request_scan()

    def connect_selected(self) -> None:
        item = self.device_list.currentItem()
        if item is not None and self._can_connect():
            device: DeviceInfo = item.data(Qt.ItemDataRole.UserRole)
            self._connect(device.addr, device.name)

    def reconnect(self) -> None:
        if self._last_addr and self._can_connect():
            self._connect(self._last_addr, self._last_name)

    def _connect(self, addr: str, name: str) -> None:
        _show_message(self.error_label, "")
        self.status_label.setText(f"Connecting to {name or addr}...")
        try:
            self._request_connect(addr)
            self._repo.set_setting(self.ADDR_KEY, addr)
            self._repo.set_setting(self.NAME_KEY, name)
        except Exception as exc:
            _show_message(self.error_label, f"Could not remember the {self.NOUN}: {exc}")

    def _can_connect(self) -> bool:
        return not self._scanning and self._link_state() is ConnectionState.DISCONNECTED

    def _update_buttons(self) -> None:
        free = self._can_connect()
        self.scan_button.setEnabled(free)
        self.connect_button.setEnabled(free and bool(self.device_list.selectedItems()))
        self.reconnect_button.setEnabled(free)

    def _on_scan(self, devices: list[DeviceInfo]) -> None:
        self._scanning = False
        self.device_list.clear()
        for device in self._ordered(devices):
            item = QListWidgetItem(f"{device.name or device.addr}  ({device.rssi} dBm)")
            item.setData(Qt.ItemDataRole.UserRole, device)
            item.setToolTip(device.addr)
            self.device_list.addItem(item)
        if devices:
            self.device_list.setCurrentRow(0)
        self.status_label.setText(
            f"{len(devices)} {self.NOUN}(s) found" if devices else f"No {self.NOUN}s found"
        )
        self._update_buttons()

    def _on_connection(self, state: ConnectionState) -> None:
        self.status_label.setText(state.value.capitalize())
        self._update_buttons()
        if state is ConnectionState.CONNECTED and self.isVisible():
            self.accept()

    def _on_error(self, text: str) -> None:
        self._scanning = False
        self.status_label.setText("")
        _show_message(self.error_label, text)
        self._update_buttons()


class HrDialog(ConnectDialog):
    NOUN = "heart rate strap"
    ADDR_KEY, NAME_KEY = HR_ADDR, HR_NAME

    def _subscribe(self) -> None:
        self._bridge.hr_scan_finished.connect(self._on_scan)
        self._bridge.hr_connection_changed.connect(self._on_connection)
        self._bridge.hr_error.connect(self._on_error)

    def _request_scan(self) -> None:
        self._bridge.hr_scan()

    def _request_connect(self, addr: str) -> None:
        self._bridge.hr_connect(addr)

    def _link_state(self) -> ConnectionState:
        return self._bridge.hr_state

    def _ordered(self, devices: list[DeviceInfo]) -> list[DeviceInfo]:
        return devices  # Wahoo straps first, see trainer/hr.py


class RidePage(QWidget):
    def __init__(
        self,
        repo: Repo,
        bridge: TrainerBridge,
        controller: RideController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._bridge = bridge
        self._controller = controller
        self._ftp_w = int(DEFAULTS[FTP_W])
        self._units = DEFAULTS[UNITS]
        self._last_addr = ""
        self._ride_error = ""
        self._drawn: tuple[list[Block], int, Zones] | None = None
        self._power_zones = power_zones(self._ftp_w)
        self._hr_zones: Zones | None = None
        self._last_strap = ""
        self._battery = ""

        self.title_label = text_label("title")
        # a long name is cut off instead of widening the window
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setMinimumWidth(160)
        self.state_label = text_label("chip")
        self.status_label = text_label("chip")
        self.reconnect_button = QPushButton()
        self.connect_button = QPushButton()
        top = QHBoxLayout()
        top.addWidget(self.title_label, 1)
        top.addWidget(self.state_label)
        top.addSpacing(GAP)
        self.hr_status_label = text_label("chip")
        self.hr_reconnect_button = QPushButton()
        self.hr_button = QPushButton()
        for widget in (
            self.hr_status_label,
            self.hr_reconnect_button,
            self.hr_button,
            self.status_label,
            self.reconnect_button,
            self.connect_button,
        ):
            top.addWidget(widget)

        self._grid = QGridLayout()
        self._grid.setSpacing(GAP)
        # every tile exists; the layout setting decides which are shown and where
        self._shown: list[str] | None = None
        self._tiles: dict[str, tuple[ValueLabel, QFrame]] = {}
        heads: dict[str, QLabel] = {}
        for key, caption in TILES.items():
            value, tile, heads[key] = self._tile(caption)
            self._tiles[key] = (value, tile)
        self.power_label, self._power_tile = self._tiles["power"]
        self.power_caption = heads["power"]
        self.target_label = self._tiles["target"][0]
        self.cadence_label = self._tiles["cadence"][0]
        self.target_rpm_label = self._tiles["target_rpm"][0]
        self.hr_label, self._hr_tile = self._tiles["hr"]
        self.hr_caption = heads["hr"]
        self.time_left_label = self._tiles["time_left"][0]
        self.block_left_label = self._tiles["block_left"][0]
        self.speed_label = self._tiles["speed"][0]
        self.ride_time_label = self._tiles["ride_time"][0]
        self.distance_label = self._tiles["distance"][0]
        self.calories_label = self._tiles["calories"][0]
        self.avg_power_label = self._tiles["avg_power"][0]
        self.avg_hr_label = self._tiles["avg_hr"][0]
        grid = self._grid

        self.plot = ProfilePlot()
        # floats over the plot, so that showing it moves nothing
        self.countdown_label = QLabel(self.plot)
        self.countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.countdown_label.setTextFormat(Qt.TextFormat.RichText)
        self.countdown_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.countdown_label.hide()
        self.error_label = _message_label()
        self.start_button = QPushButton("Start")  # Start / Pause / Resume
        self.skip_button = QPushButton("Skip")
        self.extend_button = QPushButton("+30 s")
        self.power_down_button = QPushButton(f"-{POWER_STEP_W} W")
        self.power_up_button = QPushButton(f"+{POWER_STEP_W} W")
        self.stop_button = QPushButton("Stop")
        theme.set_role(self.start_button, "primary")
        theme.set_role(self.stop_button, "danger")
        bottom = QHBoxLayout()
        bottom.setSpacing(GAP)
        for button in (
            self.start_button,
            self.skip_button,
            self.extend_button,
            self.power_down_button,
            self.power_up_button,
            self.stop_button,
        ):
            theme.set_role(button, "large", "scale")
            bottom.addWidget(button)

        layout = QVBoxLayout(self)
        page_layout(self, layout)
        layout.addLayout(top)
        layout.addLayout(grid)
        layout.addWidget(card(self.plot), 1)
        layout.addWidget(self.error_label)
        layout.addLayout(bottom)

        self.connect_button.clicked.connect(lambda: self._toggle_connection())
        self.reconnect_button.clicked.connect(lambda: self._reconnect())
        self.hr_button.clicked.connect(lambda: self._toggle_strap())
        self.hr_reconnect_button.clicked.connect(lambda: self._reconnect_strap())
        self.start_button.clicked.connect(lambda: self._toggle_ride())
        self.skip_button.clicked.connect(lambda: controller.skip())
        self.extend_button.clicked.connect(lambda: controller.extend(30))
        self.power_down_button.clicked.connect(lambda: controller.adjust_power(-POWER_STEP_W))
        self.power_up_button.clicked.connect(lambda: controller.adjust_power(POWER_STEP_W))
        self.stop_button.clicked.connect(lambda: self._stop())
        # Active only while this page is visible (Qt skips shortcuts of hidden widgets).
        # Keys come from the setting ride_keys (reload_settings).
        self._shortcuts: dict[str, QShortcut] = {}
        for action, button in (
            ("power_up", self.power_up_button),
            ("power_down", self.power_down_button),
            ("skip", self.skip_button),
            ("start", self.start_button),
            ("extend", self.extend_button),
            ("stop", self.stop_button),
        ):
            self._shortcuts[action] = QShortcut(
                QKeySequence(),
                self,
                activated=lambda button=button: button.click() if button.isEnabled() else None,
            )
        controller.updated.connect(self._refresh)
        controller.state_changed.connect(self._on_state)
        controller.error.connect(self._on_ride_error)
        bridge.error.connect(self._on_bridge_error)
        bridge.telemetry.connect(lambda _tel: self._show_telemetry(controller.latest_telemetry))
        bridge.connection_changed.connect(self._on_connection)
        bridge.hr_reading.connect(self._on_heart_rate)
        bridge.hr_connection_changed.connect(self._on_hr_connection)
        bridge.hr_error.connect(self._on_bridge_error)
        self.reload_settings()

    @staticmethod
    def _tile(caption: str) -> tuple[ValueLabel, QFrame, QLabel]:
        head = text_label("muted", caption)
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value = ValueLabel(NO_VALUE, 38)
        tile = card(head, value, margin=8)
        tile.setObjectName("tile")
        tile.layout().setSpacing(0)
        return value, tile, head

    def _apply_layout(self, ids: list[str]) -> None:
        """Show exactly `ids` in this order; the first row is large."""
        if ids == self._shown:
            return
        self._shown = list(ids)
        for _value, tile in self._tiles.values():
            self._grid.removeWidget(tile)
            tile.hide()
        for col in range(COLUMNS):
            self._grid.setColumnStretch(col, 1)
        for i, key in enumerate(ids):
            value, tile = self._tiles[key]
            row, col = divmod(i, COLUMNS)
            value.set_size(64 if row == 0 else 38)
            self._grid.addWidget(tile, row, col)
            tile.show()

    # --- public ---
    def set_workout(self, workout: Workout, unit_id: str | None = None) -> None:
        if self._controller.load(workout, unit_id):
            self.show_error("")
            self.reload_settings()

    def reload_settings(self) -> None:
        try:
            self._units = self._repo.get_setting(UNITS)
            self._apply_layout(parse_layout(self._repo.get_setting(RIDE_TILES)))
            for action, key in parse_keys(self._repo.get_setting(RIDE_KEYS)).items():
                self._shortcuts[action].setKey(QKeySequence(key))
            self._last_addr = self._repo.get_setting(TRAINER_ADDR)
            name = self._repo.get_setting(TRAINER_NAME)
            strap_name = self._repo.get_setting(HR_NAME)
            self._last_strap = self._repo.get_setting(HR_ADDR)
            profile = load_profile(self._repo)
            self._ftp_w = profile.ftp_w
            self._power_zones, self._hr_zones = profile.power_zones, profile.hr_zones
        except Exception as exc:
            self.show_error(f"Could not read the settings: {exc}")
            name, strap_name = "", ""
            if self._shown is None:
                self._apply_layout(parse_layout(""))
        self.hr_reconnect_button.setText(f"Reconnect {strap_name or self._last_strap}")
        self.reconnect_button.setText(f"Reconnect {name or self._last_addr}")
        self._refresh()

    def show_error(self, text: str) -> None:
        if not text:
            self._ride_error = ""
        _show_message(self.error_label, text)

    def _on_ride_error(self, text: str) -> None:
        self._ride_error = text
        self.show_error(text)

    def _on_bridge_error(self, text: str) -> None:
        """Keep e.g. 'connection lost, ride paused' visible next to follow-up BLE errors."""
        self.show_error(f"{self._ride_error}\n{text}" if self._ride_error else text)

    # --- overridable dialogs ---
    def confirm_stop(self) -> bool:
        answer = QMessageBox.question(self, "Stop ride", "Stop and save this ride?")
        return answer == QMessageBox.StandardButton.Yes

    def open_connect_dialog(self) -> None:
        dialog = ConnectDialog(self._repo, self._bridge, self)
        dialog.exec()
        dialog.deleteLater()

    def open_hr_dialog(self) -> None:
        dialog = HrDialog(self._repo, self._bridge, self)
        dialog.exec()
        dialog.deleteLater()

    # --- actions ---
    def _toggle_strap(self) -> None:
        if self._bridge.hr_state is not ConnectionState.DISCONNECTED:
            self._bridge.hr_disconnect()
            return
        try:
            self.open_hr_dialog()
        except Exception as exc:
            self.show_error(f"Could not open the connection dialog: {exc}")
        self.reload_settings()

    def _reconnect_strap(self) -> None:
        if self._last_strap and self._bridge.hr_state is ConnectionState.DISCONNECTED:
            self._bridge.hr_connect(self._last_strap)

    def _toggle_ride(self) -> None:
        state = self._controller.state
        if state is RunnerState.RUNNING:
            self._controller.pause()
        elif state is RunnerState.PAUSED:
            self._controller.resume()
        else:
            self._controller.start()

    def _stop(self) -> None:
        if self._controller.is_active and self.confirm_stop():
            self._controller.stop()

    def _toggle_connection(self) -> None:
        if self._bridge.state is not ConnectionState.DISCONNECTED:
            self._bridge.disconnect_trainer()
            return
        try:
            self.open_connect_dialog()
        except Exception as exc:
            self.show_error(f"Could not open the connection dialog: {exc}")
        self.reload_settings()

    def _reconnect(self) -> None:
        if self._last_addr and self._bridge.state is ConnectionState.DISCONNECTED:
            self.show_error("")
            self._bridge.connect_trainer(self._last_addr)

    # --- display ---
    def showEvent(self, event: QShowEvent | None) -> None:
        super().showEvent(event)
        self.reload_settings()

    def _on_connection(self, state: ConnectionState) -> None:
        if state is ConnectionState.CONNECTED:
            self.show_error("")
        self._refresh()

    def _on_heart_rate(self, reading: HeartRate) -> None:
        self._battery = "" if reading.battery is None else f" {reading.battery} %"
        self._refresh()

    def _on_hr_connection(self, _state: ConnectionState) -> None:
        self._battery = ""
        self._refresh()

    def _on_state(self, state: RunnerState) -> None:
        if state is RunnerState.RUNNING:
            self.show_error("")
        self._refresh()

    def _refresh(self) -> None:
        controller = self._controller
        runner = controller.runner
        workout = controller.workout
        blocks = controller.blocks
        state = controller.state

        name = "No workout selected" if workout is None else workout.name
        self.title_label.setText(name)
        self.title_label.setToolTip("" if workout is None else html.escape(name))
        self.state_label.setText(state.value.capitalize())
        self.state_label.setVisible(workout is not None and state is not RunnerState.IDLE)
        tones = {RunnerState.RUNNING: "ok", RunnerState.PAUSED: "warn"}
        theme.set_role(self.state_label, tones.get(state, ""), "tone")
        self._show_telemetry(controller.latest_telemetry)
        km = controller.distance_m / 1000
        if self._units == "imperial":
            self.distance_label.setText(f"{km / KM_PER_MILE:.2f} mi")
        else:
            self.distance_label.setText(f"{km:.2f} km")
        self.calories_label.setText(f"{controller.kcal:.0f} kcal")
        avg_p, avg_h = controller.avg_power, controller.avg_hr
        self.avg_power_label.setText(NO_VALUE if avg_p is None else f"{avg_p:.0f} W")
        self.avg_hr_label.setText(NO_VALUE if avg_h is None else f"{avg_h:.0f} bpm")
        if runner is not None:
            self.target_label.setText("Open" if runner.open_mode else f"{runner.target_w} W")
            rpm = runner.target_rpm
            self.target_rpm_label.setText(NO_VALUE if rpm is None else f"{rpm} rpm")
            self.time_left_label.setText(_countdown(runner.remaining_s))
            self.block_left_label.setText(_countdown(runner.block_remaining_s))
            self.ride_time_label.setText(mmss(runner.ride_s))
        else:
            self.target_label.setText(NO_VALUE)
            self.target_rpm_label.setText(NO_VALUE)
            self.time_left_label.setText(mmss(blocks[-1].t1) if blocks else NO_VALUE)
            self.block_left_label.setText(NO_VALUE)
            self.ride_time_label.setText(mmss(0))

        ftp = controller.ftp_w if runner is not None else self._ftp_w
        profile = (list(blocks), ftp, self._power_zones)
        if profile != self._drawn:
            self._drawn = profile
            self.plot.set_profile(*profile)
        self.plot.set_cursor(runner.elapsed_s if runner is not None else None)
        self.plot.set_trace(controller.history_t, controller.history_power)
        self._show_countdown()
        self._update_buttons(state, bool(blocks))

    def _show_countdown(self) -> None:
        """Last seconds before a sprint or a hard block; hidden while paused."""
        runner = self._controller.runner
        found = None
        seconds = 0
        if runner is not None and runner.state is RunnerState.RUNNING and runner.next_block:
            seconds = math.ceil(runner.block_remaining_s - 1e-6)
            if 0 < seconds <= COUNTDOWN_S:
                found = announcement(runner.next_block, self._controller.ftp_w, self._power_zones)
        label = self.countdown_label
        if found is None:
            label.hide()
            return
        text, zone = found
        style = (
            f"background: {theme.SURFACE}; color: {theme.TEXT}; padding: 6px 28px;"
            f" border: 3px solid {theme.ZONE_COLORS[zone]}; border-radius: 12px;"
        )
        if style != label.styleSheet():
            label.setStyleSheet(style)
        label.setText(
            f'<div align="center"><span style="font-size: 20px;">{text}</span><br>'
            f'<span style="font-size: 64px; font-weight: 600;">{seconds}</span></div>'
        )
        label.adjustSize()
        label.move(  # every second, so it also follows a resize
            max(0, (self.plot.width() - label.width()) // 2),
            max(0, (self.plot.height() - label.height()) // 2),
        )
        label.show()
        label.raise_()

    def _update_buttons(self, state: RunnerState, has_blocks: bool) -> None:
        link = self._bridge.state
        connected = link is ConnectionState.CONNECTED
        active = self._controller.is_active
        if state is RunnerState.RUNNING:
            self.start_button.setText("Pause")
            self.start_button.setEnabled(True)
        elif state is RunnerState.PAUSED:
            self.start_button.setText("Resume")
            self.start_button.setEnabled(connected)
        else:
            self.start_button.setText("Start")
            self.start_button.setEnabled(connected and has_blocks)
        for button in (
            self.skip_button,
            self.extend_button,
            self.power_down_button,
            self.power_up_button,
            self.stop_button,
        ):
            button.setEnabled(active)
        runner = self._controller.runner
        if runner is not None and runner.open_mode:  # there is no target to adjust
            self.power_down_button.setEnabled(False)
            self.power_up_button.setEnabled(False)

        self.status_label.setText(
            "No trainer" if link is ConnectionState.DISCONNECTED else f"Trainer {link.value}"
        )
        theme.set_role(self.status_label, _tone(link), "tone")
        strap = self._bridge.hr_state
        no_strap = strap is ConnectionState.DISCONNECTED
        self.hr_status_label.setText("" if no_strap else f"Strap {strap.value}{self._battery}")
        self.hr_status_label.setVisible(not no_strap)
        theme.set_role(self.hr_status_label, _tone(strap), "tone")
        self.hr_button.setText("HR strap" if no_strap else "Disconnect strap")
        self.hr_reconnect_button.setVisible(no_strap and bool(self._last_strap))
        offline = link is ConnectionState.DISCONNECTED
        self.connect_button.setText("Connect" if offline else "Disconnect")
        theme.set_role(self.connect_button, "primary" if offline else "")
        self.reconnect_button.setVisible(offline and bool(self._last_addr))

    def _show_telemetry(self, tel: Telemetry | None) -> None:
        def text(value: float | None, unit: str, digits: int = 0) -> str:
            return NO_VALUE if value is None else f"{value:.{digits}f} {unit}"

        self.power_label.setText(text(tel.power_w if tel else None, "W"))
        self._show_power_zone(tel.power_w if tel else None)
        self.cadence_label.setText(text(tel.cadence_rpm if tel else None, "rpm"))
        self._show_hr(self._controller.latest_hr)
        speed = tel.speed_kph if tel else None
        if self._units == "imperial":
            self.speed_label.setText(
                text(None if speed is None else speed / KM_PER_MILE, "mph", digits=1)
            )
        else:
            self.speed_label.setText(text(speed, "km/h", digits=1))

    def _show_hr(self, bpm: int | None) -> None:
        self.hr_label.setText(NO_VALUE if bpm is None else f"{bpm} bpm")
        zone = self._hr_zones.zone_of(bpm) if bpm is not None and self._hr_zones else None
        self._paint_zone(self.hr_label, self.hr_caption, self._hr_tile, "Heart rate", zone)

    def _show_power_zone(self, watts: float | None) -> None:
        zone = self._power_zones.zone_of(watts) if watts is not None else None
        self._paint_zone(self.power_label, self.power_caption, self._power_tile, "Power", zone)

    @staticmethod
    def _paint_zone(
        label: ValueLabel, caption: QLabel, tile: QFrame, name: str, zone: int | None
    ) -> None:
        caption.setText(name if zone is None else f"{name}  ·  Z{zone}")
        style = "" if zone is None else f"background: {theme.ZONE_COLORS[zone]};"
        if style != label.styleSheet():  # the whole tile shows the zone
            label.setStyleSheet(style)
            label.set_color(theme.ON_ZONE if style else None)
            caption.setStyleSheet(f"color: {theme.ON_ZONE};" if style else "")
            tile.setStyleSheet(f"QFrame#tile {{ {style} }}" if style else "")
