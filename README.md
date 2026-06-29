# Music bot (AkerrySlave)

This project is a starter Discord music bot. It does not ship with any real credentials.

## Setup

1. Copy [.env.example](.env.example) to `.env` and fill in your own values.
2. Install dependencies:
   ```powershell
   python -m pip install -r requirements.txt
   ```
3. Run the bot:
   ```powershell
   python .\AkerrySlave.py
   ```

## Required values

Create a `.env` file with:

```env
DISCORD_TOKEN=your_discord_bot_token_here
SPOTIFY_CLIENT_ID=your_spotify_client_id_here
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret_here
```

- `DISCORD_TOKEN` is required.
- `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` are optional for Spotify link support.

## Notes

- The bot reads secrets from environment variables and from `.env`.
- Do not commit your real `.env` file or any generated cache/data files to GitHub.
- If you plan to publish this repository, keep only the example template and your code.
