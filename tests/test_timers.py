from datetime import datetime, timedelta
from core.timers import TimerService

def test_named_timers_are_simultaneous_and_persistent(tmp_path):
    path = tmp_path / "timers.json"; now = datetime(2026, 8, 16, 12)
    service = TimerService(path); service.create("pâtes", 540, now); service.create("thé", 240, now)
    assert {x["name"] for x in service.active(now)} == {"pâtes", "thé"}
    assert len(TimerService(path).due(now + timedelta(seconds=241))) == 1
