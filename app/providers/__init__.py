from __future__ import annotations

from app.config import get_settings
from openai import OpenAI
import httpx
import subprocess
import time

from app.music_bank import populate_builtin_music_bank
from app.providers.errors import ProviderFailure
from app.providers.llm import (
    LLMProvider,
    LLMProviderRegistry,
    MockCreativeProvider,
    MinimaxCreativeProvider,
    DeepSeekCreativeProvider,
    OpenAICreativeProvider,
    QwenCreativeProvider,
    ResilientCreativeProvider,
)
from app.providers.image import (
    SemanticVerifier,
    MockImageProvider,
    LocalSemanticImageProvider,
    MinimaxImageProvider,
    PexelsStockProvider,
    PixabayStockProvider,
    ResilientStockProvider,
)
from app.providers.music import (
    MockBackgroundMusicProvider,
    LocalMusicBankProvider,
    MiniMaxBackgroundMusicProvider,
    ResilientMusicProvider,
)
from app.providers.tts import LocalSpeechFallbackProvider, EdgeTTSProvider
from app.providers.registry import ProviderRegistry

__all__ = [
    "ProviderFailure",
    "LLMProvider",
    "LLMProviderRegistry",
    "MockCreativeProvider",
    "MinimaxCreativeProvider",
    "DeepSeekCreativeProvider",
    "OpenAICreativeProvider",
    "QwenCreativeProvider",
    "ResilientCreativeProvider",
    "SemanticVerifier",
    "MockImageProvider",
    "LocalSemanticImageProvider",
    "MinimaxImageProvider",
    "PexelsStockProvider",
    "PixabayStockProvider",
    "ResilientStockProvider",
    "MockBackgroundMusicProvider",
    "LocalMusicBankProvider",
    "MiniMaxBackgroundMusicProvider",
    "ResilientMusicProvider",
    "LocalSpeechFallbackProvider",
    "EdgeTTSProvider",
    "ProviderRegistry",
]
