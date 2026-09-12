"""Le parseur de rappels comprend ce qui se dit réellement à la voix."""
from datetime import datetime, timedelta

from actions.reminder import _parse_reminder_text as parse


def test_spelled_delta_is_not_a_clock_time():
    r = parse("rappelle-moi dans deux heures de sortir le pain")
    assert r["message"] == "Sortir le pain"
    target = datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M")
    assert timedelta(hours=1, minutes=58) < target - datetime.now() <= timedelta(hours=2)


def test_evening_hint_makes_pm():
    assert parse("ce soir à 9h regarder le match")["time"] == "21:00"
    assert parse("vendredi à 8h du soir appeler maman")["time"] == "20:00"


def test_half_past_and_weekday():
    r = parse("lundi prochain à 7 heures et demie réunion")
    assert r["time"] == "07:30"
    assert datetime.strptime(r["date"], "%Y-%m-%d").weekday() == 0
    assert r["message"] == "Réunion"


def test_half_hour_spelled():
    assert parse("dans une demi-heure boire de l'eau")["message"].lower().startswith("boire")


def test_english_remind_me_is_stripped():
    assert parse("remind me in 10 minutes to check the oven")["message"] == "Check the oven"
