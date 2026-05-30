"""Provider-specific adapters.

Each adapter is a thin shim that knows how to (a) extract schemas from the
provider's native tool format and (b) round-trip a structured repair prompt
through the provider's chat API. Both are <100 LOC.

Adapters are lazy-imported — `from cruxial.adapters.openai import ...` does
not require the openai SDK unless you import it.
"""
