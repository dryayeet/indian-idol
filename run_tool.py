"""Poke at the Spotify MCP tools by hand.

    python run_tool.py                 interactive: lists the tools, asks which one,
                                       asks for each field, prints the result
    python run_tool.py get_lyrics track="Motion Sickness" artist="Phoebe Bridgers"
                                       one-shot, same thing without the prompts

Values are parsed as JSON when possible (numbers, lists), otherwise kept as text.
Calls the tools in-process, so it exercises the tools but not the stdio transport.
"""

import asyncio
import json
import sys

from spotify_mcp import app


def _coerce(raw: str, spec: dict):
    kind = spec.get("type")
    if kind == "string":
        return raw
    if kind == "array":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return [part.strip() for part in raw.split(",") if part.strip()]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parse(pairs: list[str], schema: dict) -> dict:
    props = schema.get("properties") or {}
    args = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"expected key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        args[key] = _coerce(value, props.get(key, {}))
    return args


def _signature(tool) -> str:
    props = (tool.input_schema.get("properties") or {}).items()
    required = set(tool.input_schema.get("required") or [])
    fields = [k if k in required else f"{k}={v.get('default')!r}" for k, v in props]
    return f"{tool.name}({', '.join(fields)})"


def _ask(schema: dict) -> dict:
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    args = {}
    for key, spec in props.items():
        kind = spec.get("type", "any")
        tail = "required" if key in required else f"default {spec.get('default')!r}"
        while True:
            raw = input(f"  {key} ({kind}, {tail}): ").strip()
            if raw:
                args[key] = _coerce(raw, spec)
                break
            if key not in required:
                break
            print("    needed, no default for this one")
    return args


def _show(result) -> None:
    print()
    for block in result.content:
        print(getattr(block, "text", block))


async def main(argv: list[str]) -> None:
    tools = await app.list_tools()
    by_name = {t.name: t for t in tools}

    if argv:
        if argv[0] not in by_name:
            raise SystemExit(f"no such tool: {argv[0]}")
        tool = by_name[argv[0]]
        _show(await app.call_tool(tool.name, _parse(argv[1:], tool.input_schema)))
        return

    print("Tools on the Spotify MCP server:\n")
    for i, tool in enumerate(tools, 1):
        print(f"  {i}. {_signature(tool)}")
        print(f"     {(tool.description or '').strip()}\n")

    choice = input("which one? (name or number): ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(tools):
        tool = tools[int(choice) - 1]
    elif choice in by_name:
        tool = by_name[choice]
    else:
        raise SystemExit(f"no such tool: {choice}")

    print(f"\n{tool.name} fields, blank keeps the default:")
    _show(await app.call_tool(tool.name, _ask(tool.input_schema)))


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        schema = {
            "properties": {
                "limit": {"type": "integer"},
                "name": {"type": "string"},
                "track_uris": {"type": "array"},
            }
        }
        assert _parse(["limit=5", "name=Track 2", "track_uris=a,b"], schema) == {
            "limit": 5,
            "name": "Track 2",  # stays text, not the int 2
            "track_uris": ["a", "b"],
        }
        assert _coerce('["x","y"]', {"type": "array"}) == ["x", "y"]
        assert _coerce("0.2", {"type": "number"}) == 0.2
        print("ok")
    else:
        asyncio.run(main(sys.argv[1:]))
