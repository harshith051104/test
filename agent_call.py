"""
🤖 AI Agent Voice Bridge with Google ADK Integration
==================================================

This FastAPI application creates a voice bridge where:
- One side: Human caller (via Twilio)
- Other side: Google ADK (Dialogflow) AI Agent

Flow:
📞 Human → Twilio → WebSocket → Google ADK (STT) → Agent Reasoning → Google TTS → Twilio → 🔊 Human

Features:
- Real-time bidirectional audio streaming
- Live transcription capture
- Call recording with local storage
- Google ADK integration for intelligent responses
- RESTful API for transcription retrieval
"""

import os
import json
import asyncio
import base64
import uvicorn
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from dotenv import load_dotenv
import logging

# Google Cloud imports for ADK integration
try:
    from google.cloud import dialogflow
    from google.cloud import texttospeech
    from google.cloud import speech
    import google.auth
    GOOGLE_ADK_AVAILABLE = True
    print("✅ Google ADK libraries imported successfully")
except ImportError as e:
    GOOGLE_ADK_AVAILABLE = False
    print(f"⚠️ Google ADK libraries not available: {e}")
    print("Install with: pip install google-cloud-dialogflow google-cloud-texttospeech google-cloud-speech")

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration from environment variables
PORT = int(os.getenv('PORT', 5052))
WEBHOOK_BASE_URL = os.getenv('WEBHOOK_BASE_URL', 'https://your-ngrok-url.ngrok-free.app')

# Twilio configuration
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Google Cloud configuration
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", "your-project-id")
GOOGLE_LOCATION = os.getenv("GOOGLE_LOCATION", "us-central1")

# Initialize Twilio client
if TWILIO_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
    print("✅ Twilio client initialized")
else:
    print("⚠️ Twilio credentials not configured")
    twilio_client = None

# Initialize FastAPI app
app = FastAPI(title="AI Agent Voice Bridge", version="1.0.0")

# Storage for active calls and transcriptions
active_calls: Dict[str, Dict] = {}
call_connections: Dict[str, WebSocket] = {}
call_transcriptions: Dict[str, List[Dict]] = {}  # call_sid -> list of utterances
call_recordings: Dict[str, Dict] = {}

# Create recordings directory
recordings_dir = Path("recordings")
recordings_dir.mkdir(exist_ok=True)

# =============================================================================
# Google ADK (Dialogflow) Integration Classes
# =============================================================================

class GoogleADKAgent:
    """
    Google ADK (Dialogflow) Agent for intelligent conversation handling
    Integrates Speech-to-Text, Dialogflow reasoning, and Text-to-Speech
    """
    
    def __init__(self, project_id: str, location: str = "us-central1"):
        self.project_id = project_id
        self.location = location
        self.session_path = None
        
        if GOOGLE_ADK_AVAILABLE:
            try:
                # Initialize Dialogflow client
                self.session_client = dialogflow.SessionsClient()
                
                # Initialize Speech-to-Text client
                self.speech_client = speech.SpeechClient()
                
                # Initialize Text-to-Speech client
                self.tts_client = texttospeech.TextToSpeechClient()
                
                # Configure TTS voice (natural English US voice)
                self.voice = texttospeech.VoiceSelectionParams(
                    language_code="en-US",
                    name="en-US-Journey-F",  # Natural-sounding Google voice
                    ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
                )
                
                # Configure audio output
                self.audio_config = texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.MULAW,
                    sample_rate_hertz=8000  # Twilio-compatible format
                )
                
                logger.info("✅ Google ADK Agent initialized successfully")
                
            except Exception as e:
                logger.error(f"❌ Failed to initialize Google ADK Agent: {e}")
                self.session_client = None
        else:
            logger.warning("⚠️ Google ADK not available - agent will use mock responses")
    
    def create_session(self, call_sid: str) -> str:
        """Create a new Dialogflow session for the call"""
        if self.session_client:
            self.session_path = self.session_client.session_path(
                self.project_id, call_sid
            )
            logger.info(f"🤖 Created ADK session for call: {call_sid}")
            return self.session_path
        return f"mock_session_{call_sid}"
    
    def speech_to_text(self, audio_data: bytes) -> str:
        """Convert audio to text using Google Speech-to-Text"""
        if not self.speech_client:
            return "[Mock STT: User said something]"
        
        try:
            # Configure speech recognition
            config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
                sample_rate_hertz=8000,
                language_code="en-US",
                enable_automatic_punctuation=True,
            )
            
            audio = speech.RecognitionAudio(content=audio_data)
            
            # Perform speech recognition
            response = self.speech_client.recognize(config=config, audio=audio)
            
            if response.results:
                transcript = response.results[0].alternatives[0].transcript
                logger.info(f"🎤 STT Result: {transcript}")
                return transcript
            else:
                return ""
                
        except Exception as e:
            logger.error(f"❌ STT Error: {e}")
            return ""
    
    def process_with_dialogflow(self, text_input: str, session_path: str) -> str:
        """Send text to Dialogflow agent for intelligent response"""
        if not self.session_client:
            # Mock response for demonstration
            mock_responses = [
                "Hello! I'm your AI assistant. How can I help you today?",
                "That's interesting! Could you tell me more about that?",
                "I understand. Let me help you with that.",
                "Is there anything else I can assist you with?",
                "Thank you for calling! Have a great day!"
            ]
            import random
            return random.choice(mock_responses)
        
        try:
            # Create text input for Dialogflow
            text_input_obj = dialogflow.TextInput(text=text_input, language_code="en-US")
            query_input = dialogflow.QueryInput(text=text_input_obj)
            
            # Send request to Dialogflow
            response = self.session_client.detect_intent(
                request={"session": session_path, "query_input": query_input}
            )
            
            # Extract agent response
            agent_response = response.query_result.fulfillment_text
            logger.info(f"🤖 ADK Response: {agent_response}")
            
            return agent_response if agent_response else "I'm sorry, I didn't understand that."
            
        except Exception as e:
            logger.error(f"❌ Dialogflow Error: {e}")
            return "I'm experiencing some technical difficulties. Please try again."
    
    def text_to_speech(self, text: str) -> bytes:
        """Convert text to speech using Google Text-to-Speech"""
        if not self.tts_client:
            # Return mock audio data (silence)
            return b'\x00' * 1000
        
        try:
            # Create synthesis input
            synthesis_input = texttospeech.SynthesisInput(text=text)
            
            # Perform TTS
            response = self.tts_client.synthesize_speech(
                input=synthesis_input,
                voice=self.voice,
                audio_config=self.audio_config
            )
            
            logger.info(f"🔊 TTS Generated: {len(response.audio_content)} bytes")
            return response.audio_content
            
        except Exception as e:
            logger.error(f"❌ TTS Error: {e}")
            return b'\x00' * 1000  # Return silence on error

# Initialize global ADK agent
adk_agent = GoogleADKAgent(GOOGLE_PROJECT_ID, GOOGLE_LOCATION)

# =============================================================================
# Audio Processing and Buffering
# =============================================================================

class AudioBuffer:
    """Buffer for collecting audio chunks before processing"""
    
    def __init__(self, buffer_duration_ms: int = 3000):
        self.buffer_duration_ms = buffer_duration_ms
        self.buffer = []
        self.last_process_time = datetime.now()
    
    def add_audio(self, audio_chunk: bytes):
        """Add audio chunk to buffer"""
        self.buffer.append(audio_chunk)
    
    def should_process(self) -> bool:
        """Check if buffer should be processed (based on duration or silence)"""
        now = datetime.now()
        time_since_last = (now - self.last_process_time).total_seconds() * 1000
        return time_since_last >= self.buffer_duration_ms
    
    def get_and_clear(self) -> bytes:
        """Get buffered audio and clear buffer"""
        if not self.buffer:
            return b''
        
        audio_data = b''.join(self.buffer)
        self.buffer = []
        self.last_process_time = datetime.now()
        return audio_data

# =============================================================================
# Twilio Integration - Call Handling
# =============================================================================

@app.get("/")
async def root():
    """Root endpoint - health check"""
    return {"message": "Agent bridge is running"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """
    Handle incoming calls from Twilio
    Sets up the call to connect to our AI agent via WebSocket
    """
    response = VoiceResponse()
    
    # Get caller information from Twilio
    form_data = await request.form()
    caller = form_data.get('From', 'Unknown')
    call_sid = form_data.get('CallSid', 'Unknown')
    
    logger.info(f"📞 Incoming call from {caller}, SID: {call_sid}")
    
    # Initialize call tracking
    active_calls[call_sid] = {
        'caller_number': caller,
        'start_time': datetime.now(),
        'status': 'active'
    }
    
    # Initialize transcription storage for this call
    call_transcriptions[call_sid] = []
    
    # Start call recording
    if twilio_client:
        try:
            # Enable recording for this call
            recording = twilio_client.calls(call_sid).recordings.create()
            call_recordings[call_sid] = {
                'recording_sid': recording.sid,
                'started_at': datetime.now()
            }
            logger.info(f"🎙️ Recording started for call {call_sid}")
        except Exception as e:
            logger.error(f"❌ Failed to start recording: {e}")
    
    # Greet the caller
    response.say(
        "Hello! You're now connected to an AI assistant. Please speak clearly and I'll help you with whatever you need.",
        voice="Polly.Joanna"
    )
    
    # Connect to our WebSocket for real-time audio streaming
    connect = Connect()
    websocket_url = WEBHOOK_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    connect.stream(url=f'{websocket_url}/media-stream/{call_sid}')
    response.append(connect)
    
    return Response(content=str(response), media_type="application/xml")

# =============================================================================
# WebSocket - Real-time Audio Streaming with ADK Integration
# =============================================================================

@app.websocket("/media-stream/{call_sid}")
async def handle_media_stream(websocket: WebSocket, call_sid: str):
    """
    Handle Twilio Media Stream WebSocket connection
    This is where the magic happens:
    1. Receive audio from human caller (via Twilio)
    2. Convert to text using Google STT
    3. Process with Dialogflow agent
    4. Convert response to speech using Google TTS
    5. Stream back to caller via Twilio
    """
    await websocket.accept()
    logger.info(f"🔌 WebSocket connected for call: {call_sid}")
    
    # Store connection for this call
    call_connections[call_sid] = websocket
    
    # Create ADK session for this call
    session_path = adk_agent.create_session(call_sid)
    
    # Audio buffer for collecting speech
    audio_buffer = AudioBuffer(buffer_duration_ms=3000)  # 3 second buffer
    
    # Stream metadata
    stream_sid = None
    
    try:
        async for message in websocket.iter_text():
            try:
                data = json.loads(message)
                event = data.get('event')
                
                if event == 'connected':
                    logger.info(f"🔗 Media stream connected for call: {call_sid}")
                
                elif event == 'start':
                    stream_sid = data.get('streamSid')
                    logger.info(f"🎵 Media stream started: {stream_sid}")
                    
                    # Send initial AI greeting
                    greeting = "Hello! I'm your AI assistant. How can I help you today?"
                    await send_ai_response_to_caller(websocket, stream_sid, greeting)
                
                elif event == 'media':
                    # Receive audio from caller
                    payload = data.get('media', {}).get('payload', '')
                    if payload:
                        # Decode audio data (base64 encoded μ-law)
                        audio_data = base64.b64decode(payload)
                        audio_buffer.add_audio(audio_data)
                        
                        # Process buffered audio when ready
                        if audio_buffer.should_process():
                            buffered_audio = audio_buffer.get_and_clear()
                            if len(buffered_audio) > 100:  # Only process if we have substantial audio
                                # Process with ADK agent in background
                                asyncio.create_task(
                                    process_caller_audio_with_adk(
                                        buffered_audio, websocket, stream_sid, 
                                        call_sid, session_path
                                    )
                                )
                
                elif event == 'stop':
                    logger.info(f"🛑 Media stream stopped for call: {call_sid}")
                    break
                    
            except json.JSONDecodeError:
                logger.error("❌ Invalid JSON received from Twilio")
            except Exception as e:
                logger.error(f"❌ Error processing media stream: {e}")
    
    except WebSocketDisconnect:
        logger.info(f"🔌 WebSocket disconnected for call: {call_sid}")
    
    finally:
        # Cleanup
        if call_sid in call_connections:
            del call_connections[call_sid]
        
        # Mark call as completed
        if call_sid in active_calls:
            active_calls[call_sid]['status'] = 'completed'
            active_calls[call_sid]['end_time'] = datetime.now()
        
        logger.info(f"🧹 Cleaned up connection for call: {call_sid}")

async def process_caller_audio_with_adk(
    audio_data: bytes, 
    websocket: WebSocket, 
    stream_sid: str, 
    call_sid: str, 
    session_path: str
):
    """
    Process caller's audio through the complete ADK pipeline:
    Audio → STT → Dialogflow → TTS → Response to caller
    """
    try:
        # Step 1: Convert speech to text
        transcript = adk_agent.speech_to_text(audio_data)
        
        if transcript and transcript.strip():
            # Log the transcription
            utterance = {
                'timestamp': datetime.now().isoformat(),
                'speaker': 'caller',
                'text': transcript,
                'type': 'speech_to_text'
            }
            call_transcriptions[call_sid].append(utterance)
            logger.info(f"👤 Caller said: {transcript}")
            
            # Step 2: Process with Dialogflow agent
            agent_response = adk_agent.process_with_dialogflow(transcript, session_path)
            
            if agent_response:
                # Log agent response
                response_utterance = {
                    'timestamp': datetime.now().isoformat(),
                    'speaker': 'agent',
                    'text': agent_response,
                    'type': 'agent_response'
                }
                call_transcriptions[call_sid].append(response_utterance)
                
                # Step 3: Send AI response back to caller
                await send_ai_response_to_caller(websocket, stream_sid, agent_response)
        
    except Exception as e:
        logger.error(f"❌ Error in ADK processing: {e}")
        # Send error response to caller
        error_response = "I'm sorry, I didn't catch that. Could you please repeat?"
        await send_ai_response_to_caller(websocket, stream_sid, error_response)

async def send_ai_response_to_caller(websocket: WebSocket, stream_sid: str, text_response: str):
    """
    Convert AI text response to speech and stream to caller via Twilio
    """
    try:
        # Step 1: Convert text to speech using Google TTS
        audio_data = adk_agent.text_to_speech(text_response)
        
        if audio_data:
            # Step 2: Encode audio for Twilio (base64 μ-law)
            audio_base64 = base64.b64encode(audio_data).decode('utf-8')
            
            # Step 3: Send audio to caller via Twilio WebSocket
            # Split audio into chunks for streaming
            chunk_size = 160  # Optimal chunk size for Twilio
            for i in range(0, len(audio_base64), chunk_size):
                chunk = audio_base64[i:i + chunk_size]
                
                media_message = {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {
                        "payload": chunk
                    }
                }
                
                await websocket.send_text(json.dumps(media_message))
                
                # Small delay to prevent overwhelming Twilio
                await asyncio.sleep(0.02)  # 20ms delay
            
            logger.info(f"🤖 AI response sent: {text_response}")
        
    except Exception as e:
        logger.error(f"❌ Error sending AI response: {e}")

# =============================================================================
# Recording Management
# =============================================================================

async def download_and_save_recording(call_sid: str):
    """Download call recording from Twilio and save locally"""
    if not twilio_client or call_sid not in call_recordings:
        return
    
    try:
        recording_info = call_recordings[call_sid]
        recording_sid = recording_info['recording_sid']
        
        # Get recording from Twilio
        recording = twilio_client.recordings(recording_sid).fetch()
        recording_url = f"https://api.twilio.com{recording.uri[:-5]}.wav"
        
        # Download recording
        response = requests.get(recording_url, auth=(TWILIO_SID, TWILIO_AUTH_TOKEN))
        
        if response.status_code == 200:
            # Save to local file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{call_sid}_{recording_sid}_{timestamp}.wav"
            file_path = recordings_dir / filename
            
            with open(file_path, 'wb') as f:
                f.write(response.content)
            
            logger.info(f"💾 Recording saved: {filename}")
            call_recordings[call_sid]['local_file'] = str(file_path)
        
    except Exception as e:
        logger.error(f"❌ Failed to download recording: {e}")

# =============================================================================
# Transcription API Endpoints
# =============================================================================

@app.get("/transcription/{call_sid}")
async def get_call_transcription(call_sid: str):
    """
    Get live transcription for a specific call
    Returns real-time conversation log between caller and AI agent
    """
    if call_sid not in call_transcriptions:
        return JSONResponse(
            status_code=404,
            content={"error": f"No transcription found for call {call_sid}"}
        )
    
    transcription_data = {
        "call_sid": call_sid,
        "call_info": active_calls.get(call_sid, {}),
        "transcription": call_transcriptions[call_sid],
        "total_utterances": len(call_transcriptions[call_sid])
    }
    
    return JSONResponse(content=transcription_data)

@app.get("/transcriptions")
async def list_all_transcriptions():
    """List all available call transcriptions"""
    transcription_summary = []
    
    for call_sid, utterances in call_transcriptions.items():
        call_info = active_calls.get(call_sid, {})
        summary = {
            "call_sid": call_sid,
            "caller_number": call_info.get('caller_number', 'Unknown'),
            "start_time": call_info.get('start_time', '').isoformat() if call_info.get('start_time') else '',
            "status": call_info.get('status', 'unknown'),
            "utterance_count": len(utterances)
        }
        transcription_summary.append(summary)
    
    return JSONResponse(content={
        "total_calls": len(transcription_summary),
        "calls": transcription_summary
    })

# =============================================================================
# Call Management Endpoints
# =============================================================================

@app.get("/calls")
async def list_active_calls():
    """List all active and completed calls"""
    calls_info = []
    
    for call_sid, call_data in active_calls.items():
        call_info = {
            "call_sid": call_sid,
            "caller_number": call_data.get('caller_number', 'Unknown'),
            "start_time": call_data.get('start_time', '').isoformat() if call_data.get('start_time') else '',
            "status": call_data.get('status', 'unknown'),
            "has_transcription": call_sid in call_transcriptions,
            "has_recording": call_sid in call_recordings
        }
        calls_info.append(call_info)
    
    return JSONResponse(content={
        "total_calls": len(calls_info),
        "calls": calls_info
    })

@app.get("/call/{call_sid}/status")
async def get_call_status(call_sid: str):
    """Get detailed status for a specific call"""
    if call_sid not in active_calls:
        return JSONResponse(
            status_code=404,
            content={"error": f"Call {call_sid} not found"}
        )
    
    call_data = active_calls[call_sid]
    status = {
        "call_sid": call_sid,
        "call_info": call_data,
        "transcription_available": call_sid in call_transcriptions,
        "recording_available": call_sid in call_recordings,
        "connection_active": call_sid in call_connections
    }
    
    return JSONResponse(content=status)

# =============================================================================
# System Status and Health Endpoints
# =============================================================================

@app.get("/status")
async def system_status():
    """Get overall system status"""
    status = {
        "service": "AI Agent Voice Bridge",
        "status": "running",
        "components": {
            "twilio": "✅ Connected" if twilio_client else "❌ Not configured",
            "google_adk": "✅ Available" if GOOGLE_ADK_AVAILABLE else "❌ Not available",
            "recordings_dir": "✅ Available" if recordings_dir.exists() else "❌ Not found"
        },
        "stats": {
            "active_calls": len([c for c in active_calls.values() if c.get('status') == 'active']),
            "total_calls": len(active_calls),
            "total_transcriptions": len(call_transcriptions),
            "total_recordings": len(call_recordings)
        },
        "configuration": {
            "webhook_url": WEBHOOK_BASE_URL,
            "twilio_number": TWILIO_PHONE_NUMBER,
            "google_project": GOOGLE_PROJECT_ID
        }
    }
    
    return JSONResponse(content=status)

# =============================================================================
# Application Startup and Main
# =============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize the application"""
    logger.info("🚀 Starting AI Agent Voice Bridge...")
    logger.info(f"🌐 Webhook URL: {WEBHOOK_BASE_URL}")
    logger.info(f"📞 Twilio Number: {TWILIO_PHONE_NUMBER}")
    logger.info(f"🤖 Google Project: {GOOGLE_PROJECT_ID}")
    logger.info(f"💾 Recordings Directory: {recordings_dir}")
    
    # Verify configuration
    if not TWILIO_SID or not TWILIO_AUTH_TOKEN:
        logger.warning("⚠️ Twilio credentials not configured")
    
    if not GOOGLE_ADK_AVAILABLE:
        logger.warning("⚠️ Google ADK not available - using mock responses")
    
    logger.info("✅ AI Agent Voice Bridge started successfully!")

if __name__ == "__main__":
    print("🤖 AI Agent Voice Bridge with Google ADK Integration")
    print("=" * 60)
    print(f"🌐 Server will run on: http://0.0.0.0:{PORT}")
    print(f"📞 Twilio webhook: {WEBHOOK_BASE_URL}/incoming-call")
    print(f"🔌 WebSocket endpoint: {WEBHOOK_BASE_URL}/media-stream/{{call_sid}}")
    print(f"📝 Transcription API: {WEBHOOK_BASE_URL}/transcription/{{call_sid}}")
    print("=" * 60)
    
    uvicorn.run(app, host="0.0.0.0", port=PORT)
