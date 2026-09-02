# GS Podcast Studio V5

A stable FastAPI web version of the podcast studio. It avoids the Flet runtime layer and uses normal browser HTML/JavaScript, making it better suited to Render.

## Features
- Login/register/admin
- Large multiline script/topic editor + one-click Clear Text
- AI rewrite
- Gemini, Groq, OpenRouter Free, OpenAI, Ollama Local
- Multiple API keys with fallback attempts
- Current Gemini model choices including 3.7, 3.6 and 3.5 families
- Edge TTS voices with male/female choices and gTTS fallback
- 1–4 speakers, names and genders
- Intro/outro
- Soft generated background ambience with volume control
- AI-generated title and description
- Original local podcast cover artwork
- MP3 and optional WAV export
- Chapter markers
- Episode history
- Browser Play button
- MP3/WAV downloads
- WhatsApp share link
- Logs and admin user panel
- SQLite persistence

## Render
Build: `apt-get update && apt-get install -y ffmpeg && pip install -r requirements.txt`
Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

Default administrator: `admin` / `smbagathi`

## Important Render note
Render free web services have ephemeral filesystems. For permanent production storage, move SQLite to PostgreSQL and audio/cover files to an S3-compatible object store. The application is otherwise self-contained.

Never commit real API keys to GitHub.
