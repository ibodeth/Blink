# Blink 🤖🎧

[![Python](https://img.shields.io/badge/Python-3.11-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org)
[![Gemini](https://img.shields.io/badge/Gemini-AI-orange?style=for-the-badge&logo=google-gemini&logoColor=white)](https://deepmind.google/technologies/gemini/)
[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com)
[![License](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](LICENSE)

**Blink** is an advanced, AI-driven autonomous voice assistant designed for local systems automation, real-time audio visualization, context-aware memory processing, and media playback. Powered by Google Gemini and Picovoice Porcupine, it executes commands, schedules timers, runs weather reports, and launches or terminates processes in real-time.

---

## ✨ Key Features

### 🧠 Semantic AI Routing
* **Context-Aware Processing:** Remembers preferences, status logs, and custom schedules through a local SQLite database acting as its long-term memory.
* **JSON-Based Orchestration:** Dynamically parses natural language commands into modular executor objects.
* **Smart Fallbacks:** Automatically redirects application requests to streaming searches if a command target isn't found locally.

### 🎙️ Audio Pipeline & Speech Engine
* **High-Accuracy Wake Word:** Offloads wake-word listening to an efficient local Picovoice Porcupine engine.
* **Natural Speech Synthesis:** Connects to Microsoft Edge TTS (`en-US-AriaNeural`) to speak and output audio feedback.
* **Dynamic Visualization:** Siri-style animated PyQ5 visualizer scales amplitude pulses in sync with vocal input.

### 🎵 Media Automation Engine
* **Dynamic Downloader:** Scrapes and streams audio binaries on-demand using custom `yt-dlp` and `ffmpeg` pipelines.
* **Native Mixers:** Direct hardware audio playback using Pygame Mixer with interactive playback control.

---

## 🐳 Docker Development Environment

Deploy the development environment container instantly using Docker:

```bash
# Clone the repository
git clone https://github.com/ibodeth/Blink.git
cd Blink

# Start the environment
docker compose up --build
```

---

## 📥 Local Installation & Running

### Prerequisites
* Windows 10 / 11
* Python 3.11.x (Recommended: **3.11.9**)
* A working microphone and audio speakers
* Google Gemini API Key
* Picovoice Access Key

### Setup
```bash
# 1. Clone the repository
git clone https://github.com/ibodeth/Blink.git
cd Blink

# 2. Setup virtual environment
python -m venv venv
source venv/bin/activate  # Or on Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```
*Note: FFmpeg binaries are automatically downloaded and installed in the workspace directory on the first execution.*

### Run
```bash
python main.py
```

---

## 🗣️ Voice Commands Example

Once Blink is running, say the wake-word **"Blink"** or click the chat bubble to enter text:
* *"Play some lo-fi beats"*
* *"Remind me to take a break in 10 seconds"*
* *"What is the weather like in New York?"*
* *"Open Notepad"*
* *"Check my Python versions"*
* *"Pause music"*

---

## ⚙️ Architecture Overview
* **`BackendWorker` (QThread):** Orchestrates asynchronous AI generation, wake-word hooks, and system execution.
* **`BlinkOverlay` (QMainWindow):** Handles translucent window presentation, event processing, typewriter animations, and custom Siri-style canvas drawing.
* **`MusicEngine` / `MusicDownloader`:** Runs background YouTube search streams and handles local music decoding.
* **`MemoryManager`:** Encapsulates SQLite schemas for persistent long-term storage of preferences and status logs.
* **`SystemManager`:** Direct bindings to Windows OS processes and shell calls.

---

## 👨‍💻 Developer
**İbrahim Nuryağınlı**
* [GitHub](https://github.com/ibodeth)
* [LinkedIn](https://www.linkedin.com/in/ibrahimnuryaginli/)
* [Website](https://ibodeth.github.io/)

---

## 📄 License
This project is licensed under the MIT License. See the `LICENSE` file for details.
