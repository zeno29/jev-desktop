# Jev Desktop

A Windows desktop assistant that runs everyday commands in a fraction of a second with
**TypeSafe's Jev**, and only calls **Google Gemini** when a request actually needs thinking.

```
you › play blinding lights
  jev 140 ms → action: play_music(query='blinding lights', service='default')
jev › Playing “The Weeknd - Blinding Lights” on YouTube.
```

## How a command flows

1. **Jev (one call, ~100–500 ms).** Every command goes to Jev once, asking at the same time:
   what the user wants (open app, play music, media control, search, website, system action, timer,
   email, chat, or a complex task), whether it needs deeper thinking, and every argument each route
   might need. Arguments are *selected, never generated*: code proposes every span of what you said
   (and your installed apps), and Jev picks the song, search text, site or app. Nothing gets made up.
2. **Direct action.** If the route and its arguments clear their thresholds, the action runs
   immediately. No LLM is involved.
3. **Fast LLM (Gemini 3.5 Flash-Lite, minimal thinking, ~2–3 s).** Questions, chat, email
   drafting, and anything Jev wasn't sure about. It answers general knowledge itself, looks up
   things that change (news, scores, rates) with a quick browser-free web lookup, and gets weather
   from Open-Meteo. It has the same desktop tools and can hand off to the deep tier.
4. **Deep LLM (Gemini 3.8 Flash, medium thinking).** Multi-step work, research, and doing things
   inside websites with full browser control.

Every action has a time limit and every command is stopped after `command_timeout_seconds`
(150 s), so one slow request can't freeze Jev.

If Jev can't be reached (no key, no network, timeout), a local rule set handles the obvious
commands and everything else goes to Gemini.

Measured on this PC: Jev decisions take ~330 ms median (19/19 sample commands routed correctly),
at about 2,000 input tokens each.

## Install

**You need:** Windows 10 or 11, [Python 3.12 or newer](https://www.python.org/downloads/) (tick
*Add python.exe to PATH* in the installer), and Google Chrome for web commands.

1. **Get the code.** Either `git clone <this repo's URL>`, or click **Code → Download ZIP** on
   GitHub and unzip it somewhere permanent (e.g. `Documents\JevDesktop`, not Downloads).
2. **Double-click `install.bat`.** It:
   - finds Python and creates a private environment in `.venv` (nothing is installed system-wide);
   - installs the packages (a few minutes the first time);
   - asks for your API keys one by one and saves them in `.env` (press Enter to skip any);
   - optionally connects Spotify, and optionally installs offline speech recognition;
   - adds **Jev** to the Start Menu, optionally to the Desktop, and asks whether to start Jev when
     you log in to Windows;
   - starts Jev: an arc-reactor orb appears near the top-right of your screen.

| Key | Needed for | Get it |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | Jev's fast routing (without it, simple keyword rules are used) | https://console.typesafe.ai |
| `GEMINI_API_KEY` | Answering questions, drafting email, research; a voice fallback | https://aistudio.google.com/apikey |
| `SARVAM_API_KEY` | *Optional.* The best voice for Indian accents and Hinglish | https://dashboard.sarvam.ai |
| `SPOTIFY_CLIENT_ID` | *Optional.* Playing in the Spotify app (Premium) | see [Spotify](#spotify-desktop-app-premium) |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | *Optional.* "Any important mails today?" read out by Jev | an [app password](https://myaccount.google.com/apppasswords) (needs 2-Step Verification), not your Google password |

**Starting Jev:** it starts with Windows if you said yes to autostart. Otherwise open **Jev** from
the Start Menu, or double-click `Jev.pyw`. To change autostart later: `install.bat -Startup` or
`install.bat -NoStartup`.

**Updating:** `git pull` (or download the new ZIP over the old folder, keeping your `.env`), then
run `install.bat` again. It keeps your keys and settings.

**Uninstalling:** `install.bat -Uninstall` removes the shortcuts and autostart; then delete the folder.

Your keys and personal data stay on your PC: `.env` and `data/` are in `.gitignore`, so they're
never committed if you fork the project.

**Console mode** (for testing or scripting): `.venv\Scripts\python -m jev_assistant` for an
interactive prompt, or `.venv\Scripts\python -m jev_assistant "open spotify"` for one command.
Add `--quiet` to hide the routing trace.

### Browser

Nothing to set up for everyday commands. Searches, websites, YouTube videos,
   email drafts and the Spotify web fallback open as new tabs in **your own Google Chrome**, already
   logged in to your Google account (`[browser] mode = "real"`). With several Chrome profiles they
   open in the one you used last; to pin one, set `profile_directory` (list them with
   `.venv\Scripts\python -m jev_assistant.actions.browser`).
   The deep tier's page tools (reading and clicking inside pages) can't drive your everyday Chrome,
   so they use a separate automated Chrome window with its own profile (`data/browser-profile`).
   It only opens when a deep task needs it. If that task needs your accounts, log in there once
   and the logins stay. See [Browser modes](#browser-modes) for the tradeoffs.

### Desktop HUD (JARVIS-style)

Open **Jev** from the Start Menu (or double-click `Jev.pyw`). An arc-reactor
widget appears near the top-right of the screen. It's truly transparent, stays on top, and is
hidden from the taskbar.

- **The widget is still** until something happens. It spins while listening for your voice,
  while Jev works on a command and while it speaks. Its colour shows the stage: cyan while Jev routes, green for a
  direct action, teal for Gemini Flash-Lite, indigo for deep reasoning, amber when it needs your
  approval, red for errors.
- **Click it** to open the typing console, which drops down beneath it. Type and press Enter.
  **Esc** hides the console, and it also hides itself 20 s after the last reply. **↑ / ↓** recall
  earlier commands. Approvals (sending email and so on) show an amber panel; press **Y** or **N**.
- **Drag it** to move it; the position is remembered. **Right-click it** to open the console or quit.
- **Ctrl+Alt+J is for voice** (see [Voice](#voice)): speak, and the reply is spoken and shown
  in a caption bubble beside the widget. Press it again to cancel, or to interrupt a reply.
- Under each command a tag shows how it was handled, e.g. `JEV · 465 ms → system_action`.

The widget is drawn natively with Pillow (`gui/orb.py`) as a per-pixel-alpha layered window, so
it has smooth transparent edges on any wallpaper and uses no CPU while still. Change the hotkey,
widget size, always-on-top and the auto-hide delay under `[ui]` in `config.toml`. The log is in
`data/jev.log`. Only one copy runs at a time; starting it again while it's running does nothing.

### Spotify (desktop app, Premium)

1. Go to https://developer.spotify.com/dashboard → **Create app**. Any name works. Set the
   Redirect URI to exactly `http://127.0.0.1:8888/callback` and tick **Web API**.
2. Copy the app's **Client ID** into `.env` as `SPOTIFY_CLIENT_ID=` (no secret needed).
3. Run `.venv\Scripts\python -m jev_assistant --spotify-login` and approve access in the browser tab.

Then "play blinding lights", "play arijit singh", "play some lofi playlist" or "play the dark side of
the moon album" start in the Spotify app, which is launched if it isn't running. Spotify search
returns tracks, artists, albums and playlists, and Jev picks the one you meant. Without a Client ID,
Spotify requests fall back to a Spotify web search in your Chrome, where you click the result to
play it (it only auto-plays in `automation` browser mode). Say "on youtube" to use YouTube instead.

## What it can do

| Say | Handled by |
| --- | --- |
| open / close *app* (every Start-menu app, including Store apps) | Jev → action |
| play *song / artist / album / playlist* | Jev → Spotify desktop app (Jev picks the right search result) |
| play / watch *video* [in fullscreen], e.g. "play the latest mkbhd video" | Jev → top YouTube result, playing in your logged-in Chrome (~1.5 s from command to tab) |
| pause, next, previous, mute, volume up/down, volume 40 | Jev → media keys |
| search for *…*, open *site* | Jev → new tab in your Chrome |
| what time is it, lock the screen, screenshot, open downloads | Jev → action |
| set a timer for 10 minutes | Jev → action |
| any important mails in the last 24 hours / check my email / did amazon email me today | Jev reads your Gmail inbox (IMAP, read-only) and judges each email's importance in one request, ~1 s. Without Gmail keys: the same search opens in Gmail in Chrome |
| email *someone* saying *…* | Jev → fast Gemini drafts → pre-filled draft opens in your Gmail/Outlook in Chrome → **you press Send** |
| research / compare / summarize / do *X* on a website | Jev → deep Gemini with the automated browser window |

**Safety:** nothing is sent without you. In your own Chrome, Jev only opens pages, and you press
Send yourself. In the automated window, sending email and clicking any Send / Submit / Buy /
Delete-style button always asks you first, whichever tier triggered it. Web page content is
treated as untrusted data.

## Browser modes

`[browser] mode` in `config.toml`:

| | `"real"` (default) | `"automation"` |
| --- | --- | --- |
| Where search / sites / YouTube / email open | New tab in **your everyday Chrome**, already logged in | Playwright-controlled Chrome with its own profile (`data/browser-profile`) |
| YouTube | Jev finds the top video itself (a plain HTTPS request, no browser, ~1 s) and opens its watch page | Clicks the top result in a reused "media" tab |
| Fullscreen | Jev presses YouTube's **F** shortcut once the video's tab is in front and the player is ready | Presses **F** in the page |
| Email "send" | The draft opens pre-filled; you check it and press Send (Ctrl+Enter) | Jev asks you, then clicks Send |
| Spotify web fallback | Opens the search; you click the result | Clicks Play |
| Deep tier reading/clicking pages | Automated window (opens on first use) | Automated window |

Why not automate your everyday Chrome? Chrome 136 and later ignore remote-debugging flags on the
default profile, and copying the profile elsewhere breaks its logins because the cookies are tied
to the original profile. Google also often blocks sign-in in automated Chrome. So real mode
hands Chrome a URL to open (like clicking a link) and never controls it. The costs of that:

- Jev can't click, read or type in those tabs. Each video or page opens in a **new tab**, so a
  previous video keeps playing until you pause or close it ("pause" works on whatever is playing).
- With several Chrome profiles, tabs open in the last-used one unless `profile_directory` is set.
- The deep tier's page tools still use the automated window. Log in there once for the sites it
  needs.

A small companion Chrome extension could later give Jev control of your everyday Chrome, such as
reusing one media tab or pressing Send after you confirm. It isn't built yet.

## Tuning (`config.toml`)

- `[jev]` thresholds, plus a per-action `[jev.action_thresholds]` table. Riskier actions such as
  `close_app` and `system_action` (which includes lock screen) need more confidence. Run
  without `--quiet` to see the scores behind every decision, then adjust against your own commands.
  The defaults are starting points, not tested values.
- `[app_aliases]` maps spoken names to apps (`"code" = "Visual Studio Code"`).
- `[sites]` maps site names to URLs, and `[contacts]` maps names to email addresses.
- `[llm]` sets the Gemini models, thinking levels, per-call time limits, and whether to try Gemini's
  built-in Google Search grounding (off by default: your plan's quota rejected it in testing).
  Calls use `store=False`, which is ~2x faster and means Google keeps no stored copy of the conversation.
- `[music] provider`, `[email] provider` (gmail/outlook).
- `[voice]`: providers, Gemini models and voice, end-of-speech silence, caching, the caption
  bubble, and input/output devices (see [Voice](#voice)).
- `[browser]`: `mode` (`"real"` or `"automation"`, see [Browser modes](#browser-modes)),
  `profile_directory` (the Chrome profile folder such as `"Profile 1"`, or its display name; unset
  means your last-used profile) and `chrome_path` (auto-detected). `channel`, `profile_dir`,
  `headless` and `prewarm` configure the automated window. `prewarm` only takes effect in
  automation mode, because real mode never opens that window at startup.

## Voice

Press **Ctrl+Alt+J** (the `[ui] hotkey`) anywhere and speak. A short chime plays, the widget
listens, and it stops by itself when you stop talking (0.7 s of silence; 15 s at most; it gives up
if you say nothing for 5 s). **Questions get a spoken answer** ("who won the last world cup",
"weather in Delhi", "what time is it"), captioned in a bubble beside the widget. **Actions stay
quiet** (play, pause, next, volume, open/close apps, YouTube, timers, websites): the action is the
answer, so you just get a short caption, and music comes back up straight away. If an action
fails, or Jev needs to ask you something, it speaks. Everything also appears in the console
transcript without opening it. Press the hotkey again to cancel
while it's listening, or to cut a spoken reply short and speak straight away. The console's mic
button does the same.

Typed commands are never spoken (`speak_typed = true` changes that). Approvals still open the
console for **Y / N**. Hindi-English mixes work ("Spotify pe Arijit Singh ke gaane chalao"): the
recognisers are set up to return Hinglish in Roman script, which is what Jev routes on. Your app
names, contacts and aliases are passed to the recogniser as vocabulary, so it spells them right.

### Providers

Choose them in `[voice]` in `config.toml`. With `"auto"`, Jev uses Sarvam if `SARVAM_API_KEY` is
set, otherwise Gemini if `GEMINI_API_KEY` is set, otherwise the offline options. If a provider
fails (network, quota), the next available one takes over, and the failed one sits out for a while
(3 hours after a daily-quota error, remembered across restarts in `data/voice_state.json`; delete
that file to retry sooner).

| | Speech-to-text (`stt_provider`) | Text-to-speech (`tts_provider`) |
| --- | --- | --- |
| `gemini` | Gemini Live `gemini-3.5-transcribe-live`, streamed while you speak. **~0.5 s** after you stop (measured 0.45–0.75 s). Very good on Indian English and Hinglish. | `gemini-3.8-flash-lite-tts` (or `-flash-tts`), voice `Kore`, `en-IN`. Natural, but **1.5–3.5 s** before audio starts, and the **free tier allows only 10 requests per day per model**. |
| `sarvam` | Saaras v3 (`translit` mode = Roman-script output), built for Indian languages. **0.4–0.8 s** after you stop; 4/4 test commands exactly right (it kept "saat baje" where Gemini wrote "7:00"). ~₹0.03 per command. | Bulbul v3, Indian voices (`shubh` by default), Hinglish handled natively. **~0.6 s** to first audio for a short reply, ~1.2 s for two sentences. ~₹0.18 per 60-character reply (short replies are cached, so repeats are free). |
| `local` | faster-whisper on your CPU, nothing leaves the PC. With the Hinglish model below: **~0.7–1.2 s**, 4/4 test commands right. Stock Whisper `base` translates Hinglish into (often wrong) English. | – |
| `windows` | – | The voices built into Windows (SAPI). Instant (~20–80 ms), robotic, US English. Always available; the fallback when a cloud voice is out of quota. |
| `kokoro` | – | Kokoro-82M offline neural voice. Much nicer than Windows, but ~1.5 s for a short reply on a laptop CPU, so only used when you choose it. |

Latency tricks: the cloud recogniser connects while the chime plays and you speak (audio is
buffered until it's connected); long replies are spoken sentence by sentence, with the next
sentence synthesising while the current one plays; and short replies ("Opening Spotify.") are
cached in `data/tts_cache`, so repeats play instantly (a cached copy is used even if its provider
is currently out of quota).

### Music or a TV in the background

Two things keep background audio from swallowing your command:

1. **Ducking.** When you press the hotkey (and while Jev answers), every other app's volume in the
   Windows mixer drops to `duck_level` (15%) and comes back when Jev has finished, so your music
   gets quiet instead of stopping. The original volumes are saved first, and if Jev ever crashes
   mid-duck they're restored on its next start. `duck_level = 1.0` turns this off.
2. **Background-aware end-of-speech.** For the first ~0.4 s after the chime, and whenever you're
   not talking, Jev measures how loud the background gets. A sound only counts as you if it's
   speech-like and clearly louder than that (+4 dB to start, +1.6 dB to continue). Song vocals, a
   TV or people talking nearby are speech too, but they're quieter than you talking to the laptop,
   so Jev stops listening about 0.7 s after *you* stop instead of waiting for the music to. Tested
   with continuous background talk from -20 dB to only 6 dB below the command: every command
   ended 0.2–1.2 s after it finished (never cutting it off), and background alone never started a
   command.

Best results: wait for the chime before speaking (the background is measured right after it),
and speak towards the laptop. Headphones remove the problem entirely for audio Jev can't duck,
like a TV in the room.

### Setup

- **Gemini (default if you have the key):** nothing to do. `pip install -r requirements.txt`
  installs the audio packages.
- **Sarvam (best for Indian accents and Hinglish):** put your key after `SARVAM_API_KEY=` in
  `.env` (from https://dashboard.sarvam.ai; new accounts get free credits). With `"auto"` it's
  then used for both hearing and speaking, with Gemini and the offline options as fallbacks.
- **Offline:**
  ```bash
  .venv\Scripts\python -m pip install -r requirements-local-voice.txt
  .venv\Scripts\python -m jev_assistant.voice.setup_local            # Hinglish speech-to-text
  .venv\Scripts\python -m jev_assistant.voice.setup_local --kokoro   # + the Kokoro voice (~350 MB)
  ```
  `setup_local` converts [Oriserve/Whisper-Hindi2Hinglish-Swift](https://huggingface.co/Oriserve/Whisper-Hindi2Hinglish-Swift)
  (Apache-2.0, a Whisper-base fine-tune) to `data/models/whisper-hinglish-ct2`. The converter needs
  PyTorch, which it installs into a temporary folder (~250 MB download) and deletes afterwards.
  (End-of-speech detection uses the Silero voice-activity model, which ships with Jev, so it
  works well in noisy rooms whichever provider you choose.)

Then check everything with:

```bash
.venv\Scripts\python -m jev_assistant --voice-check
```

It lists your audio devices, plays a chime, runs a 3-second microphone level test, shows which
providers are usable (and why not), and times each one. To compare recognisers on **your** voice,
run `.venv\Scripts\python -m jev_assistant.voice.bakeoff`: it shows 15 English and Hinglish
commands, records you reading each, and prints every provider's word error rate and delay.

### Privacy

The microphone is only open from the moment you press the hotkey until you stop speaking, and
audio is only sent while it is open. With `gemini` or `sarvam`, that audio (and the replies to be
spoken) go to Google or Sarvam. Google's free Gemini tier may use what you send to improve its
products; the paid tier doesn't. `local` + `windows` / `kokoro` keeps everything on your PC.

Checking mail sends the sender, subject and first ~400 characters of each recent inbox email to
TypeSafe (Jev) to judge importance. Your app password stays in `.env` and only goes to Gmail.

## Layout

```
jev_assistant/
  router.py        Jev questions + routing policy (and local fallback rules)
  spans.py         candidate spans for select-don't-generate extraction
  brain.py         Gemini fast/deep tiers (Interactions API), agent loop
  assistant.py     ties a request through router → action / LLM
  actions/
    toolkit.py     every action (shared by Jev path and LLM tools) + confirmation gates
    windows.py     app index, window close, media keys, system actions
    mail.py        Gmail inbox over IMAP (read-only) for "any important mails?"
    browser.py     your Chrome (open URLs, YouTube lookup) + Playwright automated Chrome (page tools)
    spotify.py     Spotify desktop playback via Web API (PKCE login, device launch)
  voice/           microphone, end-of-speech detection, speech-to-text / text-to-speech providers
                   (gemini, sarvam, local, windows, kokoro), fallback, the listen/speak loop,
                   --voice-check, bake-off, offline setup
  core.py          builds all components once, for either front end
  ui.py            console front end
  gui/             desktop front end: app.py (threads, console window, voice turns), orb.py
                   (native widget), bubble.py (caption bubble), hotkey.py (RegisterHotKey),
                   web/index.html (typing console)
```
