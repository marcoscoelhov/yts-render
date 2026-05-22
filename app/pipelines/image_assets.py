from __future__ import annotations

import queue
import re
import threading
from pathlib import Path
from typing import Any

from PIL import Image

from app.pipelines.common import RecoverableStepError
from app.utils import path_from_uri


NO_TEXT_IMAGE_CONSTRAINT = (
    "clean vertical cinematic scientific image, natural objects only, no readable text anywhere, "
    "no letters, no words, no numbers, no symbols, no logo, no watermark, no captions, "
    "no subtitles, no title card, no poster, no signs, no labels, no UI, no infographic, "
    "no typography, no diagrams with labels, no text printed on objects, no text on packages, "
    "no text on cups, no text on screens, no text on charts, no readable brand marks"
)

ENGLISH_SUBJECT_ALIASES = {
    "polvo": "octopus",
    "polvos": "octopuses",
    "buraco negro": "black hole",
    "buracos negros": "black holes",
    "vulcao": "volcano",
    "vulcoes": "volcanoes",
    "vulcão": "volcano",
    "vulcões": "volcanoes",
    "gato": "cat",
    "gatos": "cats",
    "felino": "cat",
    "felinos": "cats",
    "cafe": "coffee",
    "café": "coffee",
    "cafeina": "caffeine",
    "cafeína": "caffeine",
    "cafeina e foco": "caffeine and focus",
    "café e foco": "coffee and focus",
    "torre de pisa": "Leaning Tower of Pisa",
    "torre inclinada de pisa": "Leaning Tower of Pisa",
    "por que a torre de pisa não cai?": "Leaning Tower of Pisa",
    "por que a torre de pisa nao cai?": "Leaning Tower of Pisa",
}

SCENE_VISUAL_HINTS = [
    (("torre", "pisa", "séculos"), "the Leaning Tower of Pisa in Piazza dei Miracoli at golden hour, visibly tilted but stable, documentary realism"),
    (("torre", "pisa", "seculos"), "the Leaning Tower of Pisa in Piazza dei Miracoli at golden hour, visibly tilted but stable, documentary realism"),
    (("solo", "argiloso"), "cutaway view of the Leaning Tower of Pisa foundation resting on soft clay soil layers, unlabeled scientific visualization"),
    (("solo", "mole"), "cutaway view of the Leaning Tower of Pisa foundation resting on soft clay soil layers, unlabeled scientific visualization"),
    (("fundação",), "close vertical cutaway of a shallow medieval tower foundation settling into soft ground, documentary engineering realism"),
    (("fundacao",), "close vertical cutaway of a shallow medieval tower foundation settling into soft ground, documentary engineering realism"),
    (("centro", "massa"), "unlabeled visual metaphor of the Leaning Tower of Pisa balancing with its mass still over the base, no diagrams or text"),
    (("inclinação", "reduz"), "engineers stabilizing the base of the Leaning Tower of Pisa with careful soil extraction, documentary realism"),
    (("inclinacao", "reduz"), "engineers stabilizing the base of the Leaning Tower of Pisa with careful soil extraction, documentary realism"),
    (("cafeina", "foco"), "caffeine molecules near alert neurons in warm morning light, a plain unbranded coffee cup nearby"),
    (("cafeína", "foco"), "caffeine molecules near alert neurons in warm morning light, a plain unbranded coffee cup nearby"),
    (("cafe", "foco"), "plain unbranded coffee cup beside a focused morning workspace, subtle neural energy glow"),
    (("café", "foco"), "plain unbranded coffee cup beside a focused morning workspace, subtle neural energy glow"),
    (("adenosina",), "caffeine molecules blocking adenosine receptors on neurons, cinematic scientific visualization"),
    (("receptores",), "caffeine molecules fitting into neural receptors, cinematic scientific visualization"),
    (("sonolencia",), "sleep pressure fading from a human silhouette after caffeine reaches the brain, morning light"),
    (("sonolência",), "sleep pressure fading from a human silhouette after caffeine reaches the brain, morning light"),
    (("alerta",), "alert brain activity represented by glowing neural pathways beside plain coffee steam"),
    (("manhã",), "soft morning kitchen light with plain unbranded coffee steam and a person becoming alert in silhouette"),
    (("manha",), "soft morning kitchen light with plain unbranded coffee steam and a person becoming alert in silhouette"),
    (("gatos", "veem", "mundo diferente"), "cat face close-up with reflective eyes perceiving an altered night world"),
    (("terceiro", "párpado"), "macro close-up of a cat eye showing the translucent third eyelid protecting the eye"),
    (("terceiro", "parpado"), "macro close-up of a cat eye showing the translucent third eyelid protecting the eye"),
    (("orelha", "180"), "cat ears rotating independently toward subtle sound waves in a quiet room"),
    (("visão noturna",), "cat moving through a dim night scene with bright reflective eyes and low light visibility"),
    (("visao noturna",), "cat moving through a dim night scene with bright reflective eyes and low light visibility"),
    (("memória episódica",), "cat remembering a hidden toy location in a realistic home environment"),
    (("memoria episodica",), "cat remembering a hidden toy location in a realistic home environment"),
    (("cabeça", "180"), "cat turning its head sharply to monitor a distant threat, natural posture"),
    (("cabeca", "180"), "cat turning its head sharply to monitor a distant threat, natural posture"),
    (("corações", "sangue azul"), "octopus anatomy close-up showing three subtle hearts and blue copper-rich blood vessels"),
    (("coracoes", "sangue azul"), "octopus anatomy close-up showing three subtle hearts and blue copper-rich blood vessels"),
    (("hemocianina",), "blue oxygen-carrying blood flowing through octopus anatomy"),
    (("dna",), "octopus adapting underwater beside clean molecular DNA strands made of light"),
    (("células nervosas",), "octopus arms exploring rocks independently with subtle neural glow inside the tentacles"),
    (("celulas nervosas",), "octopus arms exploring rocks independently with subtle neural glow inside the tentacles"),
    (("tentáculo", "cortado"), "detached octopus arm moving reflexively on the seabed, natural biology, non-graphic"),
    (("tentaculo", "cortado"), "detached octopus arm moving reflexively on the seabed, natural biology, non-graphic"),
    (("cor", "textura", "predadores"), "octopus rapidly changing skin color and texture while camouflaging from a predator"),
]


class ImageAssetDomain:
    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline

    def generate_primary_asset(self, job_id: str, scene: dict[str, Any], output_path: Path) -> dict[str, Any]:
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        scene_for_provider = {**scene, "job_id": job_id}

        def run() -> None:
            try:
                result_queue.put(("ok", self.pipeline.providers.image.generate(scene_for_provider, output_path)), block=False)
            except BaseException as exc:  # noqa: BLE001
                result_queue.put(("error", exc), block=False)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=self.pipeline.settings.asset_generation_timeout_sec)
        if thread.is_alive():
            raise RecoverableStepError(
                f"asset primary generation timed out after {self.pipeline.settings.asset_generation_timeout_sec}s"
            )
        status, payload = result_queue.get_nowait()
        if status == "error":
            raise payload
        return payload

    def normalize_asset_uri_extension(self, asset: dict[str, Any]) -> dict[str, Any]:
        uri = str(asset.get("uri") or "")
        if not uri.startswith("file://"):
            return asset
        path = path_from_uri(uri)
        if not path.exists():
            return asset
        try:
            with Image.open(path) as image:
                fmt = (image.format or "").upper()
        except Exception:  # noqa: BLE001
            return asset
        suffix_by_format = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
        expected_suffix = suffix_by_format.get(fmt)
        if not expected_suffix or path.suffix.lower() == expected_suffix:
            return asset
        target = path.with_suffix(expected_suffix)
        counter = 2
        while target.exists() and target.resolve() != path.resolve():
            target = path.with_name(f"{path.stem}-{counter}{expected_suffix}")
            counter += 1
        path.rename(target)
        updated = dict(asset)
        updated["uri"] = target.resolve().as_uri()
        updated["file_format"] = fmt.lower()
        updated["extension_normalized"] = True
        return updated

    def score_asset(self, scene: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        return self.pipeline.providers.semantic.score(scene, asset)

    def asset_scores_pass(self, scores: dict[str, Any]) -> bool:
        return (
            scores["semantic_match"] >= self.pipeline.settings.asset_semantic_threshold
            and scores["total_score"] >= self.pipeline.settings.asset_total_threshold
            and scores.get("text_or_watermark_penalty", 0.0) <= 0.15
            and scores.get("artifact_penalty", 0.0) <= 0.30
        )

    def image_prompt_variants(self, scene: dict[str, Any], regeneration_round: int = 1) -> list[dict[str, Any]]:
        topic_text = str(scene.get("topic_hint") or scene.get("primary_subject") or "")
        primary_subject = str(scene.get("primary_subject") or scene.get("topic_hint") or "")
        base_prompt = self.semantic_english_image_prompt(scene, topic_text, primary_subject)
        english_subject = self.english_subject_hint(topic_text, primary_subject)
        narration = str(scene.get("narration_text") or "").strip()
        scene_hint = self.english_scene_visual_hint(scene, english_subject)
        variant_prompts = [
            base_prompt,
            self.with_no_text_image_constraints(
                f"vertical documentary close shot of {english_subject}, {scene_hint}, "
                f"visually illustrate this exact narration beat: {narration}, scientific documentary realism, "
                "natural lighting, one clear subject, no symbolic poster, no irrelevant props"
            ),
            self.with_no_text_image_constraints(
                f"realistic vertical YouTube Shorts visual, {english_subject} as the unmistakable central subject, "
                f"{narration}, cinematic science documentary frame, concrete factual detail, clean relevant background"
            ),
        ]
        variants: list[dict[str, Any]] = []
        seen: set[str] = set()
        for prompt in variant_prompts:
            normalized = " ".join(prompt.split())
            if regeneration_round > 1:
                normalized = (
                    f"{normalized}, alternate composition, new camera framing, different background geometry, "
                    "keep the same factual subject and no text constraints"
                )
            if normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            variants.append({**scene, "image_prompt": normalized, "regeneration_round": regeneration_round})
        return variants

    def semantic_english_image_prompt(self, scene: dict[str, Any], topic_text: str, primary_subject: str) -> str:
        prompt = str(scene.get("image_prompt", "")).replace("_", " ")
        english_subject = self.english_subject_hint(topic_text, primary_subject)
        scene_hint = self.english_scene_visual_hint(scene, english_subject)
        semantic_directive = self.semantic_scene_directive(scene, scene_hint)
        if self.should_rebuild_image_prompt(prompt):
            visual_intent = str(scene.get("visual_intent") or "scientific documentary scene").replace("_", " ")
            prompt = scene_hint or f"vertical cinematic scientific image of {english_subject}, {visual_intent}"
        else:
            prompt = self.replace_subject_aliases(prompt)
        if semantic_directive.lower() not in prompt.lower():
            prompt = f"{prompt}, {semantic_directive}".strip(", ")
        if scene_hint and scene_hint.lower() not in prompt.lower():
            prompt = f"{scene_hint}, {prompt}".strip(", ")
        elif english_subject and english_subject.lower() not in prompt.lower():
            prompt = f"{prompt}, central subject: {english_subject}".strip(", ")
        if "no movie poster" not in prompt.lower():
            prompt += ", scientific visualization, documentary realism, no movie poster, no typography, no stock-photo generic scene"
        return self.with_no_text_image_constraints(prompt)

    def english_subject_hint(self, topic_text: str, primary_subject: str) -> str:
        for value in [primary_subject, topic_text]:
            normalized = " ".join(str(value).replace("_", " ").lower().split())
            if normalized in ENGLISH_SUBJECT_ALIASES:
                return ENGLISH_SUBJECT_ALIASES[normalized]
            normalized_ascii = (
                normalized.replace("á", "a")
                .replace("à", "a")
                .replace("ã", "a")
                .replace("â", "a")
                .replace("é", "e")
                .replace("ê", "e")
                .replace("í", "i")
                .replace("ó", "o")
                .replace("õ", "o")
                .replace("ô", "o")
                .replace("ú", "u")
                .replace("ç", "c")
            )
            if "polvo" in normalized_ascii:
                return "octopus"
            if "gato" in normalized_ascii or "felino" in normalized_ascii:
                return "cat"
            if "buraco" in normalized_ascii and "negro" in normalized_ascii:
                return "black hole"
            if "vulcao" in normalized_ascii:
                return "volcano"
            if "cafeina" in normalized_ascii and "foco" in normalized_ascii:
                return "caffeine and focus"
            if "cafe" in normalized_ascii and "foco" in normalized_ascii:
                return "coffee and focus"
            if "cafeina" in normalized_ascii:
                return "caffeine"
            if "cafe" in normalized_ascii:
                return "coffee"
        return primary_subject or topic_text or "the subject"

    def english_scene_visual_hint(self, scene: dict[str, Any], english_subject: str) -> str:
        narration = str(scene.get("narration_text") or "").lower()
        normalized = (
            narration.replace("á", "a")
            .replace("à", "a")
            .replace("ã", "a")
            .replace("â", "a")
            .replace("é", "e")
            .replace("ê", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("õ", "o")
            .replace("ô", "o")
            .replace("ú", "u")
            .replace("ç", "c")
        )
        for terms, hint in SCENE_VISUAL_HINTS:
            if all(term in narration or term in normalized for term in terms):
                return hint
        return f"vertical cinematic scientific image of {english_subject}"

    def semantic_scene_directive(self, scene: dict[str, Any], scene_hint: str) -> str:
        narration = str(scene.get("narration_text") or "").strip()
        visual_intent = str(scene.get("visual_intent") or "documentary evidence").replace("_", " ")
        if narration:
            return (
                "depict the specific narration beat with concrete cause-and-effect visual evidence, "
                f"not a generic symbolic background, visual focus: {scene_hint}, scene role: {visual_intent}"
            )
        return "depict the specific narration beat with concrete cause-and-effect visual evidence, not a generic symbolic background"

    def should_rebuild_image_prompt(self, prompt: str) -> bool:
        prompt_lower = prompt.lower()
        return any(
            phrase in prompt_lower
            for phrase in [
                "ilustracao",
                "mostrando",
                "foco no fenomeno",
                "sem texto",
                "sem watermark",
                "sem capa",
                "sem tipografia",
                "focused on the described phenomenon",
                "showing subject closeup",
                "showing subject in context",
                "showing process or mechanism",
                "showing comparison",
                "showing scale reference",
                "showing historical evocation",
            ]
        )

    def replace_subject_aliases(self, prompt: str) -> str:
        updated = prompt
        for source, target in sorted(ENGLISH_SUBJECT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
            updated = re.sub(rf"\b{re.escape(source)}\b", target, updated, flags=re.IGNORECASE)
        return updated

    def with_no_text_image_constraints(self, prompt: str) -> str:
        prompt = " ".join(prompt.replace("_", " ").split())
        prompt_lower = prompt.lower()
        extra_constraints = [
            "no letters, no words, no numbers, no symbols",
            "no logo, no watermark, no captions, no subtitles",
            "every object must be completely blank and unbranded",
            "plain containers only, blank cups only, blank packages only",
            "no text on cups, no text on packages, no text on screens",
            "no labels or lettering on any object surface",
            "avoid screens, documents, books, newspapers, signs, dashboards, graphs, labels, and branded packaging",
            "no floating spheres, no random packages, no irrelevant lab props, no generic sci-fi objects",
            "the main subject must be unmistakable and relevant to the narration beat",
        ]
        if "no readable text anywhere" not in prompt_lower:
            prompt = f"{prompt}, {NO_TEXT_IMAGE_CONSTRAINT}".strip(", ")
            prompt_lower = prompt.lower()
        for constraint in extra_constraints:
            if constraint.lower() not in prompt_lower:
                prompt = f"{prompt}, {constraint}"
                prompt_lower = prompt.lower()
        return prompt
