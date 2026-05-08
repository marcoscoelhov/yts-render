from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote

import httpx

from app.schemas import SUPPORTED_NICHES


@dataclass(frozen=True)
class TrendCandidate:
    topic: str
    requested_angle: str
    source: str
    source_url: str
    score: float
    raw_title: str
    familiarity_score: float = 0.0

    def as_notes(self) -> str:
        return (
            "trend_research=real_source\n"
            f"trend_source={self.source}\n"
            f"trend_source_url={self.source_url}\n"
            f"trend_score={self.score:.3f}\n"
            f"trend_familiarity_score={self.familiarity_score:.3f}\n"
            f"trend_raw_title={self.raw_title}\n"
            "Priorize conexão imediata com temas familiares/do dia a dia. Evite obscuridade, exceto se o fato tiver payoff visual ou curiosidade muito forte. "
            "Use esta tendência como ponto de partida, mas mantenha fact-check e linguagem conservadora."
        )


class TrendResearcher:
    """Find real trend-backed topics for an empty hub request.

    Primary signal: Google Trends RSS for Brazil. It reflects what people are
    actively searching today. Wikipedia pageviews remain as a fallback/secondary
    signal, but rank lower unless the topic is familiar and curiosity-friendly.
    """

    GOOGLE_TRENDS_BR_URL = "https://trends.google.com/trending/rss?geo=BR"

    EVERYDAY_TERMS = {
        "agua", "água", "chuva", "calor", "frio", "sol", "lua", "mar", "praia", "tempo", "clima", "sono", "cerebro", "cérebro",
        "corpo", "saude", "saúde", "comida", "cafe", "café", "chocolate", "banana", "leite", "ovo", "arroz", "feijao", "feijão",
        "celular", "internet", "whatsapp", "instagram", "google", "youtube", "carro", "aviao", "avião", "energia", "luz", "dinheiro",
        "cachorro", "gato", "animal", "animais", "planta", "plantas", "flamingo", "tubarão", "tubarao", "formiga", "abelha",
        "casa", "escola", "trabalho", "memoria", "memória", "olho", "pele", "dente", "musculo", "músculo", "sangue",
    }
    OBSCURE_OR_LOW_CONNECTION_TERMS = {
        "nahui", "ollin", "anime", "manga", "mangá", "episodio", "episódio", "temporada", "campeonato", "futebol", "jogo", "time",
        "eleição", "eleicao", "partido", "senador", "deputado", "presidente", "ministro", "exército", "exercito", "concurso",
    }

    def __init__(self, timeout_sec: float = 8.0) -> None:
        self.timeout_sec = timeout_sec

    def find_topic(self, niche_id: str = "curiosidades") -> TrendCandidate | None:
        if niche_id not in SUPPORTED_NICHES:
            return None
        google_candidates = self._google_trends_candidates()
        if google_candidates:
            return max(google_candidates, key=lambda candidate: candidate.score)
        candidates = self._wikipedia_top_candidates("pt") + self._wikipedia_top_candidates("en")
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate.score)

    def _google_trends_candidates(self) -> list[TrendCandidate]:
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout_sec, connect=3.0), headers={"User-Agent": "yts-render/1.0 trend-research"}) as client:
                response = client.get(self.GOOGLE_TRENDS_BR_URL)
                response.raise_for_status()
                xml_text = response.text
        except Exception:  # noqa: BLE001
            return []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        ns = {"ht": "https://trends.google.com/trending/rss"}
        candidates: list[TrendCandidate] = []
        for rank, item in enumerate(root.findall("./channel/item"), start=1):
            title = (item.findtext("title") or "").strip()
            traffic_text = item.findtext("ht:approx_traffic", namespaces=ns) or "0"
            traffic = self._parse_traffic(traffic_text)
            if not self._is_curiosity_candidate(title):
                continue
            familiarity = self._familiarity_score(title)
            if familiarity < 0.50 and not self._has_exceptional_trend_signal(traffic, rank):
                continue
            score = (traffic or 100.0) / rank * (0.45 + familiarity)
            if familiarity < 0.5:
                score *= 0.2
            candidates.append(
                TrendCandidate(
                    topic=f"Por que {title} virou assunto agora?",
                    requested_angle=(
                        f"Usar a tendência real '{title}' como gancho, mas conectar imediatamente com uma curiosidade familiar, "
                        "visual e verificável do dia a dia. Se o tema for obscuro, explicar por que ele importa em uma frase."
                    ),
                    source="google_trends_br",
                    source_url=self.GOOGLE_TRENDS_BR_URL,
                    score=score,
                    raw_title=title,
                    familiarity_score=familiarity,
                )
            )
            if len(candidates) >= 10:
                break
        return candidates

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
            familiarity = self._familiarity_score(title)
            if familiarity < 0.50:
                continue
            score = views / max(rank, 1) * (0.25 + familiarity) * 0.35
            candidates.append(
                TrendCandidate(
                    topic=f"Por que {title} está chamando atenção?",
                    requested_angle=f"Transformar a tendência real '{title}' em uma curiosidade familiar, visual e verificável.",
                    source=f"wikipedia_pageviews_{language}",
                    source_url=url,
                    score=score,
                    raw_title=raw_title,
                    familiarity_score=familiarity,
                )
            )
            if len(candidates) >= 8:
                break
        return candidates

    def _parse_traffic(self, raw: str) -> float:
        cleaned = raw.lower().replace("+", "").replace(",", "").strip()
        multiplier = 1.0
        if cleaned.endswith("k"):
            multiplier = 1_000.0
            cleaned = cleaned[:-1]
        if cleaned.endswith("m"):
            multiplier = 1_000_000.0
            cleaned = cleaned[:-1]
        match = re.search(r"\d+(?:\.\d+)?", cleaned)
        return float(match.group(0)) * multiplier if match else 0.0

    def _clean_wikipedia_title(self, raw_title: str) -> str:
        title = unquote(raw_title).replace("_", " ").strip()
        title = re.sub(r"\s+", " ", title)
        return title

    def _is_curiosity_candidate(self, title: str) -> bool:
        lowered = title.lower().replace("_", " ")
        if len(title) < 4 or len(title) > 80:
            return False
        blocked_prefixes = ("special:", "especial:", "wikipedia:", "file:", "ficheiro:", "category:", "categoria:", "template:", "predefinição:", "predefinicao:", "portal:", "talk:", "discussão:", "discussao:", "main page")
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
        return bool(re.search(r"[a-zA-ZÀ-ÿ]{4,}", title))

    def _familiarity_score(self, title: str) -> float:
        normalized = self._normalize(title)
        tokens = set(re.findall(r"[a-z0-9à-ÿ]+", normalized))
        score = 0.35
        if tokens & {self._normalize(term) for term in self.EVERYDAY_TERMS}:
            score += 0.45
        if tokens & {self._normalize(term) for term in self.OBSCURE_OR_LOW_CONNECTION_TERMS}:
            score -= 0.35
        if len(tokens) <= 3:
            score += 0.10
        if re.search(r"\b(?:por que|como|porque)\b", normalized):
            score += 0.10
        if re.search(r"^[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-zà-ÿ]+(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-zà-ÿ]+)+$", title):
            score -= 0.15
        return max(0.0, min(1.0, score))

    def _has_exceptional_trend_signal(self, traffic: float, rank: int) -> bool:
        return traffic >= 20_000 and rank <= 5

    def _normalize(self, text: str) -> str:
        replacements = str.maketrans("áàãâäéèêëíìîïóòõôöúùûüç", "aaaaaeeeeiiiiooooouuuuc")
        return text.lower().translate(replacements)
