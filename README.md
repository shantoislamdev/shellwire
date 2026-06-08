# shellwire

**WebSocket daemon for remote shell access.**

`shellwire` runs as a WebSocket server, providing a bridge that lets remote clients execute shell commands with full system access. It's designed to be a generic, client-agnostic solution that any WebSocket-compatible application can connect to.

## Features

- **Stable token auth** — token generated once, persists across restarts
- **Single client** — one active connection at a time, with reconnection support
- **Concurrent execution** — up to 4 simultaneous commands
- **Interactive PTY sessions (POSIX)** — full pseudo-terminal support with dynamic resizing
- **Environment Tracking** — persistent working directory (CWD) and exported environment variables across commands
- **Compound Command Rewriting** — automatically rewrites bash chains (e.g. `A && B &` -> `A && { B & }`) for standard backgrounding
- **Robust Process Isolation** — prevents zombie processes via process-group escalation kills (SIGTERM → SIGKILL) and protects against runaway output
- **Health endpoint** — HTTP GET `/health` on the same port

## Installation

```bash
# Via pip
pip install shellwire
```

Or install from source:

```bash
git clone https://github.com/shantoislamdev/shellwire.git
cd shellwire
pip install -e ".[dev]"
```

### Auto-venv Runners

If you clone the repository directly, you can use the included runner scripts which will automatically create a virtual environment (`venv`) and install dependencies for you:

- **Unix / Linux**:
  - `./run.sh` — Bootstraps the daemon inside `venv`
  - `./test.sh` — Runs the test suite inside `venv`
- **Windows**:
  - `run.bat` — Bootstraps the daemon inside `venv`
  - `test.bat` — Runs the test suite inside `venv`

## Quick Start

```bash
# Start the daemon (foreground)
shellwire start

# Start as background daemon
shellwire start --daemon

# Check status
shellwire status

# View your auth token
shellwire token show

# Stop the daemon
shellwire stop
```

On first start, a stable auth token is generated and displayed. **Save it** — you'll need it to connect your client.

## Commands Reference

| Command | Description |
|---|---|
| `shellwire start` | Start the daemon |
| `shellwire start --daemon` | Start in background |
| `shellwire start --port 8080` | Use a custom port |
| `shellwire run` | Run in foreground (alias) |
| `shellwire stop` | Stop the running daemon |
| `shellwire status` | Check if daemon is running |
| `shellwire version` | Print version |
| `shellwire token` | Show the auth token |
| `shellwire token show` | Show the auth token |
| `shellwire token rotate` | Generate a new token |
| `shellwire clients list` | List known clients |
| `shellwire clients revoke ID` | Revoke a client |

## Connecting a Client

To connect a client to shellwire:

1. Install `shellwire` in your target environment
2. Run `shellwire start` to start the daemon
3. Copy the auth token shown on first start
4. In your client application, configure:
   - **Host**: The IP address or hostname where shellwire is running (default: `127.0.0.1`)
   - **Port**: The port shellwire is listening on (default: `7842`)
   - **Token**: The auth token from step 3
5. Connect via WebSocket

The protocol is documented below for client implementers.

## Protocol

All communication uses JSON over WebSocket. The client must send an `auth` message first:

```json
{
  "type": "auth",
  "token": "your-auth-token",
  "client_id": "my-client-abc123"
}
```

Then send commands:

```json
{
  "type": "execute",
  "id": "cmd-001",
  "command": "ls -la",
  "cwd": "/var/log",
  "env": {"FOO": "bar"},
  "stdin_data": "optional input\n",
  "is_pty": true,
  "cols": 120,
  "rows": 40,
  "timeout": 30
}
```

Interactive PTY sessions can be dynamically resized:

```json
{
  "type": "resize",
  "id": "cmd-001",
  "cols": 150,
  "rows": 50
}
```

The server streams output:

```json
{"type": "output", "id": "cmd-001", "data": "total 42\n...", "stream": "stdout"}
{"type": "result", "id": "cmd-001", "exit_code": 0, "duration_ms": 123.4}
```

### Health Check

```bash
curl http://127.0.0.1:7842/health
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "uptime_seconds": 3600.5,
  "active_commands": 0,
  "active_sessions": 0,
  "python_version": "3.11.4"
}
```

## Client-Side Utilities

If you are building a Python client that connects to `shellwire`, you can import and use several robust utilities we export for downstream consumers:

- **`shellwire.output.strip_ansi(text: str) -> str`**: Fast ECMA-48 compliant regex utility for stripping terminal ANSI colors and escape sequences from output.
- **`shellwire.output.truncate_output(text: str, max_chars: int) -> tuple[str, bool]`**: Safely truncates massive output payloads while preserving the first 40% and last 60% of the stream so critical context isn't lost.

## Configuration

All configuration is via CLI flags. Defaults:

| Setting | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `7842` | Listen port |
| `--log-level` | `INFO` | Logging verbosity |

Data files are stored in `~/.shellwire/`:
- `auth.token` — the auth token
- `daemon.pid` — PID file
- `daemon.log` — log file
- `clients.json` — revoked client list

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=shellwire
```

## License

Apache 2.0
