# Flask Flux Text-to-Speech

Get started using Deepgram's **Flux streaming text-to-speech** (v2 speak) with this Flask demo app.

Unlike the Flux **transcription** starter ([`flask-flux`](https://github.com/deepgram-starters/flask-flux)), which is a raw `websocket-client` proxy, this starter uses the official [`deepgram-sdk`](https://github.com/deepgram/deepgram-python-sdk) `client.speak.v2` API on the backend.

## How it works

```
Browser  ──(JSON: Speak/Flush/Close)──▶  Flask backend  ──(SDK speak.v2)──▶  Deepgram Flux TTS
Browser  ◀──(binary audio + JSON control)──  Flask backend  ◀──(SDK speak.v2)──  Deepgram Flux TTS
```

- The API key stays server-side; the browser authenticates with a short-lived JWT (`/api/session`) on the `access_token.<jwt>` WebSocket subprotocol.
- Flask is synchronous, so the SDK's blocking `start_listening()` runs in a background thread while the main handler forwards browser control messages via `send_speak` / `send_flush` / `send_close`.
- Audio frames from Deepgram (`bytes`) are forwarded to the browser as binary; control messages (`Connected`, `SpeechStarted`, `Flushed`, `Warning`, `Error`, …) as JSON.

### Client → backend message protocol

Send JSON text frames on the `/api/tts` WebSocket:

```jsonc
{ "type": "Speak", "text": "Hello from Flux." }
{ "type": "Flush" }
{ "type": "Close" }
```

## Prerequisites

- Python 3.10+
- A Deepgram API key ([console.deepgram.com](https://console.deepgram.com/))


## Local Development

```bash
make init
cp sample.env .env   # add your DEEPGRAM_API_KEY
make start           # backend on http://localhost:8081
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DEEPGRAM_API_KEY` | — | **Required.** Your Deepgram API key. |
| `PORT` | `8081` | Backend port. |
| `HOST` | `0.0.0.0` | Backend host. |
| `DEEPGRAM_TTS_MODEL` | `flux-alexis-en` | Flux voice (`flux-{voice}-{language}`). |
| `SESSION_SECRET` | random per boot | Set in production for stable JWT signing. |

`model`, `encoding`, and `sample_rate` may also be passed as query params on `/api/tts`.

## License

MIT - See [LICENSE](./LICENSE)
