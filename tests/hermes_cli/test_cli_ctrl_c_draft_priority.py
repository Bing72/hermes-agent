"""Ctrl+C / Ctrl+Q in the classic CLI: a typed draft wins over interrupting a running turn.

Mirrors the Ink TUI's ``resolveCtrlCComposerAction``: pressing Ctrl+C to wipe a half-typed
follow-up must not kill the in-flight turn; the next press on an empty prompt interrupts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.history import FileHistory

import hermes_cli.cli_tui_mixin as mixin_mod
from hermes_cli.cli_modal_mixin import CLIModalMixin
from hermes_cli.cli_stream_mixin import CLIStreamMixin


class _Cli(mixin_mod.CLITuiMixin, CLIModalMixin, CLIStreamMixin):
    def __init__(self):
        self._agent_running = True
        self.agent = object()
        self._attached_images = []
        self._last_ctrl_c_time = 0.0
        self._should_exit = False
        self._sudo_state = self._secret_state = self._approval_state = None
        self._clarify_state = self._connection_state = None

    # Voice / modal / overlay layers sit above the draft check and are covered elsewhere.
    _close_model_picker = _close_command_palette = _submit_slash_confirm_response = None

    def _tui_cancel_voice_recording(self, event):
        return False

    def _tui_cancel_foreground_ui(self, event, closers):
        return False

    def _clear_active_overlays_for_interrupt(self):
        self._sudo_state = self._secret_state = self._approval_state = None
        self._clarify_state = self._connection_state = None


@pytest.mark.parametrize("handler", ["_tui_handle_ctrl_c", "_tui_handle_ctrl_q"])
def test_draft_is_cleared_before_a_running_turn_is_interrupted(monkeypatch, handler):
    interrupts = []
    monkeypatch.setattr(mixin_mod, "request_hard_interrupt", interrupts.append)
    cli = _Cli()
    buffer = Buffer()
    buffer.text = "half typed follow-up"
    app = SimpleNamespace(current_buffer=buffer, invalidate=lambda: None,
                          exit=lambda: setattr(cli, "_should_exit", True))
    event = SimpleNamespace(app=app)

    getattr(cli, handler)(event)
    assert buffer.text == "" and interrupts == [] and not cli._should_exit

    getattr(cli, handler)(event)
    assert interrupts == [cli.agent] and not cli._should_exit


async def _load_history(buffer):
    buffer.load_history_if_not_yet_loaded()
    await buffer._load_history_task


def _event(cli, buffer):
    return SimpleNamespace(app=SimpleNamespace(
        current_buffer=buffer, invalidate=lambda: None,
        exit=lambda: setattr(cli, "_should_exit", True)), arg=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("handler", ["_tui_handle_ctrl_c", "_tui_handle_ctrl_q", "_tui_handle_double_escape"])
async def test_cancelled_draft_precedes_existing_history_without_duplicate_recall(tmp_path, busy, handler):
    history = FileHistory(str(tmp_path / "history"))
    history.append_string("older message")
    history.append_string("latest message")
    buffer = Buffer(history=history)
    await _load_history(buffer)
    cli = _Cli()
    cli._agent_running = busy
    event = _event(cli, buffer)
    buffer.text = "unfinished follow-up"

    getattr(cli, handler)(event)
    assert buffer.text == "" and not cli._should_exit
    await _load_history(buffer)
    cli._tui_history_up(event)
    assert buffer.text == "unfinished follow-up"

    getattr(cli, handler)(event)
    await _load_history(buffer)
    cli._tui_history_up(event)
    assert buffer.text == "unfinished follow-up"
    cli._tui_history_up(event)
    assert buffer.text == "latest message"
    cli._tui_history_up(event)
    assert buffer.text == "older message"
    cli._tui_history_down(event)
    assert buffer.text == "latest message"


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["_tui_handle_ctrl_c", "_tui_handle_ctrl_q", "_tui_handle_double_escape"])
async def test_cancelled_paste_recalls_content_after_the_paste_file_is_gone(tmp_path, handler):
    paste = tmp_path / "paste.txt"
    paste.write_text("first line\nsecond line", encoding="utf-8")
    buffer = Buffer(history=FileHistory(str(tmp_path / "history")))
    await _load_history(buffer)
    cli = _Cli()
    event = _event(cli, buffer)
    buffer.text = f"Please review [Pasted text #1: 2 lines → {paste}]"

    getattr(cli, handler)(event)
    paste.unlink()
    await _load_history(buffer)
    cli._tui_history_up(event)
    assert buffer.text == "Please review first line\nsecond line"


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["_tui_handle_ctrl_c", "_tui_handle_ctrl_q", "_tui_handle_double_escape"])
async def test_whitespace_cancel_does_not_insert_a_blank_history_entry(tmp_path, handler):
    history = FileHistory(str(tmp_path / "history"))
    history.append_string("previous message")
    buffer = Buffer(history=history)
    await _load_history(buffer)
    cli = _Cli()
    event = _event(cli, buffer)
    buffer.text = "  \n  "

    getattr(cli, handler)(event)
    await _load_history(buffer)
    cli._tui_history_up(event)
    assert buffer.text == "previous message"


def test_secret_prompt_cancel_never_enters_message_history(tmp_path):
    history = FileHistory(str(tmp_path / "history"))
    buffer = Buffer(history=history)
    cli = _Cli()
    cli._agent_running = False
    cli._secret_state = {"prompt": "API key"}
    buffer.text = "synthetic private response"

    cli._tui_handle_ctrl_c(_event(cli, buffer))
    assert buffer.text == "" and history.get_strings() == []
    assert not (tmp_path / "history").exists()


def test_image_only_cancel_keeps_empty_prompt_exit_behavior(tmp_path):
    history = FileHistory(str(tmp_path / "history"))
    buffer = Buffer(history=history)
    cli = _Cli()
    cli._agent_running = False
    cli._attached_images = ["synthetic-image.png"]
    event = _event(cli, buffer)

    cli._tui_handle_ctrl_c(event)
    assert cli._attached_images == [] and not cli._should_exit
    assert history.get_strings() == []
    cli._tui_handle_ctrl_c(event)
    assert cli._should_exit
