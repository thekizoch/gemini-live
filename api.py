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
        self.client = genai.Client(api_key=GOOGLE_GENAI_API_KEY)

    async def start_session(self, websocket: WebSocket):
        if self.live_session_active:
            logging.warning("Session already active. Ignoring start request.")
            await websocket.send_json({"status": "info", "message": "Session already active."})
            return

        self.websocket = websocket
        self.audio_queue = asyncio.Queue()
        self.live_session_active = True

        self.session_handler_task = asyncio.create_task(self._handle_session_lifecycle())
        
        logging.info("Live session starting process initiated.")

    async def _handle_session_lifecycle(self):
        model_name = "models/gemini-2.0-flash-live-001"
        config = {"response_modalities": ["TEXT"]} 

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

                send_audio_task = asyncio.create_task(self._send_audio_chunks(session))
                receive_responses_task = asyncio.create_task(self._receive_genai_responses(session))

                done, pending = await asyncio.wait(
                    {send_audio_task, receive_responses_task},
                    return_when=asyncio.FIRST_COMPLETED
                )

                for task in pending:
                    logging.debug(f"Cancelling pending task: {task.get_name()}")
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        logging.debug(f"Task {task.get_name()} successfully cancelled.")

                for task in done:
                    exc = task.exception()
                    if exc:
                        logging.error(f"Task {task.get_name()} failed with exception: {exc}")
                        if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                            await self.websocket.send_json({"type": "error", "message": f"Session error: {str(exc)}"})
                    else:
                        logging.info(f"Task {task.get_name()} completed.")
            
        except asyncio.CancelledError:
            logging.info("Session lifecycle task was cancelled externally (e.g., by stop_session).")
        except Exception as e:
            logging.error(f"Error during GenAI session lifecycle: {e}", exc_info=True)
            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED and not session_started_sent:
                await self.websocket.send_json({"status": "error", "message": f"Error starting session: {str(e)}"})
            elif self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                await self.websocket.send_json({"status": "error", "message": f"GenAI session error: {str(e)}"})
        finally:
            logging.info("Cleaning up session lifecycle resources.")
            if send_audio_task and not send_audio_task.done():
                send_audio_task.cancel()
                try: await send_audio_task
                except asyncio.CancelledError: pass
            if receive_responses_task and not receive_responses_task.done():
                receive_responses_task.cancel()
                try: await receive_responses_task
                except asyncio.CancelledError: pass
            
            if self.live_session_active:
                logging.info("Session lifecycle ended; ensuring full stop_session cleanup.")
                notify = self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED
                asyncio.create_task(self.stop_session(notify_client=notify))


    async def _send_audio_chunks(self, session):
        logging.info("Audio chunk sending task started.")
        try:
            while True:
                if not self.audio_queue:
                    logging.warning("_send_audio_chunks: Audio queue is None. Stopping.")
                    break
                
                audio_chunk = await self.audio_queue.get()
                
                if audio_chunk is None:
                    logging.info("Stop signal received in audio sender. Ending sending task.")
                    break

                if not self.live_session_active:
                    logging.warning("Session no longer active, stopping audio send.")
                    break
                
                logging.debug(f"Sending audio chunk of size: {len(audio_chunk)} bytes.")
                try:
                    await session.send(input={"data": audio_chunk, "mime_type": "audio/pcm"})
                    self.audio_queue.task_done()
                except Exception as e:
                    logging.error(f"Error sending audio chunk to GenAI: {e}", exc_info=True)
                    break
        except asyncio.CancelledError:
            logging.info("Audio chunk sending task cancelled.")
        except Exception as e:
            logging.error(f"Unhandled error in _send_audio_chunks: {e}", exc_info=True)
        finally:
            logging.info("Audio chunk sending task finished.")


    async def _receive_genai_responses(self, session):
        logging.info("GenAI response receiving task started.")
        try:
            async for response in session.receive():
                if not self.live_session_active:
                    logging.warning("Session no longer active, stopping response processing.")
                    break

                if response.text:
                    logging.info(f"Transcript part received: '{response.text}'")
                    if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                        await self.websocket.send_json({"type": "transcript", "data": response.text})
                
                # Removed the "if response.error:" block as LiveServerMessage for text/audio
                # does not have a direct .error attribute. Stream-level errors are caught elsewhere.
                # Specific non-fatal error signals would be in response.server_content or response.go_away.

        except asyncio.CancelledError:
            logging.info("GenAI response receiving task cancelled.")
        except Exception as e:
            logging.error(f"Error in GenAI response receiving: {e}", exc_info=True)
            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                try:
                    await self.websocket.send_json({"type": "error", "message": f"GenAI stream processing error: {str(e)}"})
                except Exception as ws_send_err:
                    logging.error(f"Error sending GenAI stream error to WebSocket: {ws_send_err}")
        finally:
            logging.info("GenAI response receiving task finished.")


    async def handle_audio_chunk(self, audio_chunk: bytes):
        if not self.live_session_active or not self.audio_queue:
            logging.warning("Received audio chunk, but session is not active or queue not ready.")
            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                 await self.websocket.send_json({"status": "warning", "message": "Session not active. Cannot process audio."})
            return
        
        logging.debug(f"Received audio chunk, putting in queue. Size: {len(audio_chunk)}. Queue size: {self.audio_queue.qsize()}")
        await self.audio_queue.put(audio_chunk)

    async def stop_session(self, notify_client=True):
        if not self.live_session_active:
            logging.info("No active session to stop, or already stopping.")
            if notify_client and self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                pass
            return

        logging.info("Stopping live session...")
        self.live_session_active = False

        if self.audio_queue:
            logging.debug("Putting None in audio queue to stop sender task.")
            await self.audio_queue.put(None)

        if self.session_handler_task and not self.session_handler_task.done():
            logging.debug("Cancelling session handler task.")
            self.session_handler_task.cancel()
            try:
                await self.session_handler_task
                logging.info("Session handler task cancelled and awaited successfully.")
            except asyncio.CancelledError:
                logging.info("Session handler task confirmed cancellation during stop.")
            except Exception as e:
                logging.error(f"Error awaiting cancelled session_handler_task: {e}", exc_info=True)
        
        self.audio_queue = None
        self.session_handler_task = None
        
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
    logging.info(f"WebSocket connection accepted from {websocket.client.host}:{websocket.client.port}")

    if manager.websocket and manager.websocket.client_state == manager.websocket.client_state.CONNECTED:
        logging.warning(f"New client connection attempt from {websocket.client.host} while another session is active/associated. Rejecting.")
        await websocket.send_json({"status": "error", "message": "A session is already active or associated with another client."})
        await websocket.close(code=1008)
        return
    
    previous_websocket = manager.websocket
    manager.websocket = websocket

    try:
        while True:
            data = await websocket.receive()

            if "text" in data:
                message_text = data["text"]
                logging.info(f"Received text message from client: {message_text}")
                try:
                    message_json = json.loads(message_text)
                    command = message_json.get("command")

                    if command == "start_session":
                        logging.info("Received start_session command.")
                        await manager.start_session(websocket)
                    elif command == "stop_session":
                        logging.info("Received stop_session command.")
                        await manager.stop_session()
                    else:
                        logging.warning(f"Unknown command: {command}")
                        await websocket.send_json({"status": "error", "message": f"Unknown command: {command}"})
                except json.JSONDecodeError:
                    logging.error(f"Failed to parse JSON command: {message_text}")
                    await websocket.send_json({"status": "error", "message": "Invalid JSON command."})
            
            elif "bytes" in data:
                audio_chunk = data["bytes"]
                if manager.live_session_active:
                    await manager.handle_audio_chunk(audio_chunk)
                else:
                    logging.warning("Received audio data, but session is not active. Discarding.")

    except WebSocketDisconnect as e:
        logging.info(f"WebSocket disconnected by client: {websocket.client.host}:{websocket.client.port}. Code: {e.code}, Reason: {e.reason}")
    except Exception as e:
        logging.error(f"Error in WebSocket handler: {e}", exc_info=True)
        if websocket.client_state == websocket.client_state.CONNECTED:
            await websocket.send_json({"status": "error", "message": f"Server error: {str(e)}"})
    finally:
        logging.info(f"Cleaning up WebSocket connection for {websocket.client.host}:{websocket.client.port}.")
        if manager.websocket == websocket:
            if manager.live_session_active:
                logging.info("WebSocket disconnected, stopping active session associated with it.")
                await manager.stop_session(notify_client=False)
            manager.websocket = None
            logging.info("Manager's websocket reference cleared.")
        elif previous_websocket == websocket:
             logging.info("Disconnected websocket was a previous websocket instance, manager.websocket already updated or cleared.")
        else:
            logging.info("Disconnected websocket was not the primary one known to the manager, or manager.websocket was already cleared.")


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