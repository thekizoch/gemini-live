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
    transcriptDiv.innerHTML += text;
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
        
        // Check if the AudioContext sample rate matches the requested one.
        // Some browsers might not honor it exactly, though it's more reliable now.
        if (audioContext.sampleRate !== AUDIO_SAMPLE_RATE) {
            updateStatus(`Warning: AudioContext running at ${audioContext.sampleRate}Hz, requested ${AUDIO_SAMPLE_RATE}Hz. This might affect quality or API compatibility.`, true);
            // For robust solution, client-side resampling (e.g. using a library or custom AudioWorklet) would be needed.
            // This example proceeds assuming the browser provides matching or close enough sample rate.
        }

        await audioContext.audioWorklet.addModule('/static/audio-processor.js');
        microphoneSource = audioContext.createMediaStreamSource(userMediaStream);
        audioProcessorNode = new AudioWorkletNode(audioContext, 'audio-processor-worklet', {
            processorOptions: {
                bufferSize: 2048 // Send chunks of this many samples (e.g. 2048 samples / 16000 Hz = 128ms)
            }
        });

        audioProcessorNode.port.onmessage = (event) => {
            if (socket && socket.readyState === WebSocket.OPEN && isSessionActive) {
                const pcm16Data = event.data; // Int16Array
                // Convert Int16Array to raw bytes (Uint8Array)
                const buffer = new ArrayBuffer(pcm16Data.length * 2);
                const view = new DataView(buffer);
                for (let i = 0; i < pcm16Data.length; i++) {
                    view.setInt16(i * 2, pcm16Data[i], true); // true for little-endian
                }
                socket.send(new Uint8Array(buffer));
            }
        };

        microphoneSource.connect(audioProcessorNode);
        // It's often good practice to connect the worklet to the destination to keep it processing,
        // even if you don't want to hear the audio.
        // audioProcessorNode.connect(audioContext.destination); // Uncomment if playback/monitoring is desired (and handle feedback)
        // For this use case (sending to backend), connecting to destination is not strictly needed
        // as long as the stream is active and data flows to the worklet.

        updateStatus("Audio capture started. Sending data...");
        console.log("Audio capture successfully started.");

    } catch (err) {
        updateStatus(`Error starting audio capture: ${err.message}`, true);
        console.error("Audio capture error:", err);
        throw err; // Re-throw to be caught by the caller
    }
}

function stopAudioCapture() {
    console.log("Attempting to stop audio capture...");
    if (userMediaStream) {
        userMediaStream.getTracks().forEach(track => track.stop());
        userMediaStream = null;
    }
    if (audioProcessorNode) {
        audioProcessorNode.port.postMessage('stop'); // Signal worklet if it needs cleanup
        audioProcessorNode.disconnect();
        audioProcessorNode = null;
    }
    if (microphoneSource) {
        microphoneSource.disconnect();
        microphoneSource = null;
    }
    if (audioContext && audioContext.state !== 'closed') {
        audioContext.close().then(() => {
            console.log("AudioContext closed.");
            audioContext = null;
        }).catch(err => console.error("Error closing AudioContext:", err));
    }
    updateStatus("Audio capture stopped.");
    console.log("Audio capture stopped.");
}

sessionButton.onclick = () => {
    if (!isSessionActive) {
        console.log("Start Session button clicked.");
        // Start session
        transcriptDiv.innerHTML = ""; // Clear previous transcript
        updateStatus("Connecting to server...");

        // Use wss:// if site is HTTPS, ws:// if HTTP
        const wsProtocol = window.location.protocol === "https:" ? "wss://" : "ws://";
        socket = new WebSocket(`${wsProtocol}${window.location.host}/ws`);

        socket.onopen = async () => {
            updateStatus("Connection established. Starting session...");
            console.log("WebSocket: Connection established. Sending start_session command.");
            socket.send(JSON.stringify({ command: "start_session" }));
        };

        socket.onmessage = (event) => {
            console.log("WebSocket: Message received from server:", event.data);
            try {
                const message = JSON.parse(event.data);
                console.debug("Parsed server message:", message); // More detailed log for parsed message

                if (message.type === "transcript") {
                    appendTranscript(message.data);
                } else if (message.status === "info") {
                    updateStatus(message.message);
                    if (message.message.includes("Live session started")) {
                        isSessionActive = true;
                        sessionButton.textContent = "Stop Session";
                        sessionButton.classList.add("recording");
                        console.log("UI updated to recording state.");
                        startAudioCapture().catch(err => {
                            updateStatus(`Audio capture failed: ${err.message}. Stopping session.`, true);
                            console.error("Audio capture failed after session start:", err.message);
                            if (socket && socket.readyState === WebSocket.OPEN) {
                                socket.send(JSON.stringify({ command: "stop_session" }));
                                console.log("Sent stop_session command due to audio capture failure.");
                            }
                            // UI reset will be handled by stop_session message or onclose
                        });
                    } else if (message.message.includes("stopped")) {
                        // This confirms session stop from backend
                        isSessionActive = false;
                        sessionButton.textContent = "Start Session";
                        sessionButton.classList.remove("recording");
                        stopAudioCapture(); // Ensure audio capture is also stopped
                        updateStatus("Session stopped."); // Final status
                        console.log("Session stopped (confirmed by backend). UI reset.");
                    }
                } else if (message.status === "error" || message.type === "error") {
                    updateStatus(`Error: ${message.message}`, true);
                    console.error("Error message from server:", message.message);
                    // If a critical server error occurs, attempt to clean up UI
                    if (isSessionActive) { // If we thought a session was active
                        isSessionActive = false;
                        sessionButton.textContent = "Start Session";
                        sessionButton.classList.remove("recording");
                        stopAudioCapture();
                        console.log("UI reset due to server error during active session.");
                    }
                     if (socket && socket.readyState === WebSocket.OPEN && !message.message.includes("already active")) { // Don't close if it's "already active" error
                        // socket.close(); // Or let server handle it
                        console.log("Server error processed, socket remains open unless it was 'already active' error related to this client.");
                    }
                }  else if (message.status === "warning") {
                    updateStatus(`Warning: ${message.message}`, false);
                    console.warn("Warning message from server:", message.message);
                }
            } catch (e) {
                console.error("Error processing message from server:", e);
                updateStatus("Received malformed message from server.", true);
            }
        };

        socket.onerror = (error) => {
            updateStatus(`WebSocket Error. Check console. Is the backend running?`, true);
            console.error("WebSocket Error: ", error);
            // No need to change isSessionActive here, onclose will handle it
        };

        socket.onclose = (event) => {
            updateStatus(`Connection closed. Code: ${event.code}. ${event.reason || ''}`);
            console.log(`WebSocket: Connection closed. Code: ${event.code}, Reason: '${event.reason}', Clean: ${event.wasClean}`);
            isSessionActive = false;
            sessionButton.textContent = "Start Session";
            sessionButton.classList.remove("recording");
            stopAudioCapture(); // Ensure audio capture is stopped when connection closes
            if (event.wasClean) {
                console.log(`WebSocket closed cleanly, code=${event.code} reason=${event.reason}`);
            } else {
                console.warn('WebSocket connection died');
            }
            socket = null; // Clear socket variable
            console.log("UI reset and socket cleared due to WebSocket close.");
        };

    } else {
        console.log("Stop Session button clicked.");
        // Stop session
        updateStatus("Stopping session...");
        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ command: "stop_session" }));
        } else {
            // If socket is not open, manually reset UI as backend won't confirm
            isSessionActive = false;
            sessionButton.textContent = "Start Session";
            sessionButton.classList.remove("recording");
            updateStatus("Session stopped (no active connection).");
            console.log("Session stopped locally (no active connection). UI reset.");
        }
        stopAudioCapture(); // Stop audio capture locally immediately
        console.log("stopAudioCapture called locally.");
        // isSessionActive and button text will be fully updated by server "stopped" message or onclose.
    }
};

window.onload = () => {
    updateStatus("Ready. Click 'Start Session' to begin.");
    console.log("Page loaded. Initial status set.");
    if (!window.AudioContext && !window.webkitAudioContext) {
        updateStatus("AudioContext not supported by this browser. Live transcription will not work.", true);
        sessionButton.disabled = true;
        console.error("AudioContext not supported.");
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        updateStatus("MediaDevices API (getUserMedia) not supported by this browser. Live transcription will not work.", true);
        sessionButton.disabled = true;
        console.error("MediaDevices API (getUserMedia) not supported.");
    }
};