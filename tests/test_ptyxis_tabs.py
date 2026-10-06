from types import SimpleNamespace

from ocdeck.ptyxis_tabs import select_tab


class Frame:
    def __init__(self, titles, index=0):
        self.titles = titles
        self.index = index
        self.steps = 0

    def get_name(self):
        return self.titles[self.index]

    def get_role_name(self):
        return "frame"

    def get_child_count(self):
        return len(self.titles)

    def get_child_at_index(self, index):
        return SimpleNamespace(
            get_name=lambda: self.titles[index],
            get_role_name=lambda: "page tab",
            get_child_count=lambda: 0,
        )

    def get_action_iface(self):
        return self

    def get_n_actions(self):
        return 1

    def get_action_name(self, index):
        return "page.next"

    def do_action(self, index):
        self.index = (self.index + 1) % len(self.titles)
        self.steps += 1
        return True

    def clear_cache(self):
        pass


def test_selects_deck_from_an_unrelated_tab_without_typing():
    frame = Frame(["OpenCode session", "Shell", "OC Deck"])
    assert select_tab(frame, "OC Deck", settle=lambda: None)
    assert frame.get_name() == "OC Deck"
    assert frame.steps == 2


def test_already_selected_tab_is_left_in_place():
    frame = Frame(["OC Deck", "Shell"])
    assert select_tab(frame, "OC Deck")
    assert frame.steps == 0


def test_missing_or_ambiguous_tab_does_not_switch_other_tabs():
    for titles in (["Shell", "OpenCode session"], ["Shell", "OC Deck", "OC Deck"]):
        frame = Frame(titles)
        assert not select_tab(frame, "OC Deck")
        assert frame.steps == 0


def test_accepted_but_ineffective_native_action_is_not_success():
    frame = Frame(["Shell", "OC Deck"])
    frame.do_action = lambda index: True
    assert not select_tab(frame, "OC Deck", settle=lambda: None)
