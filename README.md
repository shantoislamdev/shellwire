# shellwire

**WebSocket daemon for remote shell access from KothaCode.**

`shellwire` runs in a remote environment, providing a WebSocket bridge that lets the [KothaCode](https://github.com/shantoislamdev/shellwire) Android app execute shell commands with full Linux access.

## Features

- 🔐 **Stable token auth** — token generated once, persists across restarts
- 🔌 **Single client** — one active connection at a time, with reconnection support
- ⚡ **Concurrent execution** — up to 4 simultaneous commands
- 📺 **Interactive sessions** — long-running processes with stdin/stdout streaming
- 🏥 **Health endpoint** — HTTP GET `/health` on the same port
- 🧹 **Clean process kill** — process group kill ensures no orphaned children

## Installation

```bash
# In remote environment
pip install shellwire
```

Or install from source:

```bash
git clone https://github.com/shantoislamdev/shellwire.git
cd shellwire
pip install -e ".[dev]"
```

### Auto-venv Runners

If you clone the repository directly, you can use the included runner scripts which will automatically create a virtual environment (`venv`) and install dependencies for you. This allows seamless execution without manually setting up your environment:

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

On first start, a stable auth token is generated and displayed. **Save it** — you'll need it in the KothaCode app.

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

## Connecting from KothaCode

1. Install `shellwire` in remote environment
2. Run `shellwire start`
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
  "client_id": "myapp-android-abc123"
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
