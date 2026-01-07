# Blink 🤖🎧

**Yapay Zekâ Destekli Kişiselleştirilmiş Sesli Asistan**

![Python](https://img.shields.io/badge/Python-3.11-blue?style=for-the-badge\&logo=python\&logoColor=white)
![AI](https://img.shields.io/badge/Gemini-AI-orange?style=for-the-badge)
![Voice](https://img.shields.io/badge/Voice%20Assistant-Yes-success?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)

**Blink**, masaüstünde çalışan, **sesli komut algılayabilen**, **yapay zekâ destekli**, **müzik oynatabilen**, **hatırlatıcı kurabilen**, **uygulama açıp kapatabilen** gelişmiş bir **kişisel asistan** uygulamasıdır.


---

## ✨ Özellikler

### 🧠 Yapay Zekâ

* Google **Gemini API** ile doğal dil anlama
* Bağlam farkındalığı (önceki konuşmaları referans alır)
* JSON tabanlı görev ayrıştırma

### 🎙️ Sesli Asistan

* **Wake Word** (Picovoice Porcupine)
* Türkçe konuşma algılama
* Gerçek zamanlı ses görselleştirici
* Türkçe doğal TTS (Edge TTS)

### 🎵 Müzik

* YouTube üzerinden otomatik arama
* MP3 indirme & çalma (yt-dlp + FFmpeg)
* Oynat / durdur / sar
* Kapak ve süre çubuğu

### ⏰ Hatırlatıcı

* "10 dakika sonra hatırlat"
* Arka planda çalışan timer sistemi

### 🧩 Sistem Kontrolü

* Program açma / kapatma
* Sistem uygulamaları
* Python sürüm kontrolü
* Akıllı fallback (uygulama yoksa müzik olarak çalar)

### 💾 Hafıza

* SQLite tabanlı kalıcı hafıza
* Tercihler, olaylar ve durum bilgileri

### 🎨 Arayüz

* PyQt5 modern UI
* Siri tarzı animasyon
* Yazı yazma efekti
* Otomatik fade-out

---

## 🛠 Kullanılan Teknolojiler

* Python
* PyQt5
* Google Gemini API
* Picovoice Porcupine
* SpeechRecognition
* Edge TTS
* SQLite
* yt-dlp
* FFmpeg (otomatik kurulur)
* pygame

---

## 📋 Gereksinimler

> ⚠️ **Önerilen Python Sürümü:**
> **Python 3.11.9**

* Windows 10 / 11
* Mikrofon
* İnternet (ilk kurulum ve API servisleri için)

---

## 🔑 Gerekli API Anahtarları

* Google **Gemini API Key**
* **Picovoice Access Key**

İlk çalıştırmada otomatik istenir ve:

```txt
blink_keys.json
```

dosyasına kaydedilir.

---

## 📦 Kurulum

### 1️⃣ Depoyu Klonla

```bash
git clone https://github.com/ibodeth/Blink.git
cd Blink
```

### 2️⃣ Sanal Ortam (Önerilir)

```bash
python -m venv venv
venv\Scripts\activate
```

---

## ⚡ Otomatik Bağımlılık Kurulumu (TEK KOMUT)

Aşağıdaki komut **tüm bağımlılıkları otomatik kurar**:

```bash
pip install --upgrade pip && \
pip install PyQt5 pvporcupine pyaudio SpeechRecognition edge-tts google-generativeai yt-dlp pygame psutil requests AppOpener
```

> FFmpeg uygulama ilk çalıştığında **otomatik indirilir ve kurulur**.

---

## ▶️ Çalıştırma

```bash
python main.py
```

---

## 🗣️ Örnek Komutlar

```text
"Blink"
"Lo-fi çal"
"10 dakika sonra hatırlat"
"Spotify aç"
"Python sürümlerini kontrol et"
"Şarkıyı durdur"
```

---

## 🧠 Mimari Özet

* BackendWorker → AI, ses ve sistem işlemleri
* BlinkOverlay → UI & animasyon
* MusicEngine → Müzik yönetimi
* MemoryManager → Kalıcı hafıza
* SystemManager → OS entegrasyonu

---

## 👨‍💻 Geliştirici

**İbrahim Nuryağınlı**

* YouTube: [https://www.youtube.com/@ibrahim.python](https://www.youtube.com/@ibrahim.python)
* GitHub: [https://github.com/ibodeth](https://github.com/ibodeth)
* LinkedIn: [https://www.linkedin.com/in/ibrahimnuryaginli/](https://www.linkedin.com/in/ibrahimnuryaginli/)
* Website: [https://ibodeth.github.io/](https://ibodeth.github.io/)

---

## 📄 Lisans

Bu proje **MIT Lisansı** ile lisanslanmıştır.
Detaylar için `LICENSE` dosyasına bakınız.

---

⭐ Beğendiysen yıldızlamayı unutma!
