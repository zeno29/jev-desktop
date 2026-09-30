"""The two provider interfaces. Audio in is 16 kHz mono int16 PCM; audio out is mono int16 at the
provider's `rate` (the player resamples)."""

from __future__ import annotations

from typing import AsyncIterator, Callable, Protocol

STT_RATE = 16000


class STTSession(Protocol):
    """One utterance. Created when listening starts so a cloud provider can connect while the
    user is still speaking; audio fed before it's connected is buffered, not lost."""

    def feed(self, pcm: bytes) -> None: ...

    async def finish(self) -> str:
        """End of speech: return the final transcript ("" if nothing was recognised)."""
        ...

    async def cancel(self) -> None: ...


class STTProvider(Protocol):
    name: str

    def session(self, on_partial: Callable[[str], None] | None = None) -> STTSession: ...

    async def warm_up(self) -> None: ...


class TTSProvider(Protocol):
    name: str
    rate: int

    def cache_key(self) -> str:
        """Everything that changes how a text sounds (provider, model, voice, language)."""
        ...

    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM chunks as soon as they're available."""
        ...

    async def warm_up(self) -> None: ...


async def transcribe(provider: STTProvider, pcm: bytes) -> str:
    """Run a whole recorded utterance through a provider (used for fallback and the bake-off)."""
    session = provider.session()
    session.feed(pcm)
    return await session.finish()
