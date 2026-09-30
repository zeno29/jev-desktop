"""Loads config.toml into typed settings. API keys come from .env, never from here."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


@dataclass
class JevConfig:
    model: str = "jev-latest"
    timeout_seconds: float = 2.0
    tool_threshold: float = 0.70
    param_threshold: float = 0.60
    deep_threshold: float = 0.50
    action_thresholds: dict[str, float] = field(default_factory=dict)  # per-action overrides


@dataclass
class LLMConfig:
    fast_model: str = "gemini-3.5-flash-lite"
    deep_model: str = "gemini-3.8-flash"
    fast_thinking: str = "minimal"
    deep_thinking: str = "medium"
    fast_timeout_seconds: float = 30.0
    deep_timeout_seconds: float = 120.0
    deep_google_search: bool = False
    max_agent_steps: int = 25
    command_timeout_seconds: float = 150.0  # a whole command (incl. deep research) is stopped after this
    history_turns: int = 6


@dataclass
class BrowserConfig:
    mode: str = "real"             # "real": your own logged-in Chrome; "automation": Playwright profile only
    profile_directory: str = ""    # real mode: Chrome profile folder or name; "" = Chrome's last-used
    chrome_path: str = ""          # real mode: chrome.exe; "" = auto-detect
    # Playwright automation profile (all commands in automation mode; the deep agent's page tools always)
    channel: str = "chrome"
    profile_dir: str = "data/browser-profile"
    headless: bool = False
    prewarm: bool = True           # automation mode only

    @property
    def profile_path(self) -> Path:
        p = Path(self.profile_dir)
        return p if p.is_absolute() else ROOT / p


@dataclass
class MusicConfig:
    provider: str = "spotify"


@dataclass
class EmailConfig:
    provider: str = "gmail"
    outlook_base: str = "https://outlook.live.com"


@dataclass
class UIConfig:
    hotkey: str = "ctrl+alt+j"   # voice activation
    orb_size: int = 120          # logical px
    orb_on_top: bool = True      # keep the widget above other windows
    auto_hide_seconds: float = 20


@dataclass
class VoiceConfig:
    enabled: bool = True
    stt_provider: str = "auto"     # auto | gemini | sarvam | local
    tts_provider: str = "auto"     # auto | gemini | sarvam | kokoro | windows
    language: str = "en-IN"        # en-IN makes Gemini write Hinglish in Roman script, which routing needs
    # Gemini (GEMINI_API_KEY)
    gemini_stt_model: str = "gemini-3.5-transcribe-live"
    gemini_tts_model: str = "gemini-3.8-flash-lite-tts"
    gemini_voice: str = "Kore"
    gemini_tts_style: str = ""     # optional delivery note, e.g. "warm, brisk"
    # Sarvam (SARVAM_API_KEY)
    sarvam_stt_model: str = "saaras:v3"
    sarvam_stt_mode: str = "translit"   # translit = Roman-script output; codemix keeps Devanagari
    sarvam_stt_language: str = "hi-IN"
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_speaker: str = "shubh"
    # Offline
    local_stt_model: str = "auto"  # auto = converted Hinglish model if present, else "base"; or a size/path
    kokoro_voice: str = "af_heart"
    windows_voice: str = ""        # part of a SAPI voice name, e.g. "Zira"; "" = system default
    # Behaviour
    vad_silence_ms: int = 700      # trailing silence that ends an utterance
    max_seconds: float = 15        # hard cap on one utterance
    no_speech_seconds: float = 5   # give up if nothing is said
    cache: bool = True             # keep audio for short replies in data/tts_cache
    speak_typed: bool = False      # also speak replies to typed commands
    bubble: bool = True            # show spoken replies beside the widget
    chime: bool = True
    duck_level: float = 0.15       # other apps' volume while Jev listens/speaks (1.0 = leave alone)
    noise_gate: bool = True        # turn down background between your words before recognition
    input_device: str = ""         # part of a device name; "" = Windows default
    output_device: str = ""


@dataclass
class Config:
    jev: JevConfig = field(default_factory=JevConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    music: MusicConfig = field(default_factory=MusicConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    app_aliases: dict[str, str] = field(default_factory=dict)
    sites: dict[str, str] = field(default_factory=dict)
    contacts: dict[str, str] = field(default_factory=dict)


def _section(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


def load_config(path: Path = ROOT / "config.toml") -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return Config(
        jev=_section(JevConfig, raw.get("jev", {})),
        llm=_section(LLMConfig, raw.get("llm", {})),
        browser=_section(BrowserConfig, raw.get("browser", {})),
        music=_section(MusicConfig, raw.get("music", {})),
        email=_section(EmailConfig, raw.get("email", {})),
        voice=_section(VoiceConfig, raw.get("voice", {})),
        ui=_section(UIConfig, raw.get("ui", {})),
        app_aliases={k.lower(): v for k, v in raw.get("app_aliases", {}).items()},
        sites={k.lower(): v for k, v in raw.get("sites", {}).items()},
        contacts={k.lower(): v for k, v in raw.get("contacts", {}).items()},
    )
