import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request, Body, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))
TEMPERATURE = float(os.getenv('TEMPERATURE', 0.8))
SYSTEM_MESSAGE = "You are a helpful and bubbly AI assistant..."
VOICE = 'alloy'

# Twilio credentials
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# --------------------------
# 1. Health Check
# --------------------------
@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Pro Stream Server is running!"}

# --------------------------
# 2. Incoming & Outbound Answer
# --------------------------
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    response.say(
        "Please wait while we connect your call to the AI voice assistant, powered by Twilio and OpenAI Realtime API",
        voice="Google.en-US-Standard-A"  # Fixed to a valid Twilio Google voice
    )
    response.pause(length=1)
    response.say("O.K. you can start talking!", voice="Google.en-US-Standard-A")

    host = str(request.base_url).rstrip("/")
    connect = Connect()
    # Twilio expects a wss:// URL to connect the media stream to your websocket endpoint
    connect.stream(url=f'{host.replace("http", "ws")}/media-stream')
    response.append(connect)

    return HTMLResponse(content=str(response), media_type="application/xml")

# --------------------------
# 3. Outbound Call Trigger API
# --------------------------
@app.post("/make-call")
async def make_call(to_number: str = Body(..., embed=True)):
    try:
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url="https://48e59144d46a.ngrok-free.app/incoming-call"  # 👈 replace with your public ngrok/host URL
        )
        return {"status": "success", "call_sid": call.sid}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --------------------------
# 4. Web Dashboard
# --------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AI Call Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; background: #f4f4f9; text-align: center; padding: 50px; }
            h1 { color: #333; }
            input { padding: 10px; width: 250px; margin: 10px; font-size: 16px; }
            button { padding: 10px 20px; font-size: 16px; background: #4CAF50; color: white; border: none; cursor: pointer; border-radius: 5px; }
            button:hover { background: #45a049; }
            #status { margin-top: 20px; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>📞 AI Call Dashboard</h1>
        <form id="callForm">
            <input type="text" id="to_number" name="to_number" placeholder="+91XXXXXXXXXX" required />
            <br>
            <button type="submit">Make Call</button>
        </form>
        <p id="status"></p>

        <script>
            const form = document.getElementById("callForm");
            form.addEventListener("submit", async (e) => {
                e.preventDefault();
                const number = document.getElementById("to_number").value;
                document.getElementById("status").innerText = "📡 Calling...";
                try {
                    const res = await fetch("/make-call", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ to_number: number })
                    });
                    const data = await res.json();
                    if (data.status === "success") {
                        document.getElementById("status").innerText = "✅ Call initiated! SID: " + data.call_sid;
                    } else {
                        document.getElementById("status").innerText = "❌ Error: " + data.message;
                    }
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Request failed.";
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# --------------------------
# 5. Media Stream (Twilio ↔ OpenAI Realtime bridge)
# --------------------------
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    print("🔗 Twilio client connected")
    await websocket.accept()

    # Use the OpenAI realtime model endpoint
    openai_ws_uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"

    # Connect to OpenAI Realtime with proper headers
    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1")
    ]
    async with websockets.connect(openai_ws_uri, additional_headers=headers) as openai_ws:
        # Initialize session with OpenAI (audio formats set to g711_ulaw)
        await initialize_session(openai_ws)

        stream_sid = None
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        latest_media_timestamp = 0

        async def receive_from_twilio():
            nonlocal stream_sid, latest_media_timestamp, last_assistant_item
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)

                    if data.get('event') == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Twilio stream started: {stream_sid}")
                        latest_media_timestamp = 0
                        last_assistant_item = None
                        mark_queue.clear()

                    elif data.get('event') == 'media':
                        payload_b64 = data['media']['payload']
                        latest_media_timestamp = int(data['media'].get('timestamp', 0))

                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": payload_b64
                        }
                        await openai_ws.send(json.dumps(audio_append))

                    elif data.get('event') == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)

                    elif data.get('event') == 'stop':
                        print("Twilio stream stopped, committing to OpenAI and creating response")
                        await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        await openai_ws.send(json.dumps({
                            "type": "response.create",
                            "response": {
                                "modalities": ["audio", "text"],
                                "instructions": "Please respond with audio."
                            }
                        }))

            except WebSocketDisconnect:
                print("Twilio websocket disconnected")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            nonlocal last_assistant_item, response_start_timestamp_twilio

            try:
                async for openai_message in openai_ws:
                    resp = json.loads(openai_message)

                    # Debug: print all event types for troubleshooting
                    event_type = resp.get('type')
                    if event_type not in ['response.content.done', 'rate_limits.updated', 'response.done',
                                            'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
                                            'input_audio_buffer.speech_started', 'session.created']:
                        print(f"OpenAI event (other): {event_type}")

                    if resp.get('type') in ['response.content.done', 'rate_limits.updated', 'response.done',
                                            'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
                                            'input_audio_buffer.speech_started', 'session.created']:
                        print("OpenAI event:", resp.get('type'))

                    if resp.get('type') == 'error':
                        print("OpenAI error:", resp.get('error'))  # Added for debugging

                    if resp.get('type') == 'response.audio.delta':
                        b64_audio = resp.get('delta')
                        if b64_audio:
                            media_msg = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": b64_audio}
                            }
                            await websocket.send_text(json.dumps(media_msg))

                            mark_msg = {
                                "event": "mark",
                                "streamSid": stream_sid,
                                "mark": {"name": "responsePart"}
                            }
                            await websocket.send_text(json.dumps(mark_msg))
                            mark_queue.append('responsePart')

                    if resp.get('type') == 'input_audio_buffer.speech_started':
                        print("OpenAI detected speech started from caller")

                    if resp.get('type') == 'input_audio_buffer.speech_stopped':
                        print("OpenAI detected speech stopped from caller - creating response")
                        # Trigger response creation when speech stops with explicit audio output request
                        await openai_ws.send(json.dumps({
                            "type": "response.create",
                            "response": {
                                "modalities": ["audio", "text"],
                                "instructions": "Please respond with audio."
                            }
                        }))

            except Exception as e:
                print("Error in send_to_twilio:", e)

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


# --------------------------
# 6. Session Initialization (OpenAI)
# --------------------------
async def initialize_session(openai_ws):
    """
    Send initial session update telling OpenAI to accept g711_ulaw input and produce g711_ulaw output.
    Twilio sends μ-law 8kHz; we instruct OpenAI to use g711_ulaw so we can avoid conversions.
    """
    session_update = {
        "type": "session.update",
        "session": {
            "modalities": ["audio", "text"],
            "instructions": SYSTEM_MESSAGE,
            "voice": VOICE,
            "temperature": TEMPERATURE,
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "turn_detection": {
                "type": "server_vad"
            }
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)