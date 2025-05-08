const sessionButton = document.getElementById('sessionButton');
const statusDiv = document.getElementById('status');
const transcriptDiv = document.getElementById('transcript');

let socket;
let isSessionActive = false;
const AUDIO_SAMPLE_RATE = 16000; // Required by Gemini

// AudioContext and related variables for raw PCM data
let audioContext;
let microphoneSource;
let audioProcessorNode;
let userMediaStream;

// MODIFICATION: Variables for playing audio received from the server
let clientPlaybackAudioContext;
let audioBufferQueue = [];
let isPlayingModelAudio = false;
let nextModelAudioStartTime = 0;
const MODEL_AUDIO_SAMPLE_RATE = 24000; // Gemini Live API audio output is 24kHz

function updateStatus(message, isError = false) {
    statusDiv.textContent = `Status: ${message}`;
    statusDiv.style.color = isError ? '#c0392b' : '#2c3e50'; // Red for error, dark blue for normal
    if (isError) {
        console.error(`Status Update (Error): ${message}`);
    } else {
        console.log(`Status Update: ${message}`);
    }
}

function appendTranscript(text) {
    transcriptDiv.innerHTML += text; // Server now sends text with prefixes and newlines
    transcriptDiv.parentElement.scrollTop = transcriptDiv.parentElement.scrollHeight; // Auto-scroll container
    console.log("Transcript appended:", text);
}

async function startAudioCapture() {
    console.log("Attempting to start audio capture...");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        updateStatus("getUserMedia not supported on your browser!", true);
        console.error("getUserMedia not supported.");
        throw new Error("getUserMedia not supported");
    }

    try {
        userMediaStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                sampleRate: AUDIO_SAMPLE_RATE,
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true
            }
        });

        audioContext = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: AUDIO_SAMPLE_RATE
        });
        
        if (audioContext.sampleRate !== AUDIO_SAMPLE_RATE) {
            updateStatus(`Warning: AudioContext running at ${audioContext.sampleRate}Hz, requested ${AUDIO_SAMPLE_RATE}Hz. This might affect quality or API compatibility.`, true);
        }

        await audioContext.audioWorklet.addModule('/static/audio-processor.js');
        microphoneSource = audioContext.createMediaStreamSource(userMediaStream);
        audioProcessorNode = new AudioWorkletNode(audioContext, 'audio-processor-worklet', {
            processorOptions: {
                bufferSize: 2048 
            }
        });

        audioProcessorNode.port.onmessage = (event) => {
            if (socket && socket.readyState === WebSocket.OPEN && isSessionActive) {
                const pcm16Data = event.data; // Int16Array
                const buffer = new ArrayBuffer(pcm16Data.length * 2);
                const view = new DataView(buffer);
                for (let i = 0; i < pcm16Data.length; i++) {
                    view.setInt16(i * 2, pcm16Data[i], true); 
                }
                socket.send(new Uint8Array(buffer));
            }
        };

        microphoneSource.connect(audioProcessorNode);
        // audioProcessorNode.connect(audioContext.destination); // Uncomment for local monitoring (beware of feedback)

        updateStatus("Audio capture started. Sending data...");
        console.log("Audio capture successfully started.");

    } catch (err) {
        updateStatus(`Error starting audio capture: ${err.message}`, true);
        console.error("Audio capture error:", err);
        throw err; 
    }
}

function stopAudioCapture() {
    console.log("Attempting to stop audio capture (user input)...");
    if (userMediaStream) {
        userMediaStream.getTracks().forEach(track => track.stop());
        userMediaStream = null;
    }
    if (audioProcessorNode) {
        audioProcessorNode.port.postMessage('stop'); 
        audioProcessorNode.disconnect();
        audioProcessorNode = null;
    }
    if (microphoneSource) {
        microphoneSource.disconnect();
        microphoneSource = null;
    }
    if (audioContext && audioContext.state !== 'closed') {
        audioContext.close().then(() => {
            console.log("AudioContext (for user input) closed.");
            audioContext = null;
        }).catch(err => console.error("Error closing AudioContext (user input):", err));
    }
    console.log("Audio capture (user input) stopped.");
}

function initClientPlaybackAudioContext() {
    if (!clientPlaybackAudioContext || clientPlaybackAudioContext.state === 'closed') {
        console.log("Initializing AudioContext for model playback at " + MODEL_AUDIO_SAMPLE_RATE + "Hz.");
        clientPlaybackAudioContext = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: MODEL_AUDIO_SAMPLE_RATE
        });
        nextModelAudioStartTime = 0;
        audioBufferQueue = [];
        isPlayingModelAudio = false;
    }
     if (clientPlaybackAudioContext.state === 'suspended') {
        clientPlaybackAudioContext.resume().then(() => {
            console.log("Resumed AudioContext for model playback.");
        }).catch(e => console.error("Error resuming playback AudioContext", e));
    }
}

function playNextChunkFromQueue() {
    if (isPlayingModelAudio || audioBufferQueue.length === 0 || !clientPlaybackAudioContext || clientPlaybackAudioContext.state !== 'running') {
        if (audioBufferQueue.length > 0 && (!clientPlaybackAudioContext || clientPlaybackAudioContext.state !== 'running')) {
             console.warn("Playback: AudioContext not running or not initialized. Buffering audio.");
        }
        return;
    }
    isPlayingModelAudio = true;

    const pcm16Data = audioBufferQueue.shift(); 
    const float32Data = new Float32Array(pcm16Data.length);
    for (let i = 0; i < pcm16Data.length; i++) {
        float32Data[i] = pcm16Data[i] / 32768.0; 
    }

    const audioBuffer = clientPlaybackAudioContext.createBuffer(1, float32Data.length, MODEL_AUDIO_SAMPLE_RATE);
    audioBuffer.copyToChannel(float32Data, 0);

    const source = clientPlaybackAudioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(clientPlaybackAudioContext.destination);

    const currentTime = clientPlaybackAudioContext.currentTime;
    let startTime = nextModelAudioStartTime;
    if (startTime < currentTime) {
        startTime = currentTime;
    }
    
    console.debug(`Scheduling audio playback: start at ${startTime}, duration ${audioBuffer.duration}`);
    source.start(startTime);
    nextModelAudioStartTime = startTime + audioBuffer.duration;

    source.onended = () => {
        console.debug("Audio chunk playback ended.");
        isPlayingModelAudio = false;
        playNextChunkFromQueue(); 
    };
}

function stopClientPlayback() {
    console.log("Stopping client audio playback for model response.");
    audioBufferQueue = []; 
    if (clientPlaybackAudioContext && clientPlaybackAudioContext.state !== 'closed') {
        // Stop any playing sources by disconnecting and creating a new context later if needed.
        // Or, more simply, just close it. The next init will create a new one.
        clientPlaybackAudioContext.close().then(() => {
            console.log("AudioContext for model playback closed.");
        }).catch(e => console.error("Error closing playback AudioContext", e));
        clientPlaybackAudioContext = null;
    }
    isPlayingModelAudio = false;
    nextModelAudioStartTime = 0;
}

sessionButton.onclick = () => {
    if (!isSessionActive) {
        console.log("Start Session button clicked.");
        transcriptDiv.innerHTML = "";
        updateStatus("Connecting to server...");

        const wsProtocol = window.location.protocol === "https:" ? "wss://" : "ws://";
        socket = new WebSocket(`${wsProtocol}${window.location.host}/ws`);
        socket.binaryType = "arraybuffer"; // Ensure binary data is ArrayBuffer

        socket.onopen = async () => {
            updateStatus("Connection established. Initializing audio and session...");
            console.log("WebSocket: Connection established.");

            try {
                await startAudioCapture();
                console.log("User audio capture initialized by client proactively.");
                
                socket.send(JSON.stringify({ command: "start_session" }));
                console.log("Sent start_session command to server.");
            } catch (err) {
                updateStatus(`Audio capture failed: ${err.message}. Session cannot start.`, true);
                console.error("Proactive audio capture failed:", err);
                if (socket && socket.readyState === WebSocket.OPEN) {
                    socket.close();
                }
            }
        };

        socket.onmessage = (event) => {
            if (event.data instanceof ArrayBuffer) {
                console.log("WebSocket: Received binary audio data from server:", event.data.byteLength + " bytes");
                const pcm16Array = new Int16Array(event.data); 
                audioBufferQueue.push(pcm16Array.slice()); 

                if (!clientPlaybackAudioContext || clientPlaybackAudioContext.state === 'closed') {
                    initClientPlaybackAudioContext();
                } else if (clientPlaybackAudioContext.state === 'suspended') {
                    clientPlaybackAudioContext.resume().catch(e => console.error("Error resuming playback context on data receive", e));
                }
                playNextChunkFromQueue();
                return; // IMPORTANT: Return after handling binary data
            }

            // Handle text data (JSON messages)
            let messageText = event.data;
            if (typeof messageText !== 'string') {
                console.error("Received non-string, non-ArrayBuffer message from server:", messageText);
                updateStatus("Received unexpected data type from server.", true);
                return;
            }
            console.log("WebSocket: Message received from server (text):", messageText);
            try {
                const message = JSON.parse(messageText);
                console.debug("Parsed server message:", message);

                if (message.type === "model_transcript") {
                    appendTranscript(message.data); // Already prefixed by server
                } else if (message.type === "user_transcript") {
                    appendTranscript(message.data); // Already prefixed by server
                } else if (message.type === "transcript") { // Legacy, if server ever sends it
                    appendTranscript("Transcript (legacy): " + message.data + "\n");
                } else if (message.status === "info") {
                    updateStatus(message.message);
                    if (message.message.includes("Live session started")) {
                        isSessionActive = true;
                        sessionButton.textContent = "Stop Session";
                        sessionButton.classList.add("recording");
                        initClientPlaybackAudioContext(); 
                        console.log("UI updated to recording state. GenAI session active.");
                    } else if (message.message.includes("stopped")) {
                        isSessionActive = false;
                        sessionButton.textContent = "Start Session";
                        sessionButton.classList.remove("recording");
                        stopAudioCapture(); 
                        stopClientPlayback(); 
                        updateStatus("Session stopped.");
                        console.log("Session stopped (confirmed by backend). UI reset.");
                    }
                } else if (message.status === "error" || message.type === "error") {
                    updateStatus(`Error: ${message.message}`, true);
                    console.error("Error message from server:", message.message);
                    if (isSessionActive) { 
                        isSessionActive = false;
                        sessionButton.textContent = "Start Session";
                        sessionButton.classList.remove("recording");
                        stopAudioCapture();
                        stopClientPlayback(); 
                        console.log("UI reset due to server error during active session.");
                    }
                } else if (message.status === "warning") {
                    updateStatus(`Warning: ${message.message}`, false);
                    console.warn("Warning message from server:", message.message);
                }
            } catch (e) {
                console.error("Error processing text message from server:", e, "Raw data:", messageText);
                updateStatus("Received malformed JSON message from server.", true);
            }
        };

        socket.onerror = (error) => {
            updateStatus(`WebSocket Error. Check console. Is the backend running?`, true);
            console.error("WebSocket Error: ", error);
        };

        socket.onclose = (event) => {
            updateStatus(`Connection closed. Code: ${event.code}. ${event.reason || ''}`);
            console.log(`WebSocket: Connection closed. Code: ${event.code}, Reason: '${event.reason}', Clean: ${event.wasClean}`);
            isSessionActive = false;
            sessionButton.textContent = "Start Session";
            sessionButton.classList.remove("recording");
            stopAudioCapture(); 
            stopClientPlayback(); 
            socket = null; 
            console.log("UI reset and socket cleared due to WebSocket close.");
        };

    } else { // Stop session
        console.log("Stop Session button clicked.");
        updateStatus("Stopping session...");
        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ command: "stop_session" }));
        } else {
            // If no active connection, just update UI and state locally
            isSessionActive = false;
            sessionButton.textContent = "Start Session";
            sessionButton.classList.remove("recording");
            stopAudioCapture(); // Ensure local audio capture is stopped
            stopClientPlayback(); // Ensure local playback is stopped
            updateStatus("Session stopped (no active connection).");
            console.log("Session stopped locally (no active connection). UI reset.");
        }
        // stopAudioCapture and stopClientPlayback are also called here,
        // redundantly if socket was open and server confirms, but safe.
        // If socket wasn't open, this ensures cleanup.
    }
};

window.onload = () => {
    updateStatus("Ready. Click 'Start Session' to begin.");
    console.log("Page loaded. Initial status set.");
    if (!window.AudioContext && !window.webkitAudioContext) {
        updateStatus("AudioContext not supported by this browser. Live transcription will not work.", true);
        sessionButton.disabled = true;
        console.error("AudioContext not supported.");
    } else if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        updateStatus("MediaDevices API (getUserMedia) not supported by this browser. Live transcription will not work.", true);
        sessionButton.disabled = true;
        console.error("MediaDevices API (getUserMedia) not supported.");
    }
};