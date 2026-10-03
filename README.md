# Coaching Institute Enquiry Bot (Telegram + FastAPI + Gemini)

Answers parents' and students' questions from `institute_info.txt`, remembers the
conversation, collects name / class / phone, and gives the owner a leads page.

## Run it locally

1. Python 3.10+ is needed.
   ```
   python -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Copy `env.example` to `.env` and fill in the values
   (Telegram token from @BotFather, Gemini key from Google AI Studio).
3. Start a tunnel so Telegram can reach your laptop (in a second terminal):
   ```
   ngrok http 8000
   ```
   Put the `https://...` URL it prints into `PUBLIC_URL` in `.env`.
4. Start the app (it registers the webhook with Telegram automatically):
   ```
   uvicorn main:app --port 8000
   ```
5. Message your bot on Telegram.
6. Owner view: `http://localhost:8000/leads?key=YOUR_ADMIN_KEY`

Free tunnel URLs change whenever you restart the tunnel. Update `PUBLIC_URL` and
restart the app each time.

## Use it for a real institute

- Replace the contents of `institute_info.txt` with the real details and have the
  owner check every line.
- Set `INSTITUTE_NAME` in `.env`.
- Restart the app after editing `institute_info.txt`.

## Troubleshooting

- Open `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` and read `last_error_message`.
- Check the uvicorn terminal for errors.
- "Gemini error 429": you hit the free-tier rate limit. Wait a minute or check your quota.
- "Gemini error 404": the model name in `GEMINI_MODEL` is wrong or retired.

## Files

- `main.py`: the whole app
- `institute_info.txt`: the bot's knowledge
- `bot.db`: SQLite database, created automatically (chat history + leads)
