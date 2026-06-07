# kotha-shell

**WebSocket daemon for Termux shell access from KothaCode.**

`kotha-shell` runs in [Termux](https://termux.dev/) on Android, providing a WebSocket bridge that lets the [KothaCode](https://github.com/Amikotha/kotha-shell) Android app execute shell commands with full Linux access.

## Features

- 🔐 **Stable token auth** — token generated once, persists across restarts
- 🔌 **Single client** — one active connection at a time, with reconnection support
- ⚡ **Concurrent execution** — up to 4 simultaneous commands
- 📺 **Interactive sessions** — long-running processes with stdin/stdout streaming
- 🏥 **Health endpoint** — HTTP GET `/health` on the same port
- 🧹 **Clean process kill** — process group kill ensures no orphaned children

## Installation

```bash
# In Termux
pip install kotha-shell
```

Or install from source:

```bash
git clone https://github.com/Amikotha/kotha-shell.git
cd kotha-shell
pip install -e ".[dev]"
```

### Auto-venv Runners

If you clone the repository directly, you can use the included runner scripts which will automatically create a virtual environment (`venv`) and install dependencies for you. This allows seamless execution without manually setting up your environment:

- **Unix / Termux**:
  - `./run.sh` — Bootstraps the daemon inside `venv`
  - `./test.sh` — Runs the test suite inside `venv`
- **Windows**:
  - `run.bat` — Bootstraps the daemon inside `venv`
  - `test.bat` — Runs the test suite inside `venv`

## Quick Start

```bash
# Start the daemon (foreground)
kotha start

# Start as background daemon
kotha start --daemon

# Check status
kotha status

# View your auth token
kotha token show

# Stop the daemon
kotha stop
```

On first start, a stable auth token is generated and displayed. **Save it** — you'll need it in the KothaCode app.

## Commands Reference

| Command | Description |
|---|---|
| `kotha start` | Start the daemon |
| `kotha start --daemon` | Start in background |
| `kotha start --port 8080` | Use a custom port |
| `kotha run` | Run in foreground (alias) |
| `kotha stop` | Stop the running daemon |
| `kotha status` | Check if daemon is running |
| `kotha version` | Print version |
| `kotha token` | Show the auth token |
| `kotha token show` | Show the auth token |
| `kotha token rotate` | Generate a new token |
| `kotha clients list` | List known clients |
| `kotha clients revoke ID` | Revoke a client |

## Connecting from KothaCode

1. Install `kotha-shell` in Termux
2. Run `kotha start`
3. Copy the auth token shown on first start
4. In KothaCode app, go to **Settings → Shell Connection**
5. Set host to `127.0.0.1`, port to `7842`
6. Paste the auth token
7. Tap **Connect**

## Protocol

All communication uses JSON over WebSocket. The client must send an `auth` message first:

```json
{
  "type": "auth",
  "token": "your-auth-token",
  "client_id": "kothacode-android-abc123"
}
```

Then send commands:

```json
{
  "type": "execute",
  "id": "cmd-001",
  "command": "ls -la",
  "timeout": 30
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

## Configuration

All configuration is via CLI flags. Defaults:

| Setting | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `7842` | Listen port |
| `--log-level` | `INFO` | Logging verbosity |

Data files are stored in `~/.kotha-shell/`:
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
pytest --cov=kotha_shell
```

## License

Apache 2.0
