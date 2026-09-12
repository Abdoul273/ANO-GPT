"""Les tâches de fond restent visibles et leur détail suit le journal agy."""

from core.tool_dispatcher import ToolDispatcher


class _UI:
    def __init__(self):
        self.shown = []
        self.updated = []

    def show_card(self, *args):
        self.shown.append(args)

    def update_card(self, *args):
        self.updated.append(args)


def test_carte_tache_affiche_progression_et_bouton_details():
    dispatcher = ToolDispatcher.__new__(ToolDispatcher)
    dispatcher.ui = _UI()
    task = {
        "id": "task-12345678", "kind": "agent", "status": "active",
        "spec": {"mission": "Clone le dépôt."},
        "state": {"phase": "running", "progress": 40, "log_tail": "git clone…"},
    }
    dispatcher._background_tasks = type("Service", (), {
        "cancel": lambda *_: True,
        "list_tasks": lambda *_args, **_kwargs: [task],
    })()

    dispatcher._render_background_task(task)

    kind, _title, body, actions = dispatcher.ui.shown[0]
    assert kind == "agent-task"
    assert "40%" in body and "████" in body
    assert any(action["label"] == "Détails" and not action["dismiss"] for action in actions)

    dispatcher._show_background_task_details(task["id"])
    assert "git clone" in dispatcher.ui.shown[-1][2]
