// static/audio-processor.js
class AudioProcessorWorklet extends AudioWorkletProcessor {
    constructor(options) {
    super(options);
    // bufferSize specifies how many samples to collect before sending.
    // e.g., 2048 samples at 16000 Hz = 128ms of audio per message.
    this.bufferSize = options.processorOptions?.bufferSize || 2048;
    this.ringBuffer = new Int16Array(this.bufferSize);
    this.ringBufferWriteIndex = 0;
    this.stopped = false;
    this.port.onmessage = (event) => {
        if (event.data === 'stop') {
            this.stopped = true;
            // If there's any pending data in buffer when stopping, send it.
            if (this.ringBufferWriteIndex > 0) {
                this.port.postMessage(this.ringBuffer.slice(0, this.ringBufferWriteIndex));
                this.ringBufferWriteIndex = 0;
            }
        }
    };
}

process(inputs, outputs, parameters) {
    if (this.stopped) {
        return false; // Stop processing if flagged
    }

    const input = inputs[0]; // Assuming mono input from microphoneSource

    if (input && input.length > 0) {
        const channelData = input[0]; // Float32Array from -1.0 to 1.0

        if (!channelData || channelData.length === 0) {
            return true; // No data in this frame, keep processor alive
        }

        // Convert Float32 to Int16 PCM
        for (let i = 0; i < channelData.length; i++) {
            if (this.stopped) break; // Check stop flag during loop

            const s = Math.max(-1, Math.min(1, channelData[i])); // Clamp values
            this.ringBuffer[this.ringBufferWriteIndex++] = s < 0 ? s * 0x8000 : s * 0x7FFF; // Scale to Int16 range

            if (this.ringBufferWriteIndex >= this.bufferSize) {
                this.port.postMessage(this.ringBuffer.slice(0, this.ringBufferWriteIndex)); // Send a copy
                this.ringBufferWriteIndex = 0;
            }
        }
    }
    return true; // Keep processor alive
}
}
try {
registerProcessor('audio-processor-worklet', AudioProcessorWorklet);
} catch (e) {
console.error("Failed to register AudioWorkletProcessor:", e);
// This error typically means the script is not loaded in an AudioWorkletGlobalScope
}