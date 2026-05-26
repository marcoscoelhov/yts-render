from __future__ import annotations

import concurrent.futures
import json
import math
import queue
import threading
from typing import Any, Callable, Protocol

from openai import OpenAI

from app.config import get_settings
from app.editorial.research_brief import build_research_brief
from app.editorial.retention import EDITORIAL_PROMPT_VERSION, build_retention_map, build_visual_opening_brief
from app.providers.errors import ProviderFailure
from app.utils import avg_words_per_sentence, max_words_single_sentence, sentence_split, tokenize, word_tokens


VISUAL_INTENTS = [
    "subject_closeup",
    "subject_in_context",
    "process_or_mechanism",
    "comparison",
    "scale_reference",
    "historical_evocation",
    "symbolic_fallback",
]

class LLMProvider(Protocol):
    provider_name: str

    def plan_topic(
        self,
        seed_theme: str,
        attempt: int,
        history: list[dict[str, Any]],
        requested_angle: str | None,
        tone: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        ...

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        ...

    def repair_script(self, script: dict[str, Any], gate_reasons: list[str], topic_plan: dict[str, Any]) -> dict[str, Any]:
        ...

    def plan_scenes(self, script: dict[str, Any], target_scene_count: int) -> list[dict[str, Any]]:
        ...

    def audit_publish_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class MockCreativeProvider:
    provider_name = "mock"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.angle_templates = [
            "o detalhe biologico que quase ninguem nota",
            "o mecanismo oculto que explica o fenomeno",
            "a comparacao inesperada que muda a perspectiva",
        ]

    def plan_topic(
        self,
        seed_theme: str,
        attempt: int,
        history: list[dict[str, Any]],
        requested_angle: str | None,
        tone: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        base_topic = seed_theme.strip().lower()
        angle = requested_angle or self.angle_templates[(attempt - 1) % len(self.angle_templates)]
        title_candidates = [
            f"{base_topic.capitalize()}: o detalhe que quase ninguem percebe",
            f"Por que {base_topic} parece impossivel quando voce entende {angle}",
            f"O segredo de {base_topic} que muda tudo em segundos",
        ]
        return {
            "canonical_topic": base_topic,
            "angle": angle,
            "hook_promise": f"um detalhe de {base_topic} que reconfigura o jeito de olhar para o tema",
            "title_candidates": title_candidates,
            "entities": [base_topic],
            "search_terms": [base_topic, f"{base_topic} curiosidade", f"{base_topic} explicacao"],
            "quality_metrics": {
                "attempt": attempt,
                "history_checked": len(history),
                "source_provider": "mock",
                "tone": tone or "intrigante_direto",
                "notes_applied": bool(notes),
            },
        }

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        subject = topic_plan["canonical_topic"]
        angle = topic_plan["angle"]
        fact_pack = topic_plan.get("fact_pack") if isinstance(topic_plan.get("fact_pack"), dict) else {}
        verified_facts = fact_pack.get("facts") or [] if fact_pack.get("status") == "verified" else []
        grounded_claims = [str(fact.get("claim") or "").strip() for fact in verified_facts if str(fact.get("claim") or "").strip()]
        source_fact_ids = [str(fact.get("fact_id")) for fact in verified_facts if fact.get("fact_id")][:2]
        hook = f"{subject.capitalize()} escondem um detalhe biologico quase absurdo."
        if grounded_claims:
            body = [
                grounded_claims[0],
                grounded_claims[1] if len(grounded_claims) > 1 else f"Esse recorte explica {angle} sem forcar precisao falsa.",
                grounded_claims[2] if len(grounded_claims) > 2 else f"Isso deixa {subject} mais concreto para quem assiste.",
                f"Quando esses fatos entram na mesma sequencia, {subject} parece ainda mais estranho.",
                f"Esse encadeamento sustenta a promessa sem inventar detalhe tecnico extra.",
            ]
            key_facts = grounded_claims[:3]
            claim_trace = [
                {
                    "text": claim.rstrip(".!?") + ".",
                    "source_fact_ids": [str(fact.get("fact_id"))],
                    "grounding": "fact_pack",
                }
                for claim, fact in zip(grounded_claims[:3], verified_facts[:3], strict=False)
                if fact.get("fact_id")
            ]
        else:
            body = [
                f"{subject.capitalize()} parece vago so ate aparecer o efeito ao redor.",
                f"O ponto central entra em {angle} e muda a escala do tema.",
                f"Em vez do objeto isolado, o entorno entrega a pista principal.",
                f"Esse recorte deixa {subject} mais concreto para quem assiste.",
                f"Assim cada cena sustenta a ideia sem inventar elemento aleatorio.",
            ]
            key_facts = [
                f"{subject.capitalize()} reage ao ambiente antes da maioria notar.",
                f"O tema fica mais claro quando se observa o contexto e a funcao.",
                f"O comportamento do sujeito economiza energia enquanto reduz risco.",
            ]
            source_fact_ids = []
            claim_trace = []
        ending = f"No fim, {subject} deixa de ser so um nome e vira um fenomeno legivel."
        narration_parts = [hook, *body, ending]
        full_narration = " ".join(narration_parts)
        token_count = len(tokenize(full_narration))
        estimated_duration_sec = round(max(35.0, min(55.0, len(word_tokens(full_narration)) / 2.55)), 2)
        retention_map = topic_plan.get("retention_map") or build_retention_map(round(estimated_duration_sec))
        visual_opening = topic_plan.get("visual_opening") or build_visual_opening_brief(topic_plan)
        qa_metrics = {
            "hook_score": 0.92,
            "clarity_score": 0.89,
            "information_density_score": 0.84,
            "repetition_score": 0.18,
            "ending_strength_score": 0.82,
            "estimated_duration_sec": estimated_duration_sec,
            "avg_words_per_sentence": round(avg_words_per_sentence(full_narration), 2),
            "max_words_single_sentence": max_words_single_sentence(full_narration),
            "words_per_second": round(len(word_tokens(full_narration)) / estimated_duration_sec, 2),
            "script_gate_pass": True,
            "source_provider": "mock",
            "editorial_prompt_version": topic_plan.get("editorial_prompt_version") or EDITORIAL_PROMPT_VERSION,
        }
        return {
            "title": topic_plan["title_candidates"][0],
            "hook": hook,
            "body_beats": body,
            "ending": ending,
            "cta": None,
            "full_narration": full_narration,
            "estimated_duration_sec": estimated_duration_sec,
            "key_facts": key_facts,
            "source_fact_ids": source_fact_ids,
            "claim_trace": claim_trace,
            "token_count": token_count,
            "language": "pt-BR",
            "retention_map": retention_map,
            "visual_opening": visual_opening,
            "qa_metrics": qa_metrics,
            "prompt_version": f"mock-{EDITORIAL_PROMPT_VERSION}",
        }

    def repair_script(self, script: dict[str, Any], gate_reasons: list[str], topic_plan: dict[str, Any]) -> dict[str, Any]:
        repaired = dict(script)
        fact_pack = topic_plan.get("fact_pack") if isinstance(topic_plan.get("fact_pack"), dict) else {}
        verified_facts = fact_pack.get("facts") or [] if fact_pack.get("status") == "verified" else []
        grounded_claims = [str(fact.get("claim") or "").strip() for fact in verified_facts if str(fact.get("claim") or "").strip()]
        narration = str(repaired.get("full_narration") or "")
        replacements = {
            "</prosody": "",
            "</prosody>": "",
            "<prosody>": "",
            " you heard right": " voce entendeu bem",
            "right": "mesmo",
            "giving your cat a second chance to see": "dando ao gato uma segunda chance de enxergar",
            "independiente": "independente",
            "ummini": "um mini",
        }
        for source, target in replacements.items():
            narration = narration.replace(source, target)
        narration = " ".join(narration.split())
        if "fact_pack_source_ids_missing" in gate_reasons or "high_risk_claims_need_fact_pack_grounding" in gate_reasons:
            lines = [f"{repaired.get('hook') or repaired.get('title') or 'O tema fica mais claro quando voce olha o mecanismo.'}".strip()]
            lines.extend(claim for claim in grounded_claims[:3] if claim)
            lines.append(f"No fim, {topic_plan.get('canonical_topic') or 'o tema'} faz sentido sem exagero.")
            narration = " ".join(line.rstrip(".!?") + "." for line in lines if line).strip()
            repaired["body_beats"] = grounded_claims[:3]
            repaired["key_facts"] = grounded_claims[:3]
            repaired["source_fact_ids"] = [str(fact.get("fact_id")) for fact in verified_facts if fact.get("fact_id")][: max(2, min(3, len(verified_facts)))]
            repaired["claim_trace"] = [
                {
                    "text": claim.rstrip(".!?") + ".",
                    "source_fact_ids": [str(fact.get("fact_id"))],
                    "grounding": "fact_pack",
                }
                for claim, fact in zip(grounded_claims[:3], verified_facts[:3], strict=False)
                if fact.get("fact_id")
            ]
        if "avg_sentence_too_long" in gate_reasons or "sentence_too_long" in gate_reasons:
            shortened_sentences = []
            for sentence in sentence_split(narration):
                words = word_tokens(sentence)
                if len(words) <= 14:
                    shortened_sentences.append(" ".join(words).strip())
                    continue
                midpoint = max(6, min(12, len(words) // 2))
                shortened_sentences.append(" ".join(words[:midpoint]).strip())
                shortened_sentences.append(" ".join(words[midpoint:]).strip())
            narration = " ".join(f"{sentence.rstrip('.!?')}." for sentence in shortened_sentences if sentence).strip()
        repaired["full_narration"] = narration
        repaired["language"] = "pt-BR"
        repaired["estimated_duration_sec"] = round(max(35.0, min(55.0, len(word_tokens(narration)) / 2.55)), 2)
        repaired["token_count"] = len(tokenize(narration))
        metrics = dict(repaired.get("qa_metrics") or {})
        metrics.update(
            {
                "hook_score": max(float(metrics.get("hook_score", 0.9)), 0.9),
                "clarity_score": max(float(metrics.get("clarity_score", 0.9)), 0.9),
                "information_density_score": max(float(metrics.get("information_density_score", 0.82)), 0.82),
                "ending_strength_score": max(float(metrics.get("ending_strength_score", 0.82)), 0.82),
                "repetition_score": min(float(metrics.get("repetition_score", 0.1)), 0.2),
                "estimated_duration_sec": repaired["estimated_duration_sec"],
                "avg_words_per_sentence": round(avg_words_per_sentence(narration), 2),
                "max_words_single_sentence": max_words_single_sentence(narration),
                "words_per_second": round(len(word_tokens(narration)) / repaired["estimated_duration_sec"], 2),
                "script_gate_pass": True,
                "repair_provider": self.provider_name,
                "repair_reasons": gate_reasons,
            }
        )
        repaired["qa_metrics"] = metrics
        return repaired

    def audit_publish_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"passed": True, "reasons": [], "retention_score": 0.85, "metadata_score": 0.85, "factual_score": 0.85, "provider": "mock"}

    def plan_scenes(self, script: dict[str, Any], target_scene_count: int) -> list[dict[str, Any]]:
        words = word_tokens(script["full_narration"])
        total_words = len(words)
        scene_count = max(5, min(8, target_scene_count))
        chunk_size = math.ceil(total_words / scene_count)
        subject = (
            script.get("canonical_topic")
            or script.get("primary_subject")
            or script.get("topic_hint")
            or (word_tokens(script["title"])[-1] if word_tokens(script["title"]) else "tema")
        )
        scenes: list[dict[str, Any]] = []
        cursor = 0
        for idx in range(scene_count):
            start = cursor
            end = min(total_words, start + chunk_size)
            if idx == scene_count - 1:
                end = total_words
            chunk_words = words[start:end]
            sentence = " ".join(chunk_words) or script["full_narration"]
            scenes.append(
                {
                    "scene_id": f"scene-{idx + 1}",
                    "order": idx + 1,
                    "narration_text": sentence,
                    "token_start": start,
                    "token_end": max(end - 1, start),
                    "estimated_duration_sec": round(script["estimated_duration_sec"] / scene_count, 2),
                    "visual_intent": VISUAL_INTENTS[idx % len(VISUAL_INTENTS)],
                    "primary_subject": subject,
                    "topic_hint": subject,
                    "image_prompt": (
                        f"vertical cinematic scientific illustration of {subject}, "
                        f"showing {VISUAL_INTENTS[idx % len(VISUAL_INTENTS)]}, "
                        "focused on the described phenomenon, no random people, no readable text, no watermark, no collage"
                    ),
                    "fallback_queries": [subject, f"{subject} fenomeno", f"{subject} espaco"],
                }
            )
            cursor = end
        return scenes


class MinimaxCreativeProvider:
    provider_name = "minimax"
    failure_provider_name = "minimax_text"
    model_name = "MiniMax-M2.7"

    def __init__(self) -> None:
        settings = get_settings()
        api_key = settings.resolved_minimax_text_api_key
        if not api_key:
            raise ProviderFailure(self.failure_provider_name, "missing minimax text api key")
        self.timeout_sec = settings.minimax_text_timeout_sec
        self.client = OpenAI(
            api_key=api_key,
            base_url=settings.minimax_text_base_url,
            timeout=self.timeout_sec,
        )

    def plan_topic(
        self,
        seed_theme: str,
        attempt: int,
        history: list[dict[str, Any]],
        requested_angle: str | None,
        tone: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        history_text = json.dumps(history[-8:], ensure_ascii=False)
        prompt = f"""
Crie pautas de curiosidades globais para YouTube Shorts em pt-BR.
Meta: retenção máxima, replay, compartilhamento orgânico e espanto genuíno. Zero clickbait falso.
Entrada do usuario: {seed_theme}
Tom selecionado: {tone or "intrigante_direto"}
Ângulo solicitado: {requested_angle or "auto"}
Notas do hub: {notes or "-"}
Tentativa: {attempt}
Histórico recente: {history_text}

A entrada pode ser um tema bruto ou um titulo completo.
Sempre transforme tema/titulo em uma pauta com copywriting viral e SEO otimizado para YouTube.
Se a entrada for titulo completo: preserve a promessa central, mas melhore clareza, palavra-chave, curiosidade e retenção.
Se a entrada for tema: crie um recorte especifico e pesquisavel, evitando assunto generico.
Pense cada pauta com esta régua editorial:
- Título: palavra-chave cedo quando natural, promessa específica e verificável
- Hook: 0 a 2 segundos, contraste, paradoxo ou fato impossível-mas-verdadeiro
- Loop: pergunta mental de tensão que só fecha no payoff
- Beats: escalada obrigatória de fato, implicação, consequência, imagem visual e virada
- Payoff: revelação mais surpreendente no último terço
- Fechamento: recontextualiza o hook e provoca replay mental
title_candidates devem ser em pt-BR, com 45 a 75 caracteres quando possivel, palavra-chave principal cedo, curiosidade concreta e sem promessa falsa.
Evite caixa alta exagerada, emojis obrigatorios e clickbait que o roteiro nao consiga cumprir.
Todos os campos textuais do JSON devem estar em portugues do Brasil (pt-BR), exceto search_terms quando pesquisa factual em ingles ajudar a recuperar fontes primarias.
Nao use chines, ingles, espanhol ou outro idioma em frases, fatos, metricas descritivas ou listas, exceto search_terms em ingles para pesquisa factual.
Excecoes permitidas: nomes proprios, nomes cientificos, siglas, marcas, titulos de fontes e URLs.

Responda JSON estrito com:
canonical_topic, angle, hook_promise, title_candidates (3 a 5), entities, search_terms, quality_metrics.

Regras para search_terms:
- search_terms devem servir para pesquisa factual em fonte primária, não só SEO.
- evite entidade nua isolada; prefira entidade + mecanismo + efeito + contexto.
- para temas científicos, médicos, técnicos ou históricos, inclua termos específicos e pesquisáveis, não genéricos.
- quando ajudar a recuperar papers ou documentação primária, inclua também 2 ou mais search_terms em inglês.
- misture consultas curtas e consultas explicativas, mas sem frases vagas.
- quality_metrics pode refletir se a pauta sustenta loop, payoff tardio, replay e promessa verificável.
Sem markdown.
"""
        payload = self._json_completion(prompt)
        if not isinstance(payload, dict):
            raise ProviderFailure(self.failure_provider_name, "topic planner returned non-object json")
        payload = self._normalize_topic_payload(payload, seed_theme)
        payload["quality_metrics"] = {**payload.get("quality_metrics", {}), "source_provider": self.provider_name}
        return payload

    def _normalize_topic_payload(self, payload: dict[str, Any], seed_theme: str) -> dict[str, Any]:
        aliases = {
            "canonical_topic": [
                "canonical_topic",
                "tema_canonico",
                "tópico_canônico",
                "topico_canonico",
                "tema_principal",
                "topico_principal",
                "topic",
                "tema",
            ],
            "angle": ["angle", "angulo", "ângulo", "recorte", "abordagem"],
            "hook_promise": ["hook_promise", "promessa_hook", "promessa_do_hook", "gancho", "hook"],
            "title_candidates": ["title_candidates", "titulos", "títulos", "candidatos_titulo", "candidatos_de_titulo"],
            "entities": ["entities", "entidades", "elementos", "assuntos"],
            "search_terms": ["search_terms", "termos_busca", "termos_de_busca", "palavras_chave", "keywords"],
            "quality_metrics": ["quality_metrics", "metricas_qualidade", "métricas_qualidade", "metricas"],
        }
        normalized: dict[str, Any] = {}
        for target, names in aliases.items():
            for name in names:
                if name in payload and payload[name] not in (None, "", []):
                    normalized[target] = payload[name]
                    break

        canonical_topic = str(normalized.get("canonical_topic") or seed_theme).strip()
        angle = str(normalized.get("angle") or f"o detalhe mais contraintuitivo de {canonical_topic}").strip()
        hook_promise = str(normalized.get("hook_promise") or f"por que {canonical_topic} muda quando voce entende o mecanismo").strip()

        title_candidates = normalized.get("title_candidates")
        if isinstance(title_candidates, str):
            title_candidates = [title_candidates]
        if not isinstance(title_candidates, list) or not title_candidates:
            title_candidates = [f"{canonical_topic.capitalize()}: o detalhe que quase ninguem percebe"]

        entities = normalized.get("entities")
        if isinstance(entities, str):
            entities = [entities]
        if not isinstance(entities, list) or not entities:
            entities = [canonical_topic]

        search_terms = normalized.get("search_terms")
        if isinstance(search_terms, str):
            search_terms = [search_terms]
        if not isinstance(search_terms, list) or not search_terms:
            search_terms = [canonical_topic, f"{canonical_topic} curiosidades", f"{canonical_topic} explicacao"]

        quality_metrics = normalized.get("quality_metrics")
        if not isinstance(quality_metrics, dict):
            quality_metrics = {}

        normalized_payload = {
            **payload,
            "canonical_topic": canonical_topic,
            "angle": angle,
            "hook_promise": hook_promise,
            "title_candidates": [str(title).strip() for title in title_candidates if str(title).strip()][:5],
            "entities": [str(entity).strip() for entity in entities if str(entity).strip()],
            "search_terms": [str(term).strip() for term in search_terms if str(term).strip()],
            "quality_metrics": quality_metrics,
        }
        return {
            **normalized_payload,
            "research_brief": build_research_brief(
                normalized_payload,
                {
                    "seed_theme": seed_theme,
                    "requested_angle": None,
                },
            ),
        }

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        prompt = f"""
Escreva um roteiro viral de curiosidades em pt-BR.
Entrada JSON: {json.dumps(topic_plan, ensure_ascii=False)}

Retorne JSON estrito com:
title, hook, body_beats, ending, cta, full_narration, estimated_duration_sec, key_facts, source_fact_ids, claim_trace, token_count, language, retention_map, visual_opening, qa_metrics, prompt_version

Mapeamento editorial obrigatório:
- title equivale ao Título
- hook equivale ao Hook de 0 a 2 segundos
- body_beats equivale aos Beats em escalada; mantenha o loop aberto nos beats iniciais e entregue o payoff no último beat ou no último terço da narração
- ending equivale ao Fechamento; ele deve recontextualizar o hook e provocar replay mental
- hashtags não fazem parte deste JSON e não devem aparecer nos campos narrados

Regras:
- 35 a 55 segundos
- meta editorial: retenção máxima, replay, compartilhamento orgânico e espanto genuíno, sem clickbait falso
- prompt_version deve ser "{EDITORIAL_PROMPT_VERSION}" salvo se a Entrada JSON trouxer versão editorial mais nova
- se Entrada JSON.editorial_mode for "viral_curiosidades", priorize simplicidade viral, clareza e wording seguro; não force explicação mecanística específica quando o fact_pack não sustentar isso
- se Entrada JSON.editorial_mode for "factual_strict", priorize grounding factual e não complete lacunas causais com plausibilidade editorial
- retention_map deve refletir os blocos da Entrada JSON.retention_map e mapear o roteiro em: visual_hook, proof_or_tension, escalation, turn_or_payoff, loop_close
- visual_opening deve descrever o primeiro frame esperado: sujeito, contraste visual, ação/resultado e o que evitar
- os primeiros 0-2s precisam funcionar visualmente mesmo sem áudio, com resultado, movimento ou contraste concreto
- use golden_sample_brief como régua editorial: aproxime-se dos padrões bons e evite os padrões ruins
- primeira frase com no maximo 12 palavras
- media por frase <= 14
- 80 a 120 palavras no total quando possível, sem sacrificar clareza factual
- use estrutura agressiva de retenção: hook de choque, loop aberto, escalada de fatos, payoff atrasado e fechamento memoravel
- cada frase deve criar uma pergunta mental ou tensão para a frase seguinte
- cada beat precisa justificar o próximo; é proibido soar neutro, didático demais, enciclopédico ou decorativo
- cada beat deve ficar mais estranho, visual ou impactante que o anterior
- não entregue a explicação completa no primeiro beat; plante o mistério e pague no último terço
- fatos científicos são matéria-prima, não estilo: transforme termos acadêmicos em consequência visual, tensão ou surpresa concreta
- não escreva como aula, artigo, definição enciclopédica ou professor explicando; escreva como Short de curiosidade com precisão factual
- o final deve criar loop de reassistência: ele precisa fazer o primeiro frame ou a primeira frase ganhar novo significado quando o usuário vê de novo
- não use frase meta como "fecha o ciclo", "agora tudo faz sentido" ou "essa curiosidade muda como você olha" no ending
- transforme fatos em consequência visual/mental, evitando tom de Wikipedia
- todos os campos textuais do JSON devem estar em portugues do Brasil (pt-BR)
- não use travessão nem en dash nos campos narrados; proibido usar os caracteres "—" e "–"; prefira ponto, virgula, dois-pontos ou frase nova
- nao use chines, ingles, espanhol ou outro idioma em title, hook, body_beats, ending, cta, full_narration, key_facts ou valores textuais de qa_metrics
- excecoes permitidas: nomes proprios, nomes cientificos, siglas, marcas, titulos de fontes e URLs
- key_facts deve ser uma lista em pt-BR, sem trechos em outros alfabetos ou idiomas
- não use frases com cara de IA ou meta-roteiro, como "No replay", "agora tudo faz sentido", "isso muda como você olha", "holograma biológico" ou equivalentes prontos
- escreva como uma pessoa brasileira narrando: oral, concreto, com ritmo, sem floreio genérico e sem parecer resumo de IA
- title deve ser otimizado para SEO e copywriting viral, com promessa especifica e palavra-chave cedo quando natural
- title não pode parecer título de artigo científico; evite "metabolismo de", "análise de", "estudo sobre", "mecanismos de" e formule como promessa visual ou surpresa concreta
- hook deve abrir com choque, contraste ou tensão imediata, sem introducao generica
- a primeira palavra do hook deve ser, quando natural, um número, nome próprio ou verbo de ação
- proibido começar hook ou full_narration com "você sabia", "voce sabia", "já imaginou", "ja imaginou", "nesse vídeo", "nesse video" ou fórmulas genéricas equivalentes
- comece direto por contraste, consequência, conflito ou fato específico
- cada body_beat deve entregar um fato concreto que sustente a promessa do titulo e aumente a curiosidade
- fatos acima de viralidade: não invente números, nacionalidades, planos, materiais, causas técnicas ou soluções de engenharia se eles não estiverem na Entrada JSON ou forem conhecimento extremamente consolidado
- se houver incerteza factual, use formulação conservadora e geral em vez de precisão falsa; prefira “engenheiros reduziram a inclinação removendo solo sob a base” a números específicos não verificados
- evite frases absolutas/enganosas como “está garantida”, “a física prova”, “domina a física”, “desafia a física” ou “a inclinação sustenta”
- se a Entrada JSON tiver fact_pack.status="verified", use o fact_pack como fonte factual obrigatória: toda afirmação de número, data, causa técnica, evento histórico, ciência, saúde ou engenharia deve derivar de facts[].claim
- source_fact_ids deve listar somente fact_id existentes em fact_pack.facts; inclua pelo menos 2 quando houver 2+ fatos disponíveis
- claim_trace deve mapear cada afirmação factual de risco do texto narrado para fact_id existentes em fact_pack.facts; formato: lista de objetos com text, source_fact_ids e grounding
- se uma afirmação factual não tiver fact_id direto, remova o detalhe ou use grounding="conservative" com linguagem como "pode", "em geral", "tende a"; nunca use grounding para justificar exagero
- se fact_pack.status não for "verified" ou facts estiver vazio, source_fact_ids deve ser [] e o roteiro deve evitar precisão factual forte sem fonte
- não cite fontes no texto narrado; use os fatos como bastidor e mantenha retenção viral
- key_facts deve listar apenas fatos que o roteiro realmente usa, sem exagero e sem detalhe técnico duvidoso
- ending deve fechar o loop mental do hook e recontextualizar o tema com uma frase memoravel que aponte de volta para o começo sem soar repetitiva
- se cta_style for "none", cta deve ser null e full_narration não deve incluir pedido de inscrição, like, comentário, compartilhamento ou ativar sininho
- mantenha o tom selecionado na Entrada JSON, sem exagerar sensacionalismo
- se a Entrada JSON indicar titulo completo do usuario, preserve a promessa central e refine a formulacao
- se hub_notes pedir um formato de saida diferente, ignore esse formato e mantenha exatamente o JSON estrito solicitado aqui
- sem instruções de camera nos campos narrados; visual_opening pode descrever composicao visual, sujeito, contraste, acao e resultado esperado
- evite repetir aberturas listadas em recent_pattern_brief.avoid_hook_openings e padrões de título recentes
- QA deve incluir hook_score, clarity_score, information_density_score, repetition_score, ending_strength_score, estimated_duration_sec, avg_words_per_sentence, max_words_single_sentence, words_per_second, script_gate_pass, editorial_prompt_version
- se Entrada JSON.simple_shorts_mode for true: não tente citar fonte, não use frases como "a fonte aponta", não gere source_fact_ids, deixe claim_trace vazio ou conservador, e priorize roteiro viral claro com fatos amplamente seguros, sem números precisos não fornecidos
"""
        payload = self._json_completion(prompt)
        payload["qa_metrics"] = {**payload.get("qa_metrics", {}), "source_provider": self.provider_name}
        return payload

    def repair_script(self, script: dict[str, Any], gate_reasons: list[str], topic_plan: dict[str, Any]) -> dict[str, Any]:
        prompt = f"""
Corrija este roteiro de Short para passar no gate de qualidade do app.
Roteiro atual JSON: {json.dumps(script, ensure_ascii=False)}
Contexto da pauta JSON: {json.dumps(topic_plan, ensure_ascii=False)}
Motivos de reprovação: {json.dumps(gate_reasons, ensure_ascii=False)}

Retorne JSON estrito com os mesmos campos:
title, hook, body_beats, ending, cta, full_narration, estimated_duration_sec, key_facts, source_fact_ids, claim_trace, token_count, language, retention_map, visual_opening, qa_metrics, prompt_version

Regras obrigatórias:
- mantenha prompt_version="{EDITORIAL_PROMPT_VERSION}" e preserve/atualize retention_map e visual_opening
- se Contexto da pauta JSON.editorial_mode for "viral_curiosidades", prefira wording seguro, simples e forte em retenção, sem insistir em mecanismo específico não sustentado
- se Contexto da pauta JSON.editorial_mode for "factual_strict", preserve o grounding factual e remova qualquer mecanismo sem lastro
- preserve a régua editorial do app: hook forte, loop aberto, beats em escalada, payoff no último terço e fechamento que provoque replay
- se os motivos incluírem weak_loop_closure ou ending_not_connected_to_hook, corrija o bloco loop_close sem criar final genérico repetitivo
- o novo ending deve criar loop de reassistência: o início precisa ganhar novo significado na segunda visualização
- não use frase meta como "fecha o ciclo", "agora tudo faz sentido" ou "essa curiosidade muda como você olha"
- preserve tom viral mesmo quando usar fatos científicos; não transforme o roteiro em aula ou resumo acadêmico
- todos os campos textuais devem estar em portugues do Brasil (pt-BR)
- remova travessão e en dash dos campos narrados; proibido usar os caracteres "—" e "–"; use ponto, virgula, dois-pontos ou frase nova
- remova qualquer palavra, frase ou expressão em ingles, espanhol, chines ou outro idioma
- remova qualquer SSML, HTML, XML, tags, entidades ou markup
- remova caracteres fora do alfabeto latino/pt-BR, especialmente caracteres CJK
- corrija palavras coladas e erros como "ummini", "independiente", "right"
- corrija pontuação quebrada como "no. centro", "a. refletância" e palavras coladas como "unidadede"
- remova frases com cara de IA ou meta-roteiro, como "No replay", "agora tudo faz sentido", "isso muda como você olha", "holograma biológico" ou equivalentes prontos
- mantenha duração estimada entre 35 e 55 segundos
- primeira frase com no máximo 12 palavras
- média por frase <= 14 e frase máxima <= 20 palavras
- mantenha 80 a 120 palavras no total quando possível
- preserve a promessa central e os fatos úteis, mas reescreva o necessário
- a primeira palavra do hook deve ser, quando natural, um número, nome próprio ou verbo de ação
- se o hook ou full_narration começar com "você sabia", "voce sabia", "já imaginou", "ja imaginou", "nesse vídeo" ou equivalente, reescreva para começar direto por contraste, consequência, conflito ou fato específico
- aumente retenção sem inventar fatos: hook mais agressivo, loop aberto, escalada de curiosidade, payoff no ultimo terço e final memoravel
- cada beat precisa justificar o próximo; remova frase neutra, didática demais, enciclopédica ou decorativa
- cada beat deve ficar mais estranho, visual ou impactante que o anterior
- fatos acima de viralidade: remova números, nacionalidades, planos, materiais, causas técnicas ou soluções de engenharia que não estejam bem sustentados pelo contexto
- evite frases absolutas/enganosas como “está garantida”, “a física prova”, “domina a física”, “desafia a física” ou “a inclinação sustenta”
- se os motivos incluírem factual_risk_requires_conservative_rewrite, reescreva TODA afirmação de risco factual: números precisos, datas, porcentagens, medidas, causalidade técnica, claims médicos/biológicos, engenharia e frases absolutas
- se os motivos incluírem fact_pack_source_ids_missing ou high_risk_claims_need_fact_pack_grounding, use apenas facts[].claim do fact_pack no Contexto da pauta JSON e preencha source_fact_ids com os fact_id usados
- se os motivos incluírem invented_source_fact_ids, remova source_fact_ids inventados; se fact_pack não estiver verified, use source_fact_ids=[]
- se os motivos incluírem factual_claim_trace_missing, preencha claim_trace para cada afirmação factual de risco com fact_id existente ou reescreva a afirmação de modo conservador
- claim_trace deve ter objetos com text, source_fact_ids e grounding; source_fact_ids só pode conter ids existentes no fact_pack
- para afirmações de alto risco sem fonte explícita, use linguagem conservadora: “pode”, “tende a”, “em geral”, “uma das explicações”, “cerca de”, ou remova o detalhe específico
- se um detalhe técnico parecer duvidoso, substitua por formulação conservadora e verificável
- qa_metrics deve incluir hook_score, clarity_score, information_density_score, repetition_score, ending_strength_score, estimated_duration_sec, avg_words_per_sentence, max_words_single_sentence, words_per_second, script_gate_pass
Sem markdown.
"""
        payload = self._json_completion(prompt)
        payload["qa_metrics"] = {
            **payload.get("qa_metrics", {}),
            "source_provider": self.provider_name,
            "repair_provider": self.provider_name,
            "repair_reasons": gate_reasons,
        }
        return payload

    def audit_publish_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = f"""
Audite este pacote de Short antes da publicação. Seja rigoroso e prático.
Entrada JSON: {json.dumps(payload, ensure_ascii=False)}

Responda JSON estrito com:
passed, reasons, factual_score, retention_score, metadata_score, ending_score, suggestions

Regras:
- passed só pode ser true se o roteiro não parecer factualmente exagerado, o final não parecer truncado, título/hashtags forem publicáveis e não houver fonte falsa.
- reasons deve usar slugs curtos em inglês, ex: unsupported_claim, weak_ending, weak_hashtags, invented_source_fact_ids, low_retention.
- scores de 0 a 1.
- Não reescreva o roteiro; só audite.
Sem markdown.
"""
        audit = self._json_completion(prompt)
        audit["provider"] = self.provider_name
        return audit

    def plan_scenes(self, script: dict[str, Any], target_scene_count: int) -> list[dict[str, Any]]:
        prompt = f"""
Divida este roteiro em {target_scene_count} cenas renderizáveis.
Roteiro JSON: {json.dumps(script, ensure_ascii=False)}

Fonte da verdade editorial:
- O prompt viral do hub orienta a geração do roteiro.
- A partir daqui, a fonte da verdade é full_narration + key_facts + hook/body_beats/ending do roteiro já aprovado.
- Não reescreva, aumente, encurte, dramatize ou invente novos beats narrativos.
- Cenas, imagens e legendas devem apenas segmentar e visualizar o roteiro aprovado.

Retorne apenas um JSON array.
Cada item precisa ter:
scene_id, order, narration_text, token_start, token_end, estimated_duration_sec, retention_role, visual_intent, primary_subject, image_prompt, fallback_queries
Visual intents permitidos: {json.dumps(VISUAL_INTENTS)}
Retention roles permitidos: visual_hook, proof_or_tension, escalation, turn_or_payoff, loop_close.
Cobertura total dos tokens.
Regras de segmentação obrigatórias:
- scene order=1 deve ter retention_role="visual_hook" e usar visual_opening como brief visual sem inventar novo beat.
- a ultima cena deve ter retention_role="loop_close" quando cobrir o fechamento.
- narration_text deve corresponder ao trecho de full_narration coberto por token_start/token_end.
- cada cena deve ter pelo menos 5 palavras em narration_text, exceto se for a última CTA curta.
- não crie cena só com punchline, negação ou frase retórica curta, como "Oito não."; junte com a frase anterior ou próxima.
- prefira 5 a 9 cenas com blocos visualmente completos, cada um contendo um fato concreto ou uma ação visualizável.
- preserve a ordem exata do roteiro.
Todos os campos textuais devem estar em portugues do Brasil (pt-BR), exceto image_prompt.
Nao use chines, espanhol ou outro idioma em narration_text, primary_subject ou fallback_queries.
Excecoes permitidas: nomes proprios, nomes cientificos, siglas, marcas e nomes de fontes.

Regras obrigatorias para image_prompt:
- image_prompt MUST be written in English only, even when the narration is pt-BR
- describe only a vertical cinematic visual scene with natural/scientific objects
- every image_prompt must depict the concrete fact in that scene's narration_text, not just the generic visual_intent
- scene with order=1 is the visual hook frame: make it instantly legible in under one second, with a concrete result, movement, contrast, threat, paradox, or impossible-looking factual consequence tied to the hook and its own narration_text
- for scene order=1, avoid calm establishing shots, generic beauty shots, neutral portraits, abstract ambience, or vague scientific background
- for scene order=1, do not reveal a later payoff unless that payoff is already present in its narration_text
- do not copy the title, narration phrases, Portuguese words, numbers, written names, or any visible text
- avoid abstract props, floating spheres, random packages, lab glassware, generic sci-fi objects, or irrelevant backgrounds unless directly required by the narration
- make the central subject unmistakable in every frame
- never request title cards, posters, covers, signs, labels, captions, labeled diagrams, labeled charts, UI, interfaces, or infographics
- include in every image_prompt: no readable text anywhere, no letters, no words, no numbers, no logo, no watermark
- Example for narration about blue blood: "octopus anatomy close-up with blue copper-rich blood vessels, cinematic underwater realism, no readable text anywhere"
- Example for narration about color change: "octopus changing skin color and texture while camouflaging from a predator, cinematic underwater realism, no readable text anywhere"
"""
        completion = getattr(self, "_json_array_completion", self._json_completion)
        payload = completion(prompt)
        payload = self._normalize_scene_plan_payload(payload)
        if not isinstance(payload, list):
            raise ProviderFailure(self.failure_provider_name, "scene planner returned non-list json")
        return payload

    def _normalize_scene_plan_payload(self, payload: Any) -> Any:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("scenes", "items", "results", "plan"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
            for value in payload.values():
                nested = self._extract_nested_scene_list(value)
                if nested is not None:
                    return nested
        return payload

    def _extract_nested_scene_list(self, value: Any) -> list[dict[str, Any]] | None:
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            first = value[0]
            if {"scene_id", "narration_text"} & set(first):
                return value
        if isinstance(value, dict):
            for nested in value.values():
                result = self._extract_nested_scene_list(nested)
                if result is not None:
                    return result
        return None

    def _json_completion(self, prompt: str) -> Any:
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "Return valid JSON only. No markdown fences."},
                    {"role": "user", "content": prompt},
                ],
                timeout=self.timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailure(self.failure_provider_name, str(exc)) from exc
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            raise ProviderFailure(self.failure_provider_name, "empty text response")
        raw = self._strip_think(raw)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            extracted = self._extract_json(raw)
            if extracted is not None:
                try:
                    return json.loads(extracted)
                except Exception:
                    pass
            raise ProviderFailure(self.failure_provider_name, f"invalid json: {raw[:300]}") from exc

    def _extract_json(self, raw: str) -> str | None:
        candidates = []
        first_obj = raw.find("{")
        last_obj = raw.rfind("}")
        if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
            candidates.append(raw[first_obj : last_obj + 1])
        first_arr = raw.find("[")
        last_arr = raw.rfind("]")
        if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
            candidates.append(raw[first_arr : last_arr + 1])
        for candidate in candidates:
            try:
                json.loads(candidate)
                return candidate
            except Exception:
                continue
        return None

    def _strip_think(self, raw: str) -> str:
        if "</think>" in raw:
            raw = raw.split("</think>", 1)[1].strip()
        return raw


class DeepSeekCreativeProvider(MinimaxCreativeProvider):
    provider_name = "deepseek"
    failure_provider_name = "deepseek_text"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.deepseek_api_key:
            raise ProviderFailure(self.failure_provider_name, "missing deepseek api key")
        self.timeout_sec = settings.deepseek_timeout_sec
        self.model_name = settings.deepseek_model
        self.client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=self.timeout_sec,
        )


class OpenAICreativeProvider(MinimaxCreativeProvider):
    provider_name = "openai"
    failure_provider_name = "openai_text"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.openai_api_key:
            raise ProviderFailure(self.failure_provider_name, "missing openai api key")
        self.timeout_sec = settings.openai_timeout_sec
        self.model_name = settings.openai_model
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=self.timeout_sec,
        )

    def _json_completion(self, prompt: str) -> Any:
        try:
            response = self.client.responses.create(
                model=self.model_name,
                instructions="Return valid JSON only. No markdown fences.",
                input=prompt,
                text={"format": {"type": "json_object"}},
                timeout=self.timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailure(self.failure_provider_name, str(exc)) from exc
        raw = (getattr(response, "output_text", None) or "").strip()
        if not raw:
            raise ProviderFailure(self.failure_provider_name, "empty text response")
        raw = self._strip_think(raw)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            extracted = self._extract_json(raw)
            if extracted is not None:
                try:
                    return json.loads(extracted)
                except Exception:
                    pass
            raise ProviderFailure(self.failure_provider_name, f"invalid json: {raw[:300]}") from exc

    def _json_array_completion(self, prompt: str) -> Any:
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "Return valid JSON only. Top-level must be an array. No markdown fences."},
                    {"role": "user", "content": prompt},
                ],
                timeout=self.timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailure(self.failure_provider_name, str(exc)) from exc
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            raise ProviderFailure(self.failure_provider_name, "empty text response")
        raw = self._strip_think(raw)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            extracted = self._extract_json(raw)
            if extracted is not None:
                try:
                    return json.loads(extracted)
                except Exception:
                    pass
            raise ProviderFailure(self.failure_provider_name, f"invalid json array: {raw[:300]}") from exc


class QwenCreativeProvider(MinimaxCreativeProvider):
    provider_name = "qwen"
    failure_provider_name = "qwen_text"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.qwen_api_key:
            raise ProviderFailure(self.failure_provider_name, "missing qwen api key")
        self.timeout_sec = settings.qwen_timeout_sec
        self.model_name = settings.qwen_model
        self.client = OpenAI(
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            timeout=self.timeout_sec,
        )


class ResilientCreativeProvider:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.registry = LLMProviderRegistry()
        self.primary = self.registry.primary_provider()
        self.fallback = self.registry.fallback_provider()
        self.script_draft_provider = self.registry.script_draft_provider()
        self.repair_provider = self.registry.repair_provider()
        self.scene_provider = self.registry.scene_provider()
        self.strict_minimax_validation = self.settings.strict_minimax_validation

    def plan_topic(
        self,
        seed_theme: str,
        attempt: int,
        history: list[dict[str, Any]],
        requested_angle: str | None,
        tone: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if self.primary:
            timeout_sec = float(getattr(self.settings, "llm_topic_timeout_sec", self.settings.minimax_text_timeout_sec))
            try:
                return self._run_primary_with_timeout(
                    lambda: self.primary.plan_topic(seed_theme, attempt, history, requested_angle, tone=tone, notes=notes),
                    timeout_sec=timeout_sec,
                )
            except concurrent.futures.TimeoutError as exc:
                if self.strict_minimax_validation:
                    raise ProviderFailure("minimax_text", f"topic planner timed out after {timeout_sec}s") from exc
                if not self.fallback:
                    raise ProviderFailure("llm_registry", f"topic planner timed out after {timeout_sec}s and no fallback provider is available") from exc
                payload = self.fallback.plan_topic(seed_theme, attempt, history, requested_angle, tone=tone, notes=notes)
                payload["quality_metrics"]["fallback_reason"] = (
                    f"minimax_text topic planner timed out after {timeout_sec}s"
                )
                payload["quality_metrics"]["fallback_used"] = True
                payload["quality_metrics"]["fallback_stage"] = "topic_plan_timeout"
                return payload
            except ProviderFailure as exc:
                if self.strict_minimax_validation:
                    raise
                if not self.fallback:
                    raise
                payload = self.fallback.plan_topic(seed_theme, attempt, history, requested_angle, tone=tone, notes=notes)
                payload["quality_metrics"]["fallback_reason"] = str(exc)
                payload["quality_metrics"]["fallback_used"] = True
                return payload
        if self.strict_minimax_validation:
            raise ProviderFailure("llm_registry", "strict minimax validation requires a primary llm provider")
        if not self.fallback:
            raise ProviderFailure("llm_registry", "no topic llm provider is available")
        return self.fallback.plan_topic(seed_theme, attempt, history, requested_angle, tone=tone, notes=notes)

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        candidates = self._script_generation_candidates()
        if not candidates:
            raise ProviderFailure("llm_registry", "no script llm provider is available")
        failures: list[str] = []
        for index, (role, provider, timeout_sec) in enumerate(candidates):
            try:
                payload = self._run_primary_with_timeout(
                    lambda provider=provider: provider.generate_script(topic_plan),
                    timeout_sec=timeout_sec,
                )
                metrics = payload.setdefault("qa_metrics", {})
                metrics["generation_provider_role"] = role
                metrics["generation_provider"] = getattr(provider, "provider_name", role)
                metrics["script_generation_fallback_used"] = index > 0
                if failures:
                    metrics["script_generation_fallback_reasons"] = failures
                return payload
            except concurrent.futures.TimeoutError as exc:
                message = f"{getattr(provider, 'provider_name', role)} script generation timed out after {timeout_sec}s"
                failures.append(message)
                if self.strict_minimax_validation and provider is self.primary:
                    raise ProviderFailure(getattr(provider, "failure_provider_name", role), message) from exc
            except ProviderFailure as exc:
                failures.append(str(exc))
                if self.strict_minimax_validation and provider is self.primary:
                    raise
        raise ProviderFailure("llm_registry", f"script generation failed across providers: {'; '.join(failures)}")

    def audit_publish_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.primary:
            timeout_sec = float(getattr(self.settings, "llm_publish_audit_timeout_sec", self.settings.minimax_text_timeout_sec))
            try:
                return self._run_primary_with_timeout(
                    lambda: self.primary.audit_publish_package(payload),
                    timeout_sec=timeout_sec,
                )
            except concurrent.futures.TimeoutError as exc:
                if self.strict_minimax_validation:
                    raise ProviderFailure("minimax_text", f"publish audit timed out after {timeout_sec}s") from exc
                if not self.fallback:
                    raise ProviderFailure("llm_registry", f"publish audit timed out after {timeout_sec}s and no fallback provider is available") from exc
                audit = self.fallback.audit_publish_package(payload)
                audit["fallback_reason"] = f"minimax_text publish audit timed out after {timeout_sec}s"
                audit["fallback_used"] = True
                audit["fallback_stage"] = "publish_audit_timeout"
                return audit
            except ProviderFailure as exc:
                if self.strict_minimax_validation:
                    raise
                if not self.fallback:
                    raise
                audit = self.fallback.audit_publish_package(payload)
                audit["fallback_reason"] = str(exc)
                audit["fallback_used"] = True
                return audit
        if self.strict_minimax_validation:
            raise ProviderFailure("llm_registry", "strict minimax validation requires a primary llm provider")
        if not self.fallback:
            raise ProviderFailure("llm_registry", "no publish audit llm provider is available")
        return self.fallback.audit_publish_package(payload)

    def plan_scenes(self, script: dict[str, Any], target_scene_count: int) -> list[dict[str, Any]]:
        if self.primary:
            timeout_sec = float(getattr(self.settings, "llm_scene_plan_timeout_sec", self.settings.minimax_scene_plan_timeout_sec))
            try:
                return self._run_primary_with_timeout(
                    lambda: self.primary.plan_scenes(script, target_scene_count),
                    timeout_sec=timeout_sec,
                )
            except concurrent.futures.TimeoutError:
                if self.strict_minimax_validation:
                    raise ProviderFailure("minimax_text", f"scene planner timed out after {timeout_sec}s")
                provider = getattr(self, "scene_provider", None) or self.fallback
                if not provider:
                    raise ProviderFailure("llm_registry", f"scene planner timed out after {timeout_sec}s and no fallback provider is available")
                scenes = provider.plan_scenes(script, target_scene_count)
                for scene in scenes:
                    scene["provider_fallback_reason"] = (
                        f"minimax_text scene planner timed out after {timeout_sec}s"
                    )
                return scenes
            except ProviderFailure as exc:
                if self.strict_minimax_validation:
                    raise
                provider = getattr(self, "scene_provider", None) or self.fallback
                if not provider:
                    raise
                scenes = provider.plan_scenes(script, target_scene_count)
                for scene in scenes:
                    scene["provider_fallback_reason"] = str(exc)
                return scenes
        if self.strict_minimax_validation:
            raise ProviderFailure("llm_registry", "strict minimax validation requires a primary llm provider")
        provider = getattr(self, "scene_provider", None) or self.fallback
        if not provider:
            raise ProviderFailure("llm_registry", "no scene llm provider is available")
        return provider.plan_scenes(script, target_scene_count)

    def repair_script(self, script: dict[str, Any], gate_reasons: list[str], topic_plan: dict[str, Any]) -> dict[str, Any]:
        provider = getattr(self, "repair_provider", None) or self.primary
        if provider:
            try:
                return self._run_primary_with_timeout(
                    lambda: provider.repair_script(script, gate_reasons, topic_plan),
                    timeout_sec=self._provider_timeout_sec(provider, self.settings.minimax_script_timeout_sec),
                )
            except concurrent.futures.TimeoutError as exc:
                if self.strict_minimax_validation:
                    timeout_sec = self._provider_timeout_sec(provider, self.settings.minimax_script_timeout_sec)
                    raise ProviderFailure(getattr(provider, "failure_provider_name", "llm_provider"), f"script repair timed out after {timeout_sec}s") from exc
                if self.settings.llm_enable_fallback and self.fallback:
                    payload = self._run_primary_with_timeout(
                        lambda: self.fallback.repair_script(script, [*gate_reasons, str(exc)], topic_plan),
                        timeout_sec=self._provider_timeout_sec(
                            self.fallback,
                            float(getattr(self.settings, "llm_script_draft_timeout_sec", self.settings.minimax_script_timeout_sec)),
                        ),
                    )
                    payload.setdefault("qa_metrics", {})["fallback_used"] = True
                    timeout_sec = self._provider_timeout_sec(provider, self.settings.minimax_script_timeout_sec)
                    payload["qa_metrics"]["fallback_reason"] = (
                        f"{getattr(provider, 'provider_name', 'llm')} script repair timed out after {timeout_sec}s"
                    )
                    payload["qa_metrics"]["fallback_stage"] = "script_repair_timeout"
                    return payload
                raise
            except ProviderFailure as exc:
                if self.strict_minimax_validation:
                    raise
                if self.settings.llm_enable_fallback and self.fallback:
                    payload = self._run_primary_with_timeout(
                        lambda: self.fallback.repair_script(script, [*gate_reasons, str(exc)], topic_plan),
                        timeout_sec=self._provider_timeout_sec(
                            self.fallback,
                            float(getattr(self.settings, "llm_script_draft_timeout_sec", self.settings.minimax_script_timeout_sec)),
                        ),
                    )
                    payload.setdefault("qa_metrics", {})["fallback_used"] = True
                    payload["qa_metrics"]["fallback_reason"] = str(exc)
                    return payload
                raise
        if self.strict_minimax_validation:
            raise ProviderFailure("llm_registry", "strict minimax validation requires a primary llm provider")
        if not self.fallback:
            raise ProviderFailure("llm_registry", "no script repair llm provider is available")
        return self.fallback.repair_script(script, gate_reasons, topic_plan)

    def repair_script_with_fallback(self, script: dict[str, Any], gate_reasons: list[str], topic_plan: dict[str, Any]) -> dict[str, Any] | None:
        if self.strict_minimax_validation:
            return None
        if not self.settings.llm_enable_fallback or not self.fallback:
            return None
        payload = self._run_primary_with_timeout(
            lambda: self.fallback.repair_script(script, gate_reasons, topic_plan),
            timeout_sec=self._provider_timeout_sec(
                self.fallback,
                float(getattr(self.settings, "llm_script_draft_timeout_sec", self.settings.minimax_script_timeout_sec)),
            ),
        )
        payload.setdefault("qa_metrics", {})["fallback_used"] = True
        payload["qa_metrics"]["fallback_stage"] = "script_quality_gate"
        return payload

    def _provider_timeout_sec(self, provider: LLMProvider, default_timeout_sec: float) -> float:
        return float(getattr(provider, "timeout_sec", default_timeout_sec) or default_timeout_sec)

    def _script_generation_candidates(self) -> list[tuple[str, LLMProvider, float]]:
        primary_timeout = float(getattr(self.settings, "minimax_script_timeout_sec", 150.0))
        draft_timeout = float(getattr(self.settings, "llm_script_draft_timeout_sec", primary_timeout))
        if self.strict_minimax_validation:
            return [("primary", self.primary, primary_timeout)] if self.primary else []
        candidates: list[tuple[str, LLMProvider, float]] = []
        seen: set[int] = set()
        for role, provider, timeout_sec in [
            ("primary", self.primary, primary_timeout),
            ("fallback", self.fallback, self._provider_timeout_sec(self.fallback, draft_timeout) if self.fallback else draft_timeout),
            ("draft", getattr(self, "script_draft_provider", None), draft_timeout),
        ]:
            if not provider or id(provider) in seen:
                continue
            seen.add(id(provider))
            candidates.append((role, provider, timeout_sec))
        return candidates

    def _run_primary_with_timeout(self, fn: Callable[[], Any], timeout_sec: float) -> Any:
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result_queue.put(("ok", fn()), block=False)
            except BaseException as exc:  # noqa: BLE001
                result_queue.put(("error", exc), block=False)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=timeout_sec)
        if thread.is_alive():
            raise concurrent.futures.TimeoutError()
        status, payload = result_queue.get_nowait()
        if status == "error":
            raise payload
        return payload


class LLMProviderRegistry:
    def __init__(self) -> None:
        self.settings = get_settings()

    def primary_provider(self) -> LLMProvider | None:
        if self.settings.use_mock_providers:
            return MockCreativeProvider()
        return self._build_provider(self.settings.llm_primary_provider, required=True)

    def fallback_provider(self) -> LLMProvider | None:
        if self.settings.use_mock_providers:
            return MockCreativeProvider()
        provider = self._build_provider(self.settings.llm_fallback_provider, required=False)
        if provider:
            return provider
        if getattr(self.settings, "real_run_allow_mock_fallback", False):
            return MockCreativeProvider()
        return None

    def script_draft_provider(self) -> LLMProvider | None:
        if self.settings.use_mock_providers:
            return MockCreativeProvider()
        return self._build_provider(getattr(self.settings, "llm_script_draft_provider", ""), required=False)

    def repair_provider(self) -> LLMProvider | None:
        if self.settings.use_mock_providers:
            return MockCreativeProvider()
        return self._build_provider(self.settings.llm_repair_provider, required=False)

    def scene_provider(self) -> LLMProvider | None:
        if self.settings.use_mock_providers:
            return MockCreativeProvider()
        return self._build_provider(self.settings.llm_scene_provider, required=False)

    def _build_provider(self, name: str, required: bool) -> LLMProvider | None:
        normalized = (name or "").strip().lower()
        if normalized in {"", "none", "disabled"}:
            if required:
                raise ProviderFailure("llm_registry", "primary llm provider is disabled")
            return None
        if normalized in {"mock", "local"}:
            if not self.settings.use_mock_providers and not getattr(self.settings, "real_run_allow_mock_fallback", False):
                if required:
                    raise ProviderFailure("llm_registry", "mock provider is disabled for real runs")
                return None
            return MockCreativeProvider()
        try:
            if normalized in {"openai", "gpt-5", "gpt5", "gpt-5.4", "gpt5.4"}:
                return OpenAICreativeProvider()
            if normalized in {"minimax", "minimax_2_7", "minimax-m2.7"}:
                return MinimaxCreativeProvider()
            if normalized in {"deepseek", "deepseek_v4_flash", "deepseek-v4-flash", "deepseek_v4"}:
                return DeepSeekCreativeProvider()
        except ProviderFailure:
            if required:
                raise
            return None
        if required:
            raise ProviderFailure("llm_registry", f"unknown llm provider: {name}")
        return None
