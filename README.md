# shellwire

**WebSocket daemon for remote shell access. Built to empower Android apps and agents with a full desktop-like shell via Termux.**

`shellwire` runs as a WebSocket server, providing a bridge that lets remote clients execute shell commands with full system access. While fully compatible with Linux and macOS, it is uniquely engineered for Android devices. Android applications typically lack proper terminal access, making it difficult to run local AI agents or advanced tools on-device. Shellwire solves this by running inside Termux and exposing a WebSocket server, acting as a bridge to give Android apps a complete, desktop-grade shell environment.

## Features

- **Stable token auth** — token generated once, persists across restarts
- **Single client** — one active connection at a time, with reconnection support
- **Concurrent execution** — up to 4 simultaneous commands
- **Interactive PTY sessions (POSIX)** — full pseudo-terminal support with dynamic resizing
- **Environment Tracking** — persistent working directory (CWD) and exported environment variables across commands
- **Compound Command Rewriting** — automatically rewrites bash chains (e.g. `A && B &` -> `A && { B & }`) for standard backgrounding
- **Termux / Android Optimized** — Built-in resilience against mobile network handoffs, terminal DOZE states, and phantom process killers, ensuring extreme stability for long-running mobile environments.
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

## Documentation

Comprehensive enterprise-grade documentation is available in the `docs/` folder:

*   **[Overview & Architecture](docs/index.md)**: High-level overview and architectural flow.
*   **[Daemon Guide](docs/daemon_guide.md)**: Server administration, CLI commands, configuration flags, and token management.
*   **[Protocol Specification](docs/protocol_spec.md)**: Strict JSON schemas for all WebSocket messages (`auth`, `execute`, `start_session`, etc.).
*   **[Client Integration](docs/client_integration.md)**: Developer guide for building custom WebSocket clients, including complete Kotlin examples.

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
