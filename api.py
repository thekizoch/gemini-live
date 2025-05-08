import os
import asyncio
import logging
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from google import genai

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

GOOGLE_GENAI_API_KEY = os.getenv("GOOGLE_GENAI_API_KEY")
if not GOOGLE_GENAI_API_KEY:
    logging.error("GOOGLE_GENAI_API_KEY not found in .env file")
    raise ValueError("GOOGLE_GENAI_API_KEY not found in .env file")

app = FastAPI()

# Mount static files (HTML, JS, CSS)
app.mount("/static", StaticFiles(directory="static"), name="static")

class LiveSessionManager:
    def __init__(self):
        self.live_session_active = False
        self.audio_queue = None
        self.session_handler_task = None
        self.websocket = None
        self.client = genai.Client(api_key=GOOGLE_GENAI_API_KEY,
                                   http_options={"api_version": "v1beta"}) # Ensure v1beta for Live API

    async def start_session(self, websocket: WebSocket):
        if self.live_session_active:
            logging.warning("Session already active. Ignoring start request.")
            await websocket.send_json({"status": "info", "message": "Session already active."})
            return

        self.websocket = websocket
        self.audio_queue = asyncio.Queue()
        self.live_session_active = True

        self.session_handler_task = asyncio.create_task(self._handle_session_lifecycle())
        
        logging.info(f"Live session starting process initiated. Task: {self.session_handler_task.get_name()}")

    async def _handle_session_lifecycle(self):
        model_name = "models/gemini-2.0-flash-live-001"
        config = {
            "response_modalities": ["AUDIO"],
            "output_audio_transcription": {},  # Request transcription of model's audio output
        }

        send_audio_task = None
        receive_responses_task = None
        session_started_sent = False

        try:
            logging.info(f"Attempting to connect to Gemini Live API with model {model_name}...")
            async with self.client.aio.live.connect(model=model_name, config=config) as session:
                logging.info(f"Successfully connected to Gemini Live API model {model_name}.")
                if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                    await self.websocket.send_json({"status": "info", "message": "Live session started. Ready for audio."})
                    session_started_sent = True

                send_audio_task = asyncio.create_task(self._send_audio_chunks(session), name="send_audio_task")
                receive_responses_task = asyncio.create_task(self._receive_genai_data(session), name="receive_responses_task")

                logging.info("Starting asyncio.gather for send_audio and receive_responses tasks.")
                await asyncio.gather(send_audio_task, receive_responses_task)
                logging.info("asyncio.gather completed. This means both tasks finished or one errored/was cancelled.")

        except asyncio.CancelledError:
            logging.info("Session lifecycle task was cancelled externally (e.g., by stop_session).")
        except Exception as e:
            logging.error(f"Error during GenAI session lifecycle: {e}", exc_info=True)
            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                if not session_started_sent:
                    await self.websocket.send_json({"status": "error", "message": f"Error starting session: {str(e)}"})
                else: # Session started but then an error occurred
                    await self.websocket.send_json({"status": "error", "message": f"GenAI session error: {str(e)}"})
        finally:
            logging.info("Cleaning up session lifecycle resources.")
            for task, name in [(send_audio_task, "send_audio_task"), (receive_responses_task, "receive_responses_task")]:
                if task and not task.done():
                    logging.info(f"Cancelling task {name} in finally block.")
                    task.cancel()
                    try:
                        await task
                        logging.info(f"Task {name} successfully cancelled and awaited.")
                    except asyncio.CancelledError:
                        logging.info(f"Task {name} confirmed cancellation during finally.")
                    except Exception as e_task_cancel:
                        logging.error(f"Error awaiting cancelled task {name}: {e_task_cancel}", exc_info=True)
            
            if self.live_session_active: # If still marked active, means it didn't stop cleanly via stop_session
                logging.info("Session lifecycle ended; ensuring full stop_session cleanup initiated from finally block.")
                # This will ensure client is notified if WebSocket is still connected
                asyncio.create_task(self.stop_session(notify_client=(self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED)))


    async def _send_audio_chunks(self, session):
        logging.info("Audio chunk sending task started.")
        try:
            while True:
                if not self.audio_queue:
                    logging.warning("_send_audio_chunks: Audio queue is None. Stopping.")
                    break
                
                audio_chunk = await self.audio_queue.get()
                
                if audio_chunk is None: # Sentinel for stopping
                    logging.info("Stop signal (None) received in audio sender. Ending sending task.")
                    break

                if not self.live_session_active:
                    logging.warning("Session no longer active in _send_audio_chunks, stopping audio send.")
                    break
                
                logging.debug(f"Sending audio chunk of size: {len(audio_chunk)} bytes to GenAI.")
                try:
                    # Ensure using the correct method if API changes; `input` dict is common for general data.
                    # For pure audio stream with VAD, `realtime_input` might be used, but here we send chunks.
                    await session.send(input={"data": audio_chunk, "mime_type": "audio/pcm"})
                    self.audio_queue.task_done()
                except Exception as e:
                    logging.error(f"Error sending audio chunk to GenAI: {e}", exc_info=True)
                    # If sending fails, we might want to break and let the session handler clean up.
                    break 
        except asyncio.CancelledError:
            logging.info("Audio chunk sending task cancelled.")
        except Exception as e:
            logging.error(f"Unhandled error in _send_audio_chunks: {e}", exc_info=True)
        finally:
            logging.info("Audio chunk sending task finished.")


    async def _receive_genai_data(self, session):
        logging.info("GenAI data receiving task started (audio and transcript).")
        try:
            async for response in session.receive():
                if not self.live_session_active:
                    logging.warning("Session no longer active in _receive_genai_data, stopping response processing.")
                    break

                # Handle model's audio output data
                if response.data:
                    logging.debug(f"Received audio data chunk from GenAI: {len(response.data)} bytes.")
                    if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                        try:
                            await self.websocket.send_bytes(response.data)
                        except Exception as e:
                            logging.error(f"Error sending audio data to client: {e}")
                
                # Handle transcription of model's audio output
                if response.server_content and \
                   hasattr(response.server_content, 'output_transcription') and \
                   response.server_content.output_transcription and \
                   response.server_content.output_transcription.text:
                    transcript_text = response.server_content.output_transcription.text
                    logging.info(f"Model output transcript received: '{transcript_text}'")
                    if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                        await self.websocket.send_json({"type": "model_transcript", "data": transcript_text})
                
                # Handle interim transcription of user's audio input
                if response.server_content and \
                   hasattr(response.server_content, 'interim_transcription') and \
                   response.server_content.interim_transcription and \
                   response.server_content.interim_transcription.text:
                    interim_text = response.server_content.interim_transcription.text
                    is_final_part = getattr(response.server_content.interim_transcription, 'is_final', False)
                    logging.info(f"User interim transcript received: '{interim_text}' (is_final_part: {is_final_part})")
                    if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                        await self.websocket.send_json({
                            "type": "user_transcript", 
                            "data": interim_text,
                            "is_final_part": is_final_part
                        })
                        
        except asyncio.CancelledError:
            logging.info("GenAI data receiving task cancelled.")
        except Exception as e:
            logging.error(f"Error in GenAI data receiving: {e}", exc_info=True)
            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                try:
                    await self.websocket.send_json({"type": "error", "message": f"GenAI stream processing error: {str(e)}"})
                except Exception as ws_send_err:
                    logging.error(f"Error sending GenAI stream error to WebSocket: {ws_send_err}")
        finally:
            logging.info("GenAI data receiving task finished.")


    async def handle_audio_chunk(self, audio_chunk: bytes):
        if not self.live_session_active or not self.audio_queue:
            logging.warning("Received audio chunk, but session is not active or queue not ready.")
            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                 await self.websocket.send_json({"status": "warning", "message": "Session not active. Cannot process audio."})
            return
        
        logging.debug(f"Received audio chunk, putting in queue. Size: {len(audio_chunk)}. Queue size before put: {self.audio_queue.qsize()}")
        await self.audio_queue.put(audio_chunk)

    async def stop_session(self, notify_client=True):
        if not self.live_session_active:
            logging.info("No active session to stop, or stop_session already in progress.")
            # If already stopping, or no session, we might still want to ensure client is notified if requested and connected
            if notify_client and self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                # Check if a "stopped" message hasn't been sent recently or if one is truly needed
                pass
            return

        logging.info("Stopping live session...")
        self.live_session_active = False # Set this first to prevent new operations

        # Signal the audio sender to stop by putting None in the queue
        if self.audio_queue:
            logging.debug("Putting None in audio queue to stop sender task.")
            await self.audio_queue.put(None) # This will unblock `_send_audio_chunks` if it's waiting

        # Cancel the main session handler task
        if self.session_handler_task and not self.session_handler_task.done():
            logging.debug("Cancelling session handler task.")
            self.session_handler_task.cancel()
            try:
                await self.session_handler_task # Wait for it to actually cancel
                logging.info("Session handler task cancelled and awaited successfully.")
            except asyncio.CancelledError:
                logging.info("Session handler task confirmed cancellation during stop.")
            except Exception as e:
                logging.error(f"Error awaiting cancelled session_handler_task: {e}", exc_info=True)
        
        # Clean up resources
        self.audio_queue = None # Allow garbage collection
        self.session_handler_task = None # Allow garbage collection
        
        if notify_client and self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
            try:
                logging.info("Sending 'Live session stopped.' to client.")
                await self.websocket.send_json({"status": "info", "message": "Live session stopped."})
            except Exception as e:
                logging.error(f"Error sending stop confirmation to client: {e}", exc_info=True)
        
        logging.info("Live session stop process complete.")


manager = LiveSessionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = f"{websocket.client.host}:{websocket.client.port}"
    logging.info(f"WebSocket connection accepted from {client_id}")

    # Simple single-session management: if a websocket is already active, reject new one.
    if manager.websocket and manager.websocket.client_state == manager.websocket.client_state.CONNECTED:
        logging.warning(f"New client {client_id} connection attempt while another WebSocket ({manager.websocket.client.host}:{manager.websocket.client.port}) is active. Rejecting.")
        await websocket.send_json({"status": "error", "message": "A session is already active with another client."})
        await websocket.close(code=1008) # Policy Violation
        return
    
    # Store this new websocket as the active one for the manager
    # This overwrites if manager.websocket was None or a disconnected socket.
    manager.websocket = websocket 

    try:
        while True:
            data = await websocket.receive() # Blocks here until data is received

            if "text" in data:
                message_text = data["text"]
                logging.info(f"Received text message from client {client_id}: {message_text}")
                try:
                    message_json = json.loads(message_text)
                    command = message_json.get("command")

                    if command == "start_session":
                        logging.info(f"Received start_session command from {client_id}.")
                        # Pass the current websocket to start_session
                        await manager.start_session(websocket) 
                    elif command == "stop_session":
                        logging.info(f"Received stop_session command from {client_id}.")
                        await manager.stop_session() # Will use manager.websocket
                    else:
                        logging.warning(f"Unknown command from {client_id}: {command}")
                        await websocket.send_json({"status": "error", "message": f"Unknown command: {command}"})
                except json.JSONDecodeError:
                    logging.error(f"Failed to parse JSON command from {client_id}: {message_text}")
                    await websocket.send_json({"status": "error", "message": "Invalid JSON command."})
            
            elif "bytes" in data:
                audio_chunk = data["bytes"]
                if manager.live_session_active:
                    await manager.handle_audio_chunk(audio_chunk)
                else:
                    logging.warning(f"Received audio data from {client_id}, but session is not active. Discarding.")

    except WebSocketDisconnect as e:
        logging.info(f"WebSocket disconnected by client: {websocket.client.host}:{websocket.client.port}. Code: {e.code}, Reason: {e.reason}")
    except Exception as e: # Catch other exceptions during websocket handling
        logging.error(f"Error in WebSocket handler for {websocket.client.host}:{websocket.client.port}: {e}", exc_info=True)
        if websocket.client_state == websocket.client_state.CONNECTED:
            try:
                await websocket.send_json({"status": "error", "message": f"Server error: {str(e)}"})
            except Exception as send_err:
                logging.error(f"Failed to send error to disconnected/problematic websocket: {send_err}")
    finally:
        logging.info(f"Cleaning up WebSocket connection for {websocket.client.host}:{websocket.client.port}.")
        # Critical: Only stop the session if THIS websocket was the one managing the active session.
        if manager.websocket == websocket:
            if manager.live_session_active:
                logging.info("WebSocket disconnected, and it was the active session's WebSocket. Stopping GenAI session.")
                # Pass notify_client=False because the client is already disconnected or disconnecting.
                await manager.stop_session(notify_client=False) 
            manager.websocket = None # Clear the manager's reference to this websocket
            logging.info("Manager's websocket reference cleared as it was the one that disconnected.")
        else:
            # This case might happen if a new websocket connected and replaced manager.websocket,
            # and then the old one finally disconnected. Or if manager.websocket was already None.
            logging.info(f"Disconnected websocket {websocket.client.host} was not the primary one known to the manager, or manager.websocket was already cleared/updated. No session stop initiated by this finally block.")


@app.get("/", response_class=HTMLResponse)
async def get_root():
    try:
        with open("static/index.html", "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        logging.error("static/index.html not found.")
        return HTMLResponse(content="<h1>Error: Frontend not found</h1><p>Ensure 'static/index.html' exists.</p>", status_code=404)

if __name__ == "__main__":
    import uvicorn
    logging.info("Starting Uvicorn server directly for development.")
    uvicorn.run(app, host="0.0.0.0", port=8000)