export const authTabs = [
  {
    id: 'kotlin',
    label: 'Kotlin',
    code: `import okhttp3.*
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
                    "status" -> {
                        val uptime = json.getDouble("uptime_seconds")
                        println("Authenticated! Server uptime: $uptime")
                    }
                    "error" -> {
                        val msg = json.getString("message")
                        println("Error: $msg")
                    }
                    // Handle other message types...
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                println("Disconnected: $reason")
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                println("Network Error: \${t.message}")
            }
        })
    }
}`
  },
  {
    id: 'java',
    label: 'Java',
    code: `import okhttp3.*;
import okio.ByteString;
import org.json.JSONObject;

public class ShellwireClient {
    private String url;
    private String token;
    private String clientId;
    private OkHttpClient client = new OkHttpClient();
    private WebSocket webSocket = null;

    public ShellwireClient(String url, String token, String clientId) {
        this.url = url;
        this.token = token;
        this.clientId = clientId;
    }

    public void connect() {
        Request request = new Request.Builder().url(url).build();
        
        webSocket = client.newWebSocket(request, new WebSocketListener() {
            @Override
            public void onOpen(WebSocket webSocket, Response response) {
                try {
                    JSONObject authPayload = new JSONObject();
                    authPayload.put("type", "auth");
                    authPayload.put("token", token);
                    authPayload.put("client_id", clientId);
                    webSocket.send(authPayload.toString());
                } catch (Exception e) {
                    e.printStackTrace();
                }
            }

            @Override
            public void onMessage(WebSocket webSocket, String text) {
                try {
                    JSONObject json = new JSONObject(text);
                    String type = json.getString("type");
                    if ("status".equals(type)) {
                        System.out.println("Authenticated! Server uptime: " + json.getDouble("uptime_seconds"));
                    } else if ("error".equals(type)) {
                        System.out.println("Error: " + json.getString("message"));
                    }
                } catch (Exception e) {
                    e.printStackTrace();
                }
            }

            @Override
            public void onClosed(WebSocket webSocket, int code, String reason) {
                System.out.println("Disconnected: " + reason);
            }

            @Override
            public void onFailure(WebSocket webSocket, Throwable t, Response response) {
                System.out.println("Network Error: " + t.getMessage());
            }
        });
    }
}`
  },
  {
    id: 'flutter',
    label: 'Flutter',
    code: `import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';

class ShellwireClient {
  final String url;
  final String token;
  final String clientId;
  WebSocketChannel? channel;

  ShellwireClient({required this.url, required this.token, required this.clientId});

  void connect() {
    channel = WebSocketChannel.connect(Uri.parse(url));

    // Listen for messages
    channel!.stream.listen(
      (message) {
        final json = jsonDecode(message);
        switch (json['type']) {
          case 'status':
            print('Authenticated! Server uptime: \${json['uptime_seconds']}');
            break;
          case 'error':
            print('Error: \${json['message']}');
            break;
        }
      },
      onDone: () => print('Disconnected'),
      onError: (error) => print('Network Error: $error'),
    );

    // Send Authentication Payload
    final authPayload = {
      'type': 'auth',
      'token': token,
      'client_id': clientId,
    };
    channel!.sink.add(jsonEncode(authPayload));
  }
}`
  },
  {
    id: 'typescript',
    label: 'TypeScript',
    code: `class ShellwireClient {
  private ws: WebSocket | null = null;

  constructor(
    private url: string,
    private token: string,
    private clientId: string
  ) {}

  connect() {
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      // Step 1: Send Authentication Payload
      const authPayload = {
        type: 'auth',
        token: this.token,
        client_id: this.clientId
      };
      this.ws?.send(JSON.stringify(authPayload));
    };

    this.ws.onmessage = (event) => {
      const json = JSON.parse(event.data);
      if (json.type === 'status') {
        console.log(\`Authenticated! Server uptime: \${json.uptime_seconds}\`);
      } else if (json.type === 'error') {
        console.error(\`Error: \${json.message}\`);
      }
    };

    this.ws.onclose = (event) => {
      console.log(\`Disconnected: \${event.reason}\`);
    };

    this.ws.onerror = (error) => {
      console.error('Network Error', error);
    };
  }
}`
  }
];

export const dispatchTabs = [
  {
    id: 'kotlin',
    label: 'Kotlin',
    code: `fun executeCommand(commandId: String, commandStr: String) {
    val payload = JSONObject().apply {
        put("type", "execute")
        put("id", commandId)
        put("command", commandStr)
        put("timeout", 60)
    }
    webSocket?.send(payload.toString())
}`
  },
  {
    id: 'java',
    label: 'Java',
    code: `public void executeCommand(String commandId, String commandStr) {
    try {
        JSONObject payload = new JSONObject();
        payload.put("type", "execute");
        payload.put("id", commandId);
        payload.put("command", commandStr);
        payload.put("timeout", 60);
        
        if (webSocket != null) {
            webSocket.send(payload.toString());
        }
    } catch (Exception e) {
        e.printStackTrace();
    }
}`
  },
  {
    id: 'flutter',
    label: 'Flutter',
    code: `void executeCommand(String commandId, String commandStr) {
  final payload = {
    'type': 'execute',
    'id': commandId,
    'command': commandStr,
    'timeout': 60,
  };
  channel?.sink?.add(jsonEncode(payload));
}`
  },
  {
    id: 'typescript',
    label: 'TypeScript',
    code: `executeCommand(commandId: string, commandStr: string) {
  const payload = {
    type: 'execute',
    id: commandId,
    command: commandStr,
    timeout: 60
  };
  this.ws?.send(JSON.stringify(payload));
}`
  }
];

export const responseTabs = [
  {
    id: 'kotlin',
    label: 'Kotlin',
    code: `// Inside onMessage...
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
    "session_started" -> {
        val pid = json.getInt("pid")
        println("Session started with PID: $pid")
    }
    "session_ended" -> {
        val exitCode = if (json.isNull("exit_code")) null else json.getInt("exit_code")
        println("Session ended with exit code $exitCode")
    }
}`
  },
  {
    id: 'java',
    label: 'Java',
    code: `// Inside onMessage...
if ("output".equals(type)) {
    System.out.printf("[%s][%s]: %s\n", json.getString("id"), json.getString("stream"), json.getString("data"));
} else if ("result".equals(type)) {
    System.out.println("Command finished with exit code: " + json.getInt("exit_code"));
} else if ("session_started".equals(type)) {
    System.out.println("Session started with PID: " + json.getInt("pid"));
} else if ("session_ended".equals(type)) {
    String exitCode = json.isNull("exit_code") ? "null" : String.valueOf(json.getInt("exit_code"));
    System.out.println("Session ended with exit code: " + exitCode);
}`
  },
  {
    id: 'flutter',
    label: 'Flutter',
    code: `// Inside stream listener...
switch (json['type']) {
  case 'output':
    print('[\${json['id']}][\${json['stream']}]: \${json['data']}');
    break;
  case 'result':
    print('Command \${json['id']} finished with exit code \${json['exit_code']}');
    break;
  case 'session_started':
    print('Session started with PID: \${json['pid']}');
    break;
  case 'session_ended':
    print('Session ended with exit code \${json['exit_code']}');
    break;
}`
  },
  {
    id: 'typescript',
    label: 'TypeScript',
    code: `// Inside onmessage...
if (json.type === 'output') {
  console.log(\`[\${json.id}][\${json.stream}]: \${json.data}\`);
} else if (json.type === 'result') {
  console.log(\`Command \${json.id} finished with exit code \${json.exit_code}\`);
} else if (json.type === 'session_started') {
  console.log(\`Session started with PID: \${json.pid}\`);
} else if (json.type === 'session_ended') {
  console.log(\`Session ended with exit code \${json.exit_code}\`);
}`
  }
];

export const ptyTabs = [
  {
    id: 'kotlin',
    label: 'Kotlin',
    code: `fun startSession(sessionId: String, commandStr: String) {
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
}`
  },
  {
    id: 'java',
    label: 'Java',
    code: `public void startSession(String sessionId, String commandStr) {
    try {
        JSONObject payload = new JSONObject();
        payload.put("type", "start_session");
        payload.put("id", sessionId);
        payload.put("command", commandStr);
        payload.put("use_pty", true);
        payload.put("cols", 80);
        payload.put("rows", 24);
        if (webSocket != null) webSocket.send(payload.toString());
    } catch (Exception e) { e.printStackTrace(); }
}

public void sendSessionInput(String sessionId, String input) {
    try {
        JSONObject payload = new JSONObject();
        payload.put("type", "send_input");
        payload.put("id", sessionId);
        payload.put("data", input);
        if (webSocket != null) webSocket.send(payload.toString());
    } catch (Exception e) { e.printStackTrace(); }
}

public void resizeSession(String sessionId, int cols, int rows) {
    try {
        JSONObject payload = new JSONObject();
        payload.put("type", "resize");
        payload.put("id", sessionId);
        payload.put("cols", cols);
        payload.put("rows", rows);
        if (webSocket != null) webSocket.send(payload.toString());
    } catch (Exception e) { e.printStackTrace(); }
}`
  },
  {
    id: 'flutter',
    label: 'Flutter',
    code: `void startSession(String sessionId, String commandStr) {
  final payload = {
    'type': 'start_session',
    'id': sessionId,
    'command': commandStr,
    'use_pty': true,
    'cols': 80,
    'rows': 24,
  };
  channel?.sink?.add(jsonEncode(payload));
}

void sendSessionInput(String sessionId, String input) {
  final payload = {
    'type': 'send_input',
    'id': sessionId,
    'data': input,
  };
  channel?.sink?.add(jsonEncode(payload));
}

void resizeSession(String sessionId, int cols, int rows) {
  final payload = {
    'type': 'resize',
    'id': sessionId,
    'cols': cols,
    'rows': rows,
  };
  channel?.sink?.add(jsonEncode(payload));
}`
  },
  {
    id: 'typescript',
    label: 'TypeScript',
    code: `startSession(sessionId: string, commandStr: string) {
  const payload = {
    type: 'start_session',
    id: sessionId,
    command: commandStr,
    use_pty: true,
    cols: 80,
    rows: 24
  };
  this.ws?.send(JSON.stringify(payload));
}

sendSessionInput(sessionId: string, input: string) {
  const payload = {
    type: 'send_input',
    id: sessionId,
    data: input
  };
  this.ws?.send(JSON.stringify(payload));
}

resizeSession(sessionId: string, cols: number, rows: number) {
  const payload = {
    type: 'resize',
    id: sessionId,
    cols,
    rows
  };
  this.ws?.send(JSON.stringify(payload));
}`
  }
];
