from __future__ import annotations

import asyncio
import audioop
import base64
import colorsys
import concurrent.futures
import json
import math
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
import binascii
from pathlib import Path
from typing import Any, Callable, Protocol

import anthropic
import httpx
import imageio_ffmpeg
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFilter

from app.config import get_settings
from app.editorial.retention import EDITORIAL_PROMPT_VERSION, build_retention_map, build_visual_opening_brief
from app.utils import avg_words_per_sentence, max_words_single_sentence, parse_srt, sentence_split, tokenize, word_tokens, wrap_caption


VISUAL_INTENTS = [
    "subject_closeup",
    "subject_in_context",
    "process_or_mechanism",
    "comparison",
    "scale_reference",
    "historical_evocation",
    "symbolic_fallback",
]


class ProviderFailure(RuntimeError):
    def __init__(self, provider: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.details = details or {}


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
        estimated_duration_sec = round(max(28.5, min(41.5, len(word_tokens(full_narration)) / 2.55)), 2)
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
        repaired["estimated_duration_sec"] = round(max(25.0, min(42.0, len(word_tokens(narration)) / 2.55)), 2)
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
Você cria pautas de Shorts de curiosidades em pt-BR.
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
title_candidates devem ser em pt-BR, com 45 a 75 caracteres quando possivel, palavra-chave principal cedo, curiosidade concreta e sem promessa falsa.
Evite caixa alta exagerada, emojis obrigatorios e clickbait que o roteiro nao consiga cumprir.
Todos os campos textuais do JSON devem estar em portugues do Brasil (pt-BR).
Nao use chines, ingles, espanhol ou outro idioma em frases, fatos, metricas descritivas ou listas.
Excecoes permitidas: nomes proprios, nomes cientificos, siglas, marcas, titulos de fontes e URLs.

Responda JSON estrito com:
canonical_topic, angle, hook_promise, title_candidates (3 a 5), entities, search_terms, quality_metrics.
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

        return {
            **payload,
            "canonical_topic": canonical_topic,
            "angle": angle,
            "hook_promise": hook_promise,
            "title_candidates": [str(title).strip() for title in title_candidates if str(title).strip()][:5],
            "entities": [str(entity).strip() for entity in entities if str(entity).strip()],
            "search_terms": [str(term).strip() for term in search_terms if str(term).strip()],
            "quality_metrics": quality_metrics,
        }

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        prompt = f"""
Escreva um roteiro viral de curiosidades em pt-BR.
Entrada JSON: {json.dumps(topic_plan, ensure_ascii=False)}

Retorne JSON estrito com:
title, hook, body_beats, ending, cta, full_narration, estimated_duration_sec, key_facts, source_fact_ids, claim_trace, token_count, language, retention_map, visual_opening, qa_metrics, prompt_version

Regras:
- 25 a 45 segundos
- prompt_version deve ser "{EDITORIAL_PROMPT_VERSION}" salvo se a Entrada JSON trouxer versão editorial mais nova
- retention_map deve refletir os blocos da Entrada JSON.retention_map e mapear o roteiro em: visual_hook, proof_or_tension, escalation, turn_or_payoff, loop_close
- visual_opening deve descrever o primeiro frame esperado: sujeito, contraste visual, ação/resultado e o que evitar
- os primeiros 0-2s precisam funcionar visualmente mesmo sem áudio, com resultado, movimento ou contraste concreto
- use golden_sample_brief como régua editorial: aproxime-se dos padrões bons e evite os padrões ruins
- primeira frase com no maximo 12 palavras
- media por frase <= 14
- use estrutura agressiva de retenção: hook de choque, loop aberto, escalada de fatos, payoff atrasado e fechamento memoravel
- cada frase deve criar uma pergunta mental ou tensão para a frase seguinte
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
- sem instruções de camera
- evite repetir aberturas listadas em recent_pattern_brief.avoid_hook_openings e padrões de título recentes
- QA deve incluir hook_score, clarity_score, information_density_score, repetition_score, ending_strength_score, estimated_duration_sec, avg_words_per_sentence, max_words_single_sentence, words_per_second, script_gate_pass, editorial_prompt_version
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
- mantenha duração estimada entre 25 e 45 segundos
- primeira frase com no máximo 12 palavras
- média por frase <= 14 e frase máxima <= 20 palavras
- preserve a promessa central e os fatos úteis, mas reescreva o necessário
- se o hook ou full_narration começar com "você sabia", "voce sabia", "já imaginou", "ja imaginou", "nesse vídeo" ou equivalente, reescreva para começar direto por contraste, consequência, conflito ou fato específico
- aumente retenção sem inventar fatos: hook mais agressivo, loop aberto, escalada de curiosidade, payoff no ultimo terço e final memoravel
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
scene_id, order, narration_text, token_start, token_end, estimated_duration_sec, visual_intent, primary_subject, image_prompt, fallback_queries
Visual intents permitidos: {json.dumps(VISUAL_INTENTS)}
Cobertura total dos tokens.
Regras de segmentação obrigatórias:
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
- do not copy the title, narration phrases, Portuguese words, numbers, written names, or any visible text
- avoid abstract props, floating spheres, random packages, lab glassware, generic sci-fi objects, or irrelevant backgrounds unless directly required by the narration
- make the central subject unmistakable in every frame
- never request title cards, posters, covers, signs, labels, captions, labeled diagrams, labeled charts, UI, interfaces, or infographics
- include in every image_prompt: no readable text anywhere, no letters, no words, no numbers, no logo, no watermark
- Example for narration about blue blood: "octopus anatomy close-up with blue copper-rich blood vessels, cinematic underwater realism, no readable text anywhere"
- Example for narration about color change: "octopus changing skin color and texture while camouflaging from a predator, cinematic underwater realism, no readable text anywhere"
"""
        payload = self._json_completion(prompt)
        if not isinstance(payload, list):
            raise ProviderFailure(self.failure_provider_name, "scene planner returned non-list json")
        return payload

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
            ("draft", getattr(self, "script_draft_provider", None), draft_timeout),
            ("primary", self.primary, primary_timeout),
            ("fallback", self.fallback, self._provider_timeout_sec(self.fallback, draft_timeout) if self.fallback else draft_timeout),
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
            if normalized in {"minimax", "minimax_2_7", "minimax-m2.7"}:
                return MinimaxCreativeProvider()
            if normalized in {"deepseek", "deepseek_v4_flash", "deepseek-v4-flash", "deepseek_v4"}:
                return DeepSeekCreativeProvider()
            if normalized in {"qwen", "qwen_max", "qwen-max", "qwen3-max", "qwen3.6-max-preview", "qwen3_6_max_preview"}:
                return QwenCreativeProvider()
        except ProviderFailure:
            if required:
                raise
            return None
        if required:
            raise ProviderFailure("llm_registry", f"unknown llm provider: {name}")
        return None


class SemanticVerifier:
    def __init__(self) -> None:
        settings = get_settings()
        self.use_mock_providers = settings.use_mock_providers
        text_api_key = settings.resolved_minimax_text_api_key
        self.enabled = not settings.use_mock_providers and bool(text_api_key)
        self.api_key = text_api_key or ""
        self.mmx_path = shutil.which("mmx")
        self._cache: dict[str, dict[str, Any]] = {}
        self._vision_disabled_reason: str | None = None

    def score(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        if self.use_mock_providers:
            return {
                "semantic_match": 0.9,
                "subject_salience": 0.88,
                "style_match": 0.84,
                "crop_fit": 0.93,
                "safety": 0.95,
                "artifact_penalty": 0.02,
                "text_or_watermark_penalty": 0.0,
                "total_score": 0.854,
            }
        heuristic = self._heuristic_score(scene, asset)
        if asset["provider"] == "local_semantic":
            return heuristic
        cache_key = f"{asset.get('uri')}::{scene.get('topic_hint') or scene.get('primary_subject')}"
        if cache_key in self._cache:
            return {**heuristic, **self._cache[cache_key]}
        if not self.enabled or not self.mmx_path:
            return self._verification_failed_score(heuristic, "vision verifier unavailable")
        if self._vision_disabled_reason:
            return self._verification_failed_score(heuristic, self._vision_disabled_reason)
        try:
            verified = self._vision_score(scene, asset)
            self._cache[cache_key] = verified
            return {**heuristic, **verified}
        except subprocess.TimeoutExpired as exc:
            self._vision_disabled_reason = f"vision verifier timed out after {exc.timeout}s"
            return self._verification_failed_score(heuristic, self._vision_disabled_reason)
        except Exception as exc:  # noqa: BLE001
            return self._verification_failed_score(heuristic, str(exc))

    def _heuristic_score(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        expected_terms = self._keywords(
            " ".join(
                [
                    str(scene.get("topic_hint", "")),
                    str(scene.get("primary_subject", "")),
                    str(scene.get("narration_text", "")),
                    str(scene.get("image_prompt", "")),
                    " ".join(scene.get("fallback_queries", [])),
                ]
            )
        )
        prompt_text = str(asset.get("prompt_snapshot", ""))
        source_text = " ".join(
            [
                str(asset.get("source_url", "")),
                str(asset.get("attribution", "")),
                str(asset.get("license_note", "")),
            ]
        )
        raw_asset_text = " ".join([prompt_text, source_text])
        asset_terms = self._keywords(raw_asset_text)
        prompt_terms = self._keywords(prompt_text)
        source_terms = self._keywords(source_text)
        overlap = len(expected_terms & asset_terms)
        coverage = overlap / max(len(expected_terms), 1)
        semantic = max(0.15, min(0.95, round(0.25 + coverage * 1.2, 3)))
        subject_salience = max(0.2, min(0.95, round(0.2 + coverage, 3)))
        if asset["provider"] == "local_semantic":
            semantic = max(semantic, 0.86)
            subject_salience = max(subject_salience, 0.84)
        style_match = 0.82 if asset["provider"] in {"ai", "minimax", "mock_ai"} else 0.74
        crop_fit = 0.93
        safety = 0.95
        artifact_penalty = 0.04 if asset["provider"] in {"ai", "minimax", "mock_ai"} else 0.02
        negative_text_constraint = any(
            phrase in prompt_text.lower()
            for phrase in [
                "sem texto",
                "sem watermark",
                "sem poster",
                "sem tipografia",
                "sem legenda",
                "no text",
                "without text",
                "no watermark",
                "no poster",
            ]
        )
        explicit_text_markers = {"poster", "post", "postit", "postits", "nota", "notas"}
        source_has_text_marker = any(token in source_terms for token in explicit_text_markers | {"texto"})
        prompt_has_text_marker = any(token in prompt_terms for token in explicit_text_markers | {"texto"})
        text_penalty = 0.2 if source_has_text_marker else 0.0
        if prompt_has_text_marker and not negative_text_constraint:
            text_penalty = 0.2
        total = (
            0.45 * semantic
            + 0.20 * subject_salience
            + 0.10 * style_match
            + 0.10 * crop_fit
            + 0.10 * safety
            - 0.15 * artifact_penalty
            - 0.20 * text_penalty
        )
        return {
            "semantic_match": round(semantic, 3),
            "subject_salience": round(subject_salience, 3),
            "style_match": round(style_match, 3),
            "crop_fit": round(crop_fit, 3),
            "safety": round(safety, 3),
            "artifact_penalty": round(artifact_penalty, 3),
            "text_or_watermark_penalty": round(text_penalty, 3),
            "total_score": round(total, 3),
        }

    def _verification_failed_score(self, heuristic: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            **heuristic,
            "verification_fallback_reason": reason,
            "verification_mode": "prompt_heuristic",
        }

    def _vision_score(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        asset_path = Path(asset["uri"][7:]) if str(asset.get("uri", "")).startswith("file://") else Path(asset["uri"])
        prompt = (
            "Avalie se esta imagem esta semanticamente alinhada com a cena de um video curto. "
            "Responda JSON estrito com keys description, aligned_boolean, alignment_score_0_to_1, "
            "subject_visibility_score_0_to_1, style_match_score_0_to_1, text_or_watermark_penalty_0_to_1, "
            "artifact_penalty_0_to_1, reasons. "
            f"Tema esperado: {scene.get('topic_hint') or scene.get('primary_subject')}. "
            f"Narracao da cena: {scene.get('narration_text')}. "
            f"Prompt de imagem esperado: {scene.get('image_prompt')}."
        )
        result = subprocess.run(
            [
                self.mmx_path,
                "--api-key",
                self.api_key,
                "--region",
                "global",
                "--output",
                "json",
                "--non-interactive",
                "vision",
                "describe",
                "--image",
                str(asset_path),
                "--prompt",
                prompt,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=25,
        )
        raw_stdout = result.stdout.strip()
        first_json = raw_stdout.find("{")
        if first_json == -1:
            raise ProviderFailure("minimax_vision", f"invalid vision output: {raw_stdout[:200]}")
        payload = json.loads(raw_stdout[first_json:])
        content = str(payload.get("content", "")).strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(content)
        semantic = float(data.get("alignment_score_0_to_1", 0.0))
        subject_salience = float(data.get("subject_visibility_score_0_to_1", semantic))
        style_match = float(data.get("style_match_score_0_to_1", 0.8))
        text_penalty = float(data.get("text_or_watermark_penalty_0_to_1", 0.0))
        artifact_penalty = float(data.get("artifact_penalty_0_to_1", 0.03))
        crop_fit = 0.93
        safety = 0.95
        total = (
            0.45 * semantic
            + 0.20 * subject_salience
            + 0.10 * style_match
            + 0.10 * crop_fit
            + 0.10 * safety
            - 0.15 * artifact_penalty
            - 0.20 * text_penalty
        )
        return {
            "semantic_match": round(semantic, 3),
            "subject_salience": round(subject_salience, 3),
            "style_match": round(style_match, 3),
            "crop_fit": round(crop_fit, 3),
            "safety": round(safety, 3),
            "artifact_penalty": round(artifact_penalty, 3),
            "text_or_watermark_penalty": round(text_penalty, 3),
            "total_score": round(total, 3),
            "vision_description": data.get("description"),
            "vision_reasons": data.get("reasons"),
            "vision_aligned": bool(data.get("aligned_boolean")),
        }

    def _keywords(self, text: str) -> set[str]:
        stopwords = {
            "a",
            "ao",
            "as",
            "com",
            "da",
            "de",
            "do",
            "dos",
            "e",
            "em",
            "no",
            "nos",
            "na",
            "nas",
            "o",
            "os",
            "ou",
            "para",
            "por",
            "sem",
            "um",
            "uma",
        }
        normalized = []
        for token in word_tokens(text.lower()):
            token = "".join(ch for ch in token if ch.isalnum())
            if len(token) >= 3 and token not in stopwords:
                normalized.append(token)
        return set(normalized)


class MockImageProvider:
    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        self._render_scene_image(scene, output_path, variant="ai")
        return {
            "provider": "mock_ai",
            "width": 1024,
            "height": 1536,
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
        }

    def _render_scene_image(self, scene: dict[str, Any], output_path: Path, variant: str) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        hue_seed = ((scene["order"] * 53) % 360) / 360.0
        sat = 0.55 if variant == "ai" else 0.30
        val = 0.88 if variant == "ai" else 0.76
        rgb = tuple(int(channel * 255) for channel in colorsys.hsv_to_rgb(hue_seed, sat, val))
        rgb2 = tuple(int(channel * 255) for channel in colorsys.hsv_to_rgb((hue_seed + 0.12) % 1.0, sat * 0.8, val * 0.65))
        image = Image.new("RGB", (1024, 1536), rgb)
        draw = ImageDraw.Draw(image, "RGBA")
        for idx in range(6):
            width = 880 - idx * 90
            height = 1280 - idx * 120
            x0 = (1024 - width) // 2 + idx * 8
            y0 = 120 + idx * 25
            x1 = x0 + width
            y1 = y0 + height
            alpha = 45 + idx * 18
            fill = rgb2 + (alpha,)
            draw.rounded_rectangle((x0, y0, x1, y1), radius=90, fill=fill)
        draw.ellipse((250, 260, 774, 980), fill=(255, 255, 255, 36))
        draw.rounded_rectangle((180, 920, 844, 1270), radius=120, fill=(0, 0, 0, 32))
        image = image.filter(ImageFilter.GaussianBlur(radius=0.5))
        image.save(output_path, format="PNG")


class LocalSemanticImageProvider:
    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        subject = str(scene.get("topic_hint") or scene.get("primary_subject") or "fenomeno").lower()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if ("buraco" in subject and "negro" in subject) or "black hole" in subject:
            self._render_black_hole(output_path, int(scene.get("order", 1)))
        else:
            self._render_scientific_symbol(output_path, subject, int(scene.get("order", 1)))
        with Image.open(output_path) as image:
            width, height = image.size
        return {
            "provider": "local_semantic",
            "width": width,
            "height": height,
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
            "license_note": "Local semantic fallback generated by the app.",
        }

    def _render_black_hole(self, output_path: Path, order: int) -> None:
        image = Image.new("RGB", (1024, 1536), (2, 4, 16))
        draw = ImageDraw.Draw(image, "RGBA")
        shift_x = ((order % 3) - 1) * 80
        shift_y = ((order % 4) - 1) * 45
        palette = [
            (255, 207, 103),
            (90, 196, 255),
            (255, 139, 60),
            (121, 204, 255),
        ]
        hot = palette[order % len(palette)]
        cool = palette[(order + 1) % len(palette)]
        for idx in range(220):
            x = (idx * 173 + order * 41) % 1024
            y = (idx * 97 + order * 67) % 1536
            alpha = 80 + (idx % 120)
            draw.ellipse((x, y, x + 2, y + 2), fill=(210, 230, 255, alpha))
        cx, cy = 512 + shift_x, 720 + shift_y
        disk_flattening = 2 + (order % 3)
        for radius, color in [
            (470, (26, 67, 125, 28)),
            (390, (*cool, 42)),
            (315, (*hot, 72)),
            (250, (255, 207, 103, 112)),
            (190, (121, 204, 255, 92)),
        ]:
            bbox = (cx - radius, cy - radius // disk_flattening, cx + radius, cy + radius // disk_flattening)
            draw.ellipse(bbox, outline=color, width=max(8, radius // 28))
        if order % 3 == 1:
            draw.polygon([(0, 980), (330, 880), (420, 1536), (0, 1536)], fill=(14, 25, 55, 170))
            draw.arc((cx - 430, cy - 330, cx + 430, cy + 330), 205, 340, fill=(*cool, 135), width=22)
        elif order % 3 == 2:
            draw.ellipse((730, 1010, 970, 1250), fill=(16, 31, 70, 210), outline=(*cool, 110), width=10)
            draw.arc((cx - 470, cy - 280, cx + 470, cy + 280), 0, 175, fill=(*hot, 135), width=20)
        else:
            for band in range(5):
                y = 1040 + band * 62
                draw.line((70, y, 954, y - 130), fill=(*cool, 34 + band * 18), width=5)
            draw.arc((cx - 360, cy - 250, cx + 360, cy + 250), 188, 352, fill=(*cool, 120), width=16)
        draw.ellipse((cx - 178, cy - 178, cx + 178, cy + 178), fill=(0, 0, 4, 255))
        draw.ellipse((cx - 220, cy - 220, cx + 220, cy + 220), outline=(*hot, 96), width=18)
        draw.arc((cx - 420, cy - 300, cx + 420, cy + 300), 10, 170, fill=(*hot, 115), width=18)
        image = image.filter(ImageFilter.GaussianBlur(radius=0.4))
        image.save(output_path, format="PNG")

    def _render_scientific_symbol(self, output_path: Path, subject: str, order: int) -> None:
        hue = ((len(subject) * 31 + order * 47) % 360) / 360
        bg = tuple(int(channel * 255) for channel in colorsys.hsv_to_rgb(hue, 0.55, 0.16))
        accent = tuple(int(channel * 255) for channel in colorsys.hsv_to_rgb((hue + 0.12) % 1, 0.65, 0.92))
        image = Image.new("RGB", (1024, 1536), bg)
        draw = ImageDraw.Draw(image, "RGBA")
        for idx in range(9):
            pad = 110 + idx * 42
            alpha = 130 - idx * 10
            draw.ellipse((pad, 300 + idx * 24, 1024 - pad, 1220 - idx * 24), outline=accent + (alpha,), width=10)
        draw.ellipse((380, 620, 644, 884), fill=(255, 255, 255, 42), outline=accent + (180,), width=12)
        image = image.filter(ImageFilter.GaussianBlur(radius=0.35))
        image.save(output_path, format="PNG")


class MinimaxImageProvider:
    def __init__(self) -> None:
        settings = get_settings()
        api_key = settings.resolved_minimax_image_api_key
        if not api_key:
            raise ProviderFailure("minimax_image", "missing minimax image api key")
        self.url = settings.minimax_image_base_url
        self.key = api_key

    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = httpx.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={
                        "model": "image-01",
                        "prompt": scene["image_prompt"],
                        "aspect_ratio": "2:3",
                        "response_format": "base64",
                    },
                    timeout=httpx.Timeout(45.0, connect=15.0),
                )
                response.raise_for_status()
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt == 3:
                    raise ProviderFailure("minimax_image", f"connection failed after {attempt} attempts: {exc}") from exc
                time.sleep(1.5 * attempt)
        else:
            raise ProviderFailure("minimax_image", f"connection failed: {last_error}")
        payload = response.json()
        if payload.get("base_resp", {}).get("status_code", 0) not in (0, None):
            raise ProviderFailure("minimax_image", payload["base_resp"].get("status_msg", "minimax image error"))
        image_base64 = payload["data"]["image_base64"][0]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode(image_base64))
        with Image.open(output_path) as image:
            width, height = image.size
        return {
            "provider": "minimax",
            "width": width,
            "height": height,
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
        }


class PexelsStockProvider:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.pexels_api_key:
            raise ProviderFailure("pexels", "missing pexels api key")
        self.api_key = settings.pexels_api_key

    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        query = scene["fallback_queries"][0]
        response = httpx.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": self.api_key},
            params={"query": query, "orientation": "portrait", "locale": "pt-BR", "per_page": 1},
            timeout=httpx.Timeout(20.0, connect=8.0),
        )
        response.raise_for_status()
        photos = response.json().get("photos", [])
        if not photos:
            raise ProviderFailure("pexels", f"no portrait result for query {query}")
        photo = photos[0]
        image_response = httpx.get(photo["src"]["large2x"], timeout=httpx.Timeout(30.0, connect=8.0))
        image_response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(image_response.content)
        return {
            "provider": "pexels",
            "width": photo["width"],
            "height": photo["height"],
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
            "source_url": photo["url"],
            "attribution": f"Pexels photo by {photo['photographer']}",
            "license_note": "Exibir link proeminente para o Pexels.",
        }


class PixabayStockProvider:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.pixabay_api_key:
            raise ProviderFailure("pixabay", "missing pixabay api key")
        self.api_key = settings.pixabay_api_key

    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        query = scene["fallback_queries"][0]
        response = httpx.get(
            "https://pixabay.com/api/",
            params={"key": self.api_key, "q": query, "image_type": "photo", "orientation": "vertical", "per_page": 1},
            timeout=httpx.Timeout(20.0, connect=8.0),
        )
        response.raise_for_status()
        hits = response.json().get("hits", [])
        if not hits:
            raise ProviderFailure("pixabay", f"no vertical result for query {query}")
        hit = hits[0]
        image_response = httpx.get(hit["largeImageURL"], timeout=httpx.Timeout(30.0, connect=8.0))
        image_response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(image_response.content)
        return {
            "provider": "pixabay",
            "width": hit["imageWidth"],
            "height": hit["imageHeight"],
            "prompt_snapshot": scene["image_prompt"],
            "uri": output_path.resolve().as_uri(),
            "source_url": hit["pageURL"],
            "attribution": f"Pixabay photo by {hit['user']}",
            "license_note": "Fallback secundario via Pixabay.",
        }


class ResilientStockProvider:
    def __init__(self) -> None:
        self.providers: list[Any] = []
        settings = get_settings()
        if settings.pexels_api_key:
            self.providers.append(PexelsStockProvider())
        if settings.pixabay_api_key:
            self.providers.append(PixabayStockProvider())
        self.providers.append(MockImageProvider())

    def generate(self, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        last_error = "stock failed"
        for provider in self.providers:
            try:
                return provider.generate(scene, output_path)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
        raise ProviderFailure("stock", last_error)

    def generate_candidates(self, scene: dict[str, Any], output_dir: Path, max_candidates: int = 4) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen_queries: set[str] = set()
        queries = [query for query in scene.get("fallback_queries", []) if query and query not in seen_queries]
        for query in queries:
            seen_queries.add(query)
            query_scene = {**scene, "fallback_queries": [query]}
            for provider in self.providers:
                if isinstance(provider, MockImageProvider):
                    continue
                if len(candidates) >= max_candidates:
                    return candidates
                provider_name = provider.__class__.__name__.replace("StockProvider", "").replace("ImageProvider", "").lower()
                output_path = output_dir / f"stock-{len(candidates) + 1}-{provider_name}.png"
                try:
                    candidates.append(provider.generate(query_scene, output_path))
                except Exception:
                    continue
        if candidates:
            return candidates
        return []


class MockBackgroundMusicProvider:
    provider_name = "mock_music"

    def select_track(self, topic_plan: dict[str, Any], script: dict[str, Any], output_path: Path, target_duration_ms: int) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mood = self._infer_mood(topic_plan, script)
        query = self._query_hint(topic_plan, script, mood)
        self._write_mock_music(output_path, target_duration_ms, mood, query)
        return {
            "provider": self.provider_name,
            "query": query,
            "mood": mood,
            "source_url": None,
            "attribution": "Mock background bed generated locally for tests.",
            "license_note": "local_mock_background_music",
            "audio_uri": output_path.resolve().as_uri(),
            "duration_ms": target_duration_ms,
            "provider_metadata": {
                "fallback_used": True,
                "selection_mode": "generated",
            },
        }

    def _infer_mood(self, topic_plan: dict[str, Any], script: dict[str, Any]) -> str:
        surface = " ".join(
            [
                str(topic_plan.get("canonical_topic") or ""),
                str(topic_plan.get("angle") or ""),
                str(script.get("title") or ""),
                str(script.get("hook") or ""),
            ]
        ).lower()
        if any(term in surface for term in ["mist", "crime", "suspense", "mistério", "misterio", "segredo", "sombr"]):
            return "suspense"
        if any(term in surface for term in ["espaço", "espaco", "universo", "buraco negro", "tecnologia", "cafeína", "cafeina"]):
            return "technology"
        if any(term in surface for term in ["animal", "gato", "polvo", "oceano", "natureza", "flamingo"]):
            return "documentary"
        return "cinematic"

    def _query_hint(self, topic_plan: dict[str, Any], script: dict[str, Any], mood: str) -> str:
        topic = str(topic_plan.get("canonical_topic") or script.get("title") or "curiosidades").strip()
        return f"{topic} {mood}".strip()

    def _write_mock_music(self, output_path: Path, target_duration_ms: int, mood: str, seed_text: str) -> None:
        sample_rate = 24_000
        frame_count = max(1, round(sample_rate * target_duration_ms / 1000))
        base_freq = {
            "suspense": 92.5,
            "technology": 110.0,
            "documentary": 146.8,
            "cinematic": 130.8,
        }.get(mood, 123.5)
        phase_offset = (sum(ord(char) for char in seed_text) % 360) * math.pi / 180
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            frames = bytearray()
            for idx in range(frame_count):
                t = idx / sample_rate
                bed = (
                    0.55 * math.sin(2 * math.pi * base_freq * t + phase_offset)
                    + 0.28 * math.sin(2 * math.pi * (base_freq * 1.5) * t)
                    + 0.17 * math.sin(2 * math.pi * (base_freq * 2.0) * t + phase_offset / 2)
                )
                pulse = 0.65 + 0.35 * math.sin(2 * math.pi * 0.22 * t)
                fade_in = min(1.0, t / 1.5)
                fade_out = min(1.0, max(0.0, (frame_count / sample_rate - t) / 1.2))
                envelope = pulse * fade_in * fade_out
                sample = int(1400 * envelope * bed)
                frames.extend(sample.to_bytes(2, "little", signed=True))
            wav_file.writeframes(frames)


class MiniMaxBackgroundMusicProvider:
    provider_name = "minimax_music"

    def __init__(self) -> None:
        settings = get_settings()
        api_key = settings.resolved_minimax_music_api_key
        if not api_key:
            raise ProviderFailure("minimax_music", "missing minimax music api key")
        self.settings = settings
        self.api_key = api_key
        self.url = f"{settings.minimax_music_base_url.rstrip('/')}/music_generation"
        self.timeout = httpx.Timeout(settings.minimax_music_timeout_sec, connect=15.0)
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def select_track(self, topic_plan: dict[str, Any], script: dict[str, Any], output_path: Path, target_duration_ms: int) -> dict[str, Any]:
        mood = self._infer_mood(topic_plan, script)
        query = self._query_hint(topic_plan, script, mood)
        prompt = self._build_prompt(topic_plan, script, mood, target_duration_ms)
        payload = {
            "model": "music-2.6",
            "prompt": prompt,
            "lyrics": "",
            "is_instrumental": True,
            "lyrics_optimizer": False,
            "output_format": "url",
            "audio_setting": {
                "sample_rate": 44100,
                "bitrate": 256000,
                "format": "mp3",
            },
        }
        debug_payload = {
            "provider": self.provider_name,
            "url": self.url,
            "query": query,
            "mood": mood,
            "target_duration_ms": target_duration_ms,
            "timeout_sec": self.settings.minimax_music_timeout_sec,
            "request_payload": payload,
        }
        try:
            response = httpx.post(self.url, json=payload, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderFailure(
                "minimax_music",
                f"minimax music request timed out after {self.settings.minimax_music_timeout_sec}s",
                details={**debug_payload, "error_type": type(exc).__name__},
            ) from exc
        except httpx.HTTPStatusError as exc:
            response_text = exc.response.text[:500] if exc.response is not None else None
            raise ProviderFailure(
                "minimax_music",
                f"minimax music http {exc.response.status_code}: {response_text or exc}",
                details={
                    **debug_payload,
                    "error_type": type(exc).__name__,
                    "status_code": exc.response.status_code if exc.response is not None else None,
                    "response_text": response_text,
                },
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailure(
                "minimax_music",
                str(exc),
                details={**debug_payload, "error_type": type(exc).__name__},
            ) from exc
        data = body.get("data", {}) if isinstance(body.get("data"), dict) else {}
        audio_payload = str(data.get("audio") or "")
        if not audio_payload:
            raise ProviderFailure(
                "minimax_music",
                "missing audio payload from minimax music generation",
                details={
                    **debug_payload,
                    "response_trace_id": body.get("trace_id"),
                    "provider_status": data.get("status"),
                    "response_keys": sorted(body.keys()),
                },
            )
        source_url = audio_payload if self._looks_like_url(audio_payload) else None
        if source_url:
            self._download_audio_to_wav(source_url, output_path)
        else:
            self._decode_audio_to_wav(audio_payload, output_path)
        trim_metadata = self._trim_wav_to_target_duration(output_path, target_duration_ms)
        extra_info = body.get("extra_info", {}) if isinstance(body.get("extra_info"), dict) else {}
        return {
            "provider": self.provider_name,
            "query": query,
            "mood": mood,
            "source_url": source_url,
            "attribution": "AI-generated instrumental background music via MiniMax.",
            "license_note": "Generated with MiniMax music_generation API.",
            "audio_uri": output_path.resolve().as_uri(),
            "duration_ms": target_duration_ms,
            "provider_metadata": {
                "selection_mode": "generated",
                "model": "music-2.6",
                "instrumental": True,
                "output_format": "url",
                "prompt": prompt,
                "trace_id": body.get("trace_id"),
                "provider_status": data.get("status"),
                "requested_duration_ms": target_duration_ms,
                "returned_duration_ms": extra_info.get("music_duration"),
                "returned_sample_rate": extra_info.get("music_sample_rate"),
                "returned_channels": extra_info.get("music_channel"),
                "returned_bitrate": extra_info.get("bitrate"),
                **trim_metadata,
            },
        }

    def _infer_mood(self, topic_plan: dict[str, Any], script: dict[str, Any]) -> str:
        surface = " ".join(
            [
                str(topic_plan.get("canonical_topic") or ""),
                str(topic_plan.get("angle") or ""),
                str(script.get("title") or ""),
                str(script.get("hook") or ""),
            ]
        ).lower()
        if any(term in surface for term in ["mistério", "misterio", "segredo", "sombra", "buraco negro", "crime"]):
            return "suspense"
        if any(term in surface for term in ["cafeína", "cafeina", "neuro", "tecnologia", "universo", "espaço", "espaco"]):
            return "technology"
        if any(term in surface for term in ["polvo", "gato", "animal", "oceano", "natureza", "história", "historia"]):
            return "documentary"
        return "cinematic"

    def _query_hint(self, topic_plan: dict[str, Any], script: dict[str, Any], mood: str) -> str:
        topic = str(topic_plan.get("canonical_topic") or "").strip()
        angle = str(topic_plan.get("angle") or "").strip()
        title = str(script.get("title") or "").strip()
        parts = [topic, angle, title, mood]
        return " ".join(part for part in parts if part).strip()

    def _build_prompt(self, topic_plan: dict[str, Any], script: dict[str, Any], mood: str, target_duration_ms: int) -> str:
        duration_sec = max(8, round(target_duration_ms / 1000))
        topic = str(topic_plan.get("canonical_topic") or "").strip()
        angle = str(topic_plan.get("angle") or "").strip()
        title = str(script.get("title") or "").strip()
        hook = str(script.get("hook") or "").strip()
        mood_map = {
            "suspense": "tense documentary underscore, restrained, mysterious, no jump scares, no vocals",
            "technology": "modern science documentary underscore, pulsing but controlled, curious, precise, no vocals",
            "documentary": "curiosity-driven documentary underscore, warm and intelligent, organic percussion, no vocals",
            "cinematic": "cinematic short-form background score, engaging and polished, no vocals",
        }
        brief_context = ". ".join(part for part in [title, hook] if part)
        if len(brief_context) > 120:
            brief_context = brief_context[:117].rstrip(" ,.;:") + "..."
        prompt = (
            f"Instrumental only. {mood_map.get(mood, mood_map['cinematic'])}. "
            f"Designed as background music for a vertical educational short about {topic or 'a curiosity topic'}. "
            f"Angle: {angle or 'counterintuitive reveal'}. "
            f"Target duration exactly {duration_sec} seconds, matching the narration length as closely as possible. "
            "End naturally at that runtime, no long tail, intro, or outro. "
            "Fast hook in the first 2 seconds, steady mid-section, clean ending for narration ducking. "
            "No vocals, spoken words, lyrics, or stingers that overpower voice-over. "
            "Avoid pop-song structure; this should feel like underscore, not a standalone single. "
            f"Video context: {brief_context or topic or 'scientific curiosity short'}"
        )
        return " ".join(prompt.split())

    def _looks_like_url(self, audio_payload: str) -> bool:
        return audio_payload.startswith("http://") or audio_payload.startswith("https://")

    def _download_audio_to_wav(self, audio_url: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(".minimax.mp3")
        try:
            response = httpx.get(audio_url, timeout=self.timeout, follow_redirects=True)
            response.raise_for_status()
            temp_path.write_bytes(response.content)
            self._convert_audio_file_to_wav(temp_path, output_path)
        except httpx.TimeoutException as exc:
            raise ProviderFailure(
                "minimax_music",
                f"minimax music download timed out after {self.settings.minimax_music_timeout_sec}s",
            ) from exc
        finally:
            temp_path.unlink(missing_ok=True)

    def _decode_audio_to_wav(self, audio_hex: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(".minimax.mp3")
        try:
            temp_path.write_bytes(binascii.unhexlify(audio_hex))
            self._convert_audio_file_to_wav(temp_path, output_path)
        except (binascii.Error, ValueError) as exc:
            raise ProviderFailure("minimax_music", f"invalid audio payload from minimax: {exc}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

    def _convert_audio_file_to_wav(self, input_path: Path, output_path: Path) -> None:
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-i",
                str(input_path),
                "-ar",
                "24000",
                "-ac",
                "1",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def _trim_wav_to_target_duration(self, output_path: Path, target_duration_ms: int) -> dict[str, Any]:
        target_sec = max(target_duration_ms / 1000, 1.0)
        temp_path = output_path.with_suffix(".trimmed.wav")
        try:
            subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-y",
                    "-i",
                    str(output_path),
                    "-t",
                    f"{target_sec:.3f}",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    str(temp_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            temp_path.replace(output_path)
        except subprocess.CalledProcessError as exc:
            raise ProviderFailure(
                "minimax_music",
                "failed to trim minimax music to target duration",
                details={
                    "target_duration_ms": target_duration_ms,
                    "stderr": exc.stderr[-1000:] if exc.stderr else None,
                },
            ) from exc
        finally:
            temp_path.unlink(missing_ok=True)
        return {
            "source_trimmed_to_ms": round(target_sec * 1000),
            "source_trim_applied": True,
        }


class ResilientMusicProvider:
    def __init__(self) -> None:
        settings = get_settings()
        self.providers: list[Any] = []
        if not settings.use_mock_providers and settings.resolved_minimax_music_api_key:
            self.providers.append(MiniMaxBackgroundMusicProvider())
        if not settings.strict_minimax_validation:
            self.providers.append(MockBackgroundMusicProvider())

    def select_track(self, topic_plan: dict[str, Any], script: dict[str, Any], output_path: Path, target_duration_ms: int) -> dict[str, Any]:
        last_error = "music selection failed"
        last_details: dict[str, Any] = {}
        for provider in self.providers:
            try:
                return provider.select_track(topic_plan, script, output_path, target_duration_ms)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if isinstance(exc, ProviderFailure):
                    last_details = dict(exc.details or {})
        if get_settings().strict_minimax_validation:
            raise ProviderFailure(
                "background_music",
                f"strict minimax validation requires minimax music success: {last_error}",
                details=last_details,
            )
        raise ProviderFailure("background_music", last_error, details=last_details)


class LocalSpeechFallbackProvider:
    voice = "pt-BR-FranciscaNeural"

    def synthesize(self, text: str, audio_path: Path, srt_path: Path) -> dict[str, Any]:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        mode = self._write_speech_audio(text, audio_path)
        duration_ms = self._measure_audio_ms(audio_path)
        cues = self._build_cues(text, duration_ms)
        srt_path.write_text(self._render_srt(cues), encoding="utf-8")
        self._normalize_speech_envelope(audio_path, srt_path)
        self._apply_final_loudness_normalization(audio_path)
        duration_ms = self._measure_audio_ms(audio_path)
        provider = "espeak_ng" if mode == "espeak_ng" else "synthetic_wav"
        return {
            "provider": provider,
            "voice": self.voice,
            "audio_uri": audio_path.resolve().as_uri(),
            "raw_subtitles_uri": srt_path.resolve().as_uri(),
            "duration_ms": duration_ms,
            "sample_rate_hz": 24000,
            "channels": 1,
            "provider_metadata": {
                "mode": mode,
                "cue_count": len(cues),
                "fallback_used": True,
                "loudness_normalized": True,
                "loudness_target_lufs": -16.0,
                "true_peak_limit_db": -1.5,
            },
        }

    def _build_cues(self, text: str, duration_ms: int) -> list[dict[str, Any]]:
        words = text.split()
        chunks: list[str] = []
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            if len(candidate) > 42 or len(current) >= 7:
                chunks.append(" ".join(current))
                current = [word]
                continue
            current.append(word)
        if current:
            chunks.append(" ".join(current))
        if not chunks:
            chunks = [text]
        cue_duration = max(800, min(2500, duration_ms // len(chunks)))
        cues: list[dict[str, Any]] = []
        start = 0
        for idx, chunk in enumerate(chunks, start=1):
            end = duration_ms if idx == len(chunks) else min(duration_ms, start + cue_duration)
            cues.append({"idx": idx, "start_ms": start, "end_ms": end, "text": wrap_caption(chunk)})
            start = end
        return cues

    def _write_speech_audio(self, text: str, path: Path) -> str:
        if not shutil.which("espeak-ng"):
            self._write_synthetic_audio(text, path)
            return "synthetic_wav"
        raw_path = path.with_name(path.stem + ".raw.wav")
        try:
            subprocess.run(
                [
                    "espeak-ng",
                    "-v",
                    "pt-br",
                    "-s",
                    "160",
                    "-p",
                    "40",
                    "-w",
                    str(raw_path),
                    text,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-y",
                    "-i",
                    str(raw_path),
                    "-af",
                    "highpass=f=120,lowpass=f=4300,loudnorm=I=-16:LRA=11:TP=-1.5",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return "espeak_ng"
        except Exception:
            self._write_synthetic_audio(text, path)
            return "synthetic_wav"
        finally:
            raw_path.unlink(missing_ok=True)

    def _write_synthetic_audio(self, text: str, path: Path) -> None:
        sample_rate = 24_000
        duration_sec = max(25.0, min(45.0, len(word_tokens(text)) / 2.0))
        frame_count = int(sample_rate * duration_sec)
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            frames = bytearray()
            for idx in range(frame_count):
                t = idx / sample_rate
                envelope = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(2 * math.pi * 1.8 * t))
                sample = int(2600 * envelope * math.sin(2 * math.pi * 185 * t))
                frames.extend(sample.to_bytes(2, "little", signed=True))
            wav_file.writeframes(frames)

    def _measure_audio_ms(self, path: Path) -> int:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            sample_rate = wav_file.getframerate()
        return int(frames / sample_rate * 1000)

    def _normalize_speech_envelope(self, audio_path: Path, srt_path: Path, target_rms_db: float = -20.0) -> None:
        cues = parse_srt(srt_path.read_text(encoding="utf-8")) if srt_path.exists() else []
        if not cues:
            return
        with wave.open(str(audio_path), "rb") as source:
            params = source.getparams()
            frame_rate = source.getframerate()
            sample_width = source.getsampwidth()
            channels = source.getnchannels()
            audio = bytearray(source.readframes(source.getnframes()))
        if sample_width != 2:
            return
        full_scale = float(2 ** (8 * sample_width - 1))
        target_rms = full_scale * (10 ** (target_rms_db / 20))
        peak_ceiling = full_scale * (10 ** (-3.0 / 20))
        frame_size = sample_width * channels
        for cue in cues:
            start_frame = max(0, round(int(cue["start_ms"]) * frame_rate / 1000))
            end_frame = max(start_frame + 1, round(int(cue["end_ms"]) * frame_rate / 1000))
            start = start_frame * frame_size
            end = min(len(audio), end_frame * frame_size)
            segment = bytes(audio[start:end])
            if not segment:
                continue
            rms = audioop.rms(segment, sample_width)
            peak = audioop.max(segment, sample_width)
            if rms <= 0 or peak <= 0:
                continue
            gain = target_rms / rms
            gain = min(gain, peak_ceiling / peak)
            gain = max(0.45, min(gain, 4.0))
            audio[start:end] = audioop.mul(segment, sample_width, gain)
        temp_path = audio_path.with_suffix(".leveled.wav")
        with wave.open(str(temp_path), "wb") as target:
            target.setparams(params)
            target.writeframes(bytes(audio))
        temp_path.replace(audio_path)

    def _apply_final_loudness_normalization(self, audio_path: Path) -> None:
        temp_path = audio_path.with_suffix(".loudnorm.wav")
        try:
            subprocess.run(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-y",
                    "-i",
                    str(audio_path),
                    "-af",
                    "highpass=f=80,lowpass=f=12000,loudnorm=I=-16:LRA=11:TP=-1.5",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    str(temp_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            temp_path.replace(audio_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _render_srt(self, cues: list[dict[str, Any]]) -> str:
        blocks = []
        for cue in cues:
            start = self._ms_to_srt(cue["start_ms"])
            end = self._ms_to_srt(cue["end_ms"])
            blocks.append(f"{cue['idx']}\n{start} --> {end}\n{cue['text']}")
        return "\n\n".join(blocks) + "\n"

    def _ms_to_srt(self, value: int) -> str:
        hours, rem = divmod(value, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        seconds, millis = divmod(rem, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


class EdgeTTSProvider(LocalSpeechFallbackProvider):
    rate = "+12%"

    async def _run(self, text: str, audio_path: Path, srt_path: Path) -> dict[str, Any]:
        import edge_tts

        communicate = edge_tts.Communicate(
            text=text,
            voice=self.voice,
            rate=self.rate,
            connect_timeout=20,
            receive_timeout=120,
        )
        submaker = edge_tts.SubMaker()
        temp_audio = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        temp_audio_path = Path(temp_audio.name)
        temp_audio.close()
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_audio_path, "wb") as audio_file:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_file.write(chunk["data"])
                elif chunk["type"] in {"WordBoundary", "SentenceBoundary"}:
                    submaker.feed(chunk)
        self._normalize_edge_audio(temp_audio_path, audio_path)
        temp_audio_path.unlink(missing_ok=True)
        srt_path.write_text(submaker.get_srt(), encoding="utf-8")
        duration_ms = self._measure_audio_ms(audio_path)
        return {
            "provider": "edge_tts",
            "voice": self.voice,
            "audio_uri": audio_path.resolve().as_uri(),
            "raw_subtitles_uri": srt_path.resolve().as_uri(),
            "duration_ms": duration_ms,
            "sample_rate_hz": 24000,
            "channels": 1,
            "provider_metadata": {
                "mode": "edge",
                "rate": self.rate,
                "fallback_used": False,
                "loudness_normalized": True,
                "loudness_target_lufs": -16.0,
                "true_peak_limit_db": -1.5,
                "envelope_normalized": False,
                "denoise_applied": True,
            },
        }

    def synthesize(self, text: str, audio_path: Path, srt_path: Path) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                return asyncio.run(self._run(text, audio_path, srt_path))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 3:
                    time.sleep(1.5 * attempt)
                    continue
        fallback = super().synthesize(text, audio_path, srt_path)
        fallback["provider_metadata"]["fallback_reason"] = f"edge_tts failed after 3 attempts: {last_error}"
        return fallback

    def _normalize_edge_audio(self, source_path: Path, output_path: Path) -> None:
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-i",
                str(source_path),
                "-af",
                "highpass=f=70,lowpass=f=9500,afftdn=nf=-25,loudnorm=I=-16:LRA=11:TP=-1.5",
                "-ar",
                "24000",
                "-ac",
                "1",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


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
