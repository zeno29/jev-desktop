"""Voice: speech-to-text for the hotkey, spoken replies, and the providers behind them.

The GUI talks to `VoiceController` only. Providers plug in behind two small protocols in
`base.py`, and `providers.py` picks them (auto / configured) with fallback to the next one.
"""
