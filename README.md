# GS Podcast Studio V6

Upgrade of the working FastAPI podcast studio. The original workspace already had login/admin, AI rewrite, multiple speakers, Edge TTS, ambience, MP3/WAV export, browser playback, WhatsApp sharing, logs and SQLite. V6 extends that foundation rather than changing the product direction.

## Added in V6
- 20 language/script choices, including Swahili, Sheng and Swahili + English.
- More Edge TTS voices and automatic closest-voice fallback.
- Speaker gender + age profiles: Child, Adult, Old. Age is expressed with pitch processing.
- AI selector in the workspace. Gemini, OpenAI, DeepSeek, Grok/xAI, Perplexity, Qwen, Z.ai, Groq, OpenRouter, Ollama and Custom OpenAI-compatible endpoints.
- Admin system AI configuration plus encrypted per-user API overrides.
- Upload background music, choose use/remove, control music volume, and merge an extra audio track.
- Built-in synthetic audience atmosphere: cheering/applause, questions, comments and contributions.
- Online HTML5 player, MP3/WAV download, WhatsApp and email share links.
- User creation, activation/deactivation, deletion and roles.
- Per-user podcast history. Admin configuration dashboard exposes provider state, database counts and runtime/storage configuration.

## Important
- Sheng is a language/script mode, not a separate Edge TTS locale. The system uses Kenyan Swahili TTS for Sheng and Swahili+English so code-switching is preserved by the generated script.
- Free AI availability depends on each provider's current plan/model limits. OpenRouter can be used with free models when they are available.
- Render free services have ephemeral local storage. For permanent podcasts, migrate the database to PostgreSQL and media to S3-compatible object storage.
- Set `PUBLIC_URL`/Configuration public URL to your deployed URL for clean sharing links.
- Never commit API keys.

## Run locally
```bash
pip install -r requirements.txt
# FFmpeg must be installed and on PATH
uvicorn main:app --reload
```
