

import asyncio
import wave
import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

async def main():
    """Connects to Gemini, sends a message, receives audio, and saves it to a WAV file."""
    try:
        api_key = os.getenv("GOOGLE_GENAI_API_KEY")
        if not api_key:
            print("Error: GOOGLE_GENAI_API_KEY environment variable not set.")
            return


        client = genai.Client(api_key=api_key)
        
        # Model specified in the Live API documentation
        model = "gemini-2.0-flash-live-001"
        config = {"response_modalities": ["AUDIO"]}
        output_filename = "audio_from_gemini.wav"

        print(f"Attempting to connect to Gemini model: {model} using genai.Client().aio.live.connect...")
        async with client.aio.live.connect(model=model, config=config) as session:
            print(f"Successfully connected. Opening {output_filename} for writing audio.")
            with wave.open(output_filename, "wb") as wf:
                wf.setnchannels(1)      # Mono audio
                wf.setsampwidth(2)      # 16-bit PCM
                wf.setframerate(24000)  # 24kHz sample rate (as per Live API output format)

                message_to_send = "Hello? Gemini are you there? Tell me a very short and happy story."
                print("Sending message to Gemini")
                
                # Using the structure from the Live API documentation for sending client content
                await session.send_client_content(
                    turns=[{"role": "user", "parts": [{"text": message_to_send}]}], 
                    turn_complete=True
                )
                print("Message sent successfully. Waiting for audio response...")

                audio_data_received = False
                async for response in session.receive():
                    if response.data is not None:
                        wf.writeframes(response.data)
                        audio_data_received = True
                        # Print a small confirmation for each chunk to show progress
                        print(f"Received and wrote audio chunk: {len(response.data)} bytes.")
                    
                    # Check for server content updates, like generation completion
                    if response.server_content:
                        if hasattr(response.server_content, 'generation_complete') and response.server_content.generation_complete:
                            print("Model has indicated that content generation is complete.")
                            break # Exit the loop as generation is done
                    
                    # Check if the server is about to close the connection
                    if response.go_away:
                        print(f"Server sent a GoAway message: timeLeft {response.go_away.time_left}s. Connection will close.")
                        break
                
                if audio_data_received:
                    print(f"Finished receiving audio data. Audio saved to {output_filename}")
                else:
                    print("No audio data was received from Gemini.")

            print(f"WAV file '{output_filename}' has been closed.")
        print("Gemini session closed.")

    except AttributeError as e:
        print(f"An AttributeError occurred: {e}")
        print("This might be due to an outdated 'google-genai' SDK or changes in the Live API (it's in Preview).")
        print("Please ensure your SDK is up to date: pip install -U google-genai")
        import traceback
        traceback.print_exc()
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Ensure you have set your GEMINI_API_KEY as an environment variable
    # Example (in bash/zsh):
    #   export GEMINI_API_KEY="YOUR_ACTUAL_GEMINI_API_KEY"
    # Then run the script:
    #   python gemini_audio_receiver.py
    print("Starting Gemini Live API audio receiver script...")
    asyncio.run(main()) 

# running successfully creates a file called audio_from_gemini.wav