from platform import platform
from typing import Any, List
import os
import sys
import time
import signal
from functools import partial

import urwid
import zulip

from zulipterminal.version import ZT_VERSION
from zulipterminal.helper import asynch
from zulipterminal.model import Model, GetMessagesArgs, ServerConnectionFailure
from zulipterminal.ui import View, Screen
from zulipterminal.ui_tools.utils import create_msg_box_list
from zulipterminal.ui_tools.views import HelpView, MsgInfoView
from zulipterminal.config.themes import ThemeSpec
from zulipterminal.ui_tools.views import PopUpConfirmationView


class Controller:
    """
    A class responsible for setting up the model and view and running
    the application.
    """

    def __init__(self, config_file: str, theme: ThemeSpec,
                 autohide: bool, notify: bool) -> None:
        self.theme = theme
        self.autohide = autohide
        self.notify_enabled = notify
        self.editor_mode = False  # type: bool
        self.editor = None  # type: Any

        self.show_loading()
        self.client = zulip.Client(config_file=config_file,
                                   client='ZulipTerminal/{} {}'.
                                          format(ZT_VERSION, platform()))
        self.model = Model(self)
        self.view = View(self)
        # Start polling for events after view is rendered.
        self.model.poll_for_events()

    @asynch
    def show_loading(self) -> None:

        def spinning_cursor() -> Any:
            while True:
                for cursor in '|/-\\':
                    yield cursor

        spinner = spinning_cursor()
        sys.stdout.write("\033[92mWelcome to Zulip.\033[0m\n")
        while not hasattr(self, 'view'):
            next_spinner = "Loading " + next(spinner)
            sys.stdout.write(next_spinner)
            sys.stdout.flush()
            time.sleep(0.1)
            sys.stdout.write('\b'*len(next_spinner))

        self.capture_stdout()

    def capture_stdout(self, path: str='debug.log') -> None:
        if hasattr(self, '_stdout'):
            return

        self._stdout = sys.stdout
        sys.stdout = open(path, 'a')

    def restore_stdout(self) -> None:
        if not hasattr(self, '_stdout'):
            return

        sys.stdout.flush()
        sys.stdout.close()
        sys.stdout = self._stdout
        sys.stdout.write('\n')
        del self._stdout

    def update_screen(self) -> None:
        # Write something to update pipe to trigger draw_screen
        if hasattr(self, 'update_pipe'):
            os.write(self.update_pipe, b'1')

    def draw_screen(self, *args: Any, **kwargs: Any) -> None:
        self.loop.draw_screen()

    def show_pop_up(self, to_show: Any, title: str) -> None:
        double_lines = dict(tlcorner='╔', tline='═', trcorner='╗',
                            rline='║', lline='║',
                            blcorner='╚', bline='═', brcorner='╝')
        cols, rows = self.loop.screen.get_cols_rows()
        self.loop.widget = urwid.Overlay(
            urwid.LineBox(to_show,
                          title,
                          **double_lines),
            self.view,
            align='center',
            valign='middle',
            # +2 to both of the following, due to LineBox
            width=to_show.width+2,
            height=min(3*rows//4, to_show.height)+2
        )

    def exit_popup(self) -> None:
        self.loop.widget = self.view

    def show_help(self) -> None:
        help_view = HelpView(self)
        self.show_pop_up(help_view, "Help Menu (up/down scrolls)")

    def show_msg_info(self, msg: Any) -> None:
        msg_info_view = MsgInfoView(self, msg)
        self.show_pop_up(msg_info_view,
                         "Message Information (up/down scrolls)")

    def search_messages(self, text: str) -> None:
        # Search for a text in messages
        self.model.index['search'].clear()
        self.model.set_search_narrow(text)

        self.model.found_newest = False
        self.model.get_messages(num_after=0, num_before=30, anchor=10000000000)
        msg_id_list = self.model.get_message_ids_in_current_narrow()

        w_list = create_msg_box_list(self.model, msg_id_list)
        self.model.msg_view.clear()
        self.model.msg_view.extend(w_list)
        focus_position = 0
        if focus_position >= 0 and focus_position < len(w_list):
            self.model.msg_list.set_focus(focus_position)

    def stream_muting_confirmation_popup(self, button: Any) -> None:
        currently_muted = self.model.is_muted_stream(button.stream_id)
        type_of_action = "unmuting" if currently_muted else "muting"
        question = urwid.Text(("bold", "Confirm " + type_of_action +
                               " of stream '" + button.stream_name+"' ?"),
                              "center")
        mute_this_stream = partial(self.model.toggle_stream_muted_status,
                                   button.stream_id)
        self.loop.widget = PopUpConfirmationView(self, question,
                                                 mute_this_stream)

    def narrow_to_stream(self, button: Any) -> None:
        already_narrowed = self.model.set_narrow(stream=button.stream_name)
        if already_narrowed:
            return

        self.model.found_newest = False
        # store the steam id in the model (required for get_message_ids...)
        self.model.stream_id = button.stream_id
        msg_id_list = self.model.get_message_ids_in_current_narrow()

        # if no messages are found get more messages
        if len(msg_id_list) == 0:
            get_msg_opts = dict(num_before=30, num_after=10,
                                anchor=None)  # type: GetMessagesArgs
            if hasattr(button, 'message'):
                get_msg_opts['anchor'] = button.message['id']
            self.model.get_messages(**get_msg_opts)
            msg_id_list = self.model.get_message_ids_in_current_narrow()

        if hasattr(button, 'message'):
            w_list = create_msg_box_list(
                self.model, msg_id_list, button.message['id'])
        else:
            w_list = create_msg_box_list(self.model, msg_id_list)

        self._finalize_show(w_list)

    def narrow_to_topic(self, button: Any) -> None:
        already_narrowed = self.model.set_narrow(stream=button.stream_name,
                                                 topic=button.topic_name)
        if already_narrowed:
            return

        self.model.found_newest = False
        # store the steam id in the model (required for get_message_ids...)
        self.model.stream_id = button.stream_id
        msg_id_list = self.model.get_message_ids_in_current_narrow()

        if len(msg_id_list) == 0:
            get_msg_opts = dict(num_before=30, num_after=10,
                                anchor=None)  # type: GetMessagesArgs
            if hasattr(button, 'message'):
                get_msg_opts['anchor'] = button.message['id']
            self.model.get_messages(**get_msg_opts)
            msg_id_list = self.model.get_message_ids_in_current_narrow()

        if hasattr(button, 'message'):
            w_list = create_msg_box_list(
                self.model, msg_id_list, button.message['id'])
        else:
            w_list = create_msg_box_list(self.model, msg_id_list)

        self._finalize_show(w_list)

    def narrow_to_user(self, button: Any) -> None:
        if hasattr(button, 'message'):
            emails = [recipient['email']
                      for recipient in button.message['display_recipient']
                      if recipient['email'] != self.model.client.email]
            if not emails and len(button.message['display_recipient']) == 1:
                emails = [self.model.user_email]
            user_emails = ', '.join(emails)
        else:
            user_emails = button.email

        already_narrowed = self.model.set_narrow(pm_with=user_emails)
        if already_narrowed:
            return

        self.model.found_newest = False
        msg_id_list = self.model.get_message_ids_in_current_narrow()

        if len(msg_id_list) == 0:
            get_msg_opts = dict(num_before=30, num_after=10,
                                anchor=None)  # type: GetMessagesArgs
            if hasattr(button, 'message'):
                get_msg_opts['anchor'] = button.message['id']
            self.model.get_messages(**get_msg_opts)
            msg_id_list = self.model.get_message_ids_in_current_narrow()

        if hasattr(button, 'message'):
            w_list = create_msg_box_list(
                self.model, msg_id_list, button.message['id'])
        else:
            w_list = create_msg_box_list(self.model, msg_id_list)

        self._finalize_show(w_list)

    def show_all_messages(self, button: Any) -> None:
        already_narrowed = self.model.set_narrow()
        if already_narrowed:
            return

        self.model.found_newest = False
        msg_id_list = self.model.get_message_ids_in_current_narrow()

        if hasattr(button, 'message'):
            w_list = create_msg_box_list(
                self.model, msg_id_list, button.message['id'])
        else:
            w_list = create_msg_box_list(self.model, msg_id_list)

        self._finalize_show(w_list)

    def show_all_pm(self, button: Any) -> None:
        already_narrowed = self.model.set_narrow(pms=True)
        if already_narrowed:
            return

        self.model.found_newest = False
        msg_id_list = self.model.get_message_ids_in_current_narrow()

        if len(msg_id_list) == 0:
            self.model.get_messages(num_before=30, num_after=10, anchor=None)
            msg_id_list = self.model.get_message_ids_in_current_narrow()

        w_list = create_msg_box_list(self.model, msg_id_list)

        self._finalize_show(w_list)

    def show_all_starred(self, button: Any) -> None:
        already_narrowed = self.model.set_narrow(starred=True)
        if already_narrowed:
            return

        self.model.found_newest = False
        msg_id_list = self.model.get_message_ids_in_current_narrow()

        if len(msg_id_list) == 0:
            self.model.get_messages(num_before=30, num_after=10, anchor=None)
            msg_id_list = self.model.get_message_ids_in_current_narrow()

        w_list = create_msg_box_list(self.model, msg_id_list)

        self._finalize_show(w_list)

    def _finalize_show(self, w_list: List[Any]) -> None:
        focus_position = self.model.get_focus_in_current_narrow()
        if focus_position == set():
            focus_position = len(w_list) - 1
        assert not isinstance(focus_position, set)
        self.model.msg_view.clear()
        if focus_position >= 0 and focus_position < len(w_list):
            self.model.msg_view.extend(w_list, focus_position)
        else:
            self.model.msg_view.extend(w_list)
        self.editor_mode = False

    def deregister_client(self) -> None:
        queue_id = self.model.queue_id
        self.client.deregister(queue_id, 1.0)

    def exit_handler(self, signum: int, frame: Any) -> None:
        self.deregister_client()
        sys.exit(0)

    def main(self) -> None:
        screen = Screen()
        screen.set_terminal_properties(colors=256)
        self.loop = urwid.MainLoop(self.view,
                                   self.theme,
                                   screen=screen)
        self.update_pipe = self.loop.watch_pipe(self.draw_screen)

        # Register new ^C handler
        signal.signal(signal.SIGINT, self.exit_handler)

        try:
            # TODO: Enable resuming? (in which case, remove ^Z below)
            disabled_keys = {
                'susp': 'undefined',  # Disable ^Z - no suspending
                'stop': 'undefined',  # Disable ^S - enabling shortcut key use
                'quit': 'undefined',  # Disable ^\, ^4
            }
            old_signal_list = screen.tty_signal_keys(**disabled_keys)
            self.loop.run()

        except Exception:
            self.restore_stdout()
            screen.tty_signal_keys(*old_signal_list)
            raise

        finally:
            self.restore_stdout()
            screen.tty_signal_keys(*old_signal_list)
