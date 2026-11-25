"""Choosing, adding and removing riders. The switch itself is done by app.py."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pywatts.storage.repo import Repo
from pywatts.storage.riders import Rider, Riders
from pywatts.ui.widgets.common import GAP, text_label


class RiderPicker(QDialog):
    """At startup. The one who rode last is preselected."""

    def __init__(self, riders: list[Rider], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("pyWatts")
        self.setMinimumWidth(320)
        self._riders = riders
        self.names = QListWidget()
        self.names.ensurePolished()  # else rows are measured without the stylesheet
        for rider in riders:
            self.names.addItem(QListWidgetItem(rider.name))
        last = max(range(len(riders)), key=lambda i: riders[i].last_used)
        self.names.setCurrentRow(last)
        self.names.itemDoubleClicked.connect(lambda _i: self.accept())
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(GAP)
        layout.addWidget(text_label("title", "Who is riding?"))
        layout.addWidget(self.names, 1)
        layout.addWidget(self.buttons)
        self.names.setFocus()

    def chosen(self) -> Rider | None:
        row = self.names.currentRow()
        return self._riders[row] if row >= 0 else None


class RiderButton(QToolButton):
    """The current rider, with a menu of the others."""

    switch_requested = pyqtSignal(object, bool)  # Rider, newly added
    riders_changed = pyqtSignal()

    def __init__(
        self,
        riders: Riders,
        repo: Repo,
        current: Rider,
        forget_login: Callable[[Rider], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._riders = riders
        self._repo = repo
        self._forget_login = forget_login
        self.current = current
        self.is_riding: Callable[[], bool] = lambda: False
        self.setObjectName("rider")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Change the rider")
        menu = QMenu(self)
        menu.aboutToShow.connect(lambda: self._guard(self.fill_menu))
        self.setMenu(menu)
        self._show()

    def has_others(self) -> bool:
        return len(self._riders.list()) > 1

    def reload(self) -> None:
        """The name may have changed in the profile."""
        self._guard(self._reload)

    def _reload(self) -> None:
        self.current = self._riders.register(self._repo.connection, self.current.path)
        self._show()

    def _show(self) -> None:
        width = 110
        name = self.fontMetrics().elidedText(self.current.name, Qt.TextElideMode.ElideRight, width)
        self.setText(_plain(name))

    def fill_menu(self) -> None:
        menu = self.menu()
        assert menu is not None
        menu.clear()
        if self.is_riding():
            action = menu.addAction("Not during a ride")
            assert action is not None
            action.setEnabled(False)
            return
        riders = self._riders.list()
        for rider in riders:
            action = menu.addAction(_plain(rider.name))
            assert action is not None
            action.setCheckable(True)
            action.setChecked(rider.id == self.current.id)
            action.triggered.connect(lambda _c, r=rider: self._guard(lambda: self._switch(r)))
        menu.addSeparator()
        action = menu.addAction("Add rider…")
        assert action is not None
        action.triggered.connect(lambda: self._guard(self._add))
        others = [r for r in riders if r.id != self.current.id and self._riders.can_remove(r)]
        if others:
            removal = menu.addMenu("Remove rider")
            assert removal is not None
            for rider in others:
                action = removal.addAction(_plain(rider.name) + "…")
                assert action is not None
                action.triggered.connect(lambda _c, r=rider: self._guard(lambda: self._remove(r)))

    def _switch(self, rider: Rider) -> None:
        if rider.id != self.current.id:
            self.switch_requested.emit(rider, False)

    def _add(self) -> None:
        name = self.ask_name()
        if name is not None:
            rider = self._riders.add(name, self._repo)
            self.riders_changed.emit()
            self.switch_requested.emit(rider, True)

    def _remove(self, rider: Rider) -> None:
        if self.confirm_remove(rider):
            self._forget_login(rider)  # first: if it fails, nothing is removed
            kept = self._riders.remove(rider)
            self.riders_changed.emit()
            self.show_info(f"{rider.name} was removed. The data is kept in\n{kept}")

    # --- overridable dialogs ---

    def ask_name(self) -> str | None:
        name, ok = QInputDialog.getText(self, "Add rider", "Name")
        return name if ok else None

    def confirm_remove(self, rider: Rider) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Remove rider")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            f"Remove {rider.name}?\n\nThe rides, profile and plan leave the app, and the Strava "
            "login is deleted. The file with the data is kept; moving it back brings the rider "
            "back. Workouts stay in the library."
        )
        remove = box.addButton("Remove", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is remove

    def show_info(self, text: str) -> None:
        QMessageBox.information(self, "Riders", text)

    def show_error(self, text: str) -> None:
        QMessageBox.warning(self, "Riders", text)

    def _guard(self, action: Callable[[], object]) -> None:
        """Never let a failure escape a slot: PyQt6 would abort the app."""
        try:
            action()
        except Exception as e:
            self.show_error(str(e) or type(e).__name__)


def _plain(text: str) -> str:
    """Qt would take & as the mark of a shortcut."""
    return text.replace("&", "&&")
