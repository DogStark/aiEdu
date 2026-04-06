import json
import boto3
from typing import Optional
from botocore.exceptions import BotoCoreError, ClientError


def generate_story(words: list[str], student_name: str = "", use_bedrock: bool = False) -> str:
    if use_bedrock:
        story = _bedrock_story(words, student_name)
        if story:
            return story
    return _template_story(words, student_name)


def _template_story(words: list[str], student_name: str = "") -> str:
    w = words + ["friend", "day", "place"]  # pad if fewer than 3 words
    name = student_name or "A little learner"
    return (
        f"{name} went on a big adventure and found a {w[0]}. "
        f"Along the way, they also discovered a {w[1]} and smiled with joy. "
        f"At the end of the day, they went home happy, thinking about the {w[2]}."
    )


def _bedrock_story(words: list, student_name: str = "") -> Optional[str]:
    try:
        client = boto3.client("bedrock-runtime")
        word_list = ", ".join(words)
        prompt = (
            f"Write a fun 3-sentence story for a young child using these words: {word_list}. "
            "Use simple language. Include all the words naturally."
        )
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 150,
            "messages": [{"role": "user", "content": prompt}]
        })
        response = client.invoke_model(modelId="anthropic.claude-3-haiku-20240307-v1:0", body=body)
        result = json.loads(response["body"].read())
        return result["content"][0]["text"].strip()
    except (BotoCoreError, ClientError, Exception):
        return None
