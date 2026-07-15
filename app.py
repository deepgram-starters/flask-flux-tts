"""
Flask Flux TTS Starter - Backend Server

Bridges a browser WebSocket to Deepgram's Flux streaming text-to-speech
(v2 speak) using the official `deepgram-sdk` `client.speak.v2` API.

Unlike the Flux transcription starter (flask-flux), which is a raw websocket-client
proxy, the Deepgram side here goes through the SDK. Because Flask is synchronous,
the SDK's blocking `start_listening()` runs in a background thread while the main
handler forwards browser control messages.

Flow:
  browser --(JSON: Speak/Flush/Close)--> backend --(SDK speak.v2)--> Deepgram Flux TTS
  browser <--(binary audio + JSON control)-- backend <--(SDK speak.v2)-- Deepgram Flux TTS

Routes:
- GET /api/session  - JWT session token
- GET /api/metadata - Metadata from deepgram.toml
- WS  /api/tts      - Streaming TTS bridge (auth required)
"""

import os
import json
import secrets
import threading
import time

import jwt
from flask import Flask, request, jsonify
from flask_sock import Sock
from flask_cors import CORS
from simple_websocket import Server as _WsServer
import toml
from dotenv import load_dotenv

from deepgram import DeepgramClient
from deepgram.core.events import EventType
from deepgram.environment import DeepgramClientEnvironment
from deepgram.speak.v2.types import SpeakV2Speak

# Monkey-patch simple-websocket to echo back the access_token.* subprotocol
# (flask-sock uses simple-websocket for the handshake; dynamic JWT subprotocols
# are not in its static allow-list).
_original_choose_subprotocol = _WsServer.choose_subprotocol


def _choose_subprotocol_with_token(self, ws_request):
    for proto in ws_request.subprotocols:
        if proto.startswith("access_token."):
            return proto
    return _original_choose_subprotocol(self, ws_request)


_WsServer.choose_subprotocol = _choose_subprotocol_with_token

load_dotenv(override=False)

# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_MODEL = os.environ.get("DEEPGRAM_TTS_MODEL", "flux-alexis-en")
DEFAULT_ENCODING = "linear16"
DEFAULT_SAMPLE_RATE = "24000"

CONFIG = {
    "port": int(os.environ.get("PORT", 8081)),
    "host": os.environ.get("HOST", "0.0.0.0"),
}

SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
JWT_EXPIRY = 3600  # 1 hour


def validate_api_key():
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        print("\n" + "=" * 70)
        print("ERROR: Deepgram API key not found!")
        print("=" * 70)
        print("\nCreate a .env file with:\n   DEEPGRAM_API_KEY=your_api_key_here")
        print("\nGet your API key at: https://console.deepgram.com")
        print("=" * 70 + "\n")
        raise ValueError("DEEPGRAM_API_KEY environment variable is required")
    return api_key


API_KEY = validate_api_key()

# One SDK client, reused across connections; the browser never sees the API key.
# DEEPGRAM_BASE_URL (e.g. wss://api.staging.deepgram.com) overrides the default
# production endpoint. speak.v2 uses environment.production for the /v2/speak ws.
def _build_client():
    base_url = os.environ.get("DEEPGRAM_BASE_URL")
    if base_url:
        https = base_url.replace("wss://", "https://").replace("ws://", "http://")
        env = DeepgramClientEnvironment(
            base=https, production=base_url, agent=base_url, agent_rest=https
        )
        print(f"Using custom Deepgram base URL: {base_url}")
        return DeepgramClient(api_key=API_KEY, environment=env)
    return DeepgramClient(api_key=API_KEY)


deepgram = _build_client()


def validate_ws_token():
    """Validates JWT from Sec-WebSocket-Protocol: access_token.<jwt> header."""
    protocol_header = request.headers.get("Sec-WebSocket-Protocol", "")
    protocols = [p.strip() for p in protocol_header.split(",")]
    token_proto = next((p for p in protocols if p.startswith("access_token.")), None)
    if not token_proto:
        return None
    token = token_proto[len("access_token."):]
    try:
        jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
        return token_proto
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


app = Flask(__name__)
CORS(app)
sock = Sock(app)


@app.route("/api/session", methods=["GET"])
def get_session():
    token = jwt.encode(
        {"iat": int(time.time()), "exp": int(time.time()) + JWT_EXPIRY},
        SESSION_SECRET,
        algorithm="HS256",
    )
    return jsonify({"token": token})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/api/metadata", methods=["GET"])
def get_metadata():
    try:
        with open("deepgram.toml", "r") as f:
            config = toml.load(f)
        if "meta" not in config:
            return jsonify({"error": "INTERNAL_SERVER_ERROR",
                            "message": "Missing [meta] section in deepgram.toml"}), 500
        return jsonify(config["meta"]), 200
    except FileNotFoundError:
        return jsonify({"error": "INTERNAL_SERVER_ERROR",
                        "message": "deepgram.toml file not found"}), 500
    except Exception as e:
        print(f"Error reading metadata: {e}")
        return jsonify({"error": "INTERNAL_SERVER_ERROR",
                        "message": f"Failed to read metadata: {e}"}), 500


def _forward_to_browser(ws, message):
    """Forward one Deepgram message to the browser: bytes as binary, models as JSON."""
    try:
        if isinstance(message, (bytes, bytearray)):
            ws.send(bytes(message))
        elif hasattr(message, "model_dump_json"):
            ws.send(message.model_dump_json())
        else:
            ws.send(json.dumps({"type": getattr(message, "type", "Unknown")}))
    except Exception as e:
        print(f"Error forwarding to browser: {e}")


@sock.route("/api/tts")
def tts(ws):
    """Streaming TTS bridge: browser <-> Deepgram Flux (v2 speak) via the SDK."""
    if not validate_ws_token():
        ws.close(4401, "Unauthorized")
        return

    print("Client connected to /api/tts")

    model = request.args.get("model") or DEFAULT_MODEL
    encoding = request.args.get("encoding") or DEFAULT_ENCODING
    sample_rate = request.args.get("sample_rate") or DEFAULT_SAMPLE_RATE
    print(f"TTS config - model: {model}, encoding: {encoding}, sample_rate: {sample_rate}")

    stop_event = threading.Event()

    try:
        with deepgram.speak.v2.connect(
            model=model, encoding=encoding, sample_rate=sample_rate
        ) as connection:
            connection.on(EventType.MESSAGE, lambda m: _forward_to_browser(ws, m))
            connection.on(EventType.CLOSE, lambda _: stop_event.set())
            connection.on(EventType.ERROR, lambda e: (print(f"Deepgram error: {e}"), stop_event.set()))

            # start_listening() blocks, so run it in a background thread while the
            # main thread forwards browser control messages to Deepgram.
            listen_thread = threading.Thread(target=connection.start_listening, daemon=True)
            listen_thread.start()

            while not stop_event.is_set():
                try:
                    message = ws.receive(timeout=0.1)
                except Exception:
                    break  # client disconnected
                if message is None:
                    continue
                if isinstance(message, (bytes, bytearray)):
                    continue  # browser sends JSON control only
                try:
                    data = json.loads(message)
                except (ValueError, TypeError):
                    print("Ignoring non-JSON message from client")
                    continue

                msg_type = data.get("type")
                try:
                    if msg_type == "Speak":
                        connection.send_speak(SpeakV2Speak(text=data.get("text", "")))
                    elif msg_type == "Flush":
                        connection.send_flush()
                    elif msg_type == "Close":
                        connection.send_close()
                    else:
                        print(f"Ignoring unknown client message type: {msg_type}")
                except Exception as e:
                    print(f"Error forwarding to Deepgram: {e}")
    except Exception as e:
        print(f"Error setting up TTS connection: {e}")
        try:
            ws.close(1011, "Internal server error")
        except Exception:
            pass
    finally:
        stop_event.set()
        print("Client disconnected from /api/tts")


if __name__ == "__main__":
    port, host = CONFIG["port"], CONFIG["host"]
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print("\n" + "=" * 70)
    print("Flask Flux TTS Server (Backend API)")
    print("=" * 70)
    print(f"Server:   http://{host}:{port}")
    print("")
    print("GET  /api/session")
    print("WS   /api/tts (auth required)")
    print("GET  /api/metadata")
    print("GET  /health")
    print("=" * 70 + "\n")
    app.run(host=host, port=port, debug=debug)
