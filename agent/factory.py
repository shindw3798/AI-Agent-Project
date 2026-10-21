import os
from .structured_llm import StructuredQuality


def build_llms():
    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend == "openai":
        from .openai_llm import OpenAIFastLLM as Fast, OpenAIQualityLLM as Quality
        fast = os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini-2024-07-18")
        quality = os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o-2024-08-06")
    elif backend == "ollama":
        from .ollama_llm import OllamaFastLLM as Fast, OllamaQualityLLM as Quality
        fast = os.environ.get("OLLAMA_FAST_MODEL", "qwen3:0.6b")
        quality = os.environ.get("OLLAMA_QUALITY_MODEL", "qwen3:8b")
    elif backend == "fake":
        from .fake_llm import FastLLM as Fast, QualityLLM as Quality
        fast, quality = "stub", "stub"
    else:
        raise ValueError("UNKNOWN_LLM_BACKEND")
    return backend, Fast(fast, .00015), StructuredQuality(Quality(quality, .005))
