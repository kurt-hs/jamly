"""The deliberately small set of supported separation presets."""
from __future__ import annotations

DEFAULT_MODEL = "htdemucs"
MODELS = {
    DEFAULT_MODEL: {
        "label": "Balanced",
        "description": "Good quality with faster processing. Recommended for most tracks.",
    },
    "htdemucs_ft": {
        "label": "Higher quality",
        "description": "Fine-tuned Demucs; may improve separation, but takes about 4x longer "
                       "and downloads more model weights on first use. Improvements vary by track.",
    },
}


def validate_model(value: str) -> str:
    if not isinstance(value, str) or value not in MODELS:
        raise ValueError("Choose Balanced (htdemucs) or Higher quality (htdemucs_ft)")
    return value


def model_choices() -> list[dict[str, str]]:
    return [{"id": name, **details} for name, details in MODELS.items()]
