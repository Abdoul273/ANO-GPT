from core.azure_speech_stt import (
    _fast_text, _multipart_fast_request, _wav, agree, azure_verify_enabled, normalise,
)


def test_pcm_is_wrapped_as_16khz_mono_wav():
    wav = _wav(b"\x00\x00" * 320)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"


def test_azure_agreement_rejects_unrelated_sentences():
    assert agree("Quelle heure est-il", "quelle heure est il")
    assert not agree("Quelle heure est-il", "dis moi si je peux t aider")
    assert normalise("ANO, quelle heure ?") == "ano quelle heure"


def test_fast_request_contains_phrase_list_without_a_secret():
    body, boundary = _multipart_fast_request(b"RIFF", ["ANO-GPT", "Conakry", "ANO-GPT"])
    assert boundary.encode() in body
    assert body.count("ANO-GPT".encode()) == 1
    assert b"Conakry" in body


def test_fast_response_collects_segment_text():
    assert _fast_text({"combinedPhrases": [{"text": "quelle heure"}, {"text": "est-il"}]}) == "quelle heure est-il"


def test_azure_verify_env_can_disable_the_network_second_opinion(monkeypatch):
    monkeypatch.setenv("ANOGPT_AZURE_VERIFY", "off")
    assert not azure_verify_enabled(True)
