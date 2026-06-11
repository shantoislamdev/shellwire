# Protocol Specification

Shellwire communicates exclusively via a strict JSON-over-WebSocket protocol. All messages exchanged between the client and server must be valid JSON objects containing a `type` field.

## Connection Lifecycle

1.  **Connect**: Client establishes a WebSocket connection to `ws://<host>:<port>`.
2.  **Authenticate**: Client *must* send an `auth` message within 10 seconds.
3.  **Ready**: Server responds with a `status` message upon successful authentication.
4.  **Execute**: Client sends commands/sessions; Server streams outputs and results.

> [!IMPORTANT]
> Shellwire supports only **one** active client connection at a time. If a new client connects, the behavior depends on the client ID and revocation list.

---

## Client → Server Messages

These messages are sent from the client to the Shellwire daemon.

### Authentication (`auth`)
Must be the very first message sent upon connection.
```json
{
  "type": "auth",
  "token": "64_char_hex_token...",
  "client_id": "unique-client-identifier"
}
```

### Execute One-Shot Command (`execute`)
Executes a shell command and returns the final exit code.
```json
{
  "type": "execute",
  "id": "cmd-1234",
  "command": "ls -la",
  "timeout": 120,               // Optional (seconds, default 120)
  "cwd": "/var/log",            // Optional
  "env": {"DEBUG": "1"},        // Optional
  "stdin_data": "input\n"       // Optional
}
```

### Cancel Command (`cancel_command`)
Cancels a pending or currently running one-shot command.
```json
{
  "type": "cancel_command",
  "id": "cmd-1234"
}
```

### Start Interactive Session (`start_session`)
Spawns a long-running interactive shell/process.
```json
{
  "type": "start_session",
  "id": "sess-999",
  "command": "bash",
  "use_pty": true,              // Optional (default false)
  "cols": 120,                  // Optional (PTY mode only)
  "rows": 40                    // Optional (PTY mode only)
}
```

### Send Session Input (`send_input`)
Pipes data to the `stdin` of a running session.
```json
{
  "type": "send_input",
  "id": "sess-999",
  "data": "echo hello\n",
  "close_stdin": false          // Optional (default false)
}
```

### Resize PTY (`resize`)
Resizes the terminal window for a running PTY session.
```json
{
  "type": "resize",
  "id": "sess-999",
  "cols": 150,
  "rows": 50
}
```

### Kill Session (`kill_session`)
Forcefully terminates a running session via process group signals (`SIGTERM` -> `SIGKILL`).
```json
{
  "type": "kill_session",
  "id": "sess-999"
}
```

### List Active Sessions (`list_sessions`)
Requests metadata about all currently active interactive sessions.
```json
{
  "type": "list_sessions"
}
```

### Ping (`ping`)
Keep-alive ping.
```json
{
  "type": "ping"
}
```

---

## Server → Client Messages

These messages are pushed from the Shellwire daemon to the client.

### Status Response (`status`)
Sent automatically upon successful authentication.
```json
{
  "type": "status",
  "version": "0.1.0",
  "uptime_seconds": 3600.5,
  "active_commands": 0,
  "active_sessions": 1,
  "python_version": "3.11.4",
  "shell": "/bin/bash",
  "client_id": "unique-client-identifier"
}
```

### Output Stream (`output`)
Incremental stdout/stderr chunks from an `execute` or `start_session` process.
```json
{
  "type": "output",
  "id": "cmd-1234",
  "data": "total 42\n...",
  "stream": "stdout"            // "stdout" or "stderr"
}
```

### Command Result (`result`)
Final payload signifying a one-shot command has finished.
```json
{
  "type": "result",
  "id": "cmd-1234",
  "exit_code": 0,
  "duration_ms": 124.5
}
```

### Session Started (`session_started`)
Confirmation that an interactive session successfully spawned.
```json
{
  "type": "session_started",
  "id": "sess-999",
  "pid": 10243
}
```

### Session Ended (`session_ended`)
Notification that a session has died naturally or was killed.
```json
{
  "type": "session_ended",
  "id": "sess-999",
  "exit_code": 0,               // null if killed ungracefully
  "duration_ms": 5000.2
}
```

### Sessions List Response (`sessions_list`)
Response to a `list_sessions` request.
```json
{
  "type": "sessions_list",
  "sessions": [
    {
      "id": "sess-999",
      "pid": 10243,
      "command": "bash",
      "started_at": 1690000000.0,
      "uptime_seconds": 360.5,
      "is_running": true,
      "is_pty": true,
      "recent_output": ["[pty] user@host:~$ "]
    }
  ]
}
```

### Command Queued (`command_queued`)
Sent if a one-shot command exceeds the daemon's concurrent execution limit and is placed in the background queue.
```json
{
  "type": "command_queued",
  "id": "cmd-9999",
  "position": 1
}
```

### Error (`error`)
Signals a validation, execution, or internal error.
```json
{
  "type": "error",
  "id": "cmd-1234",             // null if error is not tied to a specific request
  "message": "Invalid JSON",
  "code": "INVALID_JSON"
}
```

### Daemon Stopping (`daemon_stopping`)
Sent just before the daemon closes the connection for a graceful shutdown.
```json
{
  "type": "daemon_stopping"
}
```

### Pong (`pong`)
Response to a `ping`.
```json
{
  "type": "pong"
}
```

---

## Health API

Shellwire exposes an HTTP health check on the same port as the WebSocket server.

**Request:**
`GET /health HTTP/1.1`

**Response (`200 OK`):**
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
