"""Correlate runtime events and retain terminal summaries for partial failures."""

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace

from src.core.agent_runtime import (
    AgentEvent,
    AgentEventKind,
    AgentResponse,
    ToolCall,
    ToolResult,
)
from src.core.run_options import RunOptions, scoped_run_options
from src.infra.tracer import TraceLogger
from src.infra.usage import usage_from_spans


async def observe_events(
    events: AsyncIterator[AgentEvent], session_id: str, tracer: TraceLogger,
    options: RunOptions | None,
) -> AsyncIterator[AgentEvent]:
    execution_id = uuid.uuid4().hex
    trace = tracer.current
    owns_trace = trace is None
    if trace is None:
        trace = tracer.start_trace(execution_id, 'agent_runtime')
    started = time.perf_counter()
    root_span = uuid.uuid4().hex
    correlation = {**tracer.correlation, 'execution_id': execution_id}
    correlation.pop('iteration', None)
    sequence = 0
    iteration = 0
    first_token_ms = None
    calls: dict[str, ToolCall] = {}
    terminal = False
    termination_reason = 'agent_cancelled'
    try:
        while True:
            token = tracer.bind(trace)
            try:
                with tracer.context(**{**correlation, 'iteration': iteration}), tracer.parent_span(root_span), scoped_run_options(options):
                    try:
                        event = await anext(events)
                    except StopAsyncIteration:
                        break
            finally:
                tracer.reset(token)
            iteration = max(iteration, event.iteration)
            elapsed = (time.perf_counter() - started) * 1000
            if event.kind is AgentEventKind.CHUNK and first_token_ms is None:
                first_token_ms = elapsed
            if event.tool_call:
                calls[event.tool_call.call_id] = event.tool_call
            if event.tool_result and event.tool_result.call_id in calls:
                result = event.tool_result
                calls[result.call_id] = replace(
                    calls[result.call_id], success=result.success,
                    error_code=result.error_code, latency_ms=result.latency_ms,
                    status=result.execution_status, attempts=result.attempts,
                    blocked=result.execution_status == 'rejected',
                )
            if event.kind in {AgentEventKind.DONE, AgentEventKind.ERROR}:
                terminal = True
                response = event.response or AgentResponse(
                    session_id=session_id, answer='', error_code=event.error_code,
                    error=event.message,
                )
                termination_reason = response.termination_reason or event.error_code or 'completed'
                for call_id, call in list(calls.items()):
                    if call.status != 'proposed':
                        continue
                    outcomes = [span for span in trace['spans'] if span['name'] == 'tool_execution_outcome'
                                and span.get('attributes', {}).get('call_id') == call_id]
                    attempts = max((span['attributes'].get('attempts', 0) for span in outcomes), default=0)
                    calls[call_id] = replace(call, success=False, status='aborted',
                                            error_code=termination_reason, attempts=attempts)
                    sequence += 1
                    yield AgentEvent(
                        AgentEventKind.TOOL_RESULT, iteration=iteration,
                        tool_result=ToolResult(
                            success=False, call_id=call_id, tool_name=call.name,
                            error_code=termination_reason, execution_status='aborted',
                            attempts=attempts,
                        ),
                        telemetry={
                            **correlation, 'trace_id': trace['trace_id'],
                            'sequence': sequence, 'timestamp_unix_ms': time.time() * 1000,
                            'elapsed_ms': elapsed,
                        },
                    )
                spans = [span for span in trace['spans'] if span.get('execution_id') == execution_id]
                usage = usage_from_spans(spans)
                usage['dropped_spans'] = trace.get('dropped_spans', 0)
                usage['reported'] = usage['reported'] and not usage['dropped_spans']
                response = replace(
                    response, tool_calls=tuple(calls.values()), iterations=iteration,
                    total_latency_ms=elapsed, trace_id=trace['trace_id'],
                    execution_id=execution_id, termination_reason=termination_reason,
                    first_answer_token_ms=first_token_ms, usage=usage,
                )
                event = replace(event, response=response)
            sequence += 1
            yield replace(event, telemetry={
                **correlation, 'trace_id': trace['trace_id'],
                'sequence': sequence, 'timestamp_unix_ms': time.time() * 1000,
                'elapsed_ms': round(elapsed, 3),
            })
    finally:
        token = tracer.bind(trace)
        try:
            with tracer.context(**correlation), tracer.parent_span(root_span), scoped_run_options(options):
                await events.aclose()
                if not terminal:
                    tracer.record_outcome('cancelled', 'agent_cancelled', trace)
                aborted = [call.call_id for call in calls.values() if call.status == 'proposed']
                tracer.add_span(
                    trace, 'agent_execution_summary', (time.perf_counter() - started) * 1000,
                    span_id=root_span, started_at=started, parent_span_id='',
                    status='ok' if terminal and not trace['error_code'] else 'terminated',
                    termination_reason=termination_reason, tool_proposals=len(calls),
                    aborted_call_ids=aborted,
                    tool_proposals_without_result=len(aborted),
                    first_answer_token_ms=first_token_ms,
                )
            if owns_trace:
                tracer.finish_trace(trace)
        finally:
            tracer.reset(token)
