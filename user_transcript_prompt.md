Okay, this is an interesting and valuable addition to your live transcription application! You want to get a separate, potentially delayed, transcription of the user's audio using Google Cloud Speech-to-Text, and then integrate it into the UI, maintaining the correct conversational order.

Here's a breakdown of the engineering thinking and how you can approach this:

Engineering Thinking (3 Paragraphs):

Dual Audio Path & Asynchronous Transcription: When the user speaks, their audio data needs to be sent down two paths simultaneously (or nearly so). The first path is to the Gemini Live API for the immediate conversational response and its built-in (interim) user transcription. The second path is to your custom Google Cloud Speech-to-Text (STT) pipeline. This custom STT must be asynchronous to prevent blocking the real-time interaction with Gemini. The core challenge here is to capture the entirety of a user's spoken "turn" and send that complete segment to the custom STT service.

Turn Segmentation and Audio Buffering: The Gemini Live API itself provides a crucial piece of information: the interim_transcription for the user's speech, which includes an is_final_part flag. This flag is perfect for determining when a user has finished an utterance. To implement the custom STT, your backend (api.py) will need to buffer the raw audio chunks that are being sent to Gemini. When is_final_part: true is received for the user's speech from Gemini, this signals that the buffered audio chunks constitute a complete "turn." This collection of chunks can then be dispatched to a new asynchronous task that calls the Google Cloud Speech-to-Text API.

UI Ordering and Turn-Based Display: This is where it gets a bit tricky due to the asynchronous nature. The custom user transcript might arrive after Gemini's response (and its own transcript) has already been displayed. To handle this gracefully and maintain logical order ("User said X, then Gemini said Y"), you'll need a robust way to manage "turns" in your frontend.

Turn IDs: When a user starts speaking (e.g., the first interim_transcription from Gemini arrives), the backend should generate a unique turn_id. This turn_id should be associated with the user's audio sent for custom STT, the Gemini-provided user transcript, and the subsequent model's transcript.

Frontend Placeholders/Updates: The frontend (script.js) will receive these messages tagged with turn_id. It should create a distinct visual block or context for each turn. When the custom STT result for a specific turn_id arrives, it should be inserted into the correct turn block, specifically before the model's response for that same turn, even if the model's response was rendered earlier. This might involve dynamically creating DOM elements or updating placeholders.

Guiding Code Examples from Your Repository & Provided Snippet:

api.py (Your Backend): This is the central piece for modification.

Audio Reception: It already handles receiving audio chunks via WebSockets in websocket_endpoint and passing them to manager.handle_audio_chunk.

Audio Buffering: You'll modify LiveSessionManager to buffer audio chunks that belong to the current user's speaking turn.

Turn Detection: The _receive_genai_data method, specifically where it processes response.server_content.interim_transcription and checks for is_final_part, is where you'll trigger the custom STT process with the buffered audio.

Sending to Custom STT: You'll create a new async function, similar in principle to the transcribe_streaming example you provided, but adapted to take a list of in-memory audio chunks and use the google-cloud-speech async client.

transcribe_streaming (Your STT Example): This is the template for your custom STT function.

You'll use speech.SpeechAsyncClient() for compatibility with FastAPI's asyncio environment.

Instead of reading from a file, it will consume a list/generator of audio byte chunks.

The StreamingRecognitionConfig will be similar.

static/script.js (Your Frontend): This will require significant changes to handle the new custom_user_transcript message type and the turn_id for ordering.

It will need to parse the turn_id from incoming messages.

It will need to create or identify the correct DOM container for a given turn_id.

When a custom_user_transcript arrives, it needs to be inserted into its turn's container, ensuring it's displayed before the model's response for that turn.

Conceptual Backend (api.py) Modifications:

# In api.py

import asyncio
import logging
from google.cloud import speech # Add this import
# ... other existing imports

logger = logging.getLogger(__name__)

# --- Helper function for custom STT (based on your example) ---
async def transcribe_user_audio_custom_stt(audio_chunks: list[bytes], websocket: WebSocket, turn_id: str):
    logger.info(f"CUSTOM_STT: Starting for turn_id: {turn_id} with {len(audio_chunks)} audio chunks.")
    if not audio_chunks:
        logger.warning(f"CUSTOM_STT: No audio chunks to transcribe for turn_id: {turn_id}.")
        return

    client = speech.SpeechAsyncClient()
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16, # Ensure this matches your audio
        sample_rate_hertz=16000, # Ensure this matches your audio
        language_code="en-US", # Make configurable if needed
        enable_automatic_punctuation=True,
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=config,
        interim_results=False # We only want the final transcript for this custom pass
    )

    async def audio_stream_generator():
        for chunk in audio_chunks:
            yield speech.StreamingRecognizeRequest(audio_content=chunk)
        # await asyncio.sleep(0.01) # Optional: yield control if many small chunks

    full_transcript = ""
    try:
        streaming_responses = await client.streaming_recognize(
            config=streaming_config,
            requests=audio_stream_generator(),
        )
        async for response in streaming_responses:
            for result in response.results:
                if result.is_final:
                    full_transcript += result.alternatives[0].transcript + " "
                    logger.info(f"CUSTOM_STT: Final segment for turn_id {turn_id}: '{result.alternatives[0].transcript}'")

        final_text = full_transcript.strip()
        if final_text and websocket and websocket.client_state == websocket.client_state.CONNECTED:
            logger.info(f"CUSTOM_STT: Sending final transcript for turn_id {turn_id}: '{final_text}'")
            await websocket.send_json({
                "type": "custom_user_transcript",
                "data": final_text,
                "turn_id": turn_id
            })
        elif not final_text:
             logger.warning(f"CUSTOM_STT: No final transcript generated for turn_id {turn_id}.")

    except Exception as e:
        logger.error(f"CUSTOM_STT: Error during transcription for turn_id {turn_id}: {e!r}", exc_info=True)
        if websocket and websocket.client_state == websocket.client_state.CONNECTED:
            await websocket.send_json({"type": "error", "message": f"Custom STT error: {str(e)}", "turn_id": turn_id})

class LiveSessionManager:
    def __init__(self):
        # ... (existing attributes)
        self.user_audio_buffer_for_custom_stt = []
        self.current_turn_id = None
        self.turn_counter = 0

    async def start_session(self, websocket: WebSocket):
        # ... (existing code)
        self.user_audio_buffer_for_custom_stt = []
        self.current_turn_id = None
        self.turn_counter = 0
        # ...

    async def _send_audio_chunks(self, session: genai.live.AsyncSession):
        # ...
        # Modify this if you need to directly control buffering from here,
        # but it's simpler to buffer in handle_audio_chunk or based on signals from _receive_genai_data
        # For now, let's assume audio sent to GenAI is also what we want for custom STT,
        # so buffering will happen based on signals in _receive_genai_data.
        # The audio chunks are already being put into self.audio_queue.
        # The key is to also copy them to self.user_audio_buffer_for_custom_stt
        # when a user turn is active.
        #
        # Simpler: in handle_audio_chunk, if a turn is active, add to buffer.
        # ...
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

                if audio_chunk is None: # Stop signal
                    logger.info("Stop signal (None) received in audio sender. Ending sending task.")
                    # If there's pending audio for custom STT, transcribe it
                    if self.current_turn_id and self.user_audio_buffer_for_custom_stt:
                        logger.info(f"CUSTOM_STT: Session stopping, transcribing buffered audio for turn {self.current_turn_id}.")
                        asyncio.create_task(
                            transcribe_user_audio_custom_stt(
                                list(self.user_audio_buffer_for_custom_stt), # Send a copy
                                self.websocket,
                                self.current_turn_id
                            )
                        )
                        self.user_audio_buffer_for_custom_stt.clear()
                    break
                
                if not self.live_session_active:
                    logger.warning("Session became inactive in _send_audio_chunks after getting chunk, stopping audio send.")
                    break
                
                # Add to custom STT buffer if a user turn is active
                if self.current_turn_id:
                    self.user_audio_buffer_for_custom_stt.append(audio_chunk)

                try:
                    await session.send(input={"data": audio_chunk, "mime_type": "audio/pcm"})
                    self.audio_queue.task_done()
                except Exception as e_send: 
                    logger.error(f"Error sending audio chunk to GenAI: {e_send!r}", exc_info=True)
                    self.live_session_active = False 
                    raise
                                
        except asyncio.CancelledError:
            logger.info("Audio chunk sending task cancelled.")
        except Exception as e:
            logger.error(f"Critical error in _send_audio_chunks: {e!r}", exc_info=True)
            self.live_session_active = False
            raise
        finally:
            logger.info("Audio chunk sending task finished.")


    async def _receive_genai_data(self, session: genai.live.AsyncSession):
        # ...
        try:
            while self.live_session_active:
                turn = session.receive()
                async for response in turn:
                    # ... (existing data and model_transcript handling) ...

                    if response.server_content and \
                       hasattr(response.server_content, 'interim_transcription') and \
                       response.server_content.interim_transcription and \
                       response.server_content.interim_transcription.text:
                        
                        interim_text = response.server_content.interim_transcription.text
                        is_final_part = getattr(response.server_content.interim_transcription, 'is_final', False)
                        
                        if not self.current_turn_id and interim_text.strip(): # Start of a new user turn
                            self.turn_counter += 1
                            self.current_turn_id = f"turn_{self.turn_counter}"
                            self.user_audio_buffer_for_custom_stt.clear() # Clear buffer for this new turn
                            logger.info(f"New user turn detected by interim_transcription: ID {self.current_turn_id}. Buffering audio for custom STT.")
                            if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                                await self.websocket.send_json({
                                    "type": "user_turn_start", # Notify frontend
                                    "turn_id": self.current_turn_id
                                })
                        
                        logger.info(f"User interim transcript (Gemini): '{interim_text}' (is_final_part: {is_final_part}, turn_id: {self.current_turn_id})")
                        if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                            await self.websocket.send_json({
                                "type": "user_transcript", # From Gemini
                                "data": interim_text,
                                "is_final_part": is_final_part,
                                "turn_id": self.current_turn_id
                            })
                        
                        if is_final_part and self.current_turn_id:
                            logger.info(f"CUSTOM_STT: User turn {self.current_turn_id} ended (is_final_part=True). Sending {len(self.user_audio_buffer_for_custom_stt)} buffered chunks.")
                            if self.user_audio_buffer_for_custom_stt:
                                asyncio.create_task(
                                    transcribe_user_audio_custom_stt(
                                        list(self.user_audio_buffer_for_custom_stt), # Send a copy
                                        self.websocket,
                                        self.current_turn_id
                                    )
                                )
                                self.user_audio_buffer_for_custom_stt.clear() # Clear after dispatching
                            # Keep self.current_turn_id active until the model responds to this turn

                    # Model's audio output transcription
                    if response.server_content and \
                       hasattr(response.server_content, 'output_transcription') and \
                       response.server_content.output_transcription and \
                       response.server_content.output_transcription.text:
                        model_transcript_text = response.server_content.output_transcription.text
                        logger.info(f"Model output transcript: '{model_transcript_text}' (for turn_id: {self.current_turn_id})")
                        if self.websocket and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                           await self.websocket.send_json({
                               "type": "model_transcript",
                               "data": model_transcript_text,
                               "turn_id": self.current_turn_id # Associate model response with the current user turn
                            })
                        # After model responds to the user's turn, clear the turn_id for the next cycle
                        if self.current_turn_id:
                            logger.info(f"Model has responded for turn {self.current_turn_id}. Clearing current_turn_id.")
                            self.current_turn_id = None
                # ...
        # ...

    # In handle_audio_chunk: No direct changes needed if _send_audio_chunks handles buffering
    # based on self.current_turn_id status.

    async def stop_session(self, notify_client=True):
        # ... existing code ...
        # Ensure any final buffered audio for custom STT is processed if a turn was active
        if self.current_turn_id and self.user_audio_buffer_for_custom_stt:
            logger.info(f"CUSTOM_STT: stop_session called, transcribing final buffered audio for turn {self.current_turn_id}.")
            # Create task, don't await here to avoid blocking stop_session
            asyncio.create_task(
                transcribe_user_audio_custom_stt(
                    list(self.user_audio_buffer_for_custom_stt),
                    self.websocket, # May or may not be connected by the time this runs
                    self.current_turn_id
                )
            )
            self.user_audio_buffer_for_custom_stt.clear()
        self.current_turn_id = None
        # ... rest of stop_session ...


Conceptual Frontend (script.js) Modifications:

// In static/script.js

// ... (existing variables)
const transcriptContainer = document.getElementById('transcriptContainer'); // Get the scrollable container

function appendTranscript(text, speaker, isFinalPart = false, turnId = null) {
    console.debug("Appending transcript:", { text, speaker, isFinalPart, turnId });

    let turnDiv;
    if (turnId) {
        turnDiv = document.getElementById(turnId);
        if (!turnDiv) {
            turnDiv = document.createElement('div');
            turnDiv.id = turnId;
            turnDiv.classList.add('turn-block');
            
            const turnHeader = document.createElement('p');
            turnHeader.classList.add('turn-header');
            // Extract turn number for display if desired: e.g., turn_1 -> Turn 1
            const turnNumberMatch = turnId.match(/_(\d+)$/);
            const turnNumberDisplay = turnNumberMatch ? `Turn ${turnNumberMatch[1]}` : turnId;
            turnHeader.textContent = `--- ${turnNumberDisplay} ---`;
            turnDiv.appendChild(turnHeader);
            
            transcriptDiv.appendChild(turnDiv); // Append new turn block to main transcript area
        }
    } else {
        // Fallback if no turnId - less ideal for ordering
        turnDiv = transcriptDiv; 
    }

    // Create the line for this piece of transcript
    const newLineDiv = document.createElement('div');
    newLineDiv.classList.add('transcript-line');
    // Add data attributes for easier selection/styling if needed
    newLineDiv.dataset.speaker = speaker;
    if (turnId) newLineDiv.dataset.turnId = turnId;


    const speakerPrefix = document.createElement('span');
    speakerPrefix.classList.add('speaker-prefix');
    let speakerLabel = "Unknown: ";
    if (speaker === 'user') speakerLabel = "You (Gemini): ";
    else if (speaker === 'custom_user') speakerLabel = "You (Custom STT): ";
    else if (speaker === 'model') speakerLabel = "Gemini: ";
    speakerPrefix.textContent = speakerLabel;
    newLineDiv.appendChild(speakerPrefix);

    const speakerTextSpan = document.createElement('span');
    speakerTextSpan.classList.add('speaker-text');
    
    // Handle interim results for Gemini user transcript - replace content of existing span
    if (speaker === 'user' && !isFinalPart && turnDiv.querySelector(`.transcript-line[data-speaker="user"][data-turn-id="${turnId}"] .speaker-text`)) {
        const existingUserTextSpan = turnDiv.querySelector(`.transcript-line[data-speaker="user"][data-turn-id="${turnId}"] .speaker-text`);
        if(existingUserTextSpan) {
            existingUserTextSpan.textContent = text; // Update existing interim
            transcriptContainer.scrollTop = transcriptContainer.scrollHeight;
            return; // Don't create a new line for interim updates
        }
    }
    speakerTextSpan.textContent = text;
    newLineDiv.appendChild(speakerTextSpan);


    // Logic for inserting custom_user_transcript before model's response for the same turn
    if (speaker === 'custom_user' && turnId) {
        const modelLineForTurn = turnDiv.querySelector(`.transcript-line[data-speaker="model"][data-turn-id="${turnId}"]`);
        if (modelLineForTurn) {
            turnDiv.insertBefore(newLineDiv, modelLineForTurn);
        } else {
            turnDiv.appendChild(newLineDiv); // Append if model line not yet there
        }
    } else {
         turnDiv.appendChild(newLineDiv);
    }
    
    transcriptContainer.scrollTop = transcriptContainer.scrollHeight;
}


// In socket.onmessage WebSocket handler:
// ...
const message = JSON.parse(messageText);
console.debug("Parsed server message:", message);

if (message.type === "user_turn_start") {
    // Optionally pre-create the turn div or update UI to show user is speaking
    let turnDiv = document.getElementById(message.turn_id);
    if (!turnDiv) {
        turnDiv = document.createElement('div');
        turnDiv.id = message.turn_id;
        turnDiv.classList.add('turn-block');
        const turnHeader = document.createElement('p');
        turnHeader.classList.add('turn-header');
        const turnNumberMatch = message.turn_id.match(/_(\d+)$/);
        const turnNumberDisplay = turnNumberMatch ? `Turn ${turnNumberMatch[1]}` : message.turn_id;
        turnHeader.textContent = `--- ${turnNumberDisplay} (User speaking...) ---`;
        turnDiv.appendChild(turnHeader);
        transcriptDiv.appendChild(turnDiv);
        transcriptContainer.scrollTop = transcriptContainer.scrollHeight;
    } else {
        // Update header if turn div already exists
        const header = turnDiv.querySelector('.turn-header');
        if (header) header.textContent = header.textContent.replace(" (User speaking...)", "") + " (User speaking...)";
    }
} else if (message.type === "model_transcript") {
    appendTranscript(message.data, 'model', true, message.turn_id);
} else if (message.type === "user_transcript") { // From Gemini Live API
    appendTranscript(message.data, 'user', message.is_final_part, message.turn_id);
} else if (message.type === "custom_user_transcript") { // From your custom STT
    appendTranscript(message.data, 'custom_user', true, message.turn_id);
} // ... rest of message handling ...
// ...
IGNORE_WHEN_COPYING_START
content_copy
download
Use code with caution.
JavaScript
IGNORE_WHEN_COPYING_END

Key Considerations:

Audio Format: Ensure the audio sent to Google Cloud STT (LINEAR16, 16000Hz, mono) matches what the API expects and what your client is sending.

API Keys & Authentication for Cloud STT: Your backend environment needs to be authenticated to use the Google Cloud Speech-to-Text API (usually via service account credentials or application default credentials).

Error Handling: Robust error handling for the custom STT calls is important.

Cost: Be mindful that using Google Cloud STT will incur costs separate from the Gemini API.

Complexity: This adds a fair bit of complexity to the backend and frontend logic for managing turns and asynchronous data.

This detailed plan should give you a solid foundation to implement this feature. Start with the backend changes, test the custom STT independently, and then integrate the frontend logic for displaying the transcripts in order.