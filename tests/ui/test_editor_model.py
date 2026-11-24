import pytest


def test_cadence_field():
    from pywatts.ui.widgets.editor_model import EditError, EditorModel, default_steady

    m = EditorModel()
    path = m.add(default_steady())
    m.set_field(path, "cadence", "90")
    assert m.get(path).cadence == 90
    m.set_field(path, "cadence", "")
    assert m.get(path).cadence is None
    for bad in ("0", "301", "abc", "9.5"):
        with pytest.raises(EditError):
            m.set_field(path, "cadence", bad)
