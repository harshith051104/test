import os
import json
import asyncio
import time
import base64
import uvicorn
import requests
import urllib.parse
from pathlib import Path
from fastapi import FastAPI, WebSocket, Request, Body, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
from dotenv import load_dotenv
from typing import Dict, Set
from datetime import datetime
import assemblyai as aai

# 🆕 HubSpot Integration
try:
    from MCP.hubspot_voice_integration import enhance_incoming_call_with_crm_context, log_completed_call_to_crm
    HUBSPOT_ENABLED = True
    print("🎯 HubSpot CRM integration enabled")
except ImportError as e:
    HUBSPOT_ENABLED = False
    print(f"⚠️ HubSpot integration not available: {e}")

load_dotenv()

# Configuration
PORT = int(os.getenv('PORT', 5051))
WEBHOOK_BASE_URL = os.getenv('WEBHOOK_BASE_URL', 'https://2869df5bc649.ngrok-free.app')  # Set this to your ngrok URL

# Twilio credentials
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# AssemblyAI credentials
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
if ASSEMBLYAI_API_KEY and ASSEMBLYAI_API_KEY != "your_assemblyai_api_key_here":
    aai.settings.api_key = ASSEMBLYAI_API_KEY
    print("🎯 AssemblyAI configured for transcription")
else:
    print("⚠️ AssemblyAI API key not configured. Please set ASSEMBLYAI_API_KEY in .env file")

client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
app = FastAPI()

# Store active call sessions and transcriptions
active_calls: Dict[str, Dict] = {}
call_connections: Dict[str, WebSocket] = {}
call_transcriptions: Dict[str, list] = {}  # Store transcriptions per call from Twilio
call_recordings: Dict[str, Dict] = {}  # Store recording information per call
latest_laptop_call_sid: str = None  # Track the latest laptop call for WebRTC connection
# NEW: Store stream SIDs for proper Twilio communication
stream_sids: Dict[str, str] = {}  # call_sid -> stream_sid mapping

# Create recordings directory
RECORDINGS_DIR = Path("recordings")
RECORDINGS_DIR.mkdir(exist_ok=True)

if not TWILIO_SID or not TWILIO_AUTH_TOKEN:
    raise ValueError('Missing Twilio credentials. Please set them in the .env file.')

if not WEBHOOK_BASE_URL or WEBHOOK_BASE_URL == 'https://2869df5bc649.ngrok-free.app':
    print("⚠️  WARNING: WEBHOOK_BASE_URL not set! Please set it to your ngrok URL in the .env file")
    print("⚠️  Example: WEBHOOK_BASE_URL=https://2869df5bc649.ngrok-free.app")

# --------------------------
# Twilio Transcription Functions
# --------------------------
async def transcribe_with_assemblyai(recording_url: str, call_sid: str, recording_sid: str):
    """Transcribe audio using AssemblyAI by downloading Twilio recording first"""
    try:
        print(f"🎯 Starting AssemblyAI transcription for recording {recording_sid}")
        
        if not ASSEMBLYAI_API_KEY or ASSEMBLYAI_API_KEY == "your_assemblyai_api_key_here":
            print("❌ AssemblyAI API key not configured")
            return {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "recording_sid": recording_sid,
                "transcription_sid": "NO_API_KEY",
                "text": "AssemblyAI transcription not available - API key not configured. Please set ASSEMBLYAI_API_KEY in .env file",
                "status": "no_api_key",
                "call_sid": call_sid,
                "source": "assemblyai_error"
            }
        
        # Step 1: Download the recording from Twilio with proper authentication
        print(f"📥 Downloading recording from Twilio: {recording_url}")
        
        try:
            # Use Twilio client credentials for authentication
            response = requests.get(recording_url, auth=(TWILIO_SID, TWILIO_AUTH_TOKEN))
            response.raise_for_status()
            
            # Create temporary file path
            temp_filename = f"temp_recording_{recording_sid}_{int(time.time())}.wav"
            temp_file_path = RECORDINGS_DIR / temp_filename
            
            # Save the audio data to temporary file
            with open(temp_file_path, 'wb') as f:
                f.write(response.content)
            
            print(f"💾 Saved recording to temporary file: {temp_file_path}")
            print(f"📁 File size: {len(response.content)} bytes")
            
        except Exception as download_error:
            print(f"❌ Error downloading recording from Twilio: {download_error}")
            return {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "recording_sid": recording_sid,
                "transcription_sid": "DOWNLOAD_ERROR",
                "text": f"Failed to download recording from Twilio: {str(download_error)}",
                "status": "download_error",
                "call_sid": call_sid,
                "source": "assemblyai_error"
            }
        
        # Step 2: Upload to AssemblyAI and transcribe
        try:
            print(f"🎯 Uploading to AssemblyAI for transcription...")
            
            # Configure transcription settings
            config = aai.TranscriptionConfig(
                speaker_labels=True,  # Enable speaker diarization
                auto_chapters=False,
                sentiment_analysis=False,
                auto_highlights=False,
                entity_detection=False,
            )
            
            # Create transcriber
            transcriber = aai.Transcriber(config=config)
            
            # Transcribe the local file
            print(f"🎯 Submitting local file to AssemblyAI: {temp_file_path}")
            transcript = transcriber.transcribe(str(temp_file_path))
            
            print(f"🎯 AssemblyAI transcription job submitted, ID: {transcript.id}")
            print(f"🎯 Status: {transcript.status}")
            
            if transcript.status == aai.TranscriptStatus.error:
                print(f"❌ AssemblyAI transcription failed: {transcript.error}")
                return {
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "recording_sid": recording_sid,
                    "transcription_sid": transcript.id,
                    "text": f"AssemblyAI transcription failed: {transcript.error}",
                    "status": "error",
                    "call_sid": call_sid,
                    "source": "assemblyai_error"
                }
            
            # Get the transcription text
            transcription_text = transcript.text or "No transcription text available"
            
            # Build speaker-separated text if available
            speaker_text = ""
            if transcript.utterances:
                print(f"🎯 Found {len(transcript.utterances)} speaker utterances")
                for utterance in transcript.utterances:
                    speaker_text += f"Speaker {utterance.speaker}: {utterance.text}\n"
            
            # Use speaker-separated text if available, otherwise use main text
            final_text = speaker_text.strip() if speaker_text else transcription_text
            
            print(f"✅ AssemblyAI transcription completed successfully")
            print(f"📝 Transcription text: {final_text[:100]}...")
            
            # Clean up temporary file
            try:
                temp_file_path.unlink()
                print(f"🧹 Cleaned up temporary file: {temp_filename}")
            except Exception as cleanup_error:
                print(f"⚠️ Could not clean up temporary file: {cleanup_error}")
            
            return {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "recording_sid": recording_sid,
                "transcription_sid": transcript.id,
                "text": final_text,
                "status": "completed",
                "call_sid": call_sid,
                "source": "assemblyai",
                "confidence": transcript.confidence if hasattr(transcript, 'confidence') else None,
                "audio_duration": transcript.audio_duration if hasattr(transcript, 'audio_duration') else None
            }
            
        except Exception as transcription_error:
            print(f"❌ Error during AssemblyAI transcription: {transcription_error}")
            
            # Clean up temporary file on error
            try:
                if 'temp_file_path' in locals() and temp_file_path.exists():
                    temp_file_path.unlink()
                    print(f"🧹 Cleaned up temporary file after error")
            except:
                pass
            
            return {
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "recording_sid": recording_sid,
                "transcription_sid": "TRANSCRIPTION_ERROR",
                "text": f"AssemblyAI transcription error: {str(transcription_error)}",
                "status": "error",
                "call_sid": call_sid,
                "source": "assemblyai_error"
            }
        
    except Exception as e:
        print(f"❌ General error with AssemblyAI transcription: {e}")
        return {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "recording_sid": recording_sid,
            "transcription_sid": "ERROR",
            "text": f"AssemblyAI transcription error: {str(e)}",
            "status": "error",
            "call_sid": call_sid,
            "source": "assemblyai_error"
        }

async def fetch_twilio_transcription(call_sid: str):
    """Fetch recordings and transcribe using AssemblyAI"""
    try:
        print(f"📝 Starting transcription process for call {call_sid}")
        
        # Get the call details
        call = client.calls(call_sid).fetch()
        print(f"📝 Call status: {call.status}, Duration: {call.duration}")
        
        # Get recordings for this call
        recordings = client.recordings.list(call_sid=call_sid)
        print(f"📝 Found {len(recordings)} recordings for call {call_sid}")
        
        if not recordings:
            print(f"📝 No recordings found for call {call_sid}")
            return []
        
        transcriptions = []
        for i, recording in enumerate(recordings):
            print(f"📝 Processing recording {i+1}/{len(recordings)}: {recording.sid}")
            print(f"📝 Recording status: {recording.status}, Duration: {recording.duration}")
            
            if recording.status == 'completed':
                # Build the recording URL for AssemblyAI
                recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{client.username}/Recordings/{recording.sid}.wav"
                print(f"📝 Recording URL: {recording_url}")
                
                # Transcribe with AssemblyAI
                transcription_result = await transcribe_with_assemblyai(recording_url, call_sid, recording.sid)
                transcriptions.append(transcription_result)
                
                # Also add basic recording info
                recording_info = {
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "recording_sid": recording.sid,
                    "transcription_sid": "RECORDING_INFO",
                    "text": f"📁 Recording Details: Duration {recording.duration}s, Status: {recording.status}, Download: {recording_url}",
                    "status": "recording_info",
                    "call_sid": call_sid,
                    "source": "recording_metadata",
                    "recording_url": recording_url,
                    "duration": recording.duration
                }
                transcriptions.append(recording_info)
                
            else:
                print(f"⏳ Recording {recording.sid} not ready yet (status: {recording.status})")
                # Add pending status
                pending_info = {
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "recording_sid": recording.sid,
                    "transcription_sid": "PENDING",
                    "text": f"Recording status: {recording.status}. Transcription will be available once recording is completed.",
                    "status": "pending",
                    "call_sid": call_sid,
                    "source": "recording_pending"
                }
                transcriptions.append(pending_info)
        
        # Store transcriptions
        if call_sid not in call_transcriptions:
            call_transcriptions[call_sid] = []
        call_transcriptions[call_sid].extend(transcriptions)
        
        print(f"📝 Processed {len(transcriptions)} transcription entries for call {call_sid}")
        return transcriptions
        
    except Exception as e:
        print(f"❌ Error fetching transcriptions for {call_sid}: {e}")
        return []

async def start_call_recording_with_transcription(call_sid: str):
    """Start recording for a call and then create transcription"""
    try:
        # Start recording the call first
        recording = client.calls(call_sid).recordings.create(
            recording_channels='dual'  # Record both participants separately
        )
        print(f"🎙️ Started recording for call {call_sid}: {recording.sid}")
        
        # Store recording information
        if call_sid not in call_recordings:
            call_recordings[call_sid] = []
        
        call_recordings[call_sid].append({
            "recording_sid": recording.sid,
            "call_sid": call_sid,
            "started_at": datetime.now().isoformat(),
            "status": "recording",
            "channels": "dual",
            "local_file": None
        })
        
        # Schedule recording download after a delay
        asyncio.create_task(download_recording_after_delay(call_sid, recording.sid))
        
        # Note: Transcription will be handled after recording is complete
        # We'll fetch it later using the delayed_transcription_fetch function
        
        return recording.sid
    except Exception as e:
        print(f"❌ Error starting recording for {call_sid}: {e}")
        # Try without dual channels if that's causing issues
        try:
            print(f"🔄 Retrying recording without dual channels for {call_sid}")
            recording = client.calls(call_sid).recordings.create()
            print(f"🎙️ Started basic recording for call {call_sid}: {recording.sid}")
            
            # Store recording information
            if call_sid not in call_recordings:
                call_recordings[call_sid] = []
            
            call_recordings[call_sid].append({
                "recording_sid": recording.sid,
                "call_sid": call_sid,
                "started_at": datetime.now().isoformat(),
                "status": "recording",
                "channels": "mono",
                "local_file": None
            })
            
            # Schedule recording download after a delay
            asyncio.create_task(download_recording_after_delay(call_sid, recording.sid))
            
            return recording.sid
        except Exception as e2:
            print(f"❌ Failed to start any recording for {call_sid}: {e2}")
            return None

async def download_recording_after_delay(call_sid: str, recording_sid: str, delay: int = 60):
    """Download and save recording after a delay to ensure it's ready"""
    print(f"📥 Scheduling recording download for {recording_sid} in {delay} seconds")
    await asyncio.sleep(delay)
    await download_and_save_recording(call_sid, recording_sid)

async def download_and_save_recording(call_sid: str, recording_sid: str):
    """Download recording from Twilio and save to local file"""
    try:
        print(f"📥 Downloading recording {recording_sid} for call {call_sid}")
        
        # Get recording details from Twilio
        recording = client.recordings(recording_sid).fetch()
        
        if recording.status != 'completed':
            print(f"⏳ Recording {recording_sid} not ready yet (status: {recording.status}). Will retry in 30 seconds.")
            await asyncio.sleep(30)
            await download_and_save_recording(call_sid, recording_sid)
            return
        
        # Create filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{call_sid}_{recording_sid}_{timestamp}.wav"
        file_path = RECORDINGS_DIR / filename
        
        # Download the recording
        recording_url = f"https://api.twilio.com{recording.uri.replace('.json', '.wav')}"
        
        print(f"📥 Downloading from: {recording_url}")
        
        # Use requests with Twilio credentials to download
        response = requests.get(
            recording_url,
            auth=(TWILIO_SID, TWILIO_AUTH_TOKEN)
        )
        
        if response.status_code == 200:
            # Save the recording to file
            with open(file_path, 'wb') as f:
                f.write(response.content)
            
            print(f"💾 Recording saved to: {file_path}")
            
            # Update recording information
            if call_sid in call_recordings:
                for rec_info in call_recordings[call_sid]:
                    if rec_info["recording_sid"] == recording_sid:
                        rec_info["status"] = "downloaded"
                        rec_info["local_file"] = str(file_path)
                        rec_info["file_size"] = len(response.content)
                        rec_info["duration"] = recording.duration
                        rec_info["downloaded_at"] = datetime.now().isoformat()
                        break
            
            return str(file_path)
        else:
            print(f"❌ Failed to download recording: HTTP {response.status_code}")
            return None
            
    except Exception as e:
        print(f"❌ Error downloading recording {recording_sid}: {e}")
        return None

# --------------------------
# 1. Health Check
# --------------------------
@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Human-to-Human Bridge Server is running!"}

# --------------------------
# 2. Incoming Call Handler
# --------------------------
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming calls to the Twilio number"""
    response = VoiceResponse()
    
    # Get caller information
    form_data = await request.form()
    caller = form_data.get('From', 'Unknown')
    call_sid = form_data.get('CallSid', 'Unknown')
    
    print(f"📞 Incoming call from {caller}, SID: {call_sid}")
    
    # 🆕 GET HUBSPOT CONTEXT FOR AI
    crm_context = None
    if HUBSPOT_ENABLED:
        try:
            crm_context = await enhance_incoming_call_with_crm_context(caller, call_sid)
            print(f"📋 CRM Context: {crm_context}")
        except Exception as e:
            print(f"⚠️ HubSpot context lookup failed: {e}")
            crm_context = {"error": str(e)}
    
    # Store context for the call
    if call_sid not in active_calls:
        active_calls[call_sid] = {}
    active_calls[call_sid]['caller_number'] = caller
    active_calls[call_sid]['crm_context'] = crm_context
    active_calls[call_sid]['start_time'] = datetime.now()
    
    response.say(
        "Welcome to the human-to-human bridge. Please wait while we connect your call.",
        voice="Polly.Joanna"
    )
    response.pause(length=1)
    response.say("You are now connected!", voice="Polly.Joanna")

    # Create WebSocket connection for this call
    connect = Connect()
    websocket_url = WEBHOOK_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    connect.stream(url=f'{websocket_url}/media-stream/{call_sid}')
    response.append(connect)

    # Start recording with transcription for this call
    asyncio.create_task(start_call_recording_with_transcription(call_sid))

    return HTMLResponse(content=str(response), media_type="application/xml")

# --------------------------
# 3. Outbound Call Handler
# --------------------------
@app.api_route("/outbound-call", methods=["GET", "POST"])
async def handle_outbound_call(request: Request):
    """Handle the second leg of the call (outbound to target number)"""
    response = VoiceResponse()
    
    # Get call information
    form_data = await request.form()
    call_sid = form_data.get('CallSid', 'Unknown')
    
    print(f"📞 Outbound call connected, SID: {call_sid}")
    
    response.say(
        "You have an incoming call through the bridge. You are now connected!",
        voice="Polly.Joanna"
    )

    # Create WebSocket connection for outbound call
    connect = Connect()
    websocket_url = WEBHOOK_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    connect.stream(url=f'{websocket_url}/media-stream/{call_sid}')
    response.append(connect)

    # Start recording with transcription for this call too
    asyncio.create_task(start_call_recording_with_transcription(call_sid))

    return HTMLResponse(content=str(response), media_type="application/xml")

# --------------------------
# 4. Bridge Call Creation API
# --------------------------
@app.post("/bridge-call")
async def bridge_call(request: Request, from_number: str = Body(...), to_number: str = Body(...)):
    """Create a bridge call between two numbers"""
    try:
        # Create the first call (incoming simulation)
        call1 = client.calls.create(
            to=from_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{WEBHOOK_BASE_URL}/incoming-call"  # Use environment webhook URL
        )
        
        # Wait a moment, then create the second call
        await asyncio.sleep(2)
        
        call2 = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{WEBHOOK_BASE_URL}/outbound-call"  # Use environment webhook URL
        )
        
        # Store the bridge session
        bridge_id = f"bridge_{call1.sid}_{call2.sid}"
        active_calls[bridge_id] = {
            "call1_sid": call1.sid,
            "call2_sid": call2.sid,
            "from_number": from_number,
            "to_number": to_number,
            "status": "connecting"
        }
        
        return {
            "status": "success", 
            "bridge_id": bridge_id,
            "call1_sid": call1.sid, 
            "call2_sid": call2.sid
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --------------------------
# 5. Laptop Phone Call API (Direct calling from laptop)
# --------------------------
@app.post("/laptop-call")
async def laptop_call(to_number: str = Body(..., embed=True)):
    """Make a call from laptop (using Twilio number) to another phone"""
    global latest_laptop_call_sid
    try:
        # 🆕 GET CONTEXT FOR OUTBOUND CALLS
        crm_context = None
        if HUBSPOT_ENABLED:
            try:
                crm_context = await enhance_incoming_call_with_crm_context(to_number, "outbound_prep")
                print(f"📞 Calling {to_number}: {crm_context}")
            except Exception as e:
                print(f"⚠️ HubSpot context lookup failed for outbound call: {e}")
                crm_context = {"error": str(e)}
        
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{WEBHOOK_BASE_URL}/laptop-phone-handler"  # Special handler for laptop calls
        )
        
        # Store the latest laptop call SID for WebRTC connection
        latest_laptop_call_sid = call.sid
        
        # Store for HubSpot logging
        active_calls[call.sid] = {
            'outbound_number': to_number,
            'direction': 'outbound',
            'crm_context': crm_context,
            'start_time': datetime.now()
        }
        
        return {"status": "success", "call_sid": call.sid, "message": f"Calling {to_number} from your laptop"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --------------------------
# 5a. Get Latest Call SID for WebRTC
# --------------------------
@app.get("/get-latest-call-sid")
async def get_latest_call_sid():
    """Get the latest laptop call SID for WebRTC connection"""
    global latest_laptop_call_sid
    if latest_laptop_call_sid:
        return {"status": "success", "call_sid": latest_laptop_call_sid}
    else:
        return {"status": "error", "message": "No active laptop call found"}

# --------------------------
# 5b. Connection Status Endpoint
# --------------------------
@app.get("/connection-status")
async def get_connection_status():
    """Get current WebSocket connection status"""
    connections = {}
    for key, websocket in call_connections.items():
        try:
            # Check if connection is still alive
            connection_type = "laptop" if key.startswith("laptop_") else "phone"
            connections[key] = {
                "type": connection_type,
                "state": "connected" if websocket else "disconnected"
            }
        except:
            connections[key] = {
                "type": "unknown",
                "state": "error"
            }
    
    return {
        "total_connections": len(call_connections),
        "connections": connections,
        "latest_laptop_call": latest_laptop_call_sid
    }

# --------------------------
# 5c. Debug Test Endpoint  
# --------------------------
@app.get("/test-laptop-connection/{call_sid}")
async def test_laptop_connection(call_sid: str):
    """Test endpoint to check laptop connection for a specific call"""
    laptop_key = f"laptop_{call_sid}"
    phone_key = call_sid
    
    return {
        "call_sid": call_sid,
        "laptop_key": laptop_key,
        "phone_key": phone_key,
        "laptop_connected": laptop_key in call_connections,
        "phone_connected": phone_key in call_connections,
        "all_connections": list(call_connections.keys()),
        "laptop_connection_exists": call_connections.get(laptop_key) is not None,
        "phone_connection_exists": call_connections.get(phone_key) is not None
    }

# --------------------------
# 6. Laptop Phone Handler
# --------------------------
@app.api_route("/laptop-phone-handler", methods=["GET", "POST"])
async def laptop_phone_handler(request: Request):
    """Handle calls made from laptop to external numbers"""
    response = VoiceResponse()
    
    # Get call information
    form_data = await request.form()
    call_sid = form_data.get('CallSid', 'Unknown')
    to_number = form_data.get('To', 'Unknown')
    
    print(f"📱 Laptop phone call to {to_number}, SID: {call_sid}")
    
    response.say(
        f"Connecting your laptop call to {to_number}. Please wait.",
        voice="Polly.Joanna"
    )

    # Create WebSocket connection for laptop phone call - Use the SAME endpoint as regular calls
    connect = Connect()
    websocket_url = WEBHOOK_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    connect.stream(url=f'{websocket_url}/media-stream/{call_sid}')
    response.append(connect)

    # Start recording with transcription for this call
    asyncio.create_task(start_call_recording_with_transcription(call_sid))

    return HTMLResponse(content=str(response), media_type="application/xml")

# --------------------------
# 7. Make Direct Call API (Original)
# --------------------------
@app.post("/make-call")
async def make_call(to_number: str = Body(..., embed=True)):
    """Make a direct call to a number (standard bridge mode)"""
    try:
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{WEBHOOK_BASE_URL}/incoming-call"  # Use standard incoming handler
        )
        return {"status": "success", "call_sid": call.sid}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --------------------------
# 6. Web Dashboard
# --------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Human-to-Human Bridge Dashboard with Laptop Phone</title>
        <style>
            body { 
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                background: linear-gradient(135deg, #1a1a2e, #16213e); 
                color: #e0e0e0; 
                text-align: center; 
                padding: 50px; 
                min-height: 100vh;
                margin: 0;
            }
            h1 { 
                color: #ffffff; 
                text-shadow: 0 2px 4px rgba(0,0,0,0.5);
                margin-bottom: 40px;
            }
            h2 { 
                color: #64b5f6;
                margin-top: 30px;
            }
            .section { 
                margin: 30px 0; 
                padding: 25px; 
                background: rgba(30, 30, 50, 0.8); 
                border-radius: 15px; 
                box-shadow: 0 8px 32px rgba(0,0,0,0.3);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255,255,255,0.1);
            }
            input { 
                padding: 12px; 
                width: 250px; 
                margin: 10px; 
                font-size: 16px; 
                border: 2px solid #444; 
                border-radius: 8px;
                background: rgba(40, 40, 60, 0.8);
                color: #e0e0e0;
                transition: all 0.3s ease;
            }
            input:focus {
                outline: none;
                border-color: #64b5f6;
                box-shadow: 0 0 10px rgba(100, 181, 246, 0.3);
            }
            button { 
                padding: 12px 24px; 
                font-size: 16px; 
                background: linear-gradient(135deg, #4CAF50, #45a049); 
                color: white; 
                border: none; 
                cursor: pointer; 
                border-radius: 8px; 
                margin: 8px;
                transition: all 0.3s ease;
                box-shadow: 0 4px 15px rgba(76, 175, 80, 0.3);
            }
            button:hover { 
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(76, 175, 80, 0.4);
            }
            #status { 
                margin-top: 20px; 
                font-weight: bold; 
                padding: 10px;
                border-radius: 8px;
                background: rgba(40, 40, 60, 0.6);
            }
            .bridge-btn { 
                background: linear-gradient(135deg, #2196F3, #1976D2);
                box-shadow: 0 4px 15px rgba(33, 150, 243, 0.3);
            }
            .bridge-btn:hover { 
                box-shadow: 0 6px 20px rgba(33, 150, 243, 0.4);
            }
            .laptop-btn { 
                background: linear-gradient(135deg, #9C27B0, #7B1FA2);
                box-shadow: 0 4px 15px rgba(156, 39, 176, 0.3);
            }
            .laptop-btn:hover { 
                box-shadow: 0 6px 20px rgba(156, 39, 176, 0.4);
            }
            .transcription-btn { 
                background: linear-gradient(135deg, #FF9800, #F57C00);
                box-shadow: 0 4px 15px rgba(255, 152, 0, 0.3);
            }
            .transcription-btn:hover { 
                box-shadow: 0 6px 20px rgba(255, 152, 0, 0.4);
            }
            .refresh-btn { 
                background: linear-gradient(135deg, #9C27B0, #7B1FA2);
                box-shadow: 0 4px 15px rgba(156, 39, 176, 0.3);
            }
            .refresh-btn:hover { 
                box-shadow: 0 6px 20px rgba(156, 39, 176, 0.4);
            }
            .transcription-section { 
                text-align: left; 
                max-height: 400px; 
                overflow-y: auto;
                background: rgba(20, 20, 30, 0.5);
                border-radius: 10px;
                padding: 15px;
                margin-top: 15px;
            }
            .transcription-entry { 
                margin: 15px 0; 
                padding: 15px; 
                border-left: 4px solid #64b5f6; 
                background: rgba(40, 40, 60, 0.6);
                border-radius: 8px;
                transition: all 0.3s ease;
            }
            .transcription-entry:hover {
                background: rgba(50, 50, 70, 0.8);
                transform: translateX(5px);
            }
            .timestamp { 
                font-weight: bold; 
                color: #81c784;
                font-size: 0.9em;
            }
            .speaker { 
                font-weight: bold; 
                color: #64b5f6; 
            }
            .text { 
                margin-top: 8px; 
                color: #e0e0e0;
                line-height: 1.4;
            }
            /* Custom scrollbar for dark theme */
            ::-webkit-scrollbar {
                width: 8px;
            }
            ::-webkit-scrollbar-track {
                background: rgba(30, 30, 50, 0.5);
                border-radius: 4px;
            }
            ::-webkit-scrollbar-thumb {
                background: rgba(100, 181, 246, 0.6);
                border-radius: 4px;
            }
            ::-webkit-scrollbar-thumb:hover {
                background: rgba(100, 181, 246, 0.8);
            }
            /* Animation for smooth transitions */
            * {
                transition: color 0.3s ease, background-color 0.3s ease;
            }
        </style>
    </head>
    <body>
        <h1>📱 Human-to-Human Bridge Dashboard with Laptop Phone</h1>
        
        <div class="section">
            <h2>📱 Laptop Phone (Talk from your laptop)</h2>
            <p><strong>Use your laptop as a phone!</strong> Speak through microphone, hear through speakers.</p>
            <form id="laptopForm">
                <input type="text" id="laptop_to_number" name="laptop_to_number" placeholder="+1234567890" required />
                <br>
                <button type="submit" class="laptop-btn">📱 Call from Laptop</button>
            </form>
            <div id="laptop-instructions" style="margin-top: 10px; font-size: 14px; color: #b0b0b0;">
                After clicking, you'll need to open the WebRTC phone interface to talk.
                <br><a href="/laptop-phone" target="_blank" style="color: #64b5f6; text-decoration: none;">Open Laptop Phone Interface</a>
            </div>
        </div>
        
        <div class="section">
            <h2>📞 Direct Call</h2>
            <form id="callForm">
                <input type="text" id="to_number" name="to_number" placeholder="+1234567890" required />
                <br>
                <button type="submit">Make Direct Call</button>
            </form>
        </div>
        
        <div class="section">
            <h2>🌉 Bridge Two Numbers</h2>
            <form id="bridgeForm">
                <input type="text" id="from_number" name="from_number" placeholder="From: +1234567890" required />
                <br>
                <input type="text" id="to_number_bridge" name="to_number_bridge" placeholder="To: +0987654321" required />
                <br>
                <button type="submit" class="bridge-btn">Create Bridge Call</button>
            </form>
        </div>
        
        <div class="section">
            <h2>🎯 AssemblyAI Transcriptions</h2>
            <p><strong>Note:</strong> Transcriptions are generated using AssemblyAI after call completion. Make sure to set your ASSEMBLYAI_API_KEY in the .env file.</p>
            <button onclick="loadTranscriptions()" class="transcription-btn">🔄 Load All Transcriptions</button>
            <button onclick="refreshTranscriptions()" class="refresh-btn">Refresh</button>
            <input type="text" id="call_sid_input" placeholder="Enter Call SID for specific transcription" />
            <button onclick="loadSpecificTranscription()" class="transcription-btn">Load Specific Call</button>
            <button onclick="manualFetchTranscription()" class="transcription-btn">Manual Fetch</button>
            <br><br>
            <div id="transcriptions" class="transcription-section">
                <p>Transcriptions will appear here after calls are completed. Twilio processes transcriptions automatically.</p>
            </div>
        </div>
        
        <div class="section">
            <h2>🎙️ Call Recordings</h2>
            <p><strong>Note:</strong> Recordings are automatically downloaded and saved after call completion.</p>
            <button onclick="loadRecordings()" class="transcription-btn">Load All Recordings</button>
            <button onclick="refreshRecordings()" class="refresh-btn">Refresh</button>
            <input type="text" id="recording_call_sid_input" placeholder="Enter Call SID for specific recordings" />
            <button onclick="loadSpecificRecordings()" class="transcription-btn">Load Specific Call</button>
            <br><br>
            <div id="recordings" class="transcription-section">
                <p>Recordings will appear here after calls are completed and downloaded.</p>
            </div>
        </div>
        
        <p id="status"></p>

        <script>
            // Direct call form
            const callForm = document.getElementById("callForm");
            callForm.addEventListener("submit", async (e) => {
                e.preventDefault();
                const number = document.getElementById("to_number").value;
                document.getElementById("status").innerText = "📡 Making direct call...";
                try {
                    const res = await fetch("/make-call", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ to_number: number })
                    });
                    const data = await res.json();
                    if (data.status === "success") {
                        document.getElementById("status").innerText = "✅ Direct call initiated! SID: " + data.call_sid;
                    } else {
                        document.getElementById("status").innerText = "❌ Error: " + data.message;
                    }
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Request failed.";
                }
            });

            // Laptop phone form
            const laptopForm = document.getElementById("laptopForm");
            laptopForm.addEventListener("submit", async (e) => {
                e.preventDefault();
                const number = document.getElementById("laptop_to_number").value;
                document.getElementById("status").innerText = "📱 Initiating laptop phone call...";
                try {
                    const res = await fetch("/laptop-call", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ to_number: number })
                    });
                    const data = await res.json();
                    if (data.status === "success") {
                        document.getElementById("status").innerText = "✅ Laptop call initiated! SID: " + data.call_sid + " - Open laptop phone interface to talk!";
                        // Auto-open the laptop phone interface
                        window.open("/laptop-phone", "_blank");
                    } else {
                        document.getElementById("status").innerText = "❌ Error: " + data.message;
                    }
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Request failed.";
                }
            });

            // Bridge call form
            const bridgeForm = document.getElementById("bridgeForm");
            bridgeForm.addEventListener("submit", async (e) => {
                e.preventDefault();
                const fromNumber = document.getElementById("from_number").value;
                const toNumber = document.getElementById("to_number_bridge").value;
                document.getElementById("status").innerText = "🌉 Creating bridge call...";
                try {
                    const res = await fetch("/bridge-call", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ from_number: fromNumber, to_number: toNumber })
                    });
                    const data = await res.json();
                    if (data.status === "success") {
                        document.getElementById("status").innerText = "✅ Bridge call created! ID: " + data.bridge_id;
                    } else {
                        document.getElementById("status").innerText = "❌ Error: " + data.message;
                    }
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Request failed.";
                }
            });

            // Load all transcriptions
            async function loadTranscriptions() {
                document.getElementById("status").innerText = "📝 Loading transcriptions...";
                try {
                    const res = await fetch("/transcriptions");
                    const data = await res.json();
                    displayTranscriptions(data.transcriptions);
                    document.getElementById("status").innerText = `✅ Loaded transcriptions for ${data.total_calls} calls`;
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Failed to load transcriptions.";
                }
            }

            // Load specific call transcription
            async function loadSpecificTranscription() {
                const callSid = document.getElementById("call_sid_input").value;
                if (!callSid) {
                    document.getElementById("status").innerText = "⚠️ Please enter a Call SID";
                    return;
                }
                
                document.getElementById("status").innerText = "📝 Loading specific transcription...";
                try {
                    const res = await fetch(`/transcription/${callSid}`);
                    const data = await res.json();
                    if (data.status === "not_found") {
                        document.getElementById("status").innerText = "❌ No transcription found for this Call SID";
                    } else {
                        const transcriptions = {};
                        transcriptions[callSid] = data.transcription;
                        displayTranscriptions(transcriptions);
                        document.getElementById("status").innerText = `✅ Loaded ${data.total_entries} transcription entries`;
                    }
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Failed to load transcription.";
                }
            }

            // Manual fetch transcription from Twilio
            async function manualFetchTranscription() {
                const callSid = document.getElementById("call_sid_input").value;
                if (!callSid) {
                    document.getElementById("status").innerText = "⚠️ Please enter a Call SID";
                    return;
                }
                
                document.getElementById("status").innerText = "📝 Fetching transcription from Twilio...";
                try {
                    const res = await fetch(`/fetch-transcription/${callSid}`, {
                        method: "POST"
                    });
                    const data = await res.json();
                    if (data.status === "success") {
                        document.getElementById("status").innerText = `✅ Fetched ${data.count} transcriptions from Twilio`;
                        // Reload transcriptions to show the new data
                        loadTranscriptions();
                    } else {
                        document.getElementById("status").innerText = "❌ " + data.message;
                    }
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Failed to fetch transcription.";
                }
            }

            // Display transcriptions
            function displayTranscriptions(transcriptions) {
                const container = document.getElementById("transcriptions");
                container.innerHTML = "";
                
                if (Object.keys(transcriptions).length === 0) {
                    container.innerHTML = "<p>No transcriptions available yet. Transcriptions appear after call completion.</p>";
                    return;
                }
                
                for (const [callSid, entries] of Object.entries(transcriptions)) {
                    const callSection = document.createElement("div");
                    callSection.innerHTML = `<h3>Call: ${callSid}</h3>`;
                    
                    if (entries.length === 0) {
                        callSection.innerHTML += "<p>No transcription available for this call yet.</p>";
                    } else {
                        entries.forEach(entry => {
                            const entryDiv = document.createElement("div");
                            entryDiv.className = "transcription-entry";
                            
                            let statusInfo = entry.status ? ` (Status: ${entry.status})` : '';
                            let sourceInfo = entry.source ? ` [${entry.source}]` : '';
                            
                            entryDiv.innerHTML = `
                                <div class="timestamp">[${entry.timestamp}]${statusInfo}${sourceInfo}</div>
                                <div class="text">${entry.text}</div>
                                ${entry.recording_sid ? `<div style="font-size: 0.8em; color: #888;">Recording: ${entry.recording_sid}</div>` : ''}
                            `;
                            callSection.appendChild(entryDiv);
                        });
                    }
                    
                    container.appendChild(callSection);
                }
            }

            // Refresh transcriptions (reload current view)
            function refreshTranscriptions() {
                loadTranscriptions();
            }

            // Load all recordings
            async function loadRecordings() {
                document.getElementById("status").innerText = "🎙️ Loading recordings...";
                try {
                    const res = await fetch("/recordings");
                    const data = await res.json();
                    displayRecordings(data.recordings);
                    document.getElementById("status").innerText = `✅ Loaded recordings for ${data.total_calls} calls`;
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Failed to load recordings.";
                }
            }

            // Load specific call recordings
            async function loadSpecificRecordings() {
                const callSid = document.getElementById("recording_call_sid_input").value;
                if (!callSid) {
                    document.getElementById("status").innerText = "⚠️ Please enter a Call SID";
                    return;
                }
                
                document.getElementById("status").innerText = "🎙️ Loading specific recordings...";
                try {
                    const res = await fetch(`/recording/${callSid}`);
                    const data = await res.json();
                    if (data.status === "not_found") {
                        document.getElementById("status").innerText = "❌ No recordings found for this Call SID";
                    } else {
                        const recordings = {};
                        recordings[callSid] = data.recordings;
                        displayRecordings(recordings);
                        document.getElementById("status").innerText = `✅ Loaded ${data.total_recordings} recordings`;
                    }
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Failed to load recordings.";
                }
            }

            // Display recordings
            function displayRecordings(recordings) {
                const container = document.getElementById("recordings");
                container.innerHTML = "";
                
                if (Object.keys(recordings).length === 0) {
                    container.innerHTML = "<p>No recordings available yet. Recordings appear after call completion and download.</p>";
                    return;
                }
                
                for (const [callSid, recordingList] of Object.entries(recordings)) {
                    const callSection = document.createElement("div");
                    callSection.innerHTML = `<h3>Call: ${callSid}</h3>`;
                    
                    if (recordingList.length === 0) {
                        callSection.innerHTML += "<p>No recordings available for this call yet.</p>";
                    } else {
                        recordingList.forEach(recording => {
                            const recordingDiv = document.createElement("div");
                            recordingDiv.className = "transcription-entry";
                            
                            let statusBadge = recording.status === 'downloaded' ? '✅' : '⏳';
                            let downloadLink = recording.local_file ? 
                                `<a href="/download-recording/${callSid}/${recording.recording_sid}" download>📥 Download</a>` : 
                                `<button onclick="manualDownloadRecording('${callSid}', '${recording.recording_sid}')">📥 Download Now</button>`;
                            
                            let fileInfo = recording.file_size ? 
                                `<div style="font-size: 0.8em; color: #888;">Size: ${(recording.file_size / 1024 / 1024).toFixed(2)} MB | Duration: ${recording.duration}s</div>` : '';
                            
                            recordingDiv.innerHTML = `
                                <div class="timestamp">${statusBadge} Recording: ${recording.recording_sid}</div>
                                <div>Started: ${new Date(recording.started_at).toLocaleString()}</div>
                                <div>Channels: ${recording.channels} | Status: ${recording.status}</div>
                                ${fileInfo}
                                <div style="margin-top: 10px;">${downloadLink}</div>
                                <button onclick="deleteRecording('${callSid}', '${recording.recording_sid}')" style="background: #e74c3c; color: white; margin-left: 10px;">🗑️ Delete</button>
                            `;
                            callSection.appendChild(recordingDiv);
                        });
                    }
                    
                    container.appendChild(callSection);
                }
            }

            // Manual download recording
            async function manualDownloadRecording(callSid, recordingSid) {
                document.getElementById("status").innerText = "📥 Downloading recording...";
                try {
                    const res = await fetch(`/manual-download-recording/${callSid}/${recordingSid}`, {
                        method: "POST"
                    });
                    const data = await res.json();
                    if (data.status === "success") {
                        document.getElementById("status").innerText = `✅ Recording downloaded: ${data.file_path}`;
                        loadRecordings(); // Refresh the view
                    } else {
                        document.getElementById("status").innerText = "❌ " + data.message;
                    }
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Failed to download recording.";
                }
            }

            // Delete recording
            async function deleteRecording(callSid, recordingSid) {
                if (!confirm("Are you sure you want to delete this recording?")) {
                    return;
                }
                
                document.getElementById("status").innerText = "🗑️ Deleting recording...";
                try {
                    const res = await fetch(`/recording/${callSid}/${recordingSid}`, {
                        method: "DELETE"
                    });
                    const data = await res.json();
                    if (data.status === "success") {
                        document.getElementById("status").innerText = "✅ Recording deleted";
                        loadRecordings(); // Refresh the view
                    } else {
                        document.getElementById("status").innerText = "❌ " + data.message;
                    }
                } catch (err) {
                    document.getElementById("status").innerText = "⚠️ Failed to delete recording.";
                }
            }

            // Refresh recordings (reload current view)
            function refreshRecordings() {
                loadRecordings();
            }

            // Auto-refresh recordings every 15 seconds
            setInterval(refreshRecordings, 15000);

            // Auto-refresh transcriptions every 10 seconds
            setInterval(refreshTranscriptions, 10000);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# --------------------------
# 8. Laptop Phone Interface (WebRTC)
# --------------------------
@app.get("/laptop-phone", response_class=HTMLResponse)
async def laptop_phone_interface():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>📱 Laptop Phone Interface</title>
        <style>
            body { 
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                background: linear-gradient(135deg, #1a1a2e, #16213e); 
                color: #e0e0e0; 
                text-align: center; 
                padding: 50px; 
                min-height: 100vh;
                margin: 0;
            }
            .phone-container { 
                max-width: 450px; 
                margin: 0 auto; 
                background: rgba(30, 30, 50, 0.8); 
                padding: 40px; 
                border-radius: 20px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.3);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255,255,255,0.1);
            }
            h1 {
                color: #ffffff;
                text-shadow: 0 2px 4px rgba(0,0,0,0.5);
                margin-bottom: 30px;
            }
            .status { 
                margin: 20px 0; 
                font-size: 18px; 
                font-weight: bold;
                padding: 10px;
                border-radius: 8px;
                background: rgba(40, 40, 60, 0.6);
            }
            .connected { 
                color: #81c784; 
                background: rgba(76, 175, 80, 0.2);
            }
            .disconnected { 
                color: #f48fb1; 
                background: rgba(244, 67, 54, 0.2);
            }
            button { 
                padding: 15px 30px; 
                font-size: 16px; 
                margin: 10px; 
                border: none; 
                border-radius: 10px; 
                cursor: pointer;
                transition: all 0.3s ease;
                font-weight: 600;
            }
            button:hover {
                transform: translateY(-2px);
            }
            .connect-btn { 
                background: linear-gradient(135deg, #4CAF50, #45a049); 
                color: white;
                box-shadow: 0 4px 15px rgba(76, 175, 80, 0.3);
            }
            .connect-btn:hover {
                box-shadow: 0 6px 20px rgba(76, 175, 80, 0.4);
            }
            .disconnect-btn { 
                background: linear-gradient(135deg, #f44336, #d32f2f); 
                color: white;
                box-shadow: 0 4px 15px rgba(244, 67, 54, 0.3);
            }
            .disconnect-btn:hover {
                box-shadow: 0 6px 20px rgba(244, 67, 54, 0.4);
            }
            .mute-btn { 
                background: linear-gradient(135deg, #FF9800, #F57C00); 
                color: white;
                box-shadow: 0 4px 15px rgba(255, 152, 0, 0.3);
            }
            .mute-btn:hover {
                box-shadow: 0 6px 20px rgba(255, 152, 0, 0.4);
            }
            button:disabled {
                opacity: 0.5;
                cursor: not-allowed;
                transform: none !important;
                box-shadow: none !important;
            }
            #audio-controls { 
                margin: 25px 0; 
            }
            .volume-control { 
                margin: 20px 0; 
                padding: 15px;
                background: rgba(40, 40, 60, 0.6);
                border-radius: 10px;
            }
            .volume-control label {
                color: #64b5f6;
                font-weight: 600;
            }
            input[type="range"] {
                width: 200px;
                margin: 10px;
                background: transparent;
                -webkit-appearance: none;
            }
            input[type="range"]::-webkit-slider-track {
                background: rgba(100, 181, 246, 0.3);
                height: 6px;
                border-radius: 3px;
            }
            input[type="range"]::-webkit-slider-thumb {
                -webkit-appearance: none;
                background: #64b5f6;
                height: 20px;
                width: 20px;
                border-radius: 50%;
                cursor: pointer;
                box-shadow: 0 2px 6px rgba(100, 181, 246, 0.4);
            }
            .info-text {
                margin-top: 25px; 
                font-size: 14px; 
                color: #b0b0b0;
                background: rgba(40, 40, 60, 0.4);
                padding: 15px;
                border-radius: 10px;
                border-left: 4px solid #64b5f6;
            }
            .info-text p {
                margin: 8px 0;
            }
        </style>
    </head>
    <body>
        <div class="phone-container">
            <h1>📱 Laptop Phone</h1>
            <div id="status" class="status disconnected">Disconnected</div>
            
            <div id="audio-controls">
                <button id="connectBtn" class="connect-btn" onclick="connectWebSocket()">🔌 Connect to Call</button>
                <button id="disconnectBtn" class="disconnect-btn" onclick="disconnectWebSocket()" disabled>📞 Hang Up</button>
                <br>
                <button id="muteBtn" class="mute-btn" onclick="toggleMute()" disabled>🎤 Mute</button>
            </div>
            
            <div class="volume-control">
                <label>🔊 Volume: </label>
                <input type="range" id="volumeSlider" min="0" max="100" value="50" onchange="setVolume(this.value)">
            </div>
            
            <audio id="remoteAudio" autoplay></audio>
            
            <div class="info-text">
                <p>🎤 Make sure your microphone is enabled</p>
                <p>🔊 Audio will play through your speakers</p>
                <p>📞 This connects you to active Twilio calls</p>
            </div>
        </div>

        <script>
            let websocket = null;
            let localStream = null;
            let audioContext = null;
            let isMuted = false;
            let currentCallSid = null;
            let audioProcessor = null;
            
            // Get the latest call SID from the server
            async function getLatestCallSid() {
                try {
                    const response = await fetch('/get-latest-call-sid');
                    const data = await response.json();
                    return data.call_sid;
                } catch (err) {
                    console.error("Error getting latest call SID:", err);
                    return null;
                }
            }
            
            async function connectWebSocket() {
                try {
                    document.getElementById("status").textContent = "Getting call information...";
                    document.getElementById("status").className = "status";
                    
                    // Get the latest call SID
                    currentCallSid = await getLatestCallSid();
                    if (!currentCallSid) {
                        document.getElementById("status").textContent = "No active call found. Please make a laptop call first.";
                        document.getElementById("status").className = "status disconnected";
                        return;
                    }
                    
                    document.getElementById("status").textContent = "Connecting to call " + currentCallSid + "...";
                    
                    // Get microphone access with professional audio constraints
                    localStream = await navigator.mediaDevices.getUserMedia({ 
                        audio: {
                            sampleRate: 8000,  // Higher sample rate for better quality
                            channelCount: 1,
                            echoCancellation: true,
                            noiseSuppression: true,
                            autoGainControl: true,
                            googEchoCancellation: true,
                            googAutoGainControl: true,
                            googNoiseSuppression: true,
                            googHighpassFilter: true,
                            googTypingNoiseDetection: true,
                            googAudioMirroring: false,
                            latency: 0.01,  // Low latency for real-time
                            volume: 1.0
                        } 
                    });
                    console.log("Microphone access granted");
                    
                    // Create audio context for processing with higher sample rate
                    audioContext = new (window.AudioContext || window.webkitAudioContext)({
                        sampleRate: 16000,  // Higher sample rate for better quality
                        latencyHint: 'interactive'
                    });
                    
                    // Connect to WebSocket with actual call SID - Use SEPARATE endpoint for laptop
                    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                    const wsUrl = `${protocol}//${window.location.host}/laptop-audio/${currentCallSid}`;
                    
                    websocket = new WebSocket(wsUrl);
                    
                    websocket.onopen = function() {
                        document.getElementById("status").textContent = "Connected to Call " + currentCallSid;
                        document.getElementById("status").className = "status connected";
                        document.getElementById("connectBtn").disabled = true;
                        document.getElementById("disconnectBtn").disabled = false;
                        document.getElementById("muteBtn").disabled = false;
                        
                        // Send start message
                        const startMessage = {
                            event: 'start',
                            start: {
                                streamSid: 'laptop_' + currentCallSid
                            }
                        };
                        websocket.send(JSON.stringify(startMessage));
                        
                        // Start audio processing
                        processAudio();
                    };
                    
                    websocket.onmessage = function(event) {
                        try {
                            const data = JSON.parse(event.data);
                            if (data.event === 'media' && data.media && data.media.payload) {
                                // Decode and play incoming audio from the phone call
                                playIncomingAudio(data.media.payload);
                            }
                        } catch (err) {
                            console.error("Error processing incoming message:", err);
                        }
                    };
                    
                    websocket.onerror = function(error) {
                        console.error("WebSocket error:", error);
                        document.getElementById("status").textContent = "Connection Error";
                        document.getElementById("status").className = "status disconnected";
                    };
                    
                    websocket.onclose = function() {
                        document.getElementById("status").textContent = "Disconnected";
                        document.getElementById("status").className = "status disconnected";
                        document.getElementById("connectBtn").disabled = false;
                        document.getElementById("disconnectBtn").disabled = true;
                        document.getElementById("muteBtn").disabled = true;
                    };
                    
                } catch (err) {
                    console.error("Error connecting:", err);
                    document.getElementById("status").textContent = "Connection Failed: " + err.message;
                    document.getElementById("status").className = "status disconnected";
                }
            }
            
            function disconnectWebSocket() {
                if (websocket) {
                    const stopMessage = {
                        event: 'stop'
                    };
                    websocket.send(JSON.stringify(stopMessage));
                    websocket.close();
                }
                if (localStream) {
                    localStream.getTracks().forEach(track => track.stop());
                }
                if (audioProcessor) {
                    audioProcessor.disconnect();
                }
                if (audioContext) {
                    audioContext.close();
                }
            }
            
            function toggleMute() {
                isMuted = !isMuted;
                if (localStream) {
                    localStream.getAudioTracks()[0].enabled = !isMuted;
                }
                document.getElementById("muteBtn").textContent = isMuted ? "🔇 Unmute" : "🎤 Mute";
            }
            
            function setVolume(value) {
                const audio = document.getElementById("remoteAudio");
                audio.volume = value / 100;
            }
            
            function processAudio() {
                if (!audioContext || !localStream || !websocket) return;
                
                try {
                    const source = audioContext.createMediaStreamSource(localStream);
                    
                    // Create advanced audio processing chain
                    const highPassFilter = audioContext.createBiquadFilter();
                    const lowPassFilter = audioContext.createBiquadFilter();
                    const compressor = audioContext.createDynamicsCompressor();
                    const gainNode = audioContext.createGain();
                    const limiter = audioContext.createDynamicsCompressor();
                    
                    // High-pass filter to remove low-frequency noise
                    highPassFilter.type = 'highpass';
                    highPassFilter.frequency.setValueAtTime(85, audioContext.currentTime);  // Remove rumble
                    highPassFilter.Q.setValueAtTime(0.7, audioContext.currentTime);
                    
                    // Low-pass filter to prevent aliasing
                    lowPassFilter.type = 'lowpass';
                    lowPassFilter.frequency.setValueAtTime(3400, audioContext.currentTime);  // Voice range
                    lowPassFilter.Q.setValueAtTime(0.7, audioContext.currentTime);
                    
                    // Professional compressor settings for voice
                    compressor.threshold.setValueAtTime(-20, audioContext.currentTime);
                    compressor.knee.setValueAtTime(25, audioContext.currentTime);
                    compressor.ratio.setValueAtTime(6, audioContext.currentTime);
                    compressor.attack.setValueAtTime(0.002, audioContext.currentTime);
                    compressor.release.setValueAtTime(0.08, audioContext.currentTime);
                    
                    // Limiter to prevent clipping
                    limiter.threshold.setValueAtTime(-3, audioContext.currentTime);
                    limiter.knee.setValueAtTime(0, audioContext.currentTime);
                    limiter.ratio.setValueAtTime(20, audioContext.currentTime);
                    limiter.attack.setValueAtTime(0.001, audioContext.currentTime);
                    limiter.release.setValueAtTime(0.01, audioContext.currentTime);
                    
                    // Set optimal gain
                    gainNode.gain.setValueAtTime(2.0, audioContext.currentTime);
                    
                    // Use smaller buffer for lower latency and better quality
                    audioProcessor = audioContext.createScriptProcessor(512, 1, 1);
                    
                    // Advanced resampling for Twilio compatibility
                    let resampleBuffer = [];
                    let resampleIndex = 0;
                    
                    audioProcessor.onaudioprocess = function(event) {
                        if (isMuted || websocket.readyState !== WebSocket.OPEN) return;
                        
                        const inputBuffer = event.inputBuffer;
                        const inputData = inputBuffer.getChannelData(0);
                        
                        // Advanced audio processing pipeline
                        const processedData = advancedAudioProcessing(inputData);
                        
                        // High-quality resampling from 16kHz to 8kHz for Twilio
                        const resampledData = resample16to8kHz(processedData);
                        
                        // Convert to 16-bit PCM with dithering
                        const pcmData = new Int16Array(resampledData.length);
                        for (let i = 0; i < resampledData.length; i++) {
                            // Add triangular dithering for better quality at low bit depths
                            const dither = (Math.random() - Math.random()) * 0.5;
                            const sample = resampledData[i] * 32767 + dither;
                            pcmData[i] = Math.max(-32768, Math.min(32767, Math.round(sample)));
                        }
                        
                        // Convert to μ-law with enhanced algorithm
                        const ulawData = pcmToUlawEnhanced(pcmData);
                        const base64Audio = btoa(String.fromCharCode.apply(null, ulawData));
                        
                        // Send to WebSocket with quality metadata
                        const mediaMessage = {
                            event: 'media',
                            media: {
                                payload: base64Audio,
                                timestamp: Date.now(),
                                sampleRate: 8000,
                                encoding: 'MULAW'
                            }
                        };
                        
                        websocket.send(JSON.stringify(mediaMessage));
                    };
                    
                    // Connect advanced audio chain
                    source.connect(highPassFilter);
                    highPassFilter.connect(lowPassFilter);
                    lowPassFilter.connect(compressor);
                    compressor.connect(gainNode);
                    gainNode.connect(limiter);
                    limiter.connect(audioProcessor);
                    audioProcessor.connect(audioContext.destination);
                    
                    console.log("Professional audio processing started with 16kHz input");
                } catch (err) {
                    console.error("Error setting up audio processing:", err);
                }
            }
            
            // Advanced audio processing with multiple algorithms
            function advancedAudioProcessing(inputData) {
                const processed = new Float32Array(inputData.length);
                
                // Apply spectral gate for noise reduction
                for (let i = 0; i < inputData.length; i++) {
                    let sample = inputData[i];
                    
                    // Adaptive noise gate based on signal energy
                    const energy = Math.abs(sample);
                    if (energy < 0.008) {
                        sample = sample * 0.05;  // Aggressive noise reduction
                    } else if (energy < 0.02) {
                        sample = sample * 0.3;   // Moderate noise reduction
                    }
                    
                    // Voice enhancement - boost mid frequencies
                    if (i > 2 && i < inputData.length - 2) {
                        // Simple voice enhancement filter
                        const midFreq = (inputData[i-2] + inputData[i-1] + sample + inputData[i+1] + inputData[i+2]) / 5;
                        const highFreq = sample - midFreq;
                        sample = midFreq + highFreq * 1.3;  // Boost clarity
                    }
                    
                    // Soft limiting to prevent distortion
                    if (Math.abs(sample) > 0.85) {
                        const sign = sample > 0 ? 1 : -1;
                        sample = sign * (0.85 + (Math.abs(sample) - 0.85) * 0.2);
                    }
                    
                    processed[i] = sample;
                }
                
                return processed;
            }
            
            // High-quality resampling from 16kHz to 8kHz
            function resample16to8kHz(inputData) {
                const outputLength = Math.floor(inputData.length / 2);
                const output = new Float32Array(outputLength);
                
                // Use linear interpolation with anti-aliasing
                for (let i = 0; i < outputLength; i++) {
                    const srcIndex = i * 2;
                    if (srcIndex + 1 < inputData.length) {
                        // Anti-aliasing filter (simple averaging)
                        output[i] = (inputData[srcIndex] + inputData[srcIndex + 1]) * 0.5;
                    } else {
                        output[i] = inputData[srcIndex];
                    }
                }
                
                return output;
            }
            
            // Enhanced μ-law encoding with better quality
            function pcmToUlawEnhanced(pcmData) {
                const ulawData = new Uint8Array(pcmData.length);
                for (let i = 0; i < pcmData.length; i++) {
                    ulawData[i] = linearToUlawEnhanced(pcmData[i]);
                }
                return ulawData;
            }
            
            function linearToUlawEnhanced(sample) {
                const BIAS = 0x84;
                const CLIP = 32635;
                const sign = (sample >> 8) & 0x80;
                
                if (sign !== 0) sample = -sample;
                if (sample > CLIP) sample = CLIP;
                
                sample = sample + BIAS;
                let exp = 7;
                let expMask = 0x4000;
                
                for (let i = 0; i < 7; i++) {
                    if ((sample & expMask) !== 0) break;
                    exp--;
                    expMask >>= 1;
                }
                
                const mantissa = (sample >> (exp + 3)) & 0x0F;
                const ulaw = ~(sign | (exp << 4) | mantissa);
                return ulaw & 0xFF;
            }
            
            function playIncomingAudio(base64Audio) {
                try {
                    // Decode base64 to μ-law data
                    const ulawData = new Uint8Array(atob(base64Audio).split('').map(c => c.charCodeAt(0)));
                    
                    // Convert μ-law to PCM with enhanced quality
                    const pcmData = ulawToPcmEnhanced(ulawData);
                    
                    // Apply professional audio enhancement
                    const enhancedPcm = professionalAudioEnhancement(pcmData);
                    
                    // Upsample to higher quality for playback
                    const upsampledData = upsample8to16kHz(enhancedPcm);
                    
                    // Create high-quality audio buffer
                    const audioBuffer = audioContext.createBuffer(1, upsampledData.length, 16000);
                    const channelData = audioBuffer.getChannelData(0);
                    
                    // Apply advanced normalization and dynamics processing
                    let rms = 0;
                    for (let i = 0; i < upsampledData.length; i++) {
                        const normalizedValue = upsampledData[i] / 32768.0;
                        rms += normalizedValue * normalizedValue;
                        channelData[i] = normalizedValue;
                    }
                    rms = Math.sqrt(rms / upsampledData.length);
                    
                    // Professional audio chain for playback
                    const source = audioContext.createBufferSource();
                    const preGain = audioContext.createGain();
                    const eqLow = audioContext.createBiquadFilter();
                    const eqMid = audioContext.createBiquadFilter();
                    const eqHigh = audioContext.createBiquadFilter();
                    const compressor = audioContext.createDynamicsCompressor();
                    const limiter = audioContext.createDynamicsCompressor();
                    const finalGain = audioContext.createGain();
                    
                    // Pre-gain adjustment
                    const targetRMS = 0.1;
                    const gainAdjustment = rms > 0 ? Math.min(3.0, targetRMS / rms) : 1.0;
                    preGain.gain.setValueAtTime(gainAdjustment, audioContext.currentTime);
                    
                    // 3-band EQ for voice enhancement
                    // Low-cut filter
                    eqLow.type = 'highpass';
                    eqLow.frequency.setValueAtTime(80, audioContext.currentTime);
                    eqLow.Q.setValueAtTime(0.7, audioContext.currentTime);
                    
                    // Mid-boost for voice clarity
                    eqMid.type = 'peaking';
                    eqMid.frequency.setValueAtTime(1200, audioContext.currentTime);
                    eqMid.Q.setValueAtTime(1.2, audioContext.currentTime);
                    eqMid.gain.setValueAtTime(3, audioContext.currentTime);
                    
                    // High-shelf for presence
                    eqHigh.type = 'highshelf';
                    eqHigh.frequency.setValueAtTime(3000, audioContext.currentTime);
                    eqHigh.gain.setValueAtTime(2, audioContext.currentTime);
                    
                    // Compressor for consistent levels
                    compressor.threshold.setValueAtTime(-18, audioContext.currentTime);
                    compressor.knee.setValueAtTime(20, audioContext.currentTime);
                    compressor.ratio.setValueAtTime(4, audioContext.currentTime);
                    compressor.attack.setValueAtTime(0.005, audioContext.currentTime);
                    compressor.release.setValueAtTime(0.1, audioContext.currentTime);
                    
                    // Limiter to prevent clipping
                    limiter.threshold.setValueAtTime(-1, audioContext.currentTime);
                    limiter.knee.setValueAtTime(0, audioContext.currentTime);
                    limiter.ratio.setValueAtTime(20, audioContext.currentTime);
                    limiter.attack.setValueAtTime(0.001, audioContext.currentTime);
                    limiter.release.setValueAtTime(0.01, audioContext.currentTime);
                    
                    // Final output gain
                    finalGain.gain.setValueAtTime(1.4, audioContext.currentTime);
                    
                    // Connect professional audio chain
                    source.buffer = audioBuffer;
                    source.connect(preGain);
                    preGain.connect(eqLow);
                    eqLow.connect(eqMid);
                    eqMid.connect(eqHigh);
                    eqHigh.connect(compressor);
                    compressor.connect(limiter);
                    limiter.connect(finalGain);
                    finalGain.connect(audioContext.destination);
                    
                    source.start();
                } catch (err) {
                    console.error("Error playing incoming audio:", err);
                }
            }
            
            // Enhanced μ-law to PCM conversion
            function ulawToPcmEnhanced(ulawData) {
                const pcmData = new Int16Array(ulawData.length);
                for (let i = 0; i < ulawData.length; i++) {
                    pcmData[i] = ulawToLinearEnhanced(ulawData[i]);
                }
                return pcmData;
            }
            
            function ulawToLinearEnhanced(ulaw) {
                const BIAS = 0x84;
                ulaw = ~ulaw;
                const sign = (ulaw & 0x80);
                const exponent = (ulaw >> 4) & 0x07;
                const mantissa = ulaw & 0x0F;
                let sample = mantissa << (exponent + 3);
                sample += BIAS;
                if (exponent !== 0) sample += (1 << (exponent + 2));
                return sign !== 0 ? -sample : sample;
            }
            
            // Professional audio enhancement algorithms
            function professionalAudioEnhancement(pcmData) {
                const enhanced = new Int16Array(pcmData.length);
                
                // Multi-stage enhancement pipeline
                for (let i = 0; i < pcmData.length; i++) {
                    let sample = pcmData[i];
                    
                    // Stage 1: Noise reduction with spectral subtraction
                    const energy = Math.abs(sample);
                    if (energy < 800) {
                        sample = sample * 0.1;  // Aggressive noise gate
                    } else if (energy < 1500) {
                        sample = sample * 0.4;  // Moderate noise reduction
                    }
                    
                    // Stage 2: Dynamic range expansion for low-level signals
                    if (Math.abs(sample) < 8000) {
                        const expansion = Math.pow(Math.abs(sample) / 8000, 0.8);
                        sample = sample > 0 ? expansion * 8000 : -expansion * 8000;
                    }
                    
                    // Stage 3: Harmonic enhancement for voice
                    if (i > 4 && i < pcmData.length - 4) {
                        // Calculate local derivatives for harmonic content
                        const d1 = pcmData[i] - pcmData[i-1];
                        const d2 = pcmData[i+1] - pcmData[i];
                        const harmonic = (d1 + d2) * 0.15;
                        sample += harmonic;
                    }
                    
                    // Stage 4: Soft compression for natural dynamics
                    if (Math.abs(sample) > 20000) {
                        const ratio = 0.3;
                        const excess = Math.abs(sample) - 20000;
                        const compressed = 20000 + excess * ratio;
                        sample = sample > 0 ? compressed : -compressed;
                    }
                    
                    enhanced[i] = Math.max(-32768, Math.min(32767, sample));
                }
                
                return enhanced;
            }
            
            // High-quality upsampling from 8kHz to 16kHz
            function upsample8to16kHz(inputData) {
                const outputLength = inputData.length * 2;
                const output = new Int16Array(outputLength);
                
                // Use cubic interpolation for high-quality upsampling
                for (let i = 0; i < inputData.length; i++) {
                    output[i * 2] = inputData[i];
                    
                    if (i < inputData.length - 1) {
                        // Cubic interpolation between samples
                        const x0 = i > 0 ? inputData[i-1] : inputData[i];
                        const x1 = inputData[i];
                        const x2 = inputData[i+1];
                        const x3 = i < inputData.length - 2 ? inputData[i+2] : inputData[i+1];
                        
                        const a0 = x3 - x2 - x0 + x1;
                        const a1 = x0 - x1 - a0;
                        const a2 = x2 - x0;
                        const a3 = x1;
                        
                        const t = 0.5;  // Midpoint interpolation
                        const interpolated = a0*t*t*t + a1*t*t + a2*t + a3;
                        output[i * 2 + 1] = Math.max(-32768, Math.min(32767, Math.round(interpolated)));
                    } else {
                        output[i * 2 + 1] = inputData[i];
                    }
                }
                
                return output;
            }
            
            // μ-law conversion functions
            function pcmToUlaw(pcmData) {
                const ulawData = new Uint8Array(pcmData.length);
                for (let i = 0; i < pcmData.length; i++) {
                    ulawData[i] = linearToUlaw(pcmData[i]);
                }
                return ulawData;
            }
            
            function ulawToPcm(ulawData) {
                const pcmData = new Int16Array(ulawData.length);
                for (let i = 0; i < ulawData.length; i++) {
                    pcmData[i] = ulawToLinear(ulawData[i]);
                }
                return pcmData;
            }
            
            function linearToUlaw(sample) {
                const sign = (sample >> 8) & 0x80;
                if (sign !== 0) sample = -sample;
                if (sample > 32635) sample = 32635;
                
                let exp = 7;
                let expMask = 0x4000;
                for (let i = 0; i < 7; i++) {
                    if ((sample & expMask) !== 0) break;
                    exp--;
                    expMask >>= 1;
                }
                
                const mantissa = (sample >> (exp + 3)) & 0x0F;
                const ulaw = ~(sign | (exp << 4) | mantissa);
                return ulaw & 0xFF;
            }
            
            function ulawToLinear(ulaw) {
                ulaw = ~ulaw;
                const sign = (ulaw & 0x80);
                const exp = (ulaw >> 4) & 0x07;
                const mantissa = ulaw & 0x0F;
                
                let sample = mantissa << (exp + 3);
                if (exp !== 0) sample += (0x84 << exp);
                if (sign !== 0) sample = -sample;
                
                return sample;
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# --------------------------
# 9. Laptop Audio WebSocket (Separate from phone)
# --------------------------
@app.websocket("/laptop-audio/{call_sid}")
async def laptop_audio_stream(websocket: WebSocket, call_sid: str):
    print(f"📱 LAPTOP TRYING TO CONNECT for call: {call_sid}")
    print(f"📱 Current connections before laptop connect: {list(call_connections.keys())}")
    await websocket.accept()
    print(f"📱 LAPTOP WEBSOCKET ACCEPTED for call: {call_sid}")

    # Store laptop connection separately
    laptop_key = f"laptop_{call_sid}"
    call_connections[laptop_key] = websocket
    print(f"📱 Laptop connection stored as: {laptop_key}")
    print(f"📱 All connections after laptop connect: {list(call_connections.keys())}")
    
    try:
        async for message in websocket.iter_text():
            data = json.loads(message)

            if data.get('event') == 'start':
                stream_sid = data['start']['streamSid']
                print(f"📱 Laptop audio stream started: {stream_sid} for call: {call_sid}")

            elif data.get('event') == 'media':
                # Get the audio payload from laptop microphone
                payload_b64 = data['media']['payload']
                timestamp = int(data['media'].get('timestamp', 0))
                
                print(f"📱 Received audio from laptop for call {call_sid}, payload length: {len(payload_b64)}")
                print(f"📱 Raw payload preview: {payload_b64[:50]}...")
                
                # Convert audio format if needed (WebRTC usually sends PCM, Twilio expects μ-law)
                converted_payload = await convert_audio_for_twilio(payload_b64)
                
                # Forward laptop audio to the phone's Twilio stream
                await send_laptop_audio_to_twilio(call_sid, converted_payload)

            elif data.get('event') == 'stop':
                print(f"📱 Laptop audio stream stopped for call: {call_sid}")
                break

    except WebSocketDisconnect:
        print(f"📱 Laptop audio WebSocket disconnected for call: {call_sid}")
    except Exception as e:
        print(f"📱 Error in laptop audio WebSocket for call {call_sid}: {e}")
    finally:
        # Clean up laptop connection
        if laptop_key in call_connections:
            del call_connections[laptop_key]
        print(f"📱 Cleaned up laptop audio connection for call: {call_sid}")

async def send_laptop_audio_to_twilio(call_sid: str, audio_payload: str):
    """Send laptop audio to Twilio's phone stream"""
    # Debug: Show all available connections
    print(f"📱 TRYING TO SEND LAPTOP AUDIO for call: {call_sid}")
    print(f"📱 Available connections: {list(call_connections.keys())}")
    print(f"📱 Available stream SIDs: {stream_sids}")
    print(f"📱 Looking for phone connection key: {call_sid}")
    print(f"📱 Audio payload length: {len(audio_payload)} characters")
    
    # Find the phone's WebSocket connection to Twilio
    if call_sid in call_connections and call_sid in stream_sids:
        try:
            phone_websocket = call_connections[call_sid]
            actual_stream_sid = stream_sids[call_sid]
            print(f"📱 FOUND phone WebSocket for {call_sid}")
            print(f"📱 Using stream SID: {actual_stream_sid}")
            
            # CRITICAL: Use the actual stream SID from Twilio
            media_message = {
                "event": "media",
                "streamSid": actual_stream_sid,  # Use the real stream SID!
                "media": {
                    "payload": audio_payload,
                    "timestamp": str(int(time.time() * 1000))
                }
            }
            
            print(f"📱 Sending media message with stream SID: {actual_stream_sid}")
            await phone_websocket.send_text(json.dumps(media_message))
            print(f"📱 ✅ SUCCESSFULLY sent laptop audio to Twilio for call {call_sid}")
            print(f"📱 Message size: {len(json.dumps(media_message))} bytes")
        except WebSocketDisconnect:
            print(f"📱 ℹ️ WebSocket disconnected for call {call_sid} (normal when call ends)")
        except Exception as e:
            # Only show full traceback for unexpected errors
            if "ConnectionClosedError" not in str(e) and "ClientDisconnected" not in str(e):
                print(f"❌ Error sending laptop audio to Twilio {call_sid}: {e}")
                import traceback
                traceback.print_exc()
            else:
                print(f"📱 ℹ️ Connection closed for call {call_sid} (normal when call ends)")
    else:
        print(f"📱 ❌ NO TWILIO CONNECTION OR STREAM SID FOUND for call {call_sid}")
        print(f"📱 Available connections are: {list(call_connections.keys())}")
        print(f"📱 Available stream SIDs: {list(stream_sids.keys())}")
        print(f"📱 Expected connection key: {call_sid}")

async def convert_audio_for_twilio(audio_payload_b64: str) -> str:
    """Convert audio payload to format compatible with Twilio"""
    try:
        # Decode base64 to check the audio data
        audio_bytes = base64.b64decode(audio_payload_b64)
        print(f"🔄 Converting audio payload, length: {len(audio_payload_b64)} base64 chars")
        print(f"🔄 Decoded audio bytes length: {len(audio_bytes)}")
        print(f"🔄 First few audio bytes: {audio_bytes[:10].hex() if len(audio_bytes) >= 10 else 'N/A'}")
        
        # The audio should already be μ-law encoded from the browser
        # Return as-is for now
        return audio_payload_b64
        
    except Exception as e:
        print(f"❌ Error converting audio: {e}")
        return audio_payload_b64  # Return original if conversion fails

async def send_phone_audio_to_laptop(call_sid: str, audio_payload: str):
    """Send phone audio to laptop speakers"""
    laptop_key = f"laptop_{call_sid}"
    if laptop_key in call_connections:
        try:
            laptop_websocket = call_connections[laptop_key]
            media_message = {
                "event": "media",
                "media": {
                    "payload": audio_payload
                }
            }
            await laptop_websocket.send_text(json.dumps(media_message))
            print(f"📱 Sent phone audio to laptop for call {call_sid}")
        except Exception as e:
            print(f"❌ Error sending phone audio to laptop {call_sid}: {e}")
    else:
        print(f"📱 No laptop connection found for call {call_sid}")

# --------------------------
# 10. Audio Bridge Helper Functions
# --------------------------
@app.websocket("/media-stream/{call_sid}")
async def handle_media_stream(websocket: WebSocket, call_sid: str):
    print(f"🔗 Phone WebSocket connected for call: {call_sid}")
    print(f"🔗 All connections before adding: {list(call_connections.keys())}")
    await websocket.accept()

    # This endpoint is only for phone connections (Twilio streams)
    call_connections[call_sid] = websocket
    print(f"🔗 All connections after adding: {list(call_connections.keys())}")
    
    # Initialize transcription storage for this call (will be populated by webhook)
    if call_sid not in call_transcriptions:
        call_transcriptions[call_sid] = []
    
    stream_sid = None

    try:
        async for message in websocket.iter_text():
            data = json.loads(message)

            if data.get('event') == 'start':
                stream_sid = data['start']['streamSid']
                print(f"🎵 Phone audio stream started: {stream_sid} for call: {call_sid}")
                print(f"📝 Twilio will automatically record and transcribe this call")
                
                # CRITICAL: Store the stream SID for this call
                stream_sids[call_sid] = stream_sid
                print(f"🔗 Stored stream SID {stream_sid} for call {call_sid}")

            elif data.get('event') == 'media':
                # Get the audio payload from phone call
                payload_b64 = data['media']['payload']
                timestamp = int(data['media'].get('timestamp', 0))
                
                print(f"🎵 Received audio from phone call {call_sid}, payload length: {len(payload_b64)}")
                
                # Forward phone audio to laptop (if laptop is connected)
                await send_phone_audio_to_laptop(call_sid, payload_b64)
                # Also forward to other calls for bridge functionality
                await forward_audio_to_bridge(call_sid, payload_b64, stream_sid)

            elif data.get('event') == 'stop':
                print(f"🛑 Phone audio stream stopped for call: {call_sid}")
                print(f"📝 Transcription will be available after call completion")
                
                # Fetch transcription after call ends (with a slight delay)
                asyncio.create_task(delayed_transcription_fetch(call_sid))
                break

    except WebSocketDisconnect:
        print(f"❌ Phone WebSocket disconnected for call: {call_sid}")
    except Exception as e:
        print(f"❌ Error in phone WebSocket for call {call_sid}: {e}")
    finally:
        # Clean up phone connection and stream SID
        if call_sid in call_connections:
            del call_connections[call_sid]
        if call_sid in stream_sids:
            del stream_sids[call_sid]
            print(f"🧹 Cleaned up stream SID for call: {call_sid}")
        
        print(f"🧹 Cleaned up phone connection for call: {call_sid}")

async def delayed_transcription_fetch(call_sid: str):
    """Fetch transcription after a delay to ensure call processing is complete"""
    print(f"📝 Waiting 30 seconds before fetching transcription for {call_sid}")
    await asyncio.sleep(30)  # Wait 30 seconds for call and recording to be processed
    print(f"📝 Now attempting to fetch/create transcription for {call_sid}")
    transcriptions = await fetch_twilio_transcription(call_sid)
    
    # 🆕 LOG TO HUBSPOT AFTER TRANSCRIPTION
    if HUBSPOT_ENABLED and transcriptions and call_sid in active_calls:
        try:
            call_data = active_calls[call_sid]
            caller_number = call_data.get('caller_number', 'Unknown')
            start_time = call_data.get('start_time', datetime.now())
            duration = int((datetime.now() - start_time).total_seconds())
            direction = call_data.get('direction', 'inbound')
            
            await log_completed_call_to_crm(
                call_sid=call_sid,
                caller_number=caller_number,
                duration=duration,
                transcriptions=transcriptions,
                direction=direction
            )
            
            print(f"✅ Call {call_sid} logged to HubSpot CRM")
        except Exception as e:
            print(f"⚠️ Failed to log call to HubSpot: {e}")
    
    if transcriptions:
        print(f"📝 Successfully processed {len(transcriptions)} transcriptions for {call_sid}")
    else:
        print(f"📝 No transcriptions were created for {call_sid}")

async def forward_audio_to_bridge(sender_call_sid: str, audio_payload: str, sender_stream_sid: str):
    """Forward audio from one call to all other connected calls (bridge functionality)"""
    
    # Find all other connected calls to forward audio to (excluding laptop connections)
    for call_sid, websocket in call_connections.items():
        if call_sid != sender_call_sid and not call_sid.startswith('laptop_'):  # Don't send back to sender or to laptop connections
            try:
                # Create media message for the other call
                media_message = {
                    "event": "media",
                    "streamSid": call_sid,  # Use the receiver's stream ID if available
                    "media": {
                        "payload": audio_payload
                    }
                }
                
                # Send audio to the other call
                await websocket.send_text(json.dumps(media_message))
                
                # Send mark message for tracking
                mark_message = {
                    "event": "mark",
                    "streamSid": call_sid,
                    "mark": {
                        "name": f"bridged_from_{sender_call_sid}"
                    }
                }
                await websocket.send_text(json.dumps(mark_message))
                
            except Exception as e:
                print(f"❌ Error forwarding audio from {sender_call_sid} to {call_sid}: {e}")

# --------------------------
# 11. Call Status and Management
# --------------------------
@app.get("/call-status/{bridge_id}")
async def get_call_status(bridge_id: str):
    """Get the status of a bridge call"""
    if bridge_id in active_calls:
        return active_calls[bridge_id]
    else:
        return {"status": "not_found", "message": "Bridge call not found"}

@app.get("/transcription/{call_sid}")
async def get_call_transcription(call_sid: str):
    """Get the transcription for a specific call"""
    if call_sid in call_transcriptions:
        return {
            "call_sid": call_sid,
            "transcription": call_transcriptions[call_sid],
            "total_entries": len(call_transcriptions[call_sid])
        }
    else:
        return {"status": "not_found", "message": "No transcription found for this call"}

@app.get("/transcriptions")
async def get_all_transcriptions():
    """Get all transcriptions"""
    return {
        "transcriptions": call_transcriptions,
        "total_calls": len(call_transcriptions)
    }

# --------------------------
# Recording Management Endpoints
# --------------------------
@app.get("/recordings")
async def get_all_recordings():
    """Get all call recordings"""
    return {
        "recordings": call_recordings,
        "total_calls": len(call_recordings)
    }

@app.get("/recording/{call_sid}")
async def get_call_recordings(call_sid: str):
    """Get recordings for a specific call"""
    if call_sid in call_recordings:
        return {
            "call_sid": call_sid,
            "recordings": call_recordings[call_sid],
            "total_recordings": len(call_recordings[call_sid])
        }
    else:
        return {"status": "not_found", "message": "No recordings found for this call"}

@app.get("/download-recording/{call_sid}/{recording_sid}")
async def download_recording_file(call_sid: str, recording_sid: str):
    """Download a specific recording file"""
    if call_sid in call_recordings:
        for rec_info in call_recordings[call_sid]:
            if rec_info["recording_sid"] == recording_sid and rec_info["local_file"]:
                file_path = Path(rec_info["local_file"])
                if file_path.exists():
                    return FileResponse(
                        path=str(file_path),
                        filename=file_path.name,
                        media_type="audio/wav"
                    )
                else:
                    return {"status": "error", "message": "Recording file not found on disk"}
        return {"status": "error", "message": "Recording not found"}
    else:
        return {"status": "error", "message": "No recordings found for this call"}

@app.post("/manual-download-recording/{call_sid}/{recording_sid}")
async def manual_download_recording(call_sid: str, recording_sid: str):
    """Manually trigger recording download"""
    try:
        file_path = await download_and_save_recording(call_sid, recording_sid)
        if file_path:
            return {
                "status": "success",
                "message": f"Recording downloaded and saved to {file_path}",
                "file_path": file_path
            }
        else:
            return {"status": "error", "message": "Failed to download recording"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/recording/{call_sid}/{recording_sid}")
async def delete_recording(call_sid: str, recording_sid: str):
    """Delete a recording file from local storage"""
    try:
        if call_sid in call_recordings:
            for i, rec_info in enumerate(call_recordings[call_sid]):
                if rec_info["recording_sid"] == recording_sid:
                    # Delete file if it exists
                    if rec_info["local_file"]:
                        file_path = Path(rec_info["local_file"])
                        if file_path.exists():
                            file_path.unlink()
                            print(f"🗑️ Deleted recording file: {file_path}")
                    
                    # Remove from records
                    call_recordings[call_sid].pop(i)
                    if not call_recordings[call_sid]:  # Remove call_sid if no recordings left
                        del call_recordings[call_sid]
                    
                    return {"status": "success", "message": "Recording deleted"}
            return {"status": "error", "message": "Recording not found"}
        else:
            return {"status": "error", "message": "No recordings found for this call"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/transcription/{call_sid}")
async def delete_call_transcription(call_sid: str):
    """Delete transcription for a specific call"""
    if call_sid in call_transcriptions:
        del call_transcriptions[call_sid]
        return {"status": "success", "message": f"Transcription for {call_sid} deleted"}
    else:
        return {"status": "not_found", "message": "No transcription found for this call"}

@app.post("/end-bridge/{bridge_id}")
async def end_bridge_call(bridge_id: str):
    """End a bridge call"""
    if bridge_id in active_calls:
        bridge_info = active_calls[bridge_id]
        try:
            # End both calls
            client.calls(bridge_info['call1_sid']).update(status='completed')
            client.calls(bridge_info['call2_sid']).update(status='completed')
            
            # Remove from active calls
            del active_calls[bridge_id]
            
            return {"status": "success", "message": "Bridge call ended"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    else:
        return {"status": "not_found", "message": "Bridge call not found"}

# --------------------------
# 12. Twilio Transcription Callback Webhook
# --------------------------
@app.api_route("/transcription-callback", methods=["GET", "POST"])
async def transcription_callback(request: Request):
    """Webhook endpoint for Twilio transcription callbacks"""
    try:
        form_data = await request.form()
        
        # Extract transcription data from Twilio webhook
        call_sid = form_data.get('CallSid')
        recording_sid = form_data.get('RecordingSid')
        transcription_sid = form_data.get('TranscriptionSid')
        transcription_text = form_data.get('TranscriptionText', '')
        transcription_status = form_data.get('TranscriptionStatus')
        
        print(f"📝 Received transcription callback for call {call_sid}")
        print(f"📝 Transcription: {transcription_text}")
        
        if call_sid and transcription_text:
            timestamp = datetime.now().strftime("%H:%M:%S")
            transcription_entry = {
                "timestamp": timestamp,
                "recording_sid": recording_sid,
                "transcription_sid": transcription_sid,
                "text": transcription_text,
                "status": transcription_status,
                "call_sid": call_sid,
                "source": "twilio_webhook"
            }
            
            # Store transcription
            if call_sid not in call_transcriptions:
                call_transcriptions[call_sid] = []
            call_transcriptions[call_sid].append(transcription_entry)
            
            print(f"📝 [{timestamp}] Call {call_sid}: {transcription_text}")
        
        # Return TwiML response (required by Twilio)
        return HTMLResponse(content="<?xml version='1.0' encoding='UTF-8'?><Response></Response>", 
                          media_type="application/xml")
    
    except Exception as e:
        print(f"❌ Error processing transcription callback: {e}")
        return HTMLResponse(content="<?xml version='1.0' encoding='UTF-8'?><Response></Response>", 
                          media_type="application/xml")

# --------------------------
# 13. Manual Transcription Fetch Endpoint
# --------------------------
@app.post("/fetch-transcription/{call_sid}")
async def manual_fetch_transcription(call_sid: str):
    """Manually fetch transcription for a call using AssemblyAI"""
    transcriptions = await fetch_twilio_transcription(call_sid)
    if transcriptions:
        return {
            "status": "success", 
            "call_sid": call_sid,
            "transcriptions": transcriptions,
            "count": len(transcriptions)
        }
    else:
        return {
            "status": "not_found", 
            "message": f"No transcriptions found for call {call_sid}"
        }

@app.post("/transcribe-recording/{recording_sid}")
async def manual_transcribe_recording(recording_sid: str):
    """Manually transcribe a specific recording using AssemblyAI"""
    try:
        # Get recording details
        recording = client.recordings(recording_sid).fetch()
        recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{client.username}/Recordings/{recording.sid}.wav"
        
        # Transcribe with AssemblyAI
        transcription_result = await transcribe_with_assemblyai(recording_url, "manual", recording_sid)
        
        return {
            "status": "success",
            "recording_sid": recording_sid,
            "transcription": transcription_result
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error transcribing recording {recording_sid}: {str(e)}"
        }

@app.get("/active-calls")
async def get_active_calls():
    """Get all active bridge calls"""
    return {"active_calls": active_calls, "total": len(active_calls)}

if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting Human-to-Human Bridge Server...")
    print(f"📊 Dashboard will be available at: http://localhost:{PORT}/dashboard")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
