from ui.window.positions import PositionsMixin


class _Panel:
    def __init__(self, y, height, visible=True):
        self._y, self._height, self._visible = y, height, visible

    def y(self): return self._y
    def height(self): return self._height
    def isVisible(self): return self._visible


class _Window(PositionsMixin):
    pass


def test_cards_start_below_visible_music_card():
    window = _Window()
    window._header_panel = _Panel(12, 58)
    window._music_player_panel = _Panel(74, 190)
    assert window._right_column_top() == 274


def test_cards_use_header_when_music_card_is_hidden():
    window = _Window()
    window._header_panel = _Panel(12, 58)
    window._music_player_panel = _Panel(74, 190, visible=False)
    assert window._right_column_top() == 80
