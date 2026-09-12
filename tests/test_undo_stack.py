from core import undo_stack


def setup_function():
    undo_stack.clear()


def test_undo_is_lifo():
    values = []
    undo_stack.push("première", lambda: values.append(1) or "ok")
    undo_stack.push("seconde", lambda: values.append(2) or "ok")
    assert undo_stack.history() == ["seconde", "première"]
    assert "seconde" in undo_stack.undo_last()
    assert values == [2]


def test_stack_is_bounded():
    for index in range(undo_stack.MAX_DEPTH + 5):
        undo_stack.push(str(index), lambda: None)
    assert len(undo_stack.history()) == undo_stack.MAX_DEPTH
