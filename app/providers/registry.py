from __future__ import annotations

from app.config import get_settings
from app.providers.image import LocalSemanticImageProvider, MinimaxImageProvider, MockImageProvider, ResilientStockProvider, SemanticVerifier
from app.providers.llm import ResilientCreativeProvider
from app.providers.music import ResilientMusicProvider
from app.providers.tts import EdgeTTSProvider, ElevenLabsTTSProvider, LocalSpeechFallbackProvider


class ProviderRegistry:
    def __init__(self) -> None:
        settings = get_settings()
        self.creative = ResilientCreativeProvider()
        self.image = MockImageProvider() if settings.use_mock_providers else MinimaxImageProvider()
        self.stock = ResilientStockProvider()
        tts_primary_provider = settings.tts_primary_provider.lower()
        if settings.use_mock_providers:
            self.tts = LocalSpeechFallbackProvider()
        elif tts_primary_provider == "edge_tts":
            self.tts = EdgeTTSProvider()
        else:
            self.tts = ElevenLabsTTSProvider()
        self.music = ResilientMusicProvider()
        self.semantic = SemanticVerifier()
        self.local_image = LocalSemanticImageProvider()
