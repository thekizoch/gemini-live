- GOOGLE_GENAI_API_KEY is set in the .env file
- we can start and stop the live session with a single button
- api.py file is the backend. use fastapi
- have have a simple js/html frontend that allows a user to see the transcript in real time, and send audio messages to the backend
- the session stays open so i can ask multiple questions with audio.

- we follow the google genai live api guidelines as closely as possible, if not exactly. https://ai.google.dev/gemini-api/docs/live
- for the audio part, we can use this quickstart: https://github.com/google-gemini/cookbook/blob/main/quickstarts/Get_started_LiveAPI.py

- simple without over engineering.

- we can see the transcript in real time, but without introducing latency on the audio (i.e. we send/receive audio as it comes, transcription can be delayed)