"""Listing tools must accurately distinguish reads from paid, saved answers."""

from types import SimpleNamespace

from researchlib.web.mcp import handle_message


def test_tool_listing_does_not_describe_saved_model_answers_as_readonly():
    result = handle_message(SimpleNamespace(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in result["result"]["tools"]}
    ask = tools["ask_library"]["annotations"]
    assert not ask["readOnlyHint"] and not ask["idempotentHint"]
    assert ask["openWorldHint"] and not ask["destructiveHint"]
    for name, tool in tools.items():
        if name != "ask_library":
            assert tool["annotations"]["readOnlyHint"]
            assert tool["annotations"]["idempotentHint"]
