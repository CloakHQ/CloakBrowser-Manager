"""Discover and run local automation scripts against running profiles."""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
CONTROLLED_ARGUMENTS = {"host", "profile_id", "cdp_url"}
MAX_LOG_CHARS = 80_000

RunStatus = Literal["running", "succeeded", "failed", "stopped"]


@dataclass(frozen=True)
class ScriptParameter:
    name: str
    flags: list[str]
    kind: Literal["positional", "option", "flag"]
    required: bool
    default: Any = None
    help: str | None = None
    choices: list[str] | None = None
    value_type: Literal["string", "integer", "number", "boolean", "path"] = "string"


@dataclass(frozen=True)
class ScriptDefinition:
    id: str
    filename: str
    name: str
    description: str | None
    parameters: list[ScriptParameter]
    profile_required: bool


@dataclass
class ScriptRun:
    id: str
    script_id: str
    script_name: str
    profile_id: str
    profile_name: str | None
    status: RunStatus
    started_at: float
    finished_at: float | None = None
    exit_code: int | None = None
    command: list[str] = field(default_factory=list)
    log: str = ""
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)


def _literal(node: ast.AST | None, constants: dict[str, Any] | None = None) -> Any:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        values = [_literal(item, constants) for item in node.elts]
        return values if all(value is not None for value in values) else None
    if isinstance(node, ast.Name):
        if constants and node.id in constants:
            return constants[node.id]
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _value_type(
    type_value: Any, default: Any, action: str | None
) -> Literal["string", "integer", "number", "boolean", "path"]:
    if action in ("store_true", "store_false") or isinstance(default, bool):
        return "boolean"
    if type_value in ("int", int):
        return "integer"
    if type_value in ("float", float):
        return "number"
    if type_value in ("Path", Path):
        return "path"
    return "string"


def _dest_for(flags: list[str], explicit_dest: str | None) -> str:
    if explicit_dest:
        return explicit_dest
    long_flags = [flag for flag in flags if flag.startswith("--")]
    source = long_flags[0] if long_flags else flags[0]
    return source.lstrip("-").replace("-", "_")


def _preferred_flag(parameter: ScriptParameter) -> str:
    for flag in parameter.flags:
        if flag.startswith("--"):
            return flag
    return parameter.flags[0]


def _module_constants(tree: ast.Module) -> dict[str, Any]:
    constants: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = _literal(node.value, constants)
            if value is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    constants[target.id] = value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            value = _literal(node.value, constants)
            if value is not None and node.target.id.isupper():
                constants[node.target.id] = value
    return constants


class ScriptRunner:
    def __init__(self, scripts_dir: Path = SCRIPTS_DIR):
        self.scripts_dir = scripts_dir
        self.runs: dict[str, ScriptRun] = {}

    def list_scripts(self) -> list[ScriptDefinition]:
        scripts = []
        for path in sorted(self.scripts_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            scripts.append(self.get_script(path.stem))
        return scripts

    def get_script(self, script_id: str) -> ScriptDefinition:
        path = self._script_path(script_id)
        description, parameters = self._inspect_script(path)
        visible_parameters = [
            parameter for parameter in parameters if parameter.name not in CONTROLLED_ARGUMENTS
        ]
        profile_required = any(
            parameter.name in {"profile_id", "cdp_url"} for parameter in parameters
        )
        return ScriptDefinition(
            id=path.stem,
            filename=path.name,
            name=path.stem.replace("_", " ").title(),
            description=description,
            parameters=visible_parameters,
            profile_required=profile_required,
        )

    def get_run(self, run_id: str) -> ScriptRun | None:
        return self.runs.get(run_id)

    async def start_run(
        self,
        script_id: str,
        profile_id: str,
        profile_name: str | None,
        cdp_port: int,
        manager_host: str,
        arguments: dict[str, str | int | float | bool | None],
    ) -> ScriptRun:
        definition = self.get_script(script_id)
        command = self.build_command(
            script_id=script_id,
            profile_id=profile_id,
            cdp_port=cdp_port,
            manager_host=manager_host,
            arguments=arguments,
        )
        run = ScriptRun(
            id=str(uuid.uuid4()),
            script_id=definition.id,
            script_name=definition.name,
            profile_id=profile_id,
            profile_name=profile_name,
            status="running",
            started_at=time.time(),
            command=command,
        )
        self.runs[run.id] = run
        run.task = asyncio.create_task(self._run_process(run), name=f"script-{run.id}")
        return run

    async def stop_run(self, run_id: str) -> ScriptRun | None:
        run = self.runs.get(run_id)
        if not run or run.status != "running":
            return run

        run.status = "stopped"
        run.finished_at = time.time()
        process = run.process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        return run

    def build_command(
        self,
        script_id: str,
        profile_id: str,
        cdp_port: int,
        manager_host: str,
        arguments: dict[str, str | int | float | bool | None],
    ) -> list[str]:
        path = self._script_path(script_id)
        _, parameters = self._inspect_script(path)
        parameters_by_name = {parameter.name: parameter for parameter in parameters}
        allowed = {
            parameter.name for parameter in parameters if parameter.name not in CONTROLLED_ARGUMENTS
        }
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise ValueError(f"Unknown argument(s): {', '.join(unknown)}")

        command = [sys.executable, str(path)]
        if "cdp_url" in parameters_by_name:
            command.extend(["--cdp-url", f"http://127.0.0.1:{cdp_port}"])
        else:
            if "host" in parameters_by_name:
                command.extend(["--host", manager_host.rstrip("/")])
            if "profile_id" in parameters_by_name:
                command.extend(["--profile-id", profile_id])

        for parameter in parameters:
            if parameter.name in CONTROLLED_ARGUMENTS:
                continue
            value = arguments.get(parameter.name)
            has_value = value is not None and value != ""

            if parameter.kind == "flag":
                if bool(value):
                    command.append(_preferred_flag(parameter))
                continue

            if not has_value:
                if parameter.required:
                    raise ValueError(f"Missing required argument: {parameter.name}")
                continue

            if parameter.kind == "positional":
                command.append(str(value))
            else:
                command.extend([_preferred_flag(parameter), str(value)])

        return command

    def _script_path(self, script_id: str) -> Path:
        path = (self.scripts_dir / f"{script_id}.py").resolve()
        scripts_dir = self.scripts_dir.resolve()
        if path.parent != scripts_dir or not path.exists() or path.suffix != ".py":
            raise FileNotFoundError(script_id)
        return path

    def _inspect_script(self, path: Path) -> tuple[str | None, list[ScriptParameter]]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstring = ast.get_docstring(tree)
        description = docstring.splitlines()[0] if docstring else None
        parameters: list[ScriptParameter] = []
        constants = _module_constants(tree)

        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ]
        calls.sort(key=lambda node: getattr(node, "lineno", 0))

        for node in calls:
            flags = [_literal(arg, constants) for arg in node.args]
            flags = [flag for flag in flags if isinstance(flag, str)]
            if not flags:
                continue

            kwargs = {
                keyword.arg: _literal(keyword.value, constants)
                for keyword in node.keywords
                if keyword.arg is not None
            }
            action = kwargs.get("action")
            explicit_dest = kwargs.get("dest")
            default = kwargs.get("default")
            type_value = kwargs.get("type")
            choices = kwargs.get("choices")
            is_optional = any(flag.startswith("-") for flag in flags)
            kind: Literal["positional", "option", "flag"]
            if action in ("store_true", "store_false"):
                kind = "flag"
            else:
                kind = "option" if is_optional else "positional"
            name = _dest_for(flags, explicit_dest if isinstance(explicit_dest, str) else None)
            required = bool(kwargs.get("required")) if is_optional else default is None

            parameters.append(
                ScriptParameter(
                    name=name,
                    flags=flags,
                    kind=kind,
                    required=required,
                    default=default,
                    help=kwargs.get("help") if isinstance(kwargs.get("help"), str) else None,
                    choices=choices if isinstance(choices, list) else None,
                    value_type=_value_type(type_value, default, action),
                )
            )

        return description, parameters

    async def _run_process(self, run: ScriptRun) -> None:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            process = await asyncio.create_subprocess_exec(
                *run.command,
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            run.process = process
            if process.stdout:
                while chunk := await process.stdout.read(4096):
                    self._append_log(run, chunk.decode("utf-8", errors="replace"))
            run.exit_code = await process.wait()
            if run.status == "stopped":
                return
            run.status = "succeeded" if run.exit_code == 0 else "failed"
            run.finished_at = time.time()
        except Exception as exc:
            self._append_log(run, f"\nFailed to run script: {exc}\n")
            run.status = "failed"
            run.finished_at = time.time()

    def _append_log(self, run: ScriptRun, text: str) -> None:
        run.log = (run.log + text)[-MAX_LOG_CHARS:]
