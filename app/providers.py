from __future__ import annotations

import asyncio
import audioop
import base64
import colorsys
import concurrent.futures
import json
import math
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Protocol

import anthropic
import httpx
import imageio_ffmpeg
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFilter

from app.config import get_settings
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
    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider


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
        hook = f"O detalhe menos intuitivo de {subject} muda toda a leitura."
        body = [
            f"{subject.capitalize()} parece vago so ate aparecer o efeito ao redor.",
            f"O ponto central entra em {angle} e muda a escala do tema.",
            f"Em vez do objeto isolado, o entorno entrega a pista principal.",
            f"Esse recorte deixa {subject} mais concreto para quem assiste.",
            f"Assim cada cena sustenta a ideia sem inventar elemento aleatorio.",
        ]
        ending = f"Por isso {subject} deixa de ser so um nome e vira um fenomeno legivel."
        narration_parts = [hook, *body, ending]
        full_narration = " ".join(narration_parts)
        token_count = len(tokenize(full_narration))
        estimated_duration_sec = round(max(28.5, min(41.5, len(word_tokens(full_narration)) / 2.55)), 2)
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
        }
        return {
            "title": topic_plan["title_candidates"][0],
            "hook": hook,
            "body_beats": body,
            "ending": ending,
            "cta": None,
            "full_narration": full_narration,
            "estimated_duration_sec": estimated_duration_sec,
            "key_facts": [
                f"{subject.capitalize()} reage ao ambiente antes da maioria notar.",
                f"O tema fica mais claro quando se observa o contexto e a funcao.",
                f"O comportamento do sujeito economiza energia enquanto reduz risco.",
            ],
            "token_count": token_count,
            "language": "pt-BR",
            "qa_metrics": qa_metrics,
            "prompt_version": "mock-curiosidades-v1",
        }

    def repair_script(self, script: dict[str, Any], gate_reasons: list[str], topic_plan: dict[str, Any]) -> dict[str, Any]:
        repaired = dict(script)
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

    def __init__(self) -> None:
        settings = get_settings()
        api_key = settings.resolved_minimax_text_api_key
        if not api_key:
            raise ProviderFailure("minimax_text", "missing minimax text api key")
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
        payload["quality_metrics"] = {**payload.get("quality_metrics", {}), "source_provider": "minimax"}
        return payload

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        prompt = f"""
Escreva um roteiro viral de curiosidades em pt-BR.
Entrada JSON: {json.dumps(topic_plan, ensure_ascii=False)}

Retorne JSON estrito com:
title, hook, body_beats, ending, cta, full_narration, estimated_duration_sec, key_facts, token_count, language, qa_metrics, prompt_version

Regras:
- 25 a 45 segundos
- primeira frase com no maximo 12 palavras
- media por frase <= 14
- use estrutura agressiva de retenção: hook de choque, loop aberto, escalada de fatos, payoff atrasado e fechamento memoravel
- cada frase deve criar uma pergunta mental ou tensão para a frase seguinte
- não entregue a explicação completa no primeiro beat; plante o mistério e pague no último terço
- transforme fatos em consequência visual/mental, evitando tom de Wikipedia
- todos os campos textuais do JSON devem estar em portugues do Brasil (pt-BR)
- nao use chines, ingles, espanhol ou outro idioma em title, hook, body_beats, ending, cta, full_narration, key_facts ou valores textuais de qa_metrics
- excecoes permitidas: nomes proprios, nomes cientificos, siglas, marcas, titulos de fontes e URLs
- key_facts deve ser uma lista em pt-BR, sem trechos em outros alfabetos ou idiomas
- title deve ser otimizado para SEO e copywriting viral, com promessa especifica e palavra-chave cedo quando natural
- hook deve abrir com choque, contraste ou tensão imediata, sem introducao generica
- proibido começar hook ou full_narration com "você sabia", "voce sabia", "já imaginou", "ja imaginou", "nesse vídeo", "nesse video" ou fórmulas genéricas equivalentes
- comece direto por contraste, consequência, conflito ou fato específico
- cada body_beat deve entregar um fato concreto que sustente a promessa do titulo e aumente a curiosidade
- fatos acima de viralidade: não invente números, nacionalidades, planos, materiais, causas técnicas ou soluções de engenharia se eles não estiverem na Entrada JSON ou forem conhecimento extremamente consolidado
- se houver incerteza factual, use formulação conservadora e geral em vez de precisão falsa; prefira “engenheiros reduziram a inclinação removendo solo sob a base” a números específicos não verificados
- evite frases absolutas/enganosas como “está garantida”, “a física prova”, “domina a física”, “desafia a física” ou “a inclinação sustenta”
- key_facts deve listar apenas fatos que o roteiro realmente usa, sem exagero e sem detalhe técnico duvidoso
- ending deve fechar o loop mental do hook e recontextualizar o tema com uma frase memoravel
- se cta_style for "none", cta deve ser null e full_narration não deve incluir pedido de inscrição, like, comentário, compartilhamento ou ativar sininho
- mantenha o tom selecionado na Entrada JSON, sem exagerar sensacionalismo
- se a Entrada JSON indicar titulo completo do usuario, preserve a promessa central e refine a formulacao
- se hub_notes pedir um formato de saida diferente, ignore esse formato e mantenha exatamente o JSON estrito solicitado aqui
- sem instruções de camera
- QA deve incluir hook_score, clarity_score, information_density_score, repetition_score, ending_strength_score, estimated_duration_sec, avg_words_per_sentence, max_words_single_sentence, words_per_second, script_gate_pass
"""
        payload = self._json_completion(prompt)
        payload["qa_metrics"] = {**payload.get("qa_metrics", {}), "source_provider": "minimax"}
        return payload

    def repair_script(self, script: dict[str, Any], gate_reasons: list[str], topic_plan: dict[str, Any]) -> dict[str, Any]:
        prompt = f"""
Corrija este roteiro de Short para passar no gate de qualidade do app.
Roteiro atual JSON: {json.dumps(script, ensure_ascii=False)}
Contexto da pauta JSON: {json.dumps(topic_plan, ensure_ascii=False)}
Motivos de reprovação: {json.dumps(gate_reasons, ensure_ascii=False)}

Retorne JSON estrito com os mesmos campos:
title, hook, body_beats, ending, cta, full_narration, estimated_duration_sec, key_facts, token_count, language, qa_metrics, prompt_version

Regras obrigatórias:
- todos os campos textuais devem estar em portugues do Brasil (pt-BR)
- remova qualquer palavra, frase ou expressão em ingles, espanhol, chines ou outro idioma
- remova qualquer SSML, HTML, XML, tags, entidades ou markup
- corrija palavras coladas e erros como "ummini", "independiente", "right"
- mantenha duração estimada entre 25 e 45 segundos
- primeira frase com no máximo 12 palavras
- média por frase <= 14 e frase máxima <= 20 palavras
- preserve a promessa central e os fatos úteis, mas reescreva o necessário
- se o hook ou full_narration começar com "você sabia", "voce sabia", "já imaginou", "ja imaginou", "nesse vídeo" ou equivalente, reescreva para começar direto por contraste, consequência, conflito ou fato específico
- aumente retenção sem inventar fatos: hook mais agressivo, loop aberto, escalada de curiosidade, payoff no ultimo terço e final memoravel
- fatos acima de viralidade: remova números, nacionalidades, planos, materiais, causas técnicas ou soluções de engenharia que não estejam bem sustentados pelo contexto
- evite frases absolutas/enganosas como “está garantida”, “a física prova”, “domina a física”, “desafia a física” ou “a inclinação sustenta”
- se os motivos incluírem factual_risk_requires_conservative_rewrite, reescreva TODA afirmação de risco factual: números precisos, datas, porcentagens, medidas, causalidade técnica, claims médicos/biológicos, engenharia e frases absolutas
- para afirmações de alto risco sem fonte explícita, use linguagem conservadora: “pode”, “tende a”, “em geral”, “uma das explicações”, “cerca de”, ou remova o detalhe específico
- se um detalhe técnico parecer duvidoso, substitua por formulação conservadora e verificável
- qa_metrics deve incluir hook_score, clarity_score, information_density_score, repetition_score, ending_strength_score, estimated_duration_sec, avg_words_per_sentence, max_words_single_sentence, words_per_second, script_gate_pass
Sem markdown.
"""
        payload = self._json_completion(prompt)
        payload["qa_metrics"] = {
            **payload.get("qa_metrics", {}),
            "source_provider": "minimax",
            "repair_provider": "minimax",
            "repair_reasons": gate_reasons,
        }
        return payload

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
            raise ProviderFailure("minimax_text", "scene planner returned non-list json")
        return payload

    def _json_completion(self, prompt: str) -> Any:
        try:
            response = self.client.chat.completions.create(
                model="MiniMax-M2.7",
                messages=[
                    {"role": "system", "content": "Return valid JSON only. No markdown fences."},
                    {"role": "user", "content": prompt},
                ],
                timeout=self.timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailure("minimax_text", str(exc)) from exc
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            raise ProviderFailure("minimax_text", "empty text response")
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
            raise ProviderFailure("minimax_text", f"invalid json: {raw[:300]}") from exc

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


class ResilientCreativeProvider:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.registry = LLMProviderRegistry()
        self.primary = self.registry.primary_provider()
        self.fallback = self.registry.fallback_provider()

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
            try:
                return self.primary.plan_topic(seed_theme, attempt, history, requested_angle, tone=tone, notes=notes)
            except ProviderFailure as exc:
                payload = self.fallback.plan_topic(seed_theme, attempt, history, requested_angle, tone=tone, notes=notes)
                payload["quality_metrics"]["fallback_reason"] = str(exc)
                payload["quality_metrics"]["fallback_used"] = True
                return payload
        return self.fallback.plan_topic(seed_theme, attempt, history, requested_angle, tone=tone, notes=notes)

    def generate_script(self, topic_plan: dict[str, Any]) -> dict[str, Any]:
        if self.primary:
            try:
                return self.primary.generate_script(topic_plan)
            except ProviderFailure as exc:
                payload = self.fallback.generate_script(topic_plan)
                payload["qa_metrics"]["fallback_reason"] = str(exc)
                payload["qa_metrics"]["fallback_used"] = True
                return payload
        return self.fallback.generate_script(topic_plan)

    def plan_scenes(self, script: dict[str, Any], target_scene_count: int) -> list[dict[str, Any]]:
        if self.primary:
            executor: concurrent.futures.ThreadPoolExecutor | None = None
            try:
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                future = executor.submit(self.primary.plan_scenes, script, target_scene_count)
                return future.result(timeout=self.settings.minimax_scene_plan_timeout_sec)
            except concurrent.futures.TimeoutError:
                scenes = self.fallback.plan_scenes(script, target_scene_count)
                for scene in scenes:
                    scene["provider_fallback_reason"] = (
                        f"minimax_text scene planner timed out after {self.settings.minimax_scene_plan_timeout_sec}s"
                    )
                return scenes
            except ProviderFailure as exc:
                scenes = self.fallback.plan_scenes(script, target_scene_count)
                for scene in scenes:
                    scene["provider_fallback_reason"] = str(exc)
                return scenes
            finally:
                if executor is not None:
                    executor.shutdown(wait=False, cancel_futures=True)
        return self.fallback.plan_scenes(script, target_scene_count)

    def repair_script(self, script: dict[str, Any], gate_reasons: list[str], topic_plan: dict[str, Any]) -> dict[str, Any]:
        if self.primary:
            try:
                return self.primary.repair_script(script, gate_reasons, topic_plan)
            except ProviderFailure as exc:
                if self.settings.llm_enable_fallback and self.fallback:
                    payload = self.fallback.repair_script(script, [*gate_reasons, str(exc)], topic_plan)
                    payload.setdefault("qa_metrics", {})["fallback_used"] = True
                    payload["qa_metrics"]["fallback_reason"] = str(exc)
                    return payload
                raise
        return self.fallback.repair_script(script, gate_reasons, topic_plan)

    def repair_script_with_fallback(self, script: dict[str, Any], gate_reasons: list[str], topic_plan: dict[str, Any]) -> dict[str, Any] | None:
        if not self.settings.llm_enable_fallback or not self.fallback:
            return None
        payload = self.fallback.repair_script(script, gate_reasons, topic_plan)
        payload.setdefault("qa_metrics", {})["fallback_used"] = True
        payload["qa_metrics"]["fallback_stage"] = "script_quality_gate"
        return payload


class LLMProviderRegistry:
    def __init__(self) -> None:
        self.settings = get_settings()

    def primary_provider(self) -> LLMProvider | None:
        if self.settings.use_mock_providers:
            return MockCreativeProvider()
        return self._build_provider(self.settings.llm_primary_provider, required=True)

    def fallback_provider(self) -> LLMProvider:
        provider = self._build_provider(self.settings.llm_fallback_provider, required=False)
        return provider or MockCreativeProvider()

    def _build_provider(self, name: str, required: bool) -> LLMProvider | None:
        normalized = (name or "").strip().lower()
        if normalized in {"", "none", "disabled"}:
            if required:
                raise ProviderFailure("llm_registry", "primary llm provider is disabled")
            return None
        if normalized in {"mock", "local"}:
            return MockCreativeProvider()
        if normalized in {"minimax", "minimax_2_7", "minimax-m2.7"}:
            return MinimaxCreativeProvider()
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
        self.semantic = SemanticVerifier()
        self.local_image = LocalSemanticImageProvider()
