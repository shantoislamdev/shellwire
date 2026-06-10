"""Click CLI for shellwire.

Provides the ``shellwire`` command with subcommands for starting, stopping,
and managing the daemon, tokens, and clients.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import signal
import sys
from typing import Optional

# Differentiated exit codes for restart logic.  termux-services, wrapper
# scripts, or systemd units can interpret this to auto-restart the daemon
# on planned restarts (e.g. config reload) but NOT on crashes.
# Adapted from Hermes gateway/restart.py (EX_TEMPFAIL = 75).
EXIT_RESTART = 75

import click

from shellwire import __version__
from shellwire.auth import (
    ensure_token,
    is_running,
    read_pid,
    read_token,
    remove_pid,
    rotate_token,
    write_pid,
)
from shellwire.client_manager import ClientManager
from shellwire.config import DaemonConfig
from shellwire.server import ShellwireServer

logger = logging.getLogger("shellwire")


def _setup_logging(level: str, log_file: Optional[str] = None) -> None:
    """Configure structured logging for the daemon."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("shellwire")
    root.setLevel(numeric_level)

    # Always log to stderr.
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Optionally log to file.
    if log_file:
        try:
            expanded = os.path.expanduser(log_file)
            os.makedirs(os.path.dirname(expanded), exist_ok=True)
            fh = logging.FileHandler(expanded, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:
            root.warning("Could not open log file %s: %s", log_file, exc)


def _print_banner(host: str, port: int, token: str, first_start: bool) -> None:
    """Print the startup banner."""
    click.echo()
    click.secho("  ╔══════════════════════════════════════╗", fg="cyan")
    click.secho("  ║           shellwire v{:<15s} ║".format(__version__), fg="cyan")
    click.secho("  ╚══════════════════════════════════════╝", fg="cyan")
    click.echo()
    click.echo(f"  Listening on ws://{host}:{port}")
    click.echo(f"  Health check  http://{host}:{port}/health")
    click.echo()

    if first_start:
        click.secho("  Auth token (save this!):", fg="yellow")
        click.echo(f"     {token}")
        click.echo()
        click.secho("  This token persists across restarts.", fg="green")
        click.secho("  Use 'shellwire token rotate' to generate a new one.", fg="green")
    else:
        click.echo("  Using existing auth token.")
        click.echo("     Run 'shellwire token show' to view it.")

    click.echo()
    click.echo("  Press Ctrl+C to stop.")
    click.echo()


# ======================================================================
# Crash-resistant stdio (adapted from Hermes tui_gateway/transport.py)
# ======================================================================

# Errno values that mean "the peer is gone" rather than a real I/O bug.
# On Termux/Android, the terminal app can be killed at any time by the
# OS, leaving stdout/stderr as broken pipes.
_PEER_GONE_ERRNOS = frozenset({
    errno.EPIPE,
    errno.ECONNRESET,
    errno.EBADF,
    getattr(errno, "ESHUTDOWN", -1),
} - {-1})


class _SafeWriter:
    """Wrapper around a stream that catches broken-pipe errors on write.

    Adapted from Hermes ``tui_gateway/transport.py`` ``_SafeWriter``.
    On Termux/Android, the terminal emulator can be killed at any time
    by the OS (Doze mode, phantom process killer, user swipe-away),
    leaving stdout/stderr as broken pipes.  Without this wrapper, every
    ``logger.info(...)`` after that point raises ``BrokenPipeError`` and
    produces a noisy traceback to a pipe that nobody is reading.

    This wrapper silently swallows peer-gone errors and returns the
    number of characters the caller *tried* to write, so logging and
    print calls continue without exception.
    """

    __slots__ = ("_stream",)

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, data: str) -> int:
        try:
            return self._stream.write(data)
        except BrokenPipeError:
            return len(data)
        except OSError as exc:
            if exc.errno in _PEER_GONE_ERRNOS:
                return len(data)
            raise
        except ValueError:
            # "I/O operation on closed file"
            return len(data)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except (BrokenPipeError, ValueError):
            pass
        except OSError as exc:
            if exc.errno not in _PEER_GONE_ERRNOS:
                raise

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _install_safe_writers() -> None:
    """Replace sys.stdout/stderr with crash-resistant wrappers.

    Safe to call multiple times; subsequent calls are no-ops if the
    streams are already wrapped.
    """
    if not isinstance(sys.stdout, _SafeWriter) and sys.stdout is not None:
        sys.stdout = _SafeWriter(sys.stdout)  # type: ignore[assignment]
    if not isinstance(sys.stderr, _SafeWriter) and sys.stderr is not None:
        sys.stderr = _SafeWriter(sys.stderr)  # type: ignore[assignment]



# ======================================================================
# Main group
# ======================================================================


@click.group()
@click.version_option(version=__version__, prog_name="shellwire")
def main() -> None:
    """shellwire: WebSocket daemon for remote shell access."""
    pass


# ======================================================================
# start
# ======================================================================


@main.command()
@click.option(
    "--port",
    default=7842,
    show_default=True,
    help="Port to listen on.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Host to bind to.",
)
@click.option(
    "--daemon",
    is_flag=True,
    default=False,
    help="Fork to background as a daemon.",
)
@click.option(
    "--log-level",
    default="INFO",
    show_default=True,
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False
    ),
    help="Logging verbosity.",
)
def start(port: int, host: str, daemon: bool, log_level: str) -> None:
    """Start the shellwire daemon."""
    if is_running():
        pid = read_pid()
        click.secho(f"Daemon already running (PID {pid}).", fg="yellow")
        click.echo("Use 'shellwire stop' first, or 'shellwire status' to check.")
        sys.exit(1)

    config = DaemonConfig(host=host, port=port, log_level=log_level)
    config.ensure_dirs()

    # Ensure stable token.
    existing = read_token()
    first_start = existing is None
    token = ensure_token()

    if daemon:
        _daemonize(config, token, first_start)
    else:
        _run_foreground(config, token, first_start)


# ======================================================================
# run (foreground alias)
# ======================================================================


@main.command()
@click.option("--port", default=7842, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option(
    "--log-level",
    default="INFO",
    show_default=True,
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False
    ),
)
def run(port: int, host: str, log_level: str) -> None:
    """Run the daemon in the foreground (alias for start without --daemon)."""
    if is_running():
        pid = read_pid()
        click.secho(f"Daemon already running (PID {pid}).", fg="yellow")
        sys.exit(1)

    config = DaemonConfig(host=host, port=port, log_level=log_level)
    config.ensure_dirs()

    existing = read_token()
    first_start = existing is None
    token = ensure_token()

    _run_foreground(config, token, first_start)


# ======================================================================
# stop
# ======================================================================


@main.command()
def stop() -> None:
    """Stop the running daemon."""
    if not is_running():
        click.secho("Daemon is not running.", fg="yellow")
        sys.exit(1)

    pid = read_pid()
    if pid is None:
        click.secho("No PID file found.", fg="red")
        sys.exit(1)

    click.echo(f"Stopping daemon (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        click.secho("Daemon stopped.", fg="green")
    except ProcessLookupError:
        click.secho("Process not found – cleaning up PID file.", fg="yellow")
    except PermissionError:
        click.secho("Permission denied. Try running as the same user.", fg="red")
        sys.exit(1)

    remove_pid()


# ======================================================================
# status
# ======================================================================


@main.command()
def status() -> None:
    """Check if the daemon is running."""
    if is_running():
        pid = read_pid()
        click.secho(f"Daemon is running (PID {pid}).", fg="green")
    else:
        click.secho("Daemon is not running.", fg="yellow")
        # Clean up stale PID file if present.
        if read_pid() is not None:
            remove_pid()


# ======================================================================
# version
# ======================================================================


@main.command("version")
def version_cmd() -> None:
    """Print the version and exit."""
    click.echo(f"shellwire {__version__}")


# ======================================================================
# token group
# ======================================================================


@main.group(invoke_without_command=True)
@click.pass_context
def token(ctx: click.Context) -> None:
    """Manage the auth token.

    Without a subcommand, shows the current token.
    """
    if ctx.invoked_subcommand is None:
        _show_token()


@token.command("show")
def token_show() -> None:
    """Display the current auth token."""
    _show_token()


@token.command("rotate")
def token_rotate() -> None:
    """Generate a new auth token (invalidates the old one).

    If the daemon is running, you must restart it for the new token
    to take effect.
    """
    new_token = rotate_token()
    click.secho("Token rotated successfully.", fg="green")
    click.echo()
    click.echo(f"  New token: {new_token}")
    click.echo()

    if is_running():
        click.secho(
            "  Warning: Daemon is running. Restart it for the new token to take effect.",
            fg="yellow",
        )


def _show_token() -> None:
    """Display the stored token."""
    tok = read_token()
    if tok is None:
        click.secho("No token found. Run 'shellwire start' to generate one.", fg="yellow")
        sys.exit(1)
    click.echo(tok)


# ======================================================================
# clients group
# ======================================================================


@main.group()
def clients() -> None:
    """Manage connected clients."""
    pass


@clients.command("list")
def clients_list() -> None:
    """List all known clients."""
    mgr = ClientManager()
    client_list = mgr.list_clients()

    if not client_list:
        click.echo("No clients recorded.")
        return

    click.echo(f"{'CLIENT ID':<36}  {'STATUS':<12}  {'LAST SEEN'}")
    click.echo("─" * 72)
    for c in client_list:
        status = "REVOKED" if c["is_revoked"] else (
            "CONNECTED" if c["is_connected"] else "DISCONNECTED"
        )
        import datetime
        last_seen = datetime.datetime.fromtimestamp(
            c["last_seen"]
        ).strftime("%Y-%m-%d %H:%M:%S")
        click.echo(f"{c['client_id']:<36}  {status:<12}  {last_seen}")


@clients.command("revoke")
@click.argument("client_id")
def clients_revoke(client_id: str) -> None:
    """Revoke a client by its ID.

    Revoked clients cannot reconnect until un-revoked (by editing
    ~/.shellwire/clients.json).
    """
    mgr = ClientManager()

    # We need to run the async revoke method.
    async def _do_revoke() -> None:
        await mgr.revoke(client_id)

    asyncio.get_event_loop().run_until_complete(_do_revoke())
    click.secho(f"Client '{client_id}' revoked.", fg="green")

    if is_running():
        click.secho(
            "  Warning: If the client is currently connected, "
            "it will be disconnected when the daemon processes the next request.",
            fg="yellow",
        )


# ======================================================================
# Internal helpers
# ======================================================================


def _run_foreground(
    config: DaemonConfig, token: str, first_start: bool
) -> None:
    """Run the server in the foreground."""
    _setup_logging(config.log_level, config.log_file)
    _install_safe_writers()
    _print_banner(config.host, config.port, token, first_start)

    write_pid(os.getpid())

    async def _start_server() -> None:
        server = ShellwireServer(config)
        await server.serve()

    try:
        asyncio.run(_start_server())
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
    finally:
        remove_pid()

    click.secho("Daemon stopped.", fg="green")


def _daemonize(
    config: DaemonConfig, token: str, first_start: bool
) -> None:
    """Fork to background (Unix/Linux only)."""
    try:
        pid = os.fork()
    except AttributeError:
        click.secho(
            "Daemon mode not supported on this platform. "
            "Use 'shellwire start' without --daemon.",
            fg="red",
        )
        sys.exit(1)

    if pid > 0:
        # Parent – print banner and exit.
        _print_banner(config.host, config.port, token, first_start)
        click.echo(f"  Daemon started (PID {pid}).")
        sys.exit(0)

    # Child – become session leader.
    os.setsid()

    # Second fork to fully detach.
    try:
        pid2 = os.fork()
    except OSError:
        sys.exit(1)

    if pid2 > 0:
        sys.exit(0)

    # Redirect stdio.
    sys.stdin.close()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)

    _setup_logging(config.log_level, config.log_file)
    _install_safe_writers()
    write_pid(os.getpid())

    async def _start_server() -> None:
        server = ShellwireServer(config)
        await server.serve()

    try:
        asyncio.run(_start_server())
    finally:
        remove_pid()


if __name__ == "__main__":
    main()
