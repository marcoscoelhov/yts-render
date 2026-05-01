from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote

import httpx


@dataclass(frozen=True)
class TrendCandidate:
    topic: str
    requested_angle: str
    source: str
    source_url: str
    score: float
    raw_title: str

    def as_notes(self) -> str:
        return (
            "trend_research=real_source\n"
            f"trend_source={self.source}\n"
            f"trend_source_url={self.source_url}\n"
            f"trend_score={self.score:.3f}\n"
            f"trend_raw_title={self.raw_title}\n"
            "Use esta tendência como ponto de partida, mas mantenha fact-check e linguagem conservadora."
        )


class TrendResearcher:
    """Find real trend-backed topics for an empty hub request.

    Uses free, stable public signals first. For the general curiosities niche,
    Wikipedia pageviews are a useful proxy: people are already clicking on
    entities/events, and the app can turn them into evergreen curiosity angles.
    """

    def __init__(self, timeout_sec: float = 8.0) -> None:
        self.timeout_sec = timeout_sec

    def find_topic(self, niche_id: str = "curiosidades") -> TrendCandidate | None:
        if niche_id != "curiosidades":
            return None
        candidates = self._wikipedia_top_candidates("pt") + self._wikipedia_top_candidates("en")
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate.score)

    def _wikipedia_top_candidates(self, language: str) -> list[TrendCandidate]:
        day = datetime.now(timezone.utc).date() - timedelta(days=1)
        url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{language}.wikipedia/all-access/{day:%Y/%m/%d}"
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout_sec, connect=3.0), headers={"User-Agent": "yts-render/1.0 trend-research"}) as client:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
        except Exception:  # noqa: BLE001
            return []
        articles = payload.get("items", [{}])[0].get("articles", [])
        candidates: list[TrendCandidate] = []
        for rank, article in enumerate(articles[:80], start=1):
            raw_title = str(article.get("article") or "")
            views = float(article.get("views") or 0)
            title = self._clean_wikipedia_title(raw_title)
            if not self._is_curiosity_candidate(title):
                continue
            score = views / max(rank, 1)
            candidates.append(
                TrendCandidate(
                    topic=f"Por que {title} está chamando atenção?",
                    requested_angle=f"Transformar a tendência real '{title}' em uma curiosidade geral, visual e verificável.",
                    source=f"wikipedia_pageviews_{language}",
                    source_url=url,
                    score=score,
                    raw_title=raw_title,
                )
            )
            if len(candidates) >= 8:
                break
        return candidates

    def _clean_wikipedia_title(self, raw_title: str) -> str:
        title = unquote(raw_title).replace("_", " ").strip()
        title = re.sub(r"\s+", " ", title)
        return title

    def _is_curiosity_candidate(self, title: str) -> bool:
        lowered = title.lower().replace("_", " ")
        if len(title) < 4 or len(title) > 80:
            return False
        blocked_prefixes = ("special:", "wikipedia:", "file:", "category:", "template:", "portal:", "talk:", "main page")
        if lowered.startswith(blocked_prefixes):
            return False
        blocked_terms = {
            "página principal", "main page", "buscar", "search", "portal", "wiki", "categoria", "lista de", "list of",
            "temporada", "campeonato", "eleição", "eleicao", "mortes em", "deaths in", "covid", "porn", "sex",
        }
        if any(term in lowered for term in blocked_terms):
            return False
        if re.fullmatch(r"\d{4}", lowered) or re.fullmatch(r"\d{1,2} de .+", lowered):
            return False
        # Prefer entities/concepts that can become evergreen curiosity videos.
        return bool(re.search(r"[a-zA-ZÀ-ÿ]{4,}", title))
