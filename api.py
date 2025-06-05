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
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(funcName)s - %(message)s')
logger = logging.getLogger(__name__)
# Reduce verbosity for some libraries if needed
logging.getLogger("websockets.client").setLevel(logging.INFO) # Use INFO for less noise
logging.getLogger("websockets.server").setLevel(logging.INFO)
logging.getLogger("google.ai.generativelanguage").setLevel(logging.INFO)
logging.getLogger("google.api_core").setLevel(logging.INFO)


load_dotenv()

GOOGLE_GENAI_API_KEY = os.getenv("GOOGLE_GENAI_API_KEY")
if not GOOGLE_GENAI_API_KEY:
    logger.error("GOOGLE_GENAI_API_KEY not found in .env file")
    raise ValueError("GOOGLE_GENAI_API_KEY not found in .env file")

app = FastAPI()

# Mount static files (HTML, JS, CSS)
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

class LiveSessionManager:
    def __init__(self):
        self.live_session_active = False
        self.audio_queue = None
        self.session_handler_task = None
        self.websocket = None
        self.genai_client = genai.Client(api_key=GOOGLE_GENAI_API_KEY,
                                   http_options={"api_version": "v1beta"})
        # self.model_name = "models/gemini-2.0-flash-live-001"
        self.model_name = "models/gemini-2.5-flash-preview-native-audio-dialog"

    async def start_session(self, websocket: WebSocket):
        if self.live_session_active:
            logger.warning("Session already active. Ignoring start request.")
            if websocket.client_state == websocket.client_state.CONNECTED:
                await websocket.send_json({"status": "info", "message": "Session already active."})
            return

        self.websocket = websocket
        self.audio_queue = asyncio.Queue()
        self.live_session_active = True 

        self.session_handler_task = asyncio.create_task(self._handle_session_lifecycle(), name="session_handler_task")
        logger.info(f"Live session starting process initiated. Main handler task: {self.session_handler_task.get_name()}")


    async def _handle_session_lifecycle(self):
        config = {
            "response_modalities": ["AUDIO"],
            "output_audio_transcription": {},
        }

        send_audio_task = None
        receive_responses_task = None
        genai_session_ref = None 

        try:
            logger.info(f"Attempting to connect to Gemini Live API with model {self.model_name}...")
            async with self.genai_client.aio.live.connect(model=self.model_name, config=config) as session:
                genai_session_ref = session 
                logger.info("Successfully connected to Gemini Live API.") # Removed session_id logging
                if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                    await self.websocket.send_json({"status": "info", "message": "Live session started. Ready for audio."})

                send_audio_task = asyncio.create_task(self._send_audio_chunks(session), name="send_audio_task")
                receive_responses_task = asyncio.create_task(self._receive_genai_data(session), name="receive_responses_task")

                logger.info("Starting asyncio.wait for send_audio and receive_responses tasks.")
                done, pending = await asyncio.wait(
                    [send_audio_task, receive_responses_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                logger.info(f"asyncio.wait completed. Done tasks: {[t.get_name() for t in done]}, Pending tasks: {[t.get_name() for t in pending]}")

                for task in done:
                    if task.exception():
                        logger.error(f"Task {task.get_name()} raised an exception: {task.exception()!r}", exc_info=task.exception())
                        raise task.exception() 
                    else:
                        logger.info(f"Task {task.get_name()} completed normally (but this might be due to an internal unraised error if it ended prematurely).")
                
                if self.live_session_active: # If still active, means a task ended but didn't signal failure up here
                    completed_task_names = [t.get_name() for t in done]
                    logger.warning(f"One of the core session tasks ({completed_task_names}) ended while session was still marked active. This could mean an error was handled within the task but not propagated, or the task finished its logic. Initiating shutdown.")
                    # If a task ends "normally" but the session is still active, it's unexpected. Trigger stop.
                    self.live_session_active = False # Ensure stop is triggered if not already.


        except asyncio.CancelledError:
            logger.info("Session lifecycle task (_handle_session_lifecycle) was cancelled.")
        except Exception as e:
            logger.error(f"Error during GenAI session lifecycle: {e!r}", exc_info=True)
            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                try:
                    await self.websocket.send_json({"status": "error", "message": f"GenAI session error: {str(e)}"})
                except Exception as ws_send_err:
                    logger.error(f"Failed to send GenAI session error to WebSocket: {ws_send_err!r}")
        finally:
            logger.info("Cleaning up session lifecycle resources in _handle_session_lifecycle finally block.")
            
            active_tasks_to_cancel = []
            if send_audio_task and not send_audio_task.done(): active_tasks_to_cancel.append(send_audio_task)
            if receive_responses_task and not receive_responses_task.done(): active_tasks_to_cancel.append(receive_responses_task)

            for task in active_tasks_to_cancel:
                if not task.done():
                    logger.info(f"Cancelling pending task {task.get_name()} in finally block.")
                    task.cancel()
                    try:
                        await task
                        logger.info(f"Task {task.get_name()} successfully cancelled and awaited.")
                    except asyncio.CancelledError:
                        logger.info(f"Task {task.get_name()} confirmed cancellation during finally.")
                    except Exception as e_task_cancel:
                        logger.error(f"Error awaiting cancelled task {task.get_name()}: {e_task_cancel!r}", exc_info=True)
            
            # `async with session` handles closing genai_session_ref.
            # If stop_session hasn't run yet because of how _handle_session_lifecycle exited
            if self.live_session_active: # Check if it was explicitly stopped
                 logger.info("Session lifecycle ended unexpectedly; initiating stop_session for cleanup.")
                 self.live_session_active = False 
                 asyncio.create_task(self.stop_session(notify_client=(self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED)), name="finally_stop_session_task")
            
            logger.info("_handle_session_lifecycle finished.")


    async def _send_audio_chunks(self, session: genai.live.AsyncSession):
        logger.info("Audio chunk sending task started.")
        try:
            while self.live_session_active:
                if not self.audio_queue:
                    logger.warning("_send_audio_chunks: Audio queue is None. Stopping.")
                    break
                
                try:
                    audio_chunk = await asyncio.wait_for(self.audio_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if not self.live_session_active:
                        logger.info("_send_audio_chunks: Session stopped while waiting for audio. Exiting.")
                        break
                    continue

                if audio_chunk is None:
                    logger.info("Stop signal (None) received in audio sender. Ending sending task.")
                    break

                if not self.live_session_active:
                    logger.warning("Session became inactive in _send_audio_chunks after getting chunk, stopping audio send.")
                    break
                
                # Removed session.is_closed check

                logger.debug(f"Sending audio chunk of size: {len(audio_chunk)} bytes to GenAI.")
                try:
                    await session.send(input={"data": audio_chunk, "mime_type": "audio/pcm"})
                    self.audio_queue.task_done()
                except Exception as e_send: 
                    logger.error(f"Error sending audio chunk to GenAI: {e_send!r}", exc_info=True)
                    self.live_session_active = False 
                    raise # Re-raise to signal failure to _handle_session_lifecycle
                                
        except asyncio.CancelledError:
            logger.info("Audio chunk sending task cancelled.")
        except Exception as e: # Catch re-raised exceptions or others
            logger.error(f"Critical error in _send_audio_chunks: {e!r}", exc_info=True)
            self.live_session_active = False # Ensure session stops
            raise # Propagate to _handle_session_lifecycle
        finally:
            logger.info("Audio chunk sending task finished.")


    async def _receive_genai_data(self, session: genai.live.AsyncSession):
        logger.info("GenAI data receiving task started.")
        try:
            while self.live_session_active:
                # Removed session.is_closed check here

                logger.debug("Waiting for session.receive() for a new turn from model...")
                try:
                    turn = session.receive() 
                except Exception as e_recv_turn: # Catch errors from session.receive() itself
                    logger.error(f"Error calling session.receive(): {e_recv_turn!r}", exc_info=True)
                    self.live_session_active = False 
                    raise # Re-raise to signal failure
                
                async for response in turn:
                    if not self.live_session_active:
                        logger.warning("Session became inactive during response processing. Breaking from inner loop.")
                        break 
                    
                    # Removed session.is_closed check here

                    logger.debug(f"Received response from GenAI: {type(response)}")

                    if response.data:
                        logger.debug(f"Received audio data chunk from GenAI: {len(response.data)} bytes.")
                        if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                            try:
                                await self.websocket.send_bytes(response.data)
                            except Exception as e_ws_send_bytes:
                                logger.error(f"Error sending audio data to client: {e_ws_send_bytes!r}")
                    
                    if response.server_content and \
                       hasattr(response.server_content, 'output_transcription') and \
                       response.server_content.output_transcription and \
                       response.server_content.output_transcription.text:
                        transcript_text = response.server_content.output_transcription.text
                        logger.info(f"Model output transcript: '{transcript_text}'")
                        if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                           await self.websocket.send_json({"type": "model_transcript", "data": transcript_text})
                    
                    if response.server_content and \
                       hasattr(response.server_content, 'interim_transcription') and \
                       response.server_content.interim_transcription and \
                       response.server_content.interim_transcription.text:
                        interim_text = response.server_content.interim_transcription.text
                        is_final_part = getattr(response.server_content.interim_transcription, 'is_final', False)
                        logger.info(f"User interim transcript: '{interim_text}' (is_final_part: {is_final_part})")
                        if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                            await self.websocket.send_json({
                                "type": "user_transcript", 
                                "data": interim_text,
                                "is_final_part": is_final_part
                            })
                
                if not self.live_session_active: 
                    logger.warning("Session became inactive after processing a turn. Breaking outer loop.")
                    break
                logger.debug("Completed processing a turn from the model. Looping back to session.receive().")
                        
        except asyncio.CancelledError:
            logger.info("GenAI data receiving task cancelled.")
        except Exception as e: # Catch re-raised exceptions or others
            logger.error(f"Critical error in GenAI data receiving: {e!r}", exc_info=True)
            self.live_session_active = False # Ensure session stops
            # Optionally send error to client if websocket is still up and not already handled
            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                try:
                    await self.websocket.send_json({"status": "error", "message": f"Critical error in server: {str(e)}"})
                except Exception as ws_final_err:
                    logger.error(f"Failed to send final critical error to client: {ws_final_err!r}")
            raise # Propagate to _handle_session_lifecycle
        finally:
            logger.info("GenAI data receiving task finished.")


    async def handle_audio_chunk(self, audio_chunk: bytes):
        if not self.live_session_active or not self.audio_queue:
            logger.warning("Received audio chunk, but session is not active or queue not ready.")
            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                 await self.websocket.send_json({"status": "warning", "message": "Session not active. Cannot process audio."})
            return
        
        logger.debug(f"Received audio chunk, putting in queue. Size: {len(audio_chunk)}. Queue size before put: {self.audio_queue.qsize()}")
        await self.audio_queue.put(audio_chunk)

    async def stop_session(self, notify_client=True):
        # Check if already stopping or not active
        if not self.live_session_active and self.session_handler_task is None: # More robust check
            logger.info("No active session to stop, or stop_session already fully processed.")
            return

        current_task_name = asyncio.current_task().get_name() if asyncio.current_task() else "UnknownTask"
        logger.info(f"Stopping live session... Called from: {current_task_name}. Current state: live_session_active={self.live_session_active}")
        
        # Idempotency: if already marked as not active by another part of stop_session, don't re-process parts
        if not self.live_session_active:
            logger.info("stop_session: live_session_active is already False. Minimal cleanup.")
            # Even if marked false, if task exists and isn't done, it should be cancelled.
        else:
            self.live_session_active = False # Set this first to signal all loops to stop

        # Signal the audio sender to stop by putting None in the queue
        if self.audio_queue:
            logger.debug("Putting None in audio queue to stop sender task.")
            try:
                self.audio_queue.put_nowait(None) 
            except asyncio.QueueFull:
                logger.warning("Audio queue full while trying to send stop signal (None). Sender might be stuck or queue not consumed.")
            except Exception as e_put_q:
                logger.error(f"Error putting None into audio queue: {e_put_q!r}")


        # Cancel the main session handler task
        if self.session_handler_task and not self.session_handler_task.done():
            logger.debug(f"Cancelling session handler task: {self.session_handler_task.get_name()}")
            if self.session_handler_task.cancel(): 
                 logger.info(f"Session handler task {self.session_handler_task.get_name()} cancel request sent.")
            else:
                 logger.warning(f"Session handler task {self.session_handler_task.get_name()} could not be cancelled (already done or shielded).")

            try:
                await self.session_handler_task 
                logger.info(f"Session handler task {self.session_handler_task.get_name()} awaited successfully after cancel request.")
            except asyncio.CancelledError:
                logger.info(f"Session handler task {self.session_handler_task.get_name()} confirmed cancellation during stop.")
            except Exception as e:
                logger.error(f"Error awaiting cancelled session_handler_task {self.session_handler_task.get_name()}: {e!r}", exc_info=True)
        
        # Clean up resources
        self.audio_queue = None 
        self.session_handler_task = None 
        
        if notify_client and self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
            try:
                logger.info("Sending 'Live session stopped.' to client.")
                await self.websocket.send_json({"status": "info", "message": "Live session stopped."})
            except Exception as e: 
                logger.error(f"Error sending stop confirmation to client: {e!r}", exc_info=True)
        
        logger.info("Live session stop process complete.")


manager = LiveSessionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = f"{websocket.client.host}:{websocket.client.port}"
    logger.info(f"WebSocket connection accepted from {client_id}")

    if manager.websocket and manager.websocket.client_state == manager.websocket.client_state.CONNECTED:
        logger.warning(f"New client {client_id} connection attempt while another WebSocket ({manager.websocket.client.host}:{manager.websocket.client.port}) is active. Rejecting.")
        await websocket.send_json({"status": "error", "message": "A session is already active with another client."})
        await websocket.close(code=1008) 
        return
    
    manager.websocket = websocket 

    try:
        while True:
            data = await websocket.receive() 

            if "text" in data:
                message_text = data["text"]
                logger.debug(f"Received text message from client {client_id}: {message_text}")
                try:
                    message_json = json.loads(message_text)
                    command = message_json.get("command")

                    if command == "start_session":
                        logger.info(f"Received start_session command from {client_id}.")
                        await manager.start_session(websocket) 
                    elif command == "stop_session":
                        logger.info(f"Received stop_session command from {client_id}.")
                        await manager.stop_session() 
                    else:
                        logger.warning(f"Unknown command from {client_id}: {command}")
                        await websocket.send_json({"status": "error", "message": f"Unknown command: {command}"})
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse JSON command from {client_id}: {message_text}")
                    await websocket.send_json({"status": "error", "message": "Invalid JSON command."})
            
            elif "bytes" in data:
                audio_chunk = data["bytes"]
                if manager.live_session_active:
                    await manager.handle_audio_chunk(audio_chunk)
                else:
                    logger.warning(f"Received audio data from {client_id}, but session is not active. Discarding.")

    except WebSocketDisconnect as e:
        logger.info(f"WebSocket disconnected by client {client_id}. Code: {e.code}, Reason: '{e.reason}'")
    except Exception as e: 
        logger.error(f"Error in WebSocket handler for {client_id}: {e!r}", exc_info=True)
        if websocket.client_state == websocket.client_state.CONNECTED:
            try:
                await websocket.send_json({"status": "error", "message": f"Server error: {str(e)}"})
            except Exception as send_err:
                logger.error(f"Failed to send error to {client_id} websocket: {send_err!r}")
    finally:
        logger.info(f"Cleaning up WebSocket connection for {client_id}.")
        if manager.websocket == websocket: 
            if manager.live_session_active:
                logger.info(f"WebSocket {client_id} disconnected, and it was the active session's WebSocket. Initiating GenAI session stop.")
                asyncio.create_task(manager.stop_session(notify_client=False), name="ws_disconnect_stop_session_task")
            manager.websocket = None 
            logger.info(f"Manager's websocket reference to {client_id} cleared.")
        else:
            logger.info(f"Disconnected websocket {client_id} was not the primary one known to the manager (current: {manager.websocket.client.host if manager.websocket else 'None'}), or manager.websocket was already cleared/updated. No session stop initiated by this finally block.")


@app.get("/", response_class=HTMLResponse)
async def get_root():
    try:
        with open("static/index.html", "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        logger.error("static/index.html not found.")
        return HTMLResponse(content="<h1>Error: Frontend not found</h1><p>Ensure 'static/index.html' exists.</p>", status_code=404)

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Uvicorn server directly for development.")
    uvicorn.run("api:app", host="localhost", port=8765, reload=True)