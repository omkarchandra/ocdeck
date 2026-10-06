"""Typed confirmation for dismissing every listed Sentinel alarm at once.

Dismissal stays receipt-only (C106/C108): this screen only asks the owner to
type how many alarms they are dismissing; it never shows alarm contents, so
it is safe in privacy mode.
"""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static


class DismissAllScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    DismissAllScreen { align: center middle; }
    DismissAllScreen > VerticalScroll {
        width: 60; max-width: 95%; height: auto; max-height: 95%;
        background: $surface; border: round $accent; padding: 1 2;
    }
    DismissAllScreen Static { height: auto; margin-bottom: 1; }
    """

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        noun = "alarm" if self.count == 1 else "alarms"
        with VerticalScroll():
            yield Static(f"Dismiss all {self.count} listed {noun}?", markup=False)
            yield Static(
                "Receipt only: they stay listed, dimmed, and the alarm record is never edited. "
                f"Type {self.count} and press Enter to confirm; Esc cancels.",
                markup=False,
            )
            yield Input(placeholder=str(self.count), id="dismiss-all-count")

    def on_mount(self) -> None:
        self.query_one("#dismiss-all-count", Input).focus()

    @on(Input.Submitted, "#dismiss-all-count")
    def submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() == str(self.count))

    def action_cancel(self) -> None:
        self.dismiss(False)
