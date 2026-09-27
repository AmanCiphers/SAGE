"""Tool-call reassembly from a streamed reply.

Extracted from LLMClient.stream_events so the tricky part can be tested without
a network call. Tool-call arguments arrive fragmented across chunks, one chunk
per call index, and naive handling keeps only the final fragment, which yields
invalid JSON.
"""


def assemble_tool_calls(deltas):
    """Fold a list of streamed deltas into complete tool_calls.

    Returns a list of ``{"id", "type", "function": {"name", "arguments"}}`` in
    the order the calls first appeared.
    """
    calls = {}
    order = []

    for delta in deltas:
        for call in delta.tool_calls or []:
            index = call.index if call.index is not None else 0

            if index not in calls:
                calls[index] = {"id": "", "name": "", "arguments": ""}
                order.append(index)

            if call.id:
                calls[index]["id"] = call.id

            if call.function:
                if call.function.name:
                    calls[index]["name"] += call.function.name
                if call.function.arguments:
                    calls[index]["arguments"] += call.function.arguments

    return [
        {
            "id": calls[index]["id"],
            "type": "function",
            "function": {
                "name": calls[index]["name"],
                "arguments": calls[index]["arguments"],
            },
        }
        for index in order
    ]
