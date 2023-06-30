# Copyright 2014-2021 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <https://www.gnu.org/licenses/>.

"""Loggers and utilities related to logging."""

import os
import sys
import html as pyhtml
import logging
import contextlib
import collections
import copy
import faulthandler
import traceback
import warnings
import json
import inspect
import argparse
from typing import (TYPE_CHECKING, Any, Iterator, Mapping, MutableSequence,
                    Optional, Set, Tuple, Union, TextIO, Literal, cast)

from qutebrowser.qt import core as qtcore
# Optional imports
try:
    import colorama
except ImportError:
    colorama = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from qutebrowser.config import config as configmodule

_log_inited = False
_args = None

COLORS = ['black', 'red', 'green', 'yellow', 'blue', 'purple', 'cyan', 'white']
COLOR_ESCAPES = {color: '\033[{}m'.format(i)
                 for i, color in enumerate(COLORS, start=30)}
RESET_ESCAPE = '\033[0m'


# Log formats to use.
SIMPLE_FMT = ('{green}{asctime:8}{reset} {log_color}{levelname}{reset}: '
              '{message}')
EXTENDED_FMT = ('{green}{asctime:8}{reset} '
                '{log_color}{levelname:8}{reset} '
                '{cyan}{name:10} {module}:{funcName}:{lineno}{reset} '
                '{log_color}{message}{reset}')
EXTENDED_FMT_HTML = (
    '<tr>'
    '<td><pre>%(green)s%(asctime)-8s%(reset)s</pre></td>'
    '<td><pre>%(log_color)s%(levelname)-8s%(reset)s</pre></td>'
    '<td></pre>%(cyan)s%(name)-10s</pre></td>'
    '<td><pre>%(cyan)s%(module)s:%(funcName)s:%(lineno)s%(reset)s</pre></td>'
    '<td><pre>%(log_color)s%(message)s%(reset)s</pre></td>'
    '</tr>'
)
DATEFMT = '%H:%M:%S'
LOG_COLORS = {
    'VDEBUG': 'white',
    'DEBUG': 'white',
    'INFO': 'green',
    'WARNING': 'yellow',
    'ERROR': 'red',
    'CRITICAL': 'red',
}

# We first monkey-patch logging to support our VDEBUG level before getting the
# loggers.  Based on https://stackoverflow.com/a/13638084
# mypy doesn't know about this, so we need to ignore it.
VDEBUG_LEVEL = 9
logging.addLevelName(VDEBUG_LEVEL, 'VDEBUG')
logging.VDEBUG = VDEBUG_LEVEL  # type: ignore[attr-defined]

LOG_LEVELS = {
    'VDEBUG': logging.VDEBUG,  # type: ignore[attr-defined]
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL,
}


def vdebug(self: logging.Logger,
           msg: str,
           *args: Any,
           **kwargs: Any) -> None:
    """Log with a VDEBUG level.

    VDEBUG is used when a debug message is rather verbose, and probably of
    little use to the end user or for post-mortem debugging, i.e. the content
    probably won't change unless the code changes.
    """
    if self.isEnabledFor(VDEBUG_LEVEL):
        # pylint: disable=protected-access
        self._log(VDEBUG_LEVEL, msg, args, **kwargs)
        # pylint: enable=protected-access


logging.Logger.vdebug = vdebug  # type: ignore[attr-defined]


# The different loggers used.
statusbar = logging.getLogger('statusbar')
completion = logging.getLogger('completion')
destroy = logging.getLogger('destroy')
modes = logging.getLogger('modes')
webview = logging.getLogger('webview')
mouse = logging.getLogger('mouse')
misc = logging.getLogger('misc')
url = logging.getLogger('url')
procs = logging.getLogger('procs')
commands = logging.getLogger('commands')
init = logging.getLogger('init')
signals = logging.getLogger('signals')
hints = logging.getLogger('hints')
keyboard = logging.getLogger('keyboard')
downloads = logging.getLogger('downloads')
js = logging.getLogger('js')  # Javascript console messages
qt = logging.getLogger('qt')  # Warnings produced by Qt
ipc = logging.getLogger('ipc')
shlexer = logging.getLogger('shlexer')
save = logging.getLogger('save')
message = logging.getLogger('message')
config = logging.getLogger('config')
sessions = logging.getLogger('sessions')
webelem = logging.getLogger('webelem')
prompt = logging.getLogger('prompt')
network = logging.getLogger('network')
sql = logging.getLogger('sql')
greasemonkey = logging.getLogger('greasemonkey')
extensions = logging.getLogger('extensions')

LOGGER_NAMES = [
    'statusbar', 'completion', 'init', 'url',
    'destroy', 'modes', 'webview', 'misc',
    'mouse', 'procs', 'hints', 'keyboard',
    'commands', 'signals', 'downloads',
    'js', 'qt', 'ipc', 'shlexer',
    'save', 'message', 'config', 'sessions',
    'webelem', 'prompt', 'network', 'sql',
    'greasemonkey', 'extensions',
]


ram_handler: Optional['RAMHandler'] = None
console_handler: Optional[logging.Handler] = None
console_filter = None


def stub(suffix: str = '') -> None:
    """Show a STUB: message for the calling function."""
    try:
        function = inspect.stack()[1][3]
    except IndexError:  # pragma: no cover
        misc.exception("Failed to get stack")
        function = '<unknown>'
    text = "STUB: {}".format(function)
    if suffix:
        text = '{} ({})'.format(text, suffix)
    misc.warning(text)


def init_log(args: argparse.Namespace) -> None:
    """Init loggers based on the argparse namespace passed."""
    level = (args.loglevel or "info").upper()
    try:
        numeric_level = getattr(logging, level)
    except AttributeError:
        raise ValueError("Invalid log level: {}".format(args.loglevel))

    if numeric_level > logging.DEBUG and args.debug:
        numeric_level = logging.DEBUG

    console, ram = _init_handlers(numeric_level, args.color, args.force_color,
                                  args.json_logging, args.loglines)
    root = logging.getLogger()
    global console_filter
    if console is not None:
        console_filter = LogFilter.parse(args.logfilter)
        console.addFilter(console_filter)
        root.addHandler(console)
    if ram is not None:
        root.addHandler(ram)
    else:
        # If we add no handler, we shouldn't process non visible logs at all
        #
        # disable blocks the current level (while setHandler shows the current
        # level), so -1 to avoid blocking handled messages.
        logging.disable(numeric_level - 1)

    global _log_inited, _args
    _args = args
    root.setLevel(logging.NOTSET)
    logging.captureWarnings(True)
    _init_py_warnings()
    qtcore.qInstallMessageHandler(qt_message_handler)
    _log_inited = True


@qtcore.pyqtSlot()
def shutdown_log() -> None:
    qtcore.qInstallMessageHandler(None)


def _init_py_warnings() -> None:
    """Initialize Python warning handling."""
    assert _args is not None
    warnings.simplefilter('error' if 'werror' in _args.debug_flags
                          else 'default')
    warnings.filterwarnings('ignore', module='pdb', category=ResourceWarning)
    # This happens in many qutebrowser dependencies...
    warnings.filterwarnings('ignore', category=DeprecationWarning,
                            message=r"Using or importing the ABCs from "
                            r"'collections' instead of from 'collections.abc' "
                            r"is deprecated.*")


@contextlib.contextmanager
def disable_qt_msghandler() -> Iterator[None]:
    """Contextmanager which temporarily disables the Qt message handler."""
    old_handler = qtcore.qInstallMessageHandler(None)
    try:
        yield
    finally:
        qtcore.qInstallMessageHandler(old_handler)


@contextlib.contextmanager
def py_warning_filter(
    action:
        Literal['default', 'error', 'ignore', 'always', 'module', 'once'] = 'ignore',
    **kwargs: Any,
) -> Iterator[None]:
    """Contextmanager to temporarily disable certain Python warnings."""
    warnings.filterwarnings(action, **kwargs)
    yield
    if _log_inited:
        _init_py_warnings()


def _init_handlers(
        level: int,
        color: bool,
        force_color: bool,
        json_logging: bool,
        ram_capacity: int
) -> Tuple["logging.StreamHandler[TextIO]", Optional['RAMHandler']]:
    """Init log handlers.

    Args:
        level: The numeric logging level.
        color: Whether to use color if available.
        force_color: Force colored output.
        json_logging: Output log lines in JSON (this disables all colors).
    """
    global ram_handler
    global console_handler
    console_fmt, ram_fmt, html_fmt, use_colorama = _init_formatters(
        level, color, force_color, json_logging)

    if sys.stderr is None:
        console_handler = None  # type: ignore[unreachable]
    else:
        strip = False if force_color else None
        if use_colorama:
            stream = cast(TextIO, colorama.AnsiToWin32(sys.stderr, strip=strip))
        else:
            stream = sys.stderr
        console_handler = logging.StreamHandler(stream)
        console_handler.setLevel(level)
        console_handler.setFormatter(console_fmt)

    if ram_capacity == 0:
        ram_handler = None
    else:
        ram_handler = RAMHandler(capacity=ram_capacity)
        ram_handler.setLevel(logging.DEBUG)
        ram_handler.setFormatter(ram_fmt)
        ram_handler.html_formatter = html_fmt

    return console_handler, ram_handler


def get_console_format(level: int) -> str:
    """Get the log format the console logger should use.

    Args:
        level: The numeric logging level.

    Return:
        Format of the requested level.
    """
    return EXTENDED_FMT if level <= logging.DEBUG else SIMPLE_FMT


def _init_formatters(
        level: int,
        color: bool,
        force_color: bool,
        json_logging: bool
) -> Tuple[Union['JSONFormatter', 'ColoredFormatter'],
           'ColoredFormatter', 'HTMLFormatter', bool]:
    """Init log formatters.

    Args:
        level: The numeric logging level.
        color: Whether to use color if available.
        force_color: Force colored output.
        json_logging: Format lines as JSON (disables all color).

    Return:
        A (console_formatter, ram_formatter, use_colorama) tuple.
        console_formatter/ram_formatter: logging.Formatter instances.
        use_colorama: Whether to use colorama.
    """
    console_fmt = get_console_format(level)
    ram_formatter = ColoredFormatter(EXTENDED_FMT, DATEFMT, '{',
                                     use_colors=False)
    html_formatter = HTMLFormatter(EXTENDED_FMT_HTML, DATEFMT,
                                   log_colors=LOG_COLORS)

    use_colorama = False

    if sys.stderr is None:
        console_formatter = None  # type: ignore[unreachable]
        return console_formatter, ram_formatter, html_formatter, use_colorama

    if json_logging:
        json_formatter = JSONFormatter()
        return json_formatter, ram_formatter, html_formatter, use_colorama

    color_supported = os.name == 'posix' or colorama

    if color_supported and (sys.stderr.isatty() or force_color) and color:
        use_colors = True
        if colorama and os.name != 'posix':
            use_colorama = True
    else:
        use_colors = False

    console_formatter = ColoredFormatter(console_fmt, DATEFMT, '{',
                                         use_colors=use_colors)
    return console_formatter, ram_formatter, html_formatter, use_colorama


def change_console_formatter(level: int) -> None:
    """Change console formatter based on level.

    Args:
        level: The numeric logging level
    """
    assert console_handler is not None
    old_formatter = console_handler.formatter

    if isinstance(old_formatter, ColoredFormatter):
        console_fmt = get_console_format(level)
        console_formatter = ColoredFormatter(
            console_fmt, DATEFMT, '{', use_colors=old_formatter.use_colors)
        console_handler.setFormatter(console_formatter)
    else:
        # Same format for all levels
        assert isinstance(old_formatter, JSONFormatter), old_formatter


def qt_message_handler(msg_type: qtcore.QtMsgType,
                       context: qtcore.QMessageLogContext,
                       msg: str) -> None:
    """Qt message handler to redirect qWarning etc. to the logging system.

    Args:
        msg_type: The level of the message.
        context: The source code location of the message.
        msg: The message text.
    """
    # Mapping from Qt logging levels to the matching logging module levels.
    # Note we map critical to ERROR as it's actually "just" an error, and fatal
    # to critical.
    qt_to_logging = {
        qtcore.QtMsgType.QtDebugMsg: logging.DEBUG,
        qtcore.QtMsgType.QtWarningMsg: logging.WARNING,
        qtcore.QtMsgType.QtCriticalMsg: logging.ERROR,
        qtcore.QtMsgType.QtFatalMsg: logging.CRITICAL,
        qtcore.QtMsgType.QtInfoMsg: logging.INFO,
    }

    # Change levels of some well-known messages to debug so they don't get
    # shown to the user.
    #
    # If a message starts with any text in suppressed_msgs, it's not logged as
    # error.
    suppressed_msgs = [
        # PNGs in Qt with broken color profile
        # https://bugreports.qt.io/browse/QTBUG-39788
        ('libpng warning: iCCP: Not recognizing known sRGB profile that has '
         'been edited'),
        'libpng warning: iCCP: known incorrect sRGB profile',
        # Hopefully harmless warning
        'OpenType support missing for script ',
        # Error if a QNetworkReply gets two different errors set. Harmless Qt
        # bug on some pages.
        # https://bugreports.qt.io/browse/QTBUG-30298
        ('QNetworkReplyImplPrivate::error: Internal problem, this method must '
         'only be called once.'),
        # Sometimes indicates missing text, but most of the time harmless
        'load glyph failed ',
        # Harmless, see https://bugreports.qt.io/browse/QTBUG-42479
        ('content-type missing in HTTP POST, defaulting to '
         'application/x-www-form-urlencoded. '
         'Use QNetworkRequest::setHeader() to fix this problem.'),
        # https://bugreports.qt.io/browse/QTBUG-43118
        'Using blocking call!',
        # Hopefully harmless
        ('"Method "GetAll" with signature "s" on interface '
         '"org.freedesktop.DBus.Properties" doesn\'t exist'),
        ('"Method \\"GetAll\\" with signature \\"s\\" on interface '
         '\\"org.freedesktop.DBus.Properties\\" doesn\'t exist\\n"'),
        'WOFF support requires QtWebKit to be built with zlib support.',
        # Weird Enlightment/GTK X extensions
        'QXcbWindow: Unhandled client message: "_E_',
        'QXcbWindow: Unhandled client message: "_ECORE_',
        'QXcbWindow: Unhandled client message: "_GTK_',
        # Happens on AppVeyor CI
        'SetProcessDpiAwareness failed:',
        # https://bugreports.qt.io/browse/QTBUG-49174
        ('QObject::connect: Cannot connect (null)::stateChanged('
         'QNetworkSession::State) to '
         'QNetworkReplyHttpImpl::_q_networkSessionStateChanged('
         'QNetworkSession::State)'),
        # https://bugreports.qt.io/browse/QTBUG-53989
        ("Image of format '' blocked because it is not considered safe. If "
         "you are sure it is safe to do so, you can white-list the format by "
         "setting the environment variable QTWEBKIT_IMAGEFORMAT_WHITELIST="),
        # Installing Qt from the installer may cause it looking for SSL3 or
        # OpenSSL 1.0 which may not be available on the system
        "QSslSocket: cannot resolve ",
        "QSslSocket: cannot call unresolved function ",
        # When enabling debugging with QtWebEngine
        ("Remote debugging server started successfully. Try pointing a "
         "Chromium-based browser to "),
        # https://github.com/qutebrowser/qutebrowser/issues/1287
        "QXcbClipboard: SelectionRequest too old",
        # https://github.com/qutebrowser/qutebrowser/issues/2071
        'QXcbWindow: Unhandled client message: ""',
        # https://codereview.qt-project.org/176831
        "QObject::disconnect: Unexpected null parameter",
        # https://bugreports.qt.io/browse/QTBUG-76391
        "Attribute Qt::AA_ShareOpenGLContexts must be set before "
        "QCoreApplication is created.",
        # Qt 6.4 beta 1: https://bugreports.qt.io/browse/QTBUG-104741
        "GL format 0 is not supported",
    ]
    # not using utils.is_mac here, because we can't be sure we can successfully
    # import the utils module here.
    if sys.platform == 'darwin':
        suppressed_msgs += [
            # https://bugreports.qt.io/browse/QTBUG-47154
            ('virtual void QSslSocketBackendPrivate::transmit() SSLRead '
             'failed with: -9805'),
        ]

    if not msg:
        msg = "Logged empty message!"

    if any(msg.strip().startswith(pattern) for pattern in suppressed_msgs):
        level = logging.DEBUG
    elif context.category == "qt.webenginecontext" and (
        msg.strip().startswith("GL Type: ") or  # Qt 6.3
        msg.strip().startswith("GLImplementation:")  # Qt 6.2
    ):
        level = logging.DEBUG
    else:
        level = qt_to_logging[msg_type]

    if context.line is None:
        lineno = -1  # type: ignore[unreachable]
    else:
        lineno = context.line

    if context.function is None:
        func = 'none'  # type: ignore[unreachable]
    elif ':' in context.function:
        func = '"{}"'.format(context.function)
    else:
        func = context.function

    if context.category is None or context.category == 'default':
        name = 'qt'
    else:
        name = 'qt-' + context.category
    if msg.splitlines()[0] == ('This application failed to start because it '
                               'could not find or load the Qt platform plugin '
                               '"xcb".'):
        # Handle this message specially.
        msg += ("\n\nOn Archlinux, this should fix the problem:\n"
                "    pacman -S libxkbcommon-x11")
        faulthandler.disable()

    assert _args is not None
    if _args.debug:
        stack: Optional[str] = ''.join(traceback.format_stack())
    else:
        stack = None

    record = qt.makeRecord(name=name, level=level, fn=context.file, lno=lineno,
                           msg=msg, args=(), exc_info=None, func=func,
                           sinfo=stack)
    qt.handle(record)


@contextlib.contextmanager
def hide_qt_warning(pattern: str, logger: str = 'qt') -> Iterator[None]:
    """Hide Qt warnings matching the given regex."""
    log_filter = QtWarningFilter(pattern)
    logger_obj = logging.getLogger(logger)
    logger_obj.addFilter(log_filter)
    try:
        yield
    finally:
        logger_obj.removeFilter(log_filter)


def init_from_config(conf: 'configmodule.ConfigContainer') -> None:
    """Initialize logging settings from the config.

    init_log is called before the config module is initialized, so config-based
    initialization cannot be performed there.

    Args:
        conf: The global ConfigContainer.
              This is passed rather than accessed via the module to avoid a
              cyclic import.
    """
    assert _args is not None
    if _args.debug:
        init.debug("--debug flag overrides log configs")
        return
    if ram_handler:
        ramlevel = conf.logging.level.ram
        init.debug("Configuring RAM loglevel to %s", ramlevel)
        ram_handler.setLevel(LOG_LEVELS[ramlevel.upper()])
    if console_handler:
        consolelevel = conf.logging.level.console
        if _args.loglevel:
            init.debug("--loglevel flag overrides logging.level.console")
        else:
            init.debug("Configuring console loglevel to %s", consolelevel)
            level = LOG_LEVELS[consolelevel.upper()]
            console_handler.setLevel(level)
            change_console_formatter(level)


class QtWarningFilter(logging.Filter):

    """Filter to filter Qt warnings.

    Attributes:
        _pattern: The start of the message.
    """

    def __init__(self, pattern: str) -> None:
        super().__init__()
        self._pattern = pattern

    def filter(self, record: logging.LogRecord) -> bool:
        """Determine if the specified record is to be logged."""
        do_log = not record.msg.strip().startswith(self._pattern)
        return do_log


class InvalidLogFilterError(Exception):

    """Raised when an invalid filter string is passed to LogFilter.parse()."""

    def __init__(self, names: Set[str]):
        invalid = names - set(LOGGER_NAMES)
        super().__init__("Invalid log category {} - valid categories: {}"
                         .format(', '.join(sorted(invalid)),
                                 ', '.join(LOGGER_NAMES)))


class LogFilter(logging.Filter):

    """Filter to filter log records based on the commandline argument.

    The default Filter only supports one name to show - we support a
    comma-separated list instead.

    Attributes:
        names: A set of logging names to allow.
        negated: Whether names is a set of names to log or to suppress.
        only_debug: Only filter debug logs, always show anything more important
                    than debug.
    """

    def __init__(self, names: Set[str], *, negated: bool = False,
                 only_debug: bool = True) -> None:
        super().__init__()
        self.names = names
        self.negated = negated
        self.only_debug = only_debug

    @classmethod
    def parse(cls, filter_str: Optional[str], *,
              only_debug: bool = True) -> 'LogFilter':
        """Parse a log filter from a string."""
        if filter_str is None or filter_str == 'none':
            names = set()
            negated = False
        else:
            filter_str = filter_str.lower()

            if filter_str.startswith('!'):
                negated = True
                filter_str = filter_str[1:]
            else:
                negated = False

            names = {e.strip() for e in filter_str.split(',')}

        if not names.issubset(LOGGER_NAMES):
            raise InvalidLogFilterError(names)

        return cls(names=names, negated=negated, only_debug=only_debug)

    def update_from(self, other: 'LogFilter') -> None:
        """Update this filter's properties from another filter."""
        self.names = other.names
        self.negated = other.negated
        self.only_debug = other.only_debug

    def filter(self, record: logging.LogRecord) -> bool:
        """Determine if the specified record is to be logged."""
        if not self.names:
            # No filter
            return True
        elif record.levelno > logging.DEBUG and self.only_debug:
            # More important than DEBUG, so we won't filter at all
            return True
        elif record.name.split('.')[0] in self.names:
            return not self.negated
        return self.negated


class RAMHandler(logging.Handler):

    """Logging handler which keeps the messages in a deque in RAM.

    Loosely based on logging.BufferingHandler which is unsuitable because it
    uses a simple list rather than a deque.

    Attributes:
        _data: A deque containing the logging records.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.html_formatter: Optional[HTMLFormatter] = None
        if capacity != -1:
            self._data: MutableSequence[logging.LogRecord] = collections.deque(
                maxlen=capacity
            )
        else:
            self._data = collections.deque()

    def emit(self, record: logging.LogRecord) -> None:
        self._data.append(record)

    def dump_log(self, html: bool = False, level: str = 'vdebug',
                 logfilter: LogFilter = None) -> str:
        """Dump the complete formatted log data as string.

        FIXME: We should do all the HTML formatting via jinja2.
        (probably obsolete when moving to a widget for logging,
        https://github.com/qutebrowser/qutebrowser/issues/34

        Args:
            html: Produce HTML rather than plaintext output.
            level: The minimal loglevel to show.
            logfilter: A LogFilter instance used to filter log lines.
        """
        minlevel = LOG_LEVELS.get(level.upper(), VDEBUG_LEVEL)

        if logfilter is None:
            logfilter = LogFilter(set())

        if html:
            assert self.html_formatter is not None
            fmt = self.html_formatter.format
        else:
            fmt = self.format

        self.acquire()
        try:
            lines = [fmt(record)
                     for record in self._data
                     if record.levelno >= minlevel and
                     logfilter.filter(record)]
        finally:
            self.release()
        return '\n'.join(lines)

    def change_log_capacity(self, capacity: int) -> None:
        self._data = collections.deque(self._data, maxlen=capacity)


class ColoredFormatter(logging.Formatter):

    """Logging formatter to output colored logs.

    Attributes:
        use_colors: Whether to do colored logging or not.
    """

    def __init__(self, fmt: str,
                 datefmt: str,
                 style: Literal["%", "{", "$"],
                 *,
                 use_colors: bool) -> None:
        super().__init__(fmt, datefmt, style)
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        if self.use_colors:
            color_dict = dict(COLOR_ESCAPES)
            color_dict['reset'] = RESET_ESCAPE
            log_color = LOG_COLORS[record.levelname]
            color_dict['log_color'] = COLOR_ESCAPES[log_color]
        else:
            color_dict = {color: '' for color in COLOR_ESCAPES}
            color_dict['reset'] = ''
            color_dict['log_color'] = ''
        record.__dict__.update(color_dict)
        return super().format(record)


class HTMLFormatter(logging.Formatter):

    """Formatter for HTML-colored log messages.

    Attributes:
        _log_colors: The colors to use for logging levels.
        _colordict: The colordict passed to the logger.
    """

    def __init__(self, fmt: str, datefmt: str, log_colors: Mapping[str, str]) -> None:
        """Constructor.

        Args:
            fmt: The format string to use.
            datefmt: The date format to use.
            log_colors: The colors to use for logging levels.
        """
        super().__init__(fmt, datefmt)
        self._log_colors: Mapping[str, str] = log_colors
        self._colordict: Mapping[str, str] = {}
        # We could solve this nicer by using CSS, but for this simple case this
        # works.
        for color in COLORS:
            self._colordict[color] = '<font color="{}">'.format(color)
        self._colordict['reset'] = '</font>'

    def format(self, record: logging.LogRecord) -> str:
        record_clone = copy.copy(record)
        record_clone.__dict__.update(self._colordict)
        if record_clone.levelname in self._log_colors:
            color = self._log_colors[record_clone.levelname]
            color_str = self._colordict[color]
            record_clone.log_color = color_str
        else:
            record_clone.log_color = ''
        for field in ['msg', 'filename', 'funcName', 'levelname', 'module',
                      'name', 'pathname', 'processName', 'threadName']:
            data = str(getattr(record_clone, field))
            setattr(record_clone, field, pyhtml.escape(data))
        msg = super().format(record_clone)
        if not msg.endswith(self._colordict['reset']):
            msg += self._colordict['reset']
        return msg

    def formatTime(self, record: logging.LogRecord,
                   datefmt: str = None) -> str:
        out = super().formatTime(record, datefmt)
        return pyhtml.escape(out)


class JSONFormatter(logging.Formatter):

    """Formatter for JSON-encoded log messages."""

    def format(self, record: logging.LogRecord) -> str:
        obj = {}
        for field in ['created', 'msecs', 'levelname', 'name', 'module',
                      'funcName', 'lineno', 'levelno']:
            obj[field] = getattr(record, field)
        obj['message'] = record.getMessage()
        if record.exc_info is not None:
            obj['traceback'] = super().formatException(record.exc_info)
        return json.dumps(obj)
