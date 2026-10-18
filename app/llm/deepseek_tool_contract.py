"""Finite transport contract for this project's eight DeepSeek functions.

Only the model-facing copy loses unsupported length keywords. Existing input
models, business validators and tool permissions remain the authoritative gate.
The wire representation preserves omission separately from explicit JSON null.
"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import json
import math


FUNCTIONS = frozenset({"update_requirements", "search_knowledge", "query_catalog",
    "match_requirements", "calculate_price", "explain_knowledge", "query_quote", "adjudicate_turn"})
NULL_KEY = "$wire_null"
_OMITTED = object()
_ANNOTATIONS = frozenset({"title", "description", "default"})
_RANGES = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"})


class ToolContractError(ValueError):
    """Invalid finite schema or transport; contains no input/provider payload."""


def _fail():
    raise ToolContractError("invalid_deepseek_tool_contract")


def _json_values(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail()
        return
    if type(value) is list:
        for child in value:
            _json_values(child)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _json_values(child)
        return
    _fail()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _arguments(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
            parse_constant=lambda _: _fail()) if type(raw) is str else raw
        _json_values(value)
        if type(value) is not dict:
            _fail()
        return value
    except (ValueError, TypeError, RecursionError, OverflowError):
        _fail()


def _same(value, expected):
    if type(value) in (int, float) and type(expected) in (int, float):
        return value == expected
    return type(value) is type(expected) and value == expected


def _fingerprint(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _presence(schema):
    return {"anyOf": [
        _object({"state": {"type": "string", "enum": ["omitted"]}}),
        _object({"state": {"type": "string", "enum": ["provided"]}, "value": schema})]}


@dataclass(frozen=True)
class _Node:
    kind: str
    source: dict
    wire: dict
    fields: tuple = ()
    children: tuple = ()

    def transform(self, value, *, encode):
        if self.kind == "anyOf":
            matches = []
            for child in self.children:
                try:
                    matches.append(child.transform(value, encode=encode))
                except ToolContractError:
                    pass
            if not matches or any(_fingerprint(match) != _fingerprint(matches[0]) for match in matches[1:]):
                _fail()
            return matches[0]
        if self.kind == "null":
            if encode:
                if value is not None:
                    _fail()
                return {NULL_KEY: "null"}
            if type(value) is not dict or set(value) != {NULL_KEY} or value[NULL_KEY] != "null":
                _fail()
            return None
        if self.kind == "object":
            if type(value) is not dict:
                _fail()
            names = {name for name, _, _ in self.fields}
            required = {name for name, optional, _ in self.fields if not optional}
            if set(value) - names or (required if encode else names) - set(value):
                _fail()
            result = {}
            for name, optional, child in self.fields:
                raw = value.get(name, _OMITTED)
                if not optional:
                    result[name] = child.transform(raw, encode=encode)
                elif encode:
                    result[name] = ({"state": "omitted"} if raw is _OMITTED else
                        {"state": "provided", "value": child.transform(raw, encode=True)})
                else:
                    if type(raw) is not dict or type(raw.get("state")) is not str:
                        _fail()
                    if raw["state"] == "omitted" and set(raw) == {"state"}:
                        continue
                    if raw["state"] != "provided" or set(raw) != {"state", "value"}:
                        _fail()
                    result[name] = child.transform(raw["value"], encode=False)
            return result
        if self.kind == "array":
            if type(value) is not list:
                _fail()
            return [self.children[0].transform(child, encode=encode) for child in value]
        if (self.kind == "string" and type(value) is not str
                or self.kind == "boolean" and type(value) is not bool
                or self.kind == "integer" and type(value) is not int
                or self.kind == "number" and type(value) not in (int, float)):
            _fail()
        if "enum" in self.source and not any(_same(value, item) for item in self.source["enum"]):
            _fail()
        if "const" in self.source and not _same(value, self.source["const"]):
            _fail()
        for key, bound in self.source.items():
            if (key == "minimum" and value < bound or key == "maximum" and value > bound
                    or key == "exclusiveMinimum" and value <= bound
                    or key == "exclusiveMaximum" and value >= bound):
                _fail()
            if key == "multipleOf" and Decimal(str(value)) % Decimal(str(bound)) != 0:
                _fail()
        return value


class _Compiler:
    def __init__(self, schema):
        if type(schema) is not dict or schema.get("type") != "object":
            _fail()
        _json_values(schema)
        self.definitions = schema.get("$defs", {})
        if type(self.definitions) is not dict:
            _fail()
        # Even unused definitions must be closed and understood.
        for name, definition in self.definitions.items():
            if not name or type(definition) is not dict:
                _fail()
            self.compile(definition, (name,))
        root = {key: value for key, value in schema.items() if key != "$defs"}
        self.root = self.compile(root)

    def compile(self, schema, active=()):
        if type(schema) is not dict:
            _fail()
        for key in ("title", "description"):
            if key in schema and type(schema[key]) is not str:
                _fail()
        annotations = {"description": schema["description"]} if "description" in schema else {}
        if "$ref" in schema:
            if set(schema) - (_ANNOTATIONS | {"$ref"}):
                _fail()
            reference = schema["$ref"]
            if type(reference) is not str or not reference.startswith("#/$defs/"):
                _fail()
            name = reference[len("#/$defs/"):]
            if "/" in name:
                _fail()
            name = name.replace("~1", "/").replace("~0", "~")
            if name not in self.definitions or name in active:
                _fail()
            child = self.compile(self.definitions[name], (*active, name))
            return _Node(child.kind, child.source, child.wire | annotations, child.fields, child.children)
        if "anyOf" in schema:
            branches = schema["anyOf"]
            if (set(schema) - (_ANNOTATIONS | {"anyOf"}) or type(branches) is not list
                    or len(branches) < 2):
                _fail()
            children = tuple(self.compile(branch, active) for branch in branches)
            return _Node("anyOf", schema, {"anyOf": [child.wire for child in children]} | annotations,
                         children=children)
        kind = schema.get("type")
        specific = {
            "object": {"properties", "required", "additionalProperties"},
            "array": {"items", "minItems", "maxItems"},
            "string": {"enum", "minLength", "maxLength"},
            "integer": {"enum", "const", *_RANGES},
            "number": {"enum", "const", *_RANGES},
            "boolean": {"enum"}, "null": set(),
        }
        if type(kind) is not str or kind not in specific or set(schema) - (_ANNOTATIONS | {"type"} | specific[kind]):
            _fail()
        for key in ("minLength", "maxLength", "minItems", "maxItems"):
            if key in schema and (type(schema[key]) is not int or schema[key] < 0):
                _fail()
        for low, high in (("minLength", "maxLength"), ("minItems", "maxItems"), ("minimum", "maximum")):
            if low in schema and high in schema and schema[low] > schema[high]:
                _fail()
        for key in _RANGES | {"const"}:
            if key in schema and (type(schema[key]) not in (int, float)
                    or key == "multipleOf" and schema[key] <= 0):
                _fail()
        if "enum" in schema:
            choices = schema["enum"]
            types = {"string": (str,), "boolean": (bool,), "integer": (int,), "number": (int, float)}[kind]
            if (type(choices) is not list or not choices or any(type(choice) not in types for choice in choices)
                    or any(_same(choice, old) for index, choice in enumerate(choices) for old in choices[:index])):
                _fail()
        if kind == "null":
            return _Node(kind, schema, _object({NULL_KEY: {"type": "string", "enum": ["null"]}}) | annotations)
        if kind == "object":
            properties, required = schema.get("properties"), schema.get("required", [])
            if (type(properties) is not dict or any(not name or name == NULL_KEY for name in properties)
                    or schema.get("additionalProperties") is not False or type(required) is not list
                    or any(type(name) is not str for name in required)
                    or len(set(required)) != len(required) or set(required) - set(properties)):
                _fail()
            fields = tuple((name, name not in required, self.compile(child, active))
                for name, child in properties.items())
            wire = _object({name: _presence(child.wire) if optional else child.wire
                for name, optional, child in fields}) | annotations
            return _Node(kind, schema, wire, fields=fields)
        if kind == "array":
            if "items" not in schema:
                _fail()
            child = self.compile(schema["items"], active)
            return _Node(kind, schema, {"type": kind, "items": child.wire} | annotations, children=(child,))
        wire = {key: value for key, value in schema.items()
                if key not in _ANNOTATIONS | {"minLength", "maxLength"}}
        return _Node(kind, schema, wire | annotations)


def _candidate_update(schema):
    # Lazy import avoids a verifier/client cycle. Change only the numeric
    # candidate representation; preserve caller-required fields and guidance.
    from app.llm.semantic_verifier import _CandidateEnvelope
    candidate = _CandidateEnvelope.model_json_schema()
    try:
        candidate_name = candidate["properties"]["parameters"]["items"]["$ref"].split("/")[-1]
        number = candidate["$defs"][candidate_name]["properties"]["value"]
        source_name = schema["properties"]["parameters"]["items"]["$ref"].split("/")[-1]
        value = schema["$defs"][source_name]["properties"]["value"]
        if "anyOf" in value:
            if value["anyOf"] != number["anyOf"]:
                _fail()
        elif value.get("type") != "integer" or set(value) - (_ANNOTATIONS | {"type"}):
            _fail()
        schema["$defs"][source_name]["properties"]["value"] = {
            "anyOf": deepcopy(number["anyOf"]),
            **{key: value[key] for key in _ANNOTATIONS if key in value}}
    except (KeyError, TypeError, AttributeError):
        _fail()
    return schema


def _review_rejection_alias(schema):
    # Existing parser accepts this diagnostic synonym only for reject or
    # unresolved groups. Preserve its raw value here; the owning conditional
    # validator must still reject it in an accept group.
    try:
        name = schema["properties"]["groups"]["items"]["$ref"].split("/")[-1]
        reason = schema["$defs"][name]["properties"]["reason"]
        if reason.get("type") != "string" or "conditional" not in reason.get("enum", ()):
            _fail()
        if "hypothesis" not in reason["enum"]:
            reason["enum"].append("hypothesis")
    except (KeyError, TypeError, AttributeError):
        _fail()
    return schema


class ToolContract:
    """Compile once, then project and transport either conversation direction."""

    def __init__(self, source_tools, *, candidate_update=True):
        if type(source_tools) is not list or not source_tools or type(candidate_update) is not bool:
            _fail()
        self._nodes, self._tools = {}, []
        try:
            _json_values(source_tools)
            for original in source_tools:
                if (type(original) is not dict or set(original) != {"type", "function"}
                        or original["type"] != "function" or type(original["function"]) is not dict):
                    _fail()
                function = original["function"]
                if (set(function) - {"name", "description", "parameters", "strict"}
                        or not {"name", "parameters"} <= set(function)
                        or type(function.get("name")) is not str or function["name"] not in FUNCTIONS
                        or function["name"] in self._nodes
                        or "description" in function and type(function["description"]) is not str
                        or "strict" in function and type(function["strict"]) is not bool):
                    _fail()
                schema = deepcopy(function["parameters"])
                compiler = _Compiler(schema)
                if candidate_update and function["name"] == "update_requirements":
                    compiler = _Compiler(_candidate_update(schema))
                if function["name"] == "adjudicate_turn":
                    compiler = _Compiler(_review_rejection_alias(schema))
                self._nodes[function["name"]] = compiler.root
                projected = {key: deepcopy(value) for key, value in function.items() if key != "parameters"}
                projected.update(strict=True, parameters=deepcopy(compiler.root.wire))
                self._tools.append({"type": "function", "function": projected})
        except (ValueError, TypeError, RecursionError, OverflowError):
            _fail()

    @property
    def tools(self):
        return deepcopy(self._tools)

    def _transform(self, name, raw, *, encode):
        if type(name) is not str or name not in self._nodes:
            _fail()
        try:
            decoded = self._nodes[name].transform(_arguments(raw), encode=encode)
            return json.dumps(decoded, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError, OverflowError):
            _fail()

    def decode_arguments(self, name, raw):
        return self._transform(name, raw, encode=False)

    def encode_arguments(self, name, raw):
        return self._transform(name, raw, encode=True)


def project_tools(source_tools, *, candidate_update=True):
    return ToolContract(source_tools, candidate_update=candidate_update).tools


def decode_arguments(name, raw, source_tools, *, candidate_update=True):
    return ToolContract(source_tools, candidate_update=candidate_update).decode_arguments(name, raw)


def encode_arguments(name, raw, source_tools, *, candidate_update=True):
    return ToolContract(source_tools, candidate_update=candidate_update).encode_arguments(name, raw)
