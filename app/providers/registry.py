from __future__ import annotations

from app.config import get_settings
from app.providers.image import LocalSemanticImageProvider, MinimaxImageProvider, MockImageProvider, ResilientStockProvider, SemanticVerifier
from app.providers.llm import ResilientCreativeProvider
from app.providers.music import ResilientMusicProvider
from app.providers.tts import EdgeTTSProvider, LocalSpeechFallbackProvider


class ProviderRegistry:
    def __init__(self) -> None:
        settings = get_settings()
        self.creative = ResilientCreativeProvider()
        self.image = MockImageProvider() if settings.use_mock_providers else MinimaxImageProvider()
        self.stock = ResilientStockProvider()
        self.tts = LocalSpeechFallbackProvider() if settings.use_mock_providers else EdgeTTSProvider()
        self.music = ResilientMusicProvider()
        self.semantic = SemanticVerifier()
        self.local_image = LocalSemanticImageProvider()
