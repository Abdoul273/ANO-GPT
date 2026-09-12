"""tests/test_auto_debug.py — Tests unitaires pour core/auto_debug.py et actions/auto_debug.py."""

import pytest
from unittest.mock import patch, MagicMock
from core.auto_debug import (
    clean_ansi,
    parse_error_snippet,
    resolve_local_source_context,
    ParsedError,
    auto_debug_live,
)
from actions.auto_debug import auto_debug_action
from actions.auto_debug import _apply_verified_unified_patch


def test_clean_ansi():
    raw = "\x1b[31;1mError:\x1b[0m \x1b[32mfile.py:10\x1b[0m"
    cleaned = clean_ansi(raw)
    assert cleaned == "Error: file.py:10"


def test_parse_python_traceback():
    trace = """
Traceback (most recent call last):
  File "src/processor.py", line 42, in calculate_metrics
    result = total / count
ZeroDivisionError: division by zero
"""
    parsed = parse_error_snippet(trace)
    assert parsed is not None
    assert parsed.language == "python"
    assert parsed.error_type == "ZeroDivisionError"
    assert "division by zero" in parsed.message
    assert parsed.file_path == "src/processor.py"
    assert parsed.line_number == 42
    assert parsed.culprit_function == "calculate_metrics"


def test_parse_pytest_failure():
    log = "FAILED tests/test_math.py::test_division - AssertionError: assert 0 == 1"
    parsed = parse_error_snippet(log)
    assert parsed is not None
    assert parsed.language == "python"
    assert parsed.error_type == "AssertionError"
    assert parsed.file_path == "tests/test_math.py"
    assert parsed.culprit_function == "test_division"


def test_parse_rust_panic():
    panic_text = "thread 'main' panicked at 'index out of bounds: the len is 3 but the index is 5', src/lib.rs:27:9"
    parsed = parse_error_snippet(panic_text)
    assert parsed is not None
    assert parsed.language == "rust"
    assert parsed.error_type == "RustPanic"
    assert "index out of bounds" in parsed.message
    assert parsed.file_path == "src/lib.rs"
    assert parsed.line_number == 27
    assert parsed.column == 9


def test_parse_rustc_error():
    rustc_output = """
error[E0382]: use of moved value: `data`
  --> src/main.rs:15:20
   |
14 |     let data = vec![1, 2, 3];
   |         ---- move occurs because `data` has type `Vec<i32>`
15 |     process(data);
"""
    parsed = parse_error_snippet(rustc_output)
    assert parsed is not None
    assert parsed.language == "rust"
    assert "rustc[E0382]" in parsed.error_type
    assert parsed.file_path == "src/main.rs"
    assert parsed.line_number == 15


def test_parse_cpp_gcc_error():
    gcc_output = "engine/renderer.cpp:88:24: error: no member named 'flush_pipeline' in 'GraphicsContext'"
    parsed = parse_error_snippet(gcc_output)
    assert parsed is not None
    assert parsed.language == "cpp"
    assert parsed.error_type == "CompilerError"
    assert "no member named 'flush_pipeline'" in parsed.message
    assert parsed.file_path == "engine/renderer.cpp"
    assert parsed.line_number == 88


def test_parse_cpp_linker_error():
    ld_output = "main.o: in function `main': main.cpp:12: undefined reference to `Database::connect()'"
    parsed = parse_error_snippet(ld_output)
    assert parsed is not None
    assert parsed.language == "cpp"
    assert parsed.error_type == "LinkerError"
    assert "Database::connect()" in parsed.message


def test_parse_cmake_error():
    cmake_output = "CMake Error at CMakeLists.txt:45 (find_package): Could not find OpenSSL"
    parsed = parse_error_snippet(cmake_output)
    assert parsed is not None
    assert parsed.language == "cmake"
    assert parsed.error_type == "CMakeError"
    assert parsed.file_path == "CMakeLists.txt"
    assert parsed.line_number == 45


def test_parse_segfault():
    seg_output = "Segmentation fault (core dumped)"
    parsed = parse_error_snippet(seg_output)
    assert parsed is not None
    assert parsed.language == "cpp"
    assert parsed.error_type == "SegmentationFault"


def test_parse_js_ts_error():
    js_output = """
TypeError: Cannot read properties of undefined (reading 'map')
    at renderList (/app/src/components/List.tsx:25:18)
    at App (/app/src/App.tsx:10:5)
"""
    parsed = parse_error_snippet(js_output)
    assert parsed is not None
    assert parsed.language == "typescript"
    assert parsed.error_type == "TypeError"
    assert parsed.file_path == "/app/src/components/List.tsx"
    assert parsed.line_number == 25


def test_parse_go_panic():
    go_output = """
panic: runtime error: slice bounds out of range [:10] with capacity 5

goroutine 1 [running]:
main.process(...)
	/home/user/app/main.go:34 +0x5a
"""
    parsed = parse_error_snippet(go_output)
    assert parsed is not None
    assert parsed.language == "go"
    assert parsed.error_type == "GoPanic"
    assert parsed.file_path == "/home/user/app/main.go"
    assert parsed.line_number == 34


def test_parse_shell_error():
    shell_output = "bash: ligne 5: rg: commande introuvable"
    parsed = parse_error_snippet(shell_output)
    assert parsed is not None
    assert parsed.language == "shell"
    assert parsed.error_type == "CommandNotFound"
    assert "rg" in parsed.message


def test_resolve_local_source_context(tmp_path):
    f = tmp_path / "test_script.py"
    lines = [f"line_{i} = {i}" for i in range(1, 30)]
    f.write_text("\n".join(lines), encoding="utf-8")

    ctx = resolve_local_source_context(str(f), line_number=15, radius=3)
    assert "Fichier source" in ctx
    assert "-->   15 | line_15 = 15" in ctx
    assert "      12 | line_12 = 12" in ctx


def test_auto_debug_live_flow():
    mock_diag = MagicMock()
    mock_diag.spoken_summary = "Erreur de division par zéro détectée à la ligne 42."
    mock_diag.root_cause = "Division par zéro."
    mock_diag.hud_card = {"title": "🐞 ZeroDivisionError", "body": "Détail", "type": "error"}

    with patch("core.auto_debug.screen_capture.capture_window_or_screen", return_value=(b"fake_img", "image/jpeg", {})), \
         patch("core.auto_debug.generate_debug_diagnostic", return_value=mock_diag):
        spoken, diag = auto_debug_live(user_query="C'est quoi ce bug ?", input_text="ZeroDivisionError: division by zero")
        assert "Erreur de division par zéro" in spoken
        assert diag == mock_diag


def test_auto_debug_action():
    mock_player = MagicMock()
    with patch("actions.auto_debug.auto_debug_live") as mock_live:
        mock_diag = MagicMock()
        mock_diag.code_diff = ""
        mock_diag.parsed_error = None
        mock_live.return_value = ("Division par zéro corrigée.", mock_diag)

        res = auto_debug_action({"query": "Aide-moi"}, player=mock_player)
        assert res == "Division par zéro corrigée."


def test_auto_debug_is_pinned_to_terra_and_parses_structured_response(monkeypatch):
    import core.auto_debug as debug
    monkeypatch.setattr("core.llm_client.get_api_key_for", lambda provider: "key")
    captured = {}
    def fake_call(messages, tools, timeout, **kwargs):
        captured.update(kwargs)
        return {"content": '{"root_cause":"x","spoken_summary":"x","code_diff":"","fix_command":"","verification_commands":["pytest -q"],"evidence":["trace"],"risks":[],"confidence":"high","full_explanation":"detail"}'}
    monkeypatch.setattr("core.llm_client._call_openai_compat", fake_call)
    diag = debug.generate_debug_diagnostic(parse_error_snippet("ZeroDivisionError: division by zero"))
    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-5.6-terra"
    assert diag.confidence == "high"
    assert diag.verification_commands == ["pytest -q"]


def test_auto_apply_rejects_non_patch_and_applies_valid_patch(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    assert "aucun fichier" in _apply_verified_unified_patch(target, "answer = 2")
    result = _apply_verified_unified_patch(target, "--- sample.py\n+++ sample.py\n@@ -1 +1 @@\n-answer = 1\n+answer = 2\n")
    assert "Correctif appliqué" in result
    assert target.read_text(encoding="utf-8") == "answer = 2\n"
    assert target.with_suffix(".py.bak").exists()
