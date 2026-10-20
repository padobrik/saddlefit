from saddlefit.schema_registry import SCHEMA_MODELS


def test_every_persisted_model_carries_schema_version() -> None:
    for name, model in SCHEMA_MODELS.items():
        assert "schema_version" in model.model_fields, name
