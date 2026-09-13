from core.text_clean import strip_emoji, strip_emoji_deep


def test_strip_emoji_keeps_text_and_punctuation():
    assert strip_emoji("Tentafruit 🍋😂🔥 le citron 🍌 #fyp ✨ fin 🇫🇷 ❤️ 1️⃣") == "Tentafruit le citron #fyp fin 1"
    assert strip_emoji("Coût 12 €, 50 % ; « ok » ± é — …") == "Coût 12 €, 50 % ; « ok » ± é — …"


def test_strip_emoji_deep_walks_tool_payloads():
    payload = {"result": "Vidéo 🍋🔥 top", "items": [{"desc": "😂 lol", "views": 27}]}
    assert strip_emoji_deep(payload) == {"result": "Vidéo top", "items": [{"desc": " lol", "views": 27}]}
