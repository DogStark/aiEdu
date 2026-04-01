import boto3
import json
import os

# Hint templates as fallback when Bedrock is unavailable
HINT_TEMPLATES = {
    "animals": "This is a living creature. It has {letters} letters. 🐾",
    "food": "You can eat this! It has {letters} letters. 🍽️",
    "nature": "You can find this outside in nature. It has {letters} letters. 🌿",
    "home": "You might find this in your house. It has {letters} letters. 🏠",
    "colors": "This is something you can see with your eyes. It has {letters} letters. 🎨",
    "actions": "This is something you can do with your body. It has {letters} letters. 🏃",
    "transport": "People use this to travel from place to place. It has {letters} letters. 🚗",
    "body": "This is a part of your body. It has {letters} letters. 🧍",
    "clothing": "You wear this on your body. It has {letters} letters. 👕",
    "emotions": "This describes how you feel. It has {letters} letters. 😊",
    "descriptive": "This word describes something. It has {letters} letters. ✨",
    "objects": "This is a thing you can touch or use. It has {letters} letters. 📦",
    "shapes": "This is a shape or form. It has {letters} letters. 🔷",
    "time": "This is related to time or the day. It has {letters} letters. ⏰",
    "question": "This is a word used to ask questions. It has {letters} letters. ❓"
}

FIRST_LETTER_HINT = "The first letter is '{letter}'. Can you figure out the rest?"


def get_hint(word: str, theme: str, attempt_number: int, use_bedrock: bool = True) -> str:
    """
    Returns a progressive hint based on attempt number.
    attempt 1 -> theme-based hint
    attempt 2 -> first letter hint
    attempt 3 -> reveal first + last letter
    """
    if attempt_number == 1:
        return _theme_hint(word, theme, use_bedrock)
    elif attempt_number == 2:
        return FIRST_LETTER_HINT.format(letter=word[0].upper())
    else:
        return f"The word starts with '{word[0].upper()}' and ends with '{word[-1].upper()}'. It has {len(word)} letters."


def _theme_hint(word: str, theme: str, use_bedrock: bool) -> str:
    if use_bedrock:
        try:
            return _bedrock_hint(word, theme)
        except Exception:
            pass
    template = HINT_TEMPLATES.get(theme, "This word has {letters} letters. Think carefully! 🤔")
    return template.format(letters=len(word))


def _bedrock_hint(word: str, theme: str) -> str:
    client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
    prompt = (
        f"You are a friendly teacher helping a young child (age 5-10) guess the word '{word}'. "
        f"The word belongs to the theme: {theme}. "
        "Give ONE short, fun, child-friendly hint WITHOUT saying the word. "
        "Use simple language and add one relevant emoji. Max 15 words."
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 60,
        "messages": [{"role": "user", "content": prompt}]
    })
    response = client.invoke_model(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        body=body,
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response["body"].read())
    return result["content"][0]["text"].strip()


def get_encouragement(success: bool, consecutive_failures: int) -> str:
    if success:
        messages = [
            "Amazing job! You got it! 🌟",
            "Fantastic! You're a spelling star! ⭐",
            "Brilliant! Keep it up! 🎉",
            "Wow, you nailed it! 🏆"
        ]
        import random
        return random.choice(messages)
    else:
        if consecutive_failures >= 3:
            return "That's a tricky one! Let's try an easier word first. You've got this! 💪"
        return "Not quite! Take another look at the hint. You can do it! 🤗"
