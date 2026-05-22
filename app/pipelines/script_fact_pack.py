from __future__ import annotations

import ast
import concurrent.futures
import re
import unicodedata
from typing import Any

import httpx

from app.editorial.research_brief import audit_source_relevance, build_research_brief, research_tokens
from app.editorial.topic_mode import resolve_editorial_mode
from app.manual_script import extract_ready_script_from_notes
from app.models import TopicPlan, TopicRequest
from app.pipelines.base import BasePipeline
from app.utils import word_tokens


class ScriptFactPackDomain(BasePipeline):
    def __getattr__(self, name: str) -> Any:
        return getattr(self.owner, name)

    def _requires_verified_fact_pack(self, topic_plan: TopicPlan, request: TopicRequest, fact_pack: dict[str, Any]) -> bool:
        if extract_ready_script_from_notes(request.notes) is not None:
            return False
        if self.settings.use_mock_providers:
            return False
        if fact_pack.get("status") == "verified":
            return False
        return self._topic_requires_verified_fact_pack(topic_plan, request)

    def _editorial_mode(self, topic_plan: Any, request: Any) -> str:
        return resolve_editorial_mode(topic_plan, request)

    def _topic_requires_verified_fact_pack(self, topic_plan: TopicPlan, request: TopicRequest) -> bool:
        return self._editorial_mode(topic_plan, request) == "factual_strict"

    def _simple_mode_fact_pack(self, request: TopicRequest) -> dict[str, Any]:
        return {
            "status": "skipped",
            "provider": "simple_shorts_mode",
            "query_used": request.seed_theme,
            "facts": [],
            "sources": [],
            "editorial_rule": (
                "Simple Shorts mode: do not block generation on academic fact packs. "
                "Use broadly safe wording, avoid precise numbers and source IDs, and prioritize a viral pt-BR script."
            ),
        }

    def _build_research_brief(self, topic_plan: Any, request: Any) -> dict[str, Any]:
        return build_research_brief(topic_plan, request)

    def _build_fact_pack(self, topic_plan: TopicPlan, request: TopicRequest, research_brief: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.settings.use_mock_providers:
            return {
                "status": "limited",
                "query_used": request.seed_theme,
                "facts": [],
                "sources": [],
                "editorial_rule": "Mock-provider test mode: no external fact retrieval.",
            }
        research_brief = research_brief or self._build_research_brief(topic_plan, request)
        queries = self._fact_pack_queries(request, topic_plan)
        seen: set[str] = set()
        cleaned_queries = []
        for query in queries:
            normalized = " ".join(str(query or "").split())
            if normalized and normalized.lower() not in seen and not self._is_weak_fact_query(normalized):
                cleaned_queries.append(normalized)
                seen.add(normalized.lower())
        if research_brief.get("require_mechanism_match"):
            cleaned_queries = [
                query
                for query in cleaned_queries
                if self._query_supports_research_brief(query, research_brief)
            ]
        topic_tokens = self._fact_topic_tokens(request, topic_plan)
        if topic_tokens:
            cleaned_queries = [query for query in cleaned_queries if self._query_matches_primary_fact_topic(query, topic_tokens)]
        cleaned_queries.sort(key=self._fact_query_priority)
        query_batch = cleaned_queries[:8]
        if query_batch:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(query_batch)))
            try:
                future_to_query = {
                    executor.submit(self._scientific_article_fact_pack, query, research_brief): query
                    for query in query_batch
                }
                for future in concurrent.futures.as_completed(future_to_query):
                    query = future_to_query[future]
                    try:
                        pack = future.result()
                    except Exception:  # noqa: BLE001
                        continue
                    if pack.get("facts") and self._fact_pack_matches_topic(pack, request, topic_plan, research_brief):
                        pack["query_used"] = query
                        pack["status"] = "verified"
                        pack["queries_attempted"] = query_batch
                        pack["research_brief"] = research_brief
                        return pack
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        return {
            "status": "limited",
            "query_used": cleaned_queries[0] if cleaned_queries else request.seed_theme,
            "queries_attempted": query_batch,
            "facts": [],
            "sources": [],
            "editorial_rule": "No source facts were retrieved. Script must avoid precise numbers, dates, medical/scientific/engineering causality, and absolute claims unless already present in the user input.",
            "topic_alignment": {
                "passed": False,
                "reason": "no_relevant_source_retrieved",
                "claim_scope": research_brief.get("claim_scope"),
                "primary_terms": research_brief.get("primary_terms"),
                "mechanism_terms": research_brief.get("mechanism_terms"),
            },
            "research_brief": research_brief,
        }

    def _query_supports_research_brief(self, query: str, research_brief: dict[str, Any]) -> bool:
        if not research_brief.get("require_mechanism_match"):
            return True
        query_token_set = set(research_tokens(query))
        if len(query_token_set) < 3:
            return False
        primary_terms = set(research_brief.get("primary_terms") or [])
        mechanism_terms = set(research_brief.get("mechanism_terms") or [])
        if len(query_token_set & primary_terms) >= 1 and len(query_token_set & mechanism_terms) >= 1:
            return True
        for group in research_brief.get("search_term_groups") or []:
            tokens = {str(token) for token in (group.get("tokens") or []) if str(token)}
            if not tokens:
                continue
            overlap = query_token_set & tokens
            if len(overlap) >= 3:
                return True
            non_primary_terms = {str(token) for token in (group.get("non_primary_terms") or []) if str(token)}
            if len(overlap) >= 3 and overlap & non_primary_terms:
                return True
        return False

    def _fact_topic_tokens(self, request: TopicRequest, topic_plan: TopicPlan) -> set[str]:
        source_text = f"{request.seed_theme} {topic_plan.canonical_topic}".strip()
        normalized = self._normalize_fact_text(source_text)
        stopwords = {
            "porque",
            "como",
            "sobre",
            "plataforma",
            "dominante",
            "video",
            "videos",
            "curiosidade",
            "curiosidades",
            "chamando",
            "atencao",
            "atenção",
            "maior",
            "imagina",
            "parece",
            "segredo",
            "mecanismo",
            "quase",
            "estraga",
            "estragar",
            "tira",
            "unico",
            "único",
        }
        ordered_tokens: list[str] = []
        seen_tokens: set[str] = set()
        for token in word_tokens(normalized):
            if (len(token) < 4 and token not in {"mel"}) or token in stopwords or token in seen_tokens:
                continue
            ordered_tokens.append(token)
            seen_tokens.add(token)
        tokens = set(ordered_tokens)
        protected_entities = {
            "youtube",
            "tiktok",
            "google",
            "wikipedia",
            "instagram",
            "meta",
            "flamingo",
            "flamingos",
            "polvo",
            "polvos",
            "pisa",
            "templario",
            "templarios",
            "mel",
            "honey",
            "honeys",
            "cafe",
            "cafeina",
            "caffeine",
            "adenosina",
            "adenosine",
        }
        protected = {token for token in ordered_tokens if token in protected_entities}
        if "mel" in protected:
            protected.update({"honey", "honeys"})
        if "cafe" in protected or "cafeina" in protected:
            protected.update({"caffeine", "cafeina", "adenosine", "adenosina"})
        optical_illusion_terms = {"ilusao", "otica", "visual", "movimento", "percepcao", "retina", "cerebro", "periferica", "periferico"}
        if tokens & optical_illusion_terms:
            protected.update(tokens & optical_illusion_terms)
            protected.update({"illusion", "illusory", "optical", "visual", "motion", "movement", "perception"})
            if tokens & {"movimento", "periferica", "periferico", "retina"}:
                protected.update({"peripheral", "drift", "static"})
            if tokens & {"cerebro", "percepcao"}:
                protected.update({"brain", "neural"})
        return protected or set(ordered_tokens[:3])

    def _query_matches_primary_fact_topic(self, query: str, topic_tokens: set[str]) -> bool:
        query_tokens = {token for token in word_tokens(self._normalize_fact_text(query)) if len(token) >= 4}
        if not query_tokens:
            return False
        if query_tokens & topic_tokens:
            return True
        side_entities = {"google", "wikipedia", "tiktok", "meta", "instagram", "facebook", "x", "twitter"}
        if query_tokens & side_entities:
            return False
        return len(query_tokens) > 2

    def _fact_pack_matches_topic(
        self,
        fact_pack: dict[str, Any],
        request: TopicRequest,
        topic_plan: TopicPlan,
        research_brief: dict[str, Any] | None = None,
    ) -> bool:
        research_brief = research_brief or self._build_research_brief(topic_plan, request)
        source_text = " ".join(
            str(part or "")
            for part in [
                fact_pack.get("topic_title"),
                " ".join(str(source.get("title") or "") for source in fact_pack.get("sources") or []),
                " ".join(str(fact.get("claim") or "") for fact in fact_pack.get("facts") or []),
            ]
        )
        alignment = audit_source_relevance(
            research_brief,
            str(fact_pack.get("topic_title") or ""),
            source_text,
        )
        if not research_brief.get("primary_terms"):
            alignment = {**alignment, "passed": True, "reason": "no_primary_topic_tokens"}
        fact_pack["topic_alignment"] = alignment
        fact_pack["research_brief"] = research_brief
        return bool(alignment.get("passed"))

    def _fact_query_priority(self, query: str) -> tuple[int, int, int, int, int]:
        normalized = query.lower()
        tokens = word_tokens(query)
        token_count = len(tokens)
        token_set = set(tokens)
        is_short_entity = token_count <= 3 and ":" not in query and "?" not in query
        single_token_query = token_count == 1
        two_token_query = token_count == 2
        is_exact_pisa_entity = {"torre", "pisa"} <= token_set and token_count <= 2
        has_specific_entity = any(
            term in normalized
            for term in [
                "flamingo",
                "polvo",
                "octopus",
                "torre",
                "pisa",
                "mel",
                "honey",
                "cafe",
                "café",
                "cafeina",
                "cafeína",
                "caffeine",
                "adenosina",
                "adenosine",
                "templario",
                "templarios",
                "templário",
                "templários",
            ]
        )
        has_concept_suffix = any(
            term in normalized
            for term in [
                "carotenoides",
                "carotenoid",
                "pigmentos",
                "pigment",
                "pigmentation",
                "plumage",
                "diet",
                "alimentação",
                "inclinacao",
                "inclinação",
                "engenharia",
                "solo",
                "cromatóforos",
                "chromatophore",
                "iridóforos",
                "camuflagem",
                "camouflage",
                "conservação",
                "conservacao",
                "durabilidade",
                "glucose oxidase",
                "peróxido",
                "peroxido",
                "hydrogen peroxide",
                "water activity",
                "antimicrobial",
                "adenosine",
                "adenosina",
                "caffeine",
                "cafeina",
                "cafeína",
            ]
        )
        ambiguous_short_entity = normalized in {"mel", "cafe", "café", "cafeina", "cafeína", "adenosina", "adenosine"}
        return (
            0 if is_exact_pisa_entity else (1 if has_concept_suffix else 2),
            2 if single_token_query else (1 if two_token_query and not has_concept_suffix else 0),
            0 if has_specific_entity and not ambiguous_short_entity else (1 if is_short_entity else 2),
            token_count,
            len(query),
        )

    def _is_weak_fact_query(self, query: str) -> bool:
        tokens = [token.lower() for token in word_tokens(query) if token]
        if not tokens:
            return True
        if all(token.isdigit() for token in tokens):
            return True
        weak_single_terms = {
            "auto",
            "manual",
            "segredo",
            "mecanismo",
            "processo",
            "fato",
            "fatos",
            "curiosidade",
            "curiosidades",
            "biologia",
            "ciencia",
            "ciência",
            "inteligencia",
            "inteligência",
            "surpresa",
            "visual",
            "revelar",
            "resposta",
            "motivo",
            "quimico",
            "químico",
            "pigmentos",
            "carotenoides",
            "diet",
            "alimentacao",
            "alimentação",
            "descubra",
            "apenas",
            "exatamente",
            "durante",
            "vida",
            "duas",
            "cidade",
            "cidades",
            "cafe",
            "café",
            "cafeina",
            "cafeína",
            "adenosina",
            "adenosine",
        }
        weak_multi_terms = weak_single_terms | {
            "causa",
            "causas",
            "comida",
            "alimentacao",
            "alimentação",
            "dieta",
            "cor",
            "cores",
            "segredo",
            "quimico",
            "químico",
            "visual",
            "surpresa",
            "incrivel",
            "incrível",
            "motivo",
            "deixa",
            "pinta",
            "transformacao",
            "transformação",
            "resposta",
            "explicacao",
            "explicação",
            "diet",
            "descubra",
            "apenas",
            "exatamente",
            "durante",
            "vida",
            "duas",
            "cidade",
            "cidades",
        }
        if len(tokens) == 1:
            return tokens[0] in weak_single_terms
        return all(token in weak_multi_terms for token in tokens)

    def _fact_pack_queries(self, request: TopicRequest, topic_plan: TopicPlan) -> list[str]:
        raw_sources: list[tuple[Any, bool]] = [
            (request.seed_theme, False),
            (topic_plan.canonical_topic, False),
            (topic_plan.angle, False),
            (getattr(topic_plan, "hook_promise", None), False),
            *((item, True) for item in (getattr(topic_plan, "search_terms", None) or [])),
            *((item, False) for item in (getattr(topic_plan, "entities", None) or [])),
            *((item, False) for item in (topic_plan.title_candidates or [])),
        ]
        queries: list[str] = []
        for raw_query, preserve_research_shape in raw_sources:
            for query in self._fact_query_source_texts(raw_query):
                cleaned = self._clean_fact_query(str(query or ""))
                if cleaned:
                    queries.append(cleaned)
                    cleaned_token_count = len(word_tokens(self._normalize_fact_text(cleaned)))
                    if preserve_research_shape and cleaned_token_count >= 4:
                        continue
                    entity = self._extract_fact_entity(cleaned)
                    if entity and entity != cleaned and not self._is_weak_fact_query(entity):
                        queries.append(entity)
                        for concept in self._fact_query_concepts(cleaned):
                            queries.append(f"{entity} {concept}")
                            if self._should_include_standalone_fact_concept(entity, concept, cleaned):
                                queries.append(concept)
        return queries

    def _should_include_standalone_fact_concept(self, entity: str, concept: str, query: str) -> bool:
        if entity.lower() == "mel":
            return True
        query_tokens = set(word_tokens(self._normalize_fact_text(query)))
        optical_illusion_terms = {"ilusao", "otica", "visual", "movimento", "periferica", "retina", "percepcao"}
        if query_tokens & optical_illusion_terms and concept in {"peripheral drift illusion", "illusory motion visual perception", "static motion illusion"}:
            return True
        caffeine_terms = {"cafe", "cafeina", "caffeine", "sono", "adenosina", "adenosine"}
        return bool(query_tokens & caffeine_terms and concept in {"caffeine adenosine receptor", "caffeine sleep adenosine"})

    def _fact_query_source_texts(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, dict):
            texts = [value.get(key) for key in ("name", "text", "title", "query", "term")]
            return [str(text).strip() for text in texts if str(text or "").strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    parsed = ast.literal_eval(stripped)
                except Exception:  # noqa: BLE001
                    parsed = None
                if isinstance(parsed, dict):
                    return self._fact_query_source_texts(parsed)
            return [stripped]
        return [str(value).strip()]

    def _clean_fact_query(self, query: str) -> str:
        query = unicodedata.normalize("NFKC", query).strip()
        query = re.sub(r"[?!¿¡]+", " ", query)
        query = re.sub(r"\s+", " ", query)
        query = re.sub(r"^(?:voc[eê]\s+sabia|j[aá]\s+imaginou|surpreenda-se|prepare-se)\b[:\s,.-]*", "", query, flags=re.IGNORECASE)
        query = re.sub(r"^(?:por que|porque|como|qual|quais|o que|quem|quando|onde)\s+", "", query, flags=re.IGNORECASE)
        query = re.sub(r"\b(?:fica|ficam|ficou|são|sao|é|e|era|foram|tem|têm)\b", " ", query, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", query).strip(" -–—:;,.")

    def _extract_fact_entity(self, query: str) -> str:
        stopwords = {
            "por", "que", "porque", "como", "qual", "quais", "para", "com", "uma", "um", "de", "do", "da", "dos", "das", "a", "o", "as", "os", "e",
            "fica", "ficam", "cor", "rosa", "cor-de-rosa", "não", "nao", "cai", "acontece", "segredo", "invisível", "invisivel", "parece", "artificial",
            "curiosidades", "curiosidade", "cientificas", "científicas", "cientifica", "científica", "sobre", "mais", "inteligente", "oceano",
            "animal", "explica", "explicacao", "explicação", "fatos", "fato", "voce", "você", "sabia", "surpreenda", "prepare",
            "comida", "pinta", "motivo", "incrivel", "incrível", "quimico", "químico", "deixa", "essa", "resposta", "transforma", "revelar",
            "transformacao", "transformação", "visual", "branco", "branca", "brancos", "brancas", "descubra", "apenas",
            "exatamente", "durante", "vida", "quase", "estraga", "estragar", "unico", "único", "pode", "mata", "matar",
            "destrói", "destroi", "bacterias", "bactérias", "tira",
        }
        colon_head = query.split(":", 1)[0].strip(" -–—:;,.") if ":" in query else ""
        if colon_head:
            colon_tokens = [token for token in word_tokens(colon_head) if token]
            if 1 <= len(colon_tokens) <= 4:
                return " ".join(colon_tokens)
        plain_tokens = [token for token in word_tokens(query) if token]
        if plain_tokens:
            trailing_tokens = plain_tokens[1:]
            if trailing_tokens and all(len(token) < 3 or token.lower() in stopwords for token in trailing_tokens):
                return plain_tokens[0]
        filtered_tokens = [token for token in word_tokens(query) if len(token) >= 3 and token.lower() not in stopwords]
        if 1 <= len(filtered_tokens) <= 2:
            return " ".join(filtered_tokens)
        preposition_head = re.split(r"\b(?:sobre|com|contra|versus|vs\.?|em)\b", query, maxsplit=1, flags=re.IGNORECASE)[0].strip(" -–—:;,.")
        if preposition_head:
            head_tokens = [token for token in word_tokens(preposition_head) if token]
            if 1 <= len(head_tokens) <= 4:
                return " ".join(head_tokens)
        tokens = filtered_tokens
        if not tokens:
            return query
        if len(tokens) == 1:
            return tokens[0]
        return " ".join(tokens[:2])

    def _fact_query_concepts(self, query: str) -> list[str]:
        normalized = query.lower()
        normalized_tokens = set(word_tokens(self._normalize_fact_text(query)))
        concepts: list[str] = []
        if normalized_tokens & {"rosa", "color", "pink"} or "cor-de-rosa" in normalized:
            concepts.extend(["carotenoid pigmentation", "carotenoid diet", "plumage pigmentation"])
        if any(term in normalized for term in ["polvo", "polvos", "octopus", "octopuses", "cromatóforo", "cromatoforo", "camuflagem"]):
            concepts.extend(["chromatophores", "iridophores", "camouflage"])
        if any(term in normalized for term in ["flamingo", "flamingos"]):
            concepts.extend(["carotenoid pigmentation", "carotenoid diet", "plumage pigmentation"])
        if any(term in normalized for term in ["cai", "inclina", "torre"]):
            concepts.extend(["inclinação", "engenharia", "solo"])
        if any(term in normalized for term in ["mel", "honey", "abelha", "glucose oxidase", "peróxido", "peroxido"]):
            concepts.extend(["honey antimicrobial", "honey water activity", "glucose oxidase hydrogen peroxide"])
        if normalized_tokens & {"cafe", "cafeina", "caffeine", "sono", "adenosina", "adenosine"}:
            concepts.extend(["caffeine adenosine receptor", "caffeine sleep adenosine", "cafeina adenosina"])
        if any(term in normalized for term in ["templario", "templarios", "templário", "templários", "ordem do templo"]):
            concepts.extend(["Ordem dos Templários", "templários Portugal", "Tomar Templários"])
        if normalized_tokens & {"ilusao", "otica", "visual", "movimento", "periferica", "retina", "percepcao"}:
            concepts.extend(["peripheral drift illusion", "illusory motion visual perception", "static motion illusion"])
        return concepts[:3]

    def _normalize_fact_text(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKD", str(text or "").lower())
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9\s]", " ", normalized)

    def _fact_result_is_relevant(
        self,
        query: str,
        title: str,
        extract: str,
        research_brief: dict[str, Any] | None = None,
    ) -> bool:
        query_tokens = {token for token in word_tokens(self._normalize_fact_text(query)) if len(token) >= 4}
        title_tokens = {token for token in word_tokens(self._normalize_fact_text(title)) if len(token) >= 4}
        text_tokens = {token for token in word_tokens(self._normalize_fact_text(f"{title} {extract[:500]}")) if len(token) >= 4}
        normalized_query = self._normalize_fact_text(query)
        honey_query = bool(query_tokens & {"honey"}) or bool(re.search(r"\bmel\b", normalized_query))
        if honey_query:
            honey_terms = {"honey", "bee", "bees", "antimicrobial", "glucose", "oxidase", "peroxide"}
            tts_false_friends = {"spectrogram", "spectrograms", "wavenet", "tacotron", "speech", "synthesis", "vocoder"}
            if text_tokens & tts_false_friends and not text_tokens & honey_terms:
                return False
            if "antimicrobial" in query_tokens and not (
                text_tokens
                & {
                    "antimicrobial",
                    "antibacterial",
                    "bactericidal",
                    "bacteria",
                    "bacterial",
                    "staphylococcus",
                    "aureus",
                    "escherichia",
                    "coli",
                    "pseudomonas",
                }
            ):
                return False
            if {"water", "activity"} <= query_tokens and not {"water", "activity"} <= text_tokens:
                return False
            if {"glucose", "oxidase"} <= query_tokens and not (
                {"glucose", "oxidase"} <= text_tokens or {"hydrogen", "peroxide"} <= text_tokens
            ):
                return False
        coffee_query = bool(query_tokens & {"cafe", "cafeina", "caffeine", "adenosine", "adenosina"})
        if coffee_query:
            coffee_terms = {"caffeine", "cafeina", "coffee", "adenosine", "adenosina", "sleep", "sono", "receptor", "receptors"}
            if "world" in text_tokens and "cafe" in text_tokens and not (text_tokens & (coffee_terms - {"coffee"})):
                return False
            if not (text_tokens & coffee_terms):
                return False
        if research_brief is not None:
            return bool(audit_source_relevance(research_brief, title, extract).get("passed"))
        if not query_tokens:
            return True
        if query_tokens & title_tokens:
            return True
        if len(query_tokens) == 1:
            return False
        if len(query_tokens & text_tokens) >= min(2, len(query_tokens)):
            return True
        return False

    def _fact_sentence_is_useful(self, sentence: str, query: str) -> bool:
        normalized = self._normalize_fact_text(sentence)
        tokens = [token for token in word_tokens(normalized) if len(token) >= 4]
        if len(tokens) < 8:
            return False
        weak_patterns = [
            r"\b(?:o\s+)?conteudo\s+e\s+apresentado\b",
            r"\bintroducao\b.*\bmetodos?\b.*\breferencia\b",
            r"\breferencias?\s+bibliograficas?\b",
            r"\bthis\s+(?:article|paper|study)\s+(?:presents|describes|reviews|discusses)\b",
            r"\bthe\s+(?:content|paper)\s+is\s+(?:organized|presented)\b",
        ]
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in weak_patterns):
            return False
        query_tokens = {
            token
            for token in word_tokens(self._normalize_fact_text(query))
            if len(token) >= 4 and token not in {"sobre", "porque", "como", "qual", "quais"}
        }
        if len(query_tokens) <= 2 and query_tokens and not (query_tokens & set(tokens)):
            return False
        return True

    def _scientific_article_fact_pack(self, query: str, research_brief: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0), headers={"User-Agent": "yts-render/1.0 fact-pack"}) as client:
                response = client.get(
                    "https://api.openalex.org/works",
                    params={
                        "search": query,
                        "filter": "type:article,has_abstract:true",
                        "per-page": 5,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except Exception:  # noqa: BLE001
            return {"status": "limited", "facts": [], "sources": []}
        if not isinstance(payload, dict):
            return {"status": "limited", "facts": [], "sources": []}
        for result in payload.get("results") or []:
            if not isinstance(result, dict):
                continue
            title = str(result.get("display_name") or "").strip()
            abstract = self._openalex_abstract_text(result.get("abstract_inverted_index")).strip()
            if re.search(r"\bdoes not have an abstract\b", abstract, re.IGNORECASE):
                continue
            if not title or not abstract or not self._fact_result_is_relevant(query, title, abstract, research_brief):
                continue
            sentences = [
                part.strip()
                for part in re.split(r"(?<=[.!?])\s+", abstract)
                if len(part.strip()) > 30 and self._fact_sentence_is_useful(part, query)
            ]
            if not sentences:
                continue
            facts = [
                {
                    "fact_id": f"F{index}",
                    "claim": sentence[:260],
                    "source_id": "S1",
                }
                for index, sentence in enumerate(sentences[:5], start=1)
            ]
            primary_location = result.get("primary_location") if isinstance(result.get("primary_location"), dict) else {}
            source = primary_location.get("source") if isinstance(primary_location.get("source"), dict) else {}
            source_url = str(result.get("doi") or primary_location.get("landing_page_url") or result.get("id") or "")
            return {
                "status": "verified",
                "provider": "openalex",
                "query_used": query,
                "topic_title": title,
                "publication_year": result.get("publication_year"),
                "facts": facts,
                "sources": [
                    {
                        "source_id": "S1",
                        "title": title,
                        "url": source_url,
                        "provider": "openalex",
                        "container": source.get("display_name"),
                        "publication_year": result.get("publication_year"),
                    }
                ],
                "editorial_rule": "Use peer-reviewed or scholarly article facts as source material only. Preserve viral pacing, but every precise number, date, technical cause, history claim, or scientific claim must be grounded in fact_id references or rewritten conservatively. Do not use Wikipedia as a factual source.",
            }
        return {"status": "limited", "facts": [], "sources": []}

    def _openalex_abstract_text(self, abstract_inverted_index: Any) -> str:
        if not isinstance(abstract_inverted_index, dict):
            return ""
        positioned_words: list[tuple[int, str]] = []
        for word, positions in abstract_inverted_index.items():
            if not isinstance(positions, list):
                continue
            for position in positions:
                if isinstance(position, int):
                    positioned_words.append((position, str(word)))
        positioned_words.sort(key=lambda item: item[0])
        return " ".join(word for _, word in positioned_words)
