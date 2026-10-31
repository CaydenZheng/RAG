"""Opt-in local MCP examples; no credentials, external requests or writes.

Configure this script as a stdio MCP server named workspace_demo. Member
visibility still requires the operator allowlist and every call uses Registry.
It is never registered automatically by the application or the Pages demo.
"""

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    ElicitResult,
    InputRequiredResult,
)
from mcp.types.version import is_version_at_least
from pydantic import BaseModel, Field, ValidationError

server = MCPServer("workspace-interaction-examples")


class ExampleForm(BaseModel):
    topic: str = Field(title="示例主题", min_length=1, max_length=40)


def _example_input(
    ctx: Context,
    key: str,
    params: ElicitRequestFormParams | ElicitRequestURLParams,
) -> dict[str, str] | InputRequiredResult:
    """Modern calls carry input in the result, not a server back-channel.

    The fixed state identifies this stateless, side-effect-free example only;
    it confers no permission and never substitutes for Registry approval.
    """
    response = (ctx.input_responses or {}).get(key)
    if ctx.request_state != key or response is None:
        return InputRequiredResult(
            input_requests={key: ElicitRequest(params=params)}, request_state=key,
        )
    if not isinstance(response, ElicitResult):
        raise ToolError("Invalid synthetic interaction response")
    if params.mode == "form" and response.action == "accept":
        try:
            ExampleForm.model_validate(response.content)
        except ValidationError:
            raise ToolError("Invalid synthetic form response") from None
    return {"status": response.action}


@server.tool()
def confirm_preview() -> dict[str, str]:
    """Controlled approval example; returns a fixed preview without writes."""
    return {"status": "preview_confirmed"}


@server.tool()
async def request_form(ctx: Context) -> dict[str, str] | InputRequiredResult:
    """Controlled form Elicitation; submit synthetic text only."""
    if ctx.protocol_version and is_version_at_least(ctx.protocol_version, "2026-07-28"):
        return _example_input(ctx, "example-form", ElicitRequestFormParams(
            message="本地受控表单示例，请只填写合成文字，不要提交个人信息。",
            requested_schema=ExampleForm.model_json_schema(),
        ))
    outcome = await ctx.elicit("本地受控表单示例，请只填写合成文字，不要提交个人信息。", ExampleForm)
    return {"status": outcome.action}


@server.tool()
async def request_url(ctx: Context) -> dict[str, str] | InputRequiredResult:
    """Controlled URL Elicitation; no real OAuth or external verification."""
    if ctx.protocol_version and is_version_at_least(ctx.protocol_version, "2026-07-28"):
        return _example_input(ctx, "example-url", ElicitRequestURLParams(
            message="本地受控URL示例；这不是真实OAuth。",
            url="https://example.org/?workspace_demo=synthetic",
            elicitation_id="workspace-demo-only",
        ))
    outcome = await ctx.elicit_url(
        "本地受控URL示例；这不是真实OAuth。",
        "https://example.org/?workspace_demo=synthetic", "workspace-demo-only",
    )
    return {"status": outcome.action}


if __name__ == "__main__":
    server.run("stdio")
