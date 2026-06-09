/**
 * Blink Jarvis — Frontend Refactor
 * =================================
 * A glassmorphic, cyberpunk-HUD interface for the Blink voice assistant.
 *
 * Design goals:
 *  - Pixel-perfect, premium dark glassmorphism with neon accents.
 *  - Fault-tolerant: auto-reconnecting WebSocket (exponential backoff),
 *    schema-defensive event parsing, and React error boundaries around every
 *    critical widget so one failing widget never takes down the app.
 *  - High performance: the high-frequency `audio_level` stream is applied via
 *    direct DOM / CSS-variable mutation inside a requestAnimationFrame loop,
 *    NOT React state, so it never triggers full-app re-renders.
 *
 * The component tree is intentionally modular and self-documenting:
 *   <App>
 *     <SetupOverlay/>            (shown when no API key is configured)
 *     <ReconnectBanner/>         (non-intrusive connection status)
 *     <Header/>                  (drag handle + live status pill)
 *     <VisualizerOrb/>           (HUD centerpiece, imperative audio reactivity)
 *     <ErrorBoundary><ChatHUD/></ErrorBoundary>
 *     <ErrorBoundary><TelemetryHUD/></ErrorBoundary>
 *     <ErrorBoundary><MediaPlayer/></ErrorBoundary>
 *     <Footer/>
 */

import React, {
  useState,
  useEffect,
  useRef,
  useCallback,
  useMemo,
  forwardRef,
  useImperativeHandle,
} from 'react';

/* ------------------------------------------------------------------ *
 * Theme — one accent palette per assistant state.
 * ------------------------------------------------------------------ */
const THEME = {
  idle: { color: '#38bdf8', glow: 'rgba(56, 189, 248, 0.30)', label: 'SLEEPING' },
  initializing: { color: '#f59e0b', glow: 'rgba(245, 158, 11, 0.45)', label: 'BOOTING' },
  listening: { color: '#f43f5e', glow: 'rgba(244, 63, 94, 0.55)', label: 'LISTENING' },
  thinking: { color: '#06b6d4', glow: 'rgba(6, 182, 212, 0.55)', label: 'PROCESSING' },
  speaking: { color: '#8b5cf6', glow: 'rgba(139, 92, 246, 0.55)', label: 'SPEAKING' },
};
const themeFor = (status) => THEME[status] || THEME.idle;

/* ------------------------------------------------------------------ *
 * Event normalisation.
 *
 * The backend is inconsistent: the worker thread emits FLAT events
 * (`{ type, status }`) while the FastAPI layer wraps data in a
 * `payload` object (`{ type, payload: { has_keys } }`). We flatten both
 * shapes into a single object so downstream code never has to care.
 * ------------------------------------------------------------------ */
function normalizeEvent(raw) {
  if (!raw || typeof raw !== 'object') return { type: 'unknown' };
  const payload =
    raw.payload && typeof raw.payload === 'object' ? raw.payload : {};
  // Flat keys take precedence; both shapes are merged defensively.
  return { ...payload, ...raw };
}

/** Safe numeric coercion with a fallback (never NaN). */
const num = (v, d = 0) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
};
/** Clamp helper. */
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/* ------------------------------------------------------------------ *
 * Minimal, safe Markdown renderer (no external deps / no network).
 * Escapes HTML first, then applies a small subset of Markdown.
 * ------------------------------------------------------------------ */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]));
}
function renderMarkdown(text) {
  let s = escapeHtml(text);
  s = s.replace(/```([\s\S]*?)```/g, (_, c) => `<pre><code>${c.replace(/^\n+|\n+$/g, '')}</code></pre>`);
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  s = s.replace(/\n/g, '<br/>');
  return s;
}

/** Format seconds -> M:SS (defensive against junk input). */
function formatTime(secs) {
  const total = Math.max(0, Math.floor(num(secs)));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}

/* ================================================================== *
 * ErrorBoundary — wraps each critical widget so a render/parse error
 * shows a graceful "Widget Offline" card instead of crashing the app.
 * ================================================================== */
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError() {
    return { hasError: true };
  }
  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error(`[Blink] Widget "${this.props.name}" crashed:`, error, info);
  }
  reset = () => this.setState({ hasError: false });
  render() {
    if (this.state.hasError) {
      return (
        <div className="widget-offline glass" role="alert">
          <div className="widget-offline__icon">⚠</div>
          <div className="widget-offline__title">{this.props.name} Offline</div>
          <div className="widget-offline__hint">A non-fatal error occurred in this widget.</div>
          <button className="widget-offline__retry" onClick={this.reset}>
            Retry
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

/* ================================================================== *
 * useBlinkSocket — auto-reconnecting WebSocket with exponential backoff.
 *
 * Returns { connected, reconnectIn, send }. `onEvent` is called with a
 * normalised event object for every inbound message. A ref keeps the
 * latest handler so reconnection logic never has to re-subscribe.
 * ================================================================== */
function useBlinkSocket(onEvent) {
  const [connected, setConnected] = useState(false);
  const [reconnectIn, setReconnectIn] = useState(0);

  const wsRef = useRef(null);
  const attemptRef = useRef(0);
  const timerRef = useRef(null);
  const countdownRef = useRef(null);
  const closedRef = useRef(false);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const token = useMemo(() => {
    try {
      return new URLSearchParams(window.location.search).get('token') || '';
    } catch {
      return '';
    }
  }, []);

  const buildUrl = useCallback(() => {
    const loc = window.location;
    const devPort =
      (import.meta.env && import.meta.env.VITE_BACKEND_PORT) || '8000';
    const backendPort = loc.port && loc.port !== '5173' ? loc.port : devPort;
    const scheme = loc.protocol === 'https:' ? 'wss' : 'ws';
    const q = token ? `?token=${encodeURIComponent(token)}` : '';
    return `${scheme}://${loc.hostname}:${backendPort}/ws${q}`;
  }, [token]);

  const clearTimers = () => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (countdownRef.current) clearInterval(countdownRef.current);
    timerRef.current = null;
    countdownRef.current = null;
  };

  const connect = useCallback(() => {
    clearTimers();
    setReconnectIn(0);

    let ws;
    try {
      ws = new WebSocket(buildUrl());
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('[Blink] WebSocket construction failed:', err);
      scheduleReconnect();
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      attemptRef.current = 0;
      setConnected(true);
      setReconnectIn(0);
    };

    ws.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn('[Blink] Dropped non-JSON WS frame.');
        return;
      }
      try {
        onEventRef.current?.(normalizeEvent(data));
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('[Blink] Event handler error:', err);
      }
    };

    ws.onclose = () => {
      setConnected(false);
      if (!closedRef.current) scheduleReconnect();
    };

    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        /* noop */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildUrl]);

  /** Exponential backoff: 1s, 2s, 4s, 8s, capped at 16s, with a live countdown. */
  const scheduleReconnect = useCallback(() => {
    clearTimers();
    const delay = Math.min(16000, 1000 * 2 ** attemptRef.current);
    attemptRef.current += 1;

    let remaining = Math.round(delay / 1000);
    setReconnectIn(remaining);
    countdownRef.current = setInterval(() => {
      remaining -= 1;
      setReconnectIn(Math.max(0, remaining));
    }, 1000);

    timerRef.current = setTimeout(() => {
      if (!closedRef.current) connect();
    }, delay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connect]);

  useEffect(() => {
    closedRef.current = false;
    connect();
    return () => {
      closedRef.current = true;
      clearTimers();
      try {
        wsRef.current?.close();
      } catch {
        /* noop */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const send = useCallback(
    (obj) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      try {
        ws.send(JSON.stringify(token ? { token, ...obj } : obj));
        return true;
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('[Blink] Send failed:', err);
        return false;
      }
    },
    [token]
  );

  return { connected, reconnectIn, send };
}

/* ================================================================== *
 * VisualizerOrb — HUD centerpiece.
 *
 * Audio reactivity is applied imperatively: the parent pushes raw
 * `audio_level` values through the ref, and a requestAnimationFrame loop
 * eases the value into the `--level` CSS variable. No React state is
 * touched on the hot path, so 12.5 events/sec cause zero re-renders.
 * ================================================================== */
const VisualizerOrb = forwardRef(function VisualizerOrb({ status }, ref) {
  const orbRef = useRef(null);
  const targetRef = useRef(0);
  const currentRef = useRef(0);
  const rafRef = useRef(0);
  // Mirror the latest status into a ref so the rAF loop (mounted once) always
  // reads the current value without stale-closure issues.
  const statusRef = useRef(status);
  statusRef.current = status;

  useImperativeHandle(
    ref,
    () => ({
      pushLevel(level) {
        targetRef.current = clamp(num(level), 0, 1);
      },
      reset() {
        targetRef.current = 0;
        currentRef.current = 0;
      },
    }),
    []
  );

  useEffect(() => {
    let startTime = Date.now();
    const tick = () => {
      let levelVal = 0;
      if (statusRef.current === 'idle') {
        // Smooth breathing via sine wave: 4.5s cycle, range [-0.04, 0.04].
        const elapsed = (Date.now() - startTime) / 1000;
        const sine = Math.sin((elapsed * 2 * Math.PI) / 4.5);
        levelVal = sine * 0.04;
        currentRef.current = levelVal; // Keep the eased level in sync.
      } else {
        // Eased audio levels: ease toward the latest target, then decay so the
        // orb settles when no new audio frames arrive. Starting from the exact
        // breathing value means the transition out of sleep is seamless.
        currentRef.current += (targetRef.current - currentRef.current) * 0.22;
        targetRef.current *= 0.9;
        levelVal = currentRef.current;
      }

      const el = orbRef.current;
      if (el) el.style.setProperty('--level', levelVal.toFixed(4));
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, []);

  const isSleeping = status === 'idle';

  return (
    <div className={`orb-stage state-${status}${isSleeping ? ' is-sleeping' : ''}`}>
      <div className="orb-ring orb-ring--1" />
      <div className="orb-ring orb-ring--2" />
      <div className="orb-ring orb-ring--3" />
      <div className="orb-core" ref={orbRef}>
        <div className="orb-core__glow" />
        <div className="orb-core__sweep" />
        <div className="orb-core__label">{isSleeping ? 'ZZZ' : themeFor(status).label}</div>
      </div>
    </div>
  );
});

/* ================================================================== *
 * ChatHUD — scrollable message feed with auto scroll-to-bottom,
 * distinct bubble styles, Markdown rendering, voice/keyboard origin
 * indicators, and copy-to-clipboard on assistant messages.
 * ================================================================== */
function ChatBubble({ msg }) {
  const [copied, setCopied] = useState(false);
  const isAssistant = msg.role === 'blink';
  const html = isAssistant ? { __html: renderMarkdown(msg.text) } : null;

  const copy = useCallback(() => {
    const done = () => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    };
    try {
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(msg.text).then(done).catch(() => {});
      }
    } catch {
      /* noop */
    }
  }, [msg.text]);

  return (
    <div className={`bubble bubble--${msg.role}`}>
      <div className="bubble__meta">
        <span className="bubble__author">
          {msg.role === 'user' ? 'You' : msg.role === 'blink' ? 'Blink' : 'System'}
        </span>
        {msg.role === 'user' && (
          <span className="bubble__origin" title={msg.via === 'voice' ? 'Voice input' : 'Typed input'}>
            {msg.via === 'voice' ? '🎤' : '⌨️'}
          </span>
        )}
      </div>
      {isAssistant ? (
        <div className="bubble__text" dangerouslySetInnerHTML={html} />
      ) : (
        <div className="bubble__text">{msg.text}</div>
      )}
      {isAssistant && (
        <button className="bubble__copy" onClick={copy} title="Copy to clipboard">
          {copied ? 'Copied' : 'Copy'}
        </button>
      )}
    </div>
  );
}

function ChatHUD({ messages, displayText, status, connected, onSend }) {
  const [input, setInput] = useState('');
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages]);

  const submit = (e) => {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    onSend(text);
    setInput('');
  };

  return (
    <section className="glass panel chat">
      <header className="panel__head">
        <h2 className="panel__title">Conversation</h2>
        <span className="panel__sub">{themeFor(status).label}</span>
      </header>

      <div className="chat__feed">
        {messages.map((m) => (
          <ChatBubble key={m.id} msg={m} />
        ))}
        <div ref={endRef} />
      </div>

      {displayText ? <div className="chat__ticker">{displayText}</div> : null}

      <form className="chat__input" onSubmit={submit}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={connected ? 'Type a command…' : 'Disconnected…'}
          disabled={!connected}
        />
        <button type="submit" disabled={!connected || !input.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}

/* ================================================================== *
 * MediaPlayer — bottom HUD. Seek bar with time readout, volume control,
 * transport buttons, and a music-reactive equalizer. Mount/unmount is
 * handled smoothly via CSS so metadata updates don't flicker.
 * ================================================================== */
function Equalizer({ active }) {
  return (
    <div className={`eq ${active ? 'eq--on' : 'eq--off'}`} aria-hidden="true">
      {[0, 1, 2, 3, 4].map((i) => (
        <span key={i} className="eq__bar" style={{ animationDelay: `${i * 0.13}s` }} />
      ))}
    </div>
  );
}

function MediaPlayer({ music, progress, volume, onToggle, onStop, onSeek, onVolume, onPrev, onNext }) {
  if (!music) return null;
  const duration = Math.max(0, num(music.duration));
  const pos = clamp(num(progress), 0, duration || 0);

  return (
    <section className="glass panel player">
      <div className="player__row">
        <div className="player__art">
          {music.thumbnail ? (
            <img src={music.thumbnail} alt="" onError={(e) => (e.currentTarget.style.display = 'none')} />
          ) : (
            <span className="player__art-fallback">🎵</span>
          )}
        </div>

        <div className="player__meta">
          <div className="player__title" title={music.title}>
            {music.title || 'Unknown track'}
          </div>
          <div className="player__time">
            {formatTime(pos)} / {formatTime(duration)}
          </div>
          <input
            className="player__seek"
            type="range"
            min={0}
            max={duration || 1}
            value={pos}
            onChange={(e) => onSeek(num(e.target.value))}
          />
        </div>

        <Equalizer active={!!music.playing} />
      </div>

      <div className="player__controls">
        <div className="player__buttons">
          <button className="ctrl" onClick={onPrev} title="Previous">⏮</button>
          <button className="ctrl ctrl--primary" onClick={onToggle} title={music.playing ? 'Pause' : 'Play'}>
            {music.playing ? '⏸' : '▶'}
          </button>
          <button className="ctrl" onClick={onStop} title="Stop">⏹</button>
          <button className="ctrl" onClick={onNext} title="Next">⏭</button>
        </div>

        <div className="player__volume">
          <span className="player__volume-icon">🔊</span>
          <input
            type="range"
            min={0}
            max={100}
            value={clamp(num(volume, 50), 0, 100)}
            onChange={(e) => onVolume(num(e.target.value))}
          />
        </div>
      </div>
    </section>
  );
}

/* ================================================================== *
 * SetupOverlay — translucent modal shown when no API key is configured.
 * ================================================================== */
function SetupOverlay({ onSave }) {
  const [key, setKey] = useState('');
  const [model, setModel] = useState('openrouter/auto');
  const [origins, setOrigins] = useState('');

  const submit = (e) => {
    e.preventDefault();
    if (!key.trim()) return;
    onSave({ openrouter_key: key.trim(), model: model.trim() || 'openrouter/auto', origins });
  };

  return (
    <div className="overlay">
      <form className="glass overlay__panel" onSubmit={submit}>
        <h2 className="overlay__title">Configure Blink</h2>
        <p className="overlay__hint">Enter your OpenRouter credentials to bring the assistant online.</p>

        <label className="field">
          <span className="field__label">OpenRouter API Key</span>
          <input
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="sk-or-v1-…"
            required
          />
        </label>

        <label className="field">
          <span className="field__label">Model</span>
          <input
            type="text"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="openrouter/auto"
          />
        </label>

        <label className="field">
          <span className="field__label">Allowed Origins</span>
          <textarea
            value={origins}
            onChange={(e) => setOrigins(e.target.value)}
            placeholder="http://localhost:5173, http://localhost:8000"
            rows={2}
          />
        </label>

        <button type="submit" className="overlay__save" disabled={!key.trim()}>
          Save Credentials
        </button>
      </form>
    </div>
  );
}

/* ================================================================== *
 * ReconnectBanner — non-intrusive connection status indicator.
 * ================================================================== */
function ReconnectBanner({ connected, reconnectIn }) {
  if (connected) return null;
  return (
    <div className="reconnect glass">
      <span className="reconnect__dot" />
      {reconnectIn > 0 ? `Reconnecting in ${reconnectIn}s…` : 'Reconnecting…'}
    </div>
  );
}

/* ================================================================== *
 * App — top-level orchestrator.
 * ================================================================== */
let MSG_ID = 0;
const nextId = () => `m${++MSG_ID}`;

export default function App() {
  /* --- High-level assistant state (low-frequency, safe in React state) --- */
  const [status, setStatus] = useState('idle');
  const [displayText, setDisplayText] = useState('Sleeping Mode…');
  const [hasKeys, setHasKeys] = useState(true);
  const [model, setModel] = useState('openrouter/auto');
  const [messages, setMessages] = useState([
    { id: nextId(), role: 'system', text: 'Blink interface online. Jarvis protocol initialised.' },
  ]);
  const [telemetry, setTelemetry] = useState({
    stats: { cpu: 0, ram: 0, disk: 0, battery: { percent: 100, power_plugged: true }, uptime: '—' },
    wifi: { adapter_enabled: false, connected_ssid: '', strength: 0 },
    bluetooth: { adapter_enabled: false, devices: [] },
    audio: { volume: 50, muted: false },
    display: { brightness: 50 },
    processes: [],
  });
  const [music, setMusic] = useState(null);
  const [musicProgress, setMusicProgress] = useState(0);
  const [initialized, setInitialized] = useState(false);

  const orbRef = useRef(null);
  const statusRef = useRef(status);
  statusRef.current = status;

  /* --- Initialization overlay fallback timer (max 8s) --- */
  useEffect(() => {
    if (initialized || connected) return undefined;
    const id = setTimeout(() => setInitialized(true), 8000);
    return () => clearTimeout(id);
  }, [initialized, connected]);

  /* --- Chat append with simple de-duplication --- */
  const appendMessage = useCallback((role, text, extra = {}) => {
    if (text == null || text === '') return;
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.role === role && last.text === text) return prev;
      return [...prev, { id: nextId(), role, text, ...extra }];
    });
  }, []);

  /* --- Central, schema-defensive event handler --- */
  const handleEvent = useCallback(
    (e) => {
      switch (e.type) {
        case 'config_status':
          if (typeof e.has_keys === 'boolean') {
            setHasKeys(e.has_keys);
            if (!e.has_keys) setInitialized(true);
          }
          if (e.model) setModel(e.model);
          break;

        case 'assistant_started':
        case 'assistant_status':
          setHasKeys(true);
          break;

        case 'status':
          if (e.status) {
            setStatus(e.status);
            if (e.status === 'idle') {
              orbRef.current?.reset();
              setInitialized(true);
            }
          }
          break;

        case 'system_telemetry':
          if (e.telemetry && typeof e.telemetry === 'object') {
            // Merge so a partial payload never wipes existing sections.
            setTelemetry((prev) => ({ ...prev, ...e.telemetry }));
          }
          break;

        case 'text': {
          const text = String(e.text ?? '');
          if (text.toLowerCase().includes('hello')) {
            setInitialized(true);
          }
          setDisplayText(text);
          if (text.startsWith('🗣️')) {
            appendMessage('user', text.replace('🗣️', '').trim(), { via: 'voice' });
          } else if (text.startsWith('⏰')) {
            appendMessage('system', text);
          } else {
            const transient = [
              'listening',
              'understanding',
              'sleeping',
              'searching',
              'downloading',
              'processing',
              'loading',
              'scanning',
            ];
            const lowerText = text.toLowerCase();
            if (text && !transient.some((t) => lowerText.includes(t))) {
              appendMessage('blink', text);
            }
          }
          break;
        }

        case 'audio_level':
          // HOT PATH: imperative update only — never setState here.
          if (statusRef.current !== 'idle') {
            orbRef.current?.pushLevel(e.level);
          }
          break;

        case 'music_start':
          setMusic({
            title: e.title,
            thumbnail: e.thumbnail,
            duration: num(e.duration),
            playing: true,
          });
          setMusicProgress(0);
          break;

        case 'music_stop':
          setMusic(null);
          setMusicProgress(0);
          break;

        case 'music_state_changed':
          setMusic((prev) =>
            prev ? { ...prev, playing: e.state === 'Resumed' } : prev
          );
          break;

        case 'reminder':
          appendMessage('system', `⏰ ${e.message || 'Reminder'}`);
          break;

        case 'conversation_history':
          if (Array.isArray(e.history)) {
            const historyMsgs = e.history.map((t) => ({
              id: nextId(),
              role: t.role,
              text: t.text,
              via: t.role === 'user' ? 'text' : undefined,
            }));
            setMessages([
              { id: nextId(), role: 'system', text: 'Blink interface online. Jarvis protocol initialised.' },
              ...historyMsgs,
            ]);
          }
          break;

        case 'wake_word':
        case 'sleep':
          break;

        case 'error':
          appendMessage('system', `⚠ ${e.message || 'An error occurred.'}`);
          break;

        default:
          break;
      }
    },
    [appendMessage]
  );

  const { connected, reconnectIn, send } = useBlinkSocket(handleEvent);

  /* --- Local music progress ticker --- */
  useEffect(() => {
    if (!music || !music.playing) return undefined;
    const id = setInterval(() => {
      setMusicProgress((p) => (p >= num(music.duration) ? p : p + 1));
    }, 1000);
    return () => clearInterval(id);
  }, [music]);

  /* --- Push the active theme onto the root as CSS variables --- */
  const theme = themeFor(status);
  const rootStyle = useMemo(
    () => ({ '--accent': theme.color, '--accent-glow': theme.glow }),
    [theme.color, theme.glow]
  );

  /* --- Outbound actions --- */
  const sendCommand = useCallback(
    (text) => {
      if (send({ type: 'manual_text', text })) {
        appendMessage('user', text, { via: 'text' });
      }
    },
    [send, appendMessage]
  );

  const musicControl = useCallback(
    (action, value) => send({ type: 'music_control', action, value }),
    [send]
  );

  const saveConfig = useCallback(
    ({ openrouter_key, model: m }) => {
      send({ type: 'save_config', openrouter_key, model: m });
      setModel(m);
    },
    [send]
  );

  const volume = telemetry?.audio?.volume;

  /* --- Window drag passthrough for the pywebview frameless shell --- */
  const onHeaderMouseDown = useCallback((e) => {
    if (e.button !== 0) return;
    let lastX = e.screenX;
    let lastY = e.screenY;
    const move = (ev) => {
      const dx = ev.screenX - lastX;
      const dy = ev.screenY - lastY;
      lastX = ev.screenX;
      lastY = ev.screenY;
      if (dx === 0 && dy === 0) return;
      const api = window.pywebview && window.pywebview.api;
      if (api && api.move_window) {
        api.move_window(dx, dy);
      }
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }, []);

  return (
    <div id="blink-root" className={`state-${status} ${music ? 'music-active' : ''}`} style={rootStyle}>
      <div className={`init-overlay ${initialized ? 'init-overlay--hidden' : ''}`}>
        <div className="init-overlay__content">
          <div className="init-overlay__orb">
            <div className="init-overlay__orb-glow" />
          </div>
          <h1 className="init-overlay__title">Blink Jarvis</h1>
          <p className="init-overlay__status">
            {(!displayText || displayText === 'Sleeping Mode…') ? 'Initializing...' : displayText}
          </p>
        </div>
      </div>

      <div className="bg-grid" aria-hidden="true" />
      <div className="bg-glow" aria-hidden="true" />

      {!hasKeys && <SetupOverlay onSave={saveConfig} />}

      <ReconnectBanner connected={connected} reconnectIn={reconnectIn} />

      <header className="topbar" onMouseDown={onHeaderMouseDown}>
        <div className="topbar__brand">
          Blink<span> Jarvis</span>
        </div>
        <div className="topbar__status">
          <span className="status-dot" />
          <span className="status-label">{theme.label}</span>
        </div>
      </header>

      <div className="app-body">
        <main className="layout">
          <div className="layout__center">
            <VisualizerOrb ref={orbRef} status={status} />
          </div>

          <aside className="layout__side">
            <ErrorBoundary name="Chat">
              <ChatHUD
                messages={messages}
                displayText={displayText}
                status={status}
                connected={connected}
                onSend={sendCommand}
              />
            </ErrorBoundary>
          </aside>
        </main>

        {music && (
          <ErrorBoundary name="Media Player">
            <MediaPlayer
              music={music}
              progress={musicProgress}
              volume={volume}
              onToggle={() => musicControl('toggle')}
              onStop={() => musicControl('stop')}
              onSeek={(v) => {
                setMusicProgress(v);
                musicControl('seek', v);
              }}
              onVolume={(v) => musicControl('set_volume', v)}
              onPrev={() => musicControl('previous')}
              onNext={() => musicControl('next')}
            />
          </ErrorBoundary>
        )}
      </div>

      <footer className="footbar">
        <span>{connected ? 'CONNECTED' : 'OFFLINE'}</span>
        <span>MODEL {model}</span>
        <span>CORE v4.2.0</span>
      </footer>
    </div>
  );
}
