# Encrypted AI Provider Keys

Run `SETUP.bat` to choose an AI provider and save that provider's API key.

Recommended public option: OpenRouter.

The encrypted key vault is named `api_keys.env.enc`. It may contain an OpenRouter, DeepSeek, OpenAI, Anthropic, Google, or Gemini key.

The passphrase is never stored in this folder. `RUN.bat` asks for it when needed and loads the key only into the running process environment.

Do not share a folder containing your own `api_keys.env.enc` unless you intend the recipient to use it and know the passphrase.
