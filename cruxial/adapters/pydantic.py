"""Pydantic adapter.

Define a tool once as a Pydantic model and get cruxial validation for free — no
hand-written JSON Schema. Pydantic is near-default in the Python stack (FastAPI,
instructor, PydanticAI), so this is the lowest-friction way to put a cruxial
guard in front of tools you've already modeled.

    from pydantic import BaseModel
    from cruxial.adapters.pydantic import guard_models

    class SendEmail(BaseModel):
        to: str
        subject: str
        body: str

    cruxial = guard_models([SendEmail], executors={"SendEmail": send_email})
    cruxial.execute("SendEmail", {"to": "a@b.com", "subject": "Hi", "body": "yo"})

It also emits the provider tool-call definition from the same model, so a single
source of truth drives both the LLM tool schema and the runtime guard:

    from cruxial.adapters.pydantic import tool_schema
    tools = [tool_schema(SendEmail)]                        # OpenAI function format
    tools = [tool_schema(SendEmail, provider="anthropic")]  # Anthropic format

Pydantic v2 lands nested models / enums in ``$defs`` with ``$ref``; cruxial's
validator resolves those internally (no network hop), so nested schemas validate
end to end — including the nested constraints.

Optional dependency: ``pip install cruxial[pydantic]`` (Pydantic v2). The import
is lazy — code that doesn't use this module doesn't pay for it, and importing
cruxial itself never requires Pydantic.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Union

from cruxial.core import Cruxial, GuardConfig, guard
from cruxial.telemetry import Sink


__all__ = [
    "extract_schemas",
    "guard_models",
    "register_models",
    "tool_schema",
]

# A single model, an iterable of models, or a {name: model} mapping.
ModelsArg = Union[type, Iterable[type], Mapping[str, type]]


def _require_pydantic():
    """Import Pydantic lazily with a clear, actionable error if it's missing."""
    try:
        import pydantic
    except ImportError as e:  # pragma: no cover - exercised only without pydantic
        raise ImportError(
            "The Pydantic adapter requires Pydantic v2. "
            "Install it with `pip install cruxial[pydantic]`."
        ) from e
    major = int(str(pydantic.VERSION).split(".")[0])
    if major < 2:
        raise ImportError(
            f"The Pydantic adapter requires Pydantic v2 (found {pydantic.VERSION}). "
            "Upgrade with `pip install 'pydantic>=2'`."
        )
    return pydantic


def _is_model(obj: Any) -> bool:
    pydantic = _require_pydantic()
    return isinstance(obj, type) and issubclass(obj, pydantic.BaseModel)


def _json_schema(model: type) -> dict[str, Any]:
    return model.model_json_schema()


def extract_schemas(models: ModelsArg) -> dict[str, dict[str, Any]]:
    """Return ``{tool_name: json_schema}`` from Pydantic model(s).

    Accepts:
      * a single ``BaseModel`` subclass  -> keyed by the model's ``__name__``
      * an iterable of subclasses        -> keyed by each ``__name__``
      * a ``{name: Model}`` mapping       -> keyed by your chosen names (e.g.
        snake_case ``{"send_email": SendEmail}``)
    """
    pydantic = _require_pydantic()
    if isinstance(models, Mapping):
        items = list(models.items())
    elif _is_model(models):
        items = [(models.__name__, models)]
    elif isinstance(models, pydantic.BaseModel):
        raise TypeError(
            "extract_schemas expects model class(es), not an instance "
            "— pass SendEmail, not SendEmail(...)."
        )
    else:
        try:
            sequence = list(models)
        except TypeError as e:
            raise TypeError(
                "extract_schemas expects a Pydantic model class, an iterable of "
                "classes, or a {name: Model} mapping."
            ) from e
        items = []
        seen: set[str] = set()
        for m in sequence:
            if not _is_model(m):
                raise TypeError(f"{m!r} is not a Pydantic BaseModel subclass.")
            if m.__name__ in seen:
                raise ValueError(
                    f"Duplicate tool name {m.__name__!r}: two models map to the "
                    "same name. Pass a {name: Model} mapping to disambiguate, e.g. "
                    "{'send_v1': SendV1, 'send_v2': SendV2}."
                )
            seen.add(m.__name__)
            items.append((m.__name__, m))

    out: dict[str, dict[str, Any]] = {}
    for name, model in items:
        if not _is_model(model):
            raise TypeError(
                f"{name!r} -> {model!r} is not a Pydantic BaseModel subclass."
            )
        out[name] = _json_schema(model)
    return out


def guard_models(
    models: ModelsArg,
    executors: Mapping[str, Callable[..., Any]] | None = None,
    *,
    config: GuardConfig | None = None,
    sink: Sink | None = None,
) -> Cruxial:
    """Build a cruxial guard directly from Pydantic models.

    Equivalent to ``guard(extract_schemas(models), executors, ...)``. Tool names
    default to each model's ``__name__``; pass a ``{name: Model}`` mapping to
    choose your own (e.g. snake_case to match your executor keys).
    """
    return guard(extract_schemas(models), executors, config=config, sink=sink)


def register_models(cruxial: Cruxial, models: ModelsArg, **kwargs: Any) -> int:
    """Register Pydantic models into an existing guard. Returns the count."""
    return cruxial.register_schemas(extract_schemas(models), **kwargs)


def tool_schema(
    model: type,
    *,
    name: str | None = None,
    description: str | None = None,
    provider: str = "openai",
) -> dict[str, Any]:
    """Emit a provider tool-call definition from a Pydantic model.

    One source of truth: the same model drives the LLM tool schema *and* the
    cruxial guard, so they can never drift. ``provider`` is ``"openai"``
    (default, function-calling format) or ``"anthropic"``. Description falls back
    to the model's schema ``description`` / class docstring.
    """
    _require_pydantic()
    if not _is_model(model):
        raise TypeError("tool_schema expects a Pydantic BaseModel subclass.")
    schema = _json_schema(model)
    tool_name = name or model.__name__
    desc = description or schema.get("description") or (model.__doc__ or "").strip()

    if provider == "anthropic":
        return {"name": tool_name, "description": desc, "input_schema": schema}
    if provider == "openai":
        return {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": desc,
                "parameters": schema,
            },
        }
    raise ValueError(f"Unknown provider {provider!r}; use 'openai' or 'anthropic'.")
