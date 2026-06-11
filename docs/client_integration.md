# Client Integration Guide

Building a custom client for Shellwire involves establishing a WebSocket connection, adhering to the strict JSON protocol, and implementing network resilience logic. This guide covers the essential workflow using **Kotlin** and standard WebSocket libraries (like OkHttp).

## Integration Workflow

The standard lifecycle for a Shellwire client:
1.  **Connect**: Open a WebSocket connection.
2.  **Authenticate**: Send the `auth` JSON payload immediately.
3.  **Await Ready**: Listen for the `status` response from the server.
4.  **Dispatch**: Send `execute` or `start_session` messages.
5.  **Process Output**: Parse incoming `output` messages. Note that terminal outputs may contain ANSI escape sequences, which you might need to render or strip on the client side.

## Kotlin Implementation Examples

The following examples utilize the `OkHttp` library, which is an industry standard for Android and Kotlin networking.

> [!NOTE]
> While these snippets can be used for any JVM client, they are specifically designed to demonstrate how an **Android developer** can connect their local application (or on-device AI agent) to a Termux-hosted Shellwire daemon via WebSockets to gain full shell access.

### 1. Connection & Authentication

```kotlin
import okhttp3.*
import okio.ByteString
import org.json.JSONObject

class ShellwireClient(private val url: String, private val token: String, private val clientId: String) {
    private val client = OkHttpClient()
    private var webSocket: WebSocket? = null

    fun connect() {
        val request = Request.Builder().url(url).build()
        
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                // Step 1: Send Authentication Payload
                val authPayload = JSONObject().apply {
                    put("type", "auth")
                    put("token", token)
                    put("client_id", clientId)
                }
                webSocket.send(authPayload.toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val json = JSONObject(text)
                when (json.getString("type")) {
                    "status" -> println("Authenticated! Server uptime: ${json.getDouble("uptime_seconds")}")
                    "error" -> println("Error: ${json.getString("message")}")
                    // Handle other message types...
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                println("Disconnected: $reason")
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                println("Network Error: ${t.message}")
            }
        })
    }
}
```

### 2. Dispatching a One-Shot Command

Once authenticated (i.e., after receiving the `status` message), you can dispatch commands.

```kotlin
fun executeCommand(commandId: String, commandStr: String) {
    val payload = JSONObject().apply {
        put("type", "execute")
        put("id", commandId)
        put("command", commandStr)
        put("timeout", 60)
    }
    webSocket?.send(payload.toString())
}
```

To handle the response, update your `onMessage` listener:

```kotlin
// Inside onMessage...
when (json.getString("type")) {
    "output" -> {
        val cmdId = json.getString("id")
        val data = json.getString("data")
        val stream = json.getString("stream") // "stdout" or "stderr"
        print("[$cmdId][$stream]: $data")
    }
    "result" -> {
        val cmdId = json.getString("id")
        val exitCode = json.getInt("exit_code")
        println("Command $cmdId finished with exit code $exitCode")
    }
}
```

### 3. Starting an Interactive Session (PTY)

Interactive sessions allow sending input and dynamically resizing the terminal.

```kotlin
fun startSession(sessionId: String, commandStr: String) {
    val payload = JSONObject().apply {
        put("type", "start_session")
        put("id", sessionId)
        put("command", commandStr)
        put("use_pty", true)
        put("cols", 80)
        put("rows", 24)
    }
    webSocket?.send(payload.toString())
}

fun sendSessionInput(sessionId: String, input: String) {
    val payload = JSONObject().apply {
        put("type", "send_input")
        put("id", sessionId)
        put("data", input)
    }
    webSocket?.send(payload.toString())
}

fun resizeSession(sessionId: String, cols: Int, rows: Int) {
    val payload = JSONObject().apply {
        put("type", "resize")
        put("id", sessionId)
        put("cols", cols)
        put("rows", rows)
    }
    webSocket?.send(payload.toString())
}
```

## Resilience & Stability Guidelines

Building clients for mobile environments (like Android/Termux) requires handling network flakiness.

1.  **Handoffs & Disconnects**: Mobile network handoffs (e.g., WiFi -> LTE) will sever WebSocket connections. Implement an exponential backoff reconnection strategy within the `onFailure` and `onClosed` callbacks of your WebSocket listener.
2.  **Ping/Pong**: The server sends PING frames to ensure the TCP connection is alive. The OkHttp WebSocket implementation handles standard WebSocket PING/PONG automatically. However, Shellwire *also* supports application-level `ping` and `pong` JSON messages if your library requires manual keep-alives.
3.  **ANSI Escape Codes**: When `use_pty` is enabled or specific CLI tools output color, `data` in `output` messages will contain ANSI escape sequences. You must use a terminal emulator view (e.g., Termux terminal view or Xterm.js for web) to render these, or manually strip them using a Regex algorithm on the client side.
