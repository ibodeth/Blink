# Blink

A local voice assistant that executes system commands, schedules timers, runs weather reports, and automates media playback based on voice commands.

## How it Works
The application uses Picovoice Porcupine for local wake-word detection, Microsoft Edge TTS for voice feedback, and Google Gemini to parse user intent into structured JSON commands. A PyQt5 interface renders a real-time waveform visualization synchronized with audio input.

## Tech Stack
- **Languages/Frameworks:** Python, PyQt5
- **Services/Libraries:** Google Gemini API, Picovoice Porcupine, Microsoft Edge TTS, Pygame, SQLite
- **Infrastructure:** Docker, Windows, Linux

## Quick Start (Docker)
```bash
docker compose up --build
```

## Local Setup
1. Clone the repository:
   ```bash
   git clone https://github.com/ibodeth/Blink.git
   cd Blink
   ```
2. Set up a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Run the application:
   ```bash
   python main.py
   ```

## License
MIT
