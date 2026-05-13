from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

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
    source_title: str | None = None

    def as_notes(self) -> str:
        return (
            "trend_research=real_source\n"
            f"trend_source={self.source}\n"
            f"trend_source_url={self.source_url}\n"
            f"trend_score={self.score:.3f}\n"
            f"trend_familiarity_score={self.familiarity_score:.3f}\n"
            f"trend_raw_title={self.raw_title}\n"
            f"trend_source_title={self.source_title or self.raw_title}\n"
            "Priorize conexão imediata com temas familiares/do dia a dia. Evite obscuridade, exceto se o fato tiver payoff visual ou curiosidade muito forte. "
            "Use esta tendência como ponto de partida, mas mantenha fact-check e linguagem conservadora."
        )

    def as_report(self) -> dict[str, Any]:
        return {
            "trend_research": "real_source",
            "topic": self.topic,
            "requested_angle": self.requested_angle,
            "source": self.source,
            "source_url": self.source_url,
            "score": self.score,
            "raw_title": self.raw_title,
            "source_title": self.source_title or self.raw_title,
            "familiarity_score": self.familiarity_score,
        }


class TrendResearcher:
    """Find real trend-backed topics for an empty hub request.

    Primary signal: Google Trends RSS for Brazil. It reflects what people are
    actively searching today. The RSS feed often exposes short query labels, so
    related news titles are used as the higher-quality topic surface.
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
        "al", "hilal", "elfsborg", "playstation", "copa", "saudita", "liga", "champions", "celebridade", "cantor", "show",
    }
    FACT_FRIENDLY_TERMS = {
        "agua", "chuva", "calor", "frio", "sol", "lua", "mar", "praia", "tempo", "clima", "sono", "cerebro", "corpo", "saude",
        "comida", "cafe", "chocolate", "banana", "leite", "ovo", "arroz", "feijao", "celular", "internet", "energia", "luz",
        "animal", "animais", "planta", "plantas", "flamingo", "tubarao", "formiga", "abelha", "olho", "pele", "dente",
        "musculo", "sangue", "memoria", "planeta", "vulcao", "oceano", "fungo", "fungos", "raio", "raios",
    }

    def __init__(self, timeout_sec: float = 8.0) -> None:
        self.timeout_sec = timeout_sec

    def find_topic(self, niche_id: str = "curiosidades") -> TrendCandidate | None:
        if niche_id not in SUPPORTED_NICHES:
            return None
        candidates = self._google_trends_candidates()
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
            topic_title = self._best_topic_title(item, title)
            if not topic_title or not self._is_curiosity_candidate(topic_title):
                continue
            familiarity = self._familiarity_score(topic_title)
            exceptional_signal = self._has_exceptional_trend_signal(traffic, rank)
            normalized_tokens = set(re.findall(r"[a-z0-9à-ÿ]+", self._normalize(topic_title)))
            if not normalized_tokens & self.FACT_FRIENDLY_TERMS and familiarity < 0.80:
                continue
            if familiarity < 0.30 and not exceptional_signal:
                continue
            score = (traffic or 100.0) / rank * (0.45 + familiarity)
            if familiarity < 0.5:
                score *= 0.08 if familiarity < 0.30 else (0.65 if exceptional_signal else 0.35)
            candidates.append(
                TrendCandidate(
                    topic=f"Por que {topic_title} virou assunto agora?",
                    requested_angle=(
                        f"Usar a tendência real '{topic_title}' como gancho, mas conectar imediatamente com uma curiosidade familiar, "
                        "visual e verificável do dia a dia. Se o tema for obscuro, explicar por que ele importa em uma frase."
                    ),
                    source="google_trends_br",
                    source_url=self.GOOGLE_TRENDS_BR_URL,
                    score=score,
                    raw_title=title,
                    source_title=topic_title,
                    familiarity_score=familiarity,
                )
            )
            if len(candidates) >= 10:
                break
        return candidates

    def _best_topic_title(self, item: ET.Element, title: str) -> str:
        news_titles = [
            (node.text or "").strip()
            for node in item.findall(".//ht:news_item_title", namespaces={"ht": "https://trends.google.com/trending/rss"})
            if (node.text or "").strip()
        ]
        surfaces = [*news_titles[:3], title]
        ranked = [surface for surface in surfaces if self._is_curiosity_candidate(surface)]
        if not ranked:
            return ""
        return max(ranked, key=lambda surface: (self._familiarity_score(surface), -len(surface)))

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
            "assassinato", "homicídio", "homicidio", "morre", "morte", "tragédia", "tragedia", "acidente", "acidentes",
            "onde assistir", "ao vivo", "tv aberta", "transmissão", "transmissao", "horário", "horario", "escalação",
            "escalacao", "palpite", "odds", "precedentes", "placar", "resultado",
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
