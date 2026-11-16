"""Provider-reported usage with explicit completeness and cache accounting."""

from typing import Any


def usage_from_spans(spans: list[dict[str, Any]]) -> dict[str, Any]:
    model_spans = [span['attributes'] for span in spans if 'model' in span.get('attributes', {})]
    missing = sum(not span.get('usage_reported') and not span.get('cache_hit') for span in model_spans)
    prompt = sum(int(span.get('prompt_tokens') or 0) for span in model_spans)
    completion = sum(int(span.get('completion_tokens') or 0) for span in model_spans)
    attempts = [span for span in spans if span.get('name') == 'model_http_attempt']
    return {
        'prompt_tokens': prompt,
        'completion_tokens': completion,
        'total_tokens': prompt + completion,
        'reported': bool(model_spans) and missing == 0,
        'missing_usage_calls': missing,
        'model_calls': len(model_spans),
        'cache_hits': sum(bool(span.get('cache_hit')) for span in model_spans),
        'provider_request_attempts': len(attempts) if attempts else None,
        'provider_retries': sum((span['attributes'].get('attempt_number') or 0) > 1 for span in attempts)
        if attempts else None,
    }
