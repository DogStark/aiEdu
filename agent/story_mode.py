import boto3
import json
import os


FALLBACK_STORIES = {
    ("cat", "bat", "hat"): "The cat wore a hat and played with a bat. The hat fell off when the bat flew by. The cat laughed and put the hat back on!",
    ("dog", "log", "frog"): "A dog sat on a log near a pond. A frog jumped onto the log too. The dog and the frog became best friends!",
}


def generate_story(words: list[str], student_name: str = "the student", use_bedrock: bool = True) -> str:
    """Generate a short 3-sentence story using the given words."""
    if use_bedrock:
        try:
            return _bedrock_story(words, student_name)
        except Exception:
            pass
    return _fallback_story(words, student_name)


def _bedrock_story(words: list[str], student_name: str) -> str:
    client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
    word_list = ", ".join(words)
    prompt = (
        f"Write a fun, 3-sentence story for a young child named {student_name}. "
        f"The story MUST use ALL of these words: {word_list}. "
        "Use simple vocabulary suitable for ages 5-10. Make it exciting and positive. "
        "Bold each of the target words in the story."
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 150,
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


def _fallback_story(words: list[str], student_name: str) -> str:
    key = tuple(sorted(words))
    if key in FALLBACK_STORIES:
        return FALLBACK_STORIES[key]
    word_str = " and ".join(words)
    return (
        f"{student_name} learned the words: {word_str}. "
        f"One day, {student_name} used all these words in a sentence and everyone was amazed! "
        "Keep learning new words every day — you're doing great! 🌟"
    )
