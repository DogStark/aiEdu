import json
import logging
import random
import boto3
from typing import Optional
from botocore.exceptions import BotoCoreError, ClientError

from agent.log_config import get_logger

logger = get_logger(__name__)

THEME_HINTS = {
    "animals": "It's a living creature 🐾",
    "food": "You can eat or drink it 🍎",
    "nature": "You can find it outside in nature 🌿",
    "home": "You'd find this inside a house 🏠",
    "colors": "It describes a color 🎨",
    "actions": "It's something you can do 🏃",
    "transport": "It helps you get from place to place 🚗",
    "body": "It's part of your body 🧍",
    "clothing": "You wear it 👕",
    "emotions": "It describes a feeling 😊",
    "descriptive": "It describes something 📝",
    "objects": "It's a thing you can touch 📦",
    "shapes": "It's a shape or form 🔷",
    "time": "It's related to time ⏰",
    "question": "It's a question word ❓",
}


def get_hint(word: str, theme: str, attempt_number: int, use_bedrock: bool = False) -> str:
    if attempt_number == 1:
        hint = _theme_hint(theme)
        if use_bedrock:
            hint = _bedrock_hint(word, theme) or hint
    elif attempt_number == 2:
        hint = f"It starts with the letter '{word[0].upper()}'"
    else:
        hint = f"It starts with '{word[0].upper()}' and ends with '{word[-1].upper()}'"
    return hint


ENCOURAGEMENT_SUCCESS = ["Amazing!", "Fantastic!", "Brilliant!", "Wow, great job!"]
ENCOURAGEMENT_STRUGGLE = [
    "Keep trying, you're doing great!",
    "Almost there, don't give up!",
    "That's a tricky one — let's try again!",
]
ENCOURAGEMENT_FRUSTRATED = "That one's tricky! Let's try an easier word. 💪"


def get_encouragement(success: bool, consecutive_failures: int) -> str:
    if consecutive_failures >= 3:
        return ENCOURAGEMENT_FRUSTRATED
    if success:
        return random.choice(ENCOURAGEMENT_SUCCESS)
    return random.choice(ENCOURAGEMENT_STRUGGLE)


def _theme_hint(theme: str) -> str:
    return THEME_HINTS.get(theme, f"It belongs to the '{theme}' category")


def _bedrock_hint(word: str, theme: str) -> Optional[str]:
    try:
        client = boto3.client("bedrock-runtime")
        prompt = (
            f"Give a single child-friendly hint for the word '{word}' (theme: {theme}). "
            "One sentence only. Do not say the word."
        )
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 60,
            "messages": [{"role": "user", "content": prompt}]
        })
        response = client.invoke_model(modelId="anthropic.claude-3-haiku-20240307-v1:0", body=body)
        result = json.loads(response["body"].read())
        return result["content"][0]["text"].strip()
    except (BotoCoreError, ClientError) as exc:
        logger.warning(
            "Bedrock hint unavailable for word '%s': %s",
            word, exc,
            extra={"source_module": __name__, "source_function": "_bedrock_hint", "word": word},
        )
        return None
    except Exception as exc:
        logger.error(
            "Bedrock hint generation failed unexpectedly for word '%s': %s",
            word, exc,
            extra={"source_module": __name__, "source_function": "_bedrock_hint", "word": word},
        )
        return None
