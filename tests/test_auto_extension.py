from __future__ import annotations

import pytest

from core.auto_extension import AutoExtensionManager


def test_needs_are_grouped_and_become_eligible(tmp_path):
    manager = AutoExtensionManager(tmp_path / "extensions.json")
    for _ in range(3):
        manager.record_unmet("ANO, je veux piloter mes ampoules connectées", "outil absent")

    needs = manager.eligible(threshold=3, days=14)

    assert len(needs) == 1
    assert len(needs[0].samples) == 3


def test_plugin_core_import_is_refused(tmp_path):
    manager = AutoExtensionManager(tmp_path / "extensions.json")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "unsafe.py").write_text(
        "import core.audio_engine\nPLUGIN={'name':'unsafe','description':'x','parameters':{'type':'OBJECT'}}\ndef run(parameters): return 'x'\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="protégé"):
        manager._validated_plugin(stage)


def test_plugin_contract_is_accepted(tmp_path):
    manager = AutoExtensionManager(tmp_path / "extensions.json")
    stage = tmp_path / "stage"
    stage.mkdir()
    plugin = stage / "lights.py"
    plugin.write_text(
        "PLUGIN={'name':'lights','description':'x','parameters':{'type':'OBJECT'}}\ndef run(parameters): return 'ok'\n",
        encoding="utf-8",
    )

    assert manager._validated_plugin(stage) == plugin
