import json
import mimetypes
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TASKS_DIR = DATA_DIR / "tasks"
LOGS_DIR = DATA_DIR / "logs"
UPLOADS_DIR = DATA_DIR / "uploads"
PHOTONICS_CLI = BASE_DIR / "photonics_agent.py"


class ConfigError(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def parse_chat_ids(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def ensure_dirs() -> None:
    for directory in (DATA_DIR, TASKS_DIR, LOGS_DIR, UPLOADS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def bridge_help_text() -> str:
    return "\n".join(
        [
            "포토닉 설계용 명령 예시는 아래처럼 쓰시면 됩니다.",
            "",
            "/mode width=500 height=220 wavelength=1550",
            "/mode width=700 height=220 slab=90 wavelength=1550",
            "/sweep start=400 stop=700 step=25 height=220 wavelength=1550",
            "/dc width=500 height=220 gap=200 coupling_length=20",
            "/dc_sweep parameter=gap start=100 stop=300 step=50 width=500 height=220",
            "/fdtd_test width=500 height=220 length=2",
            "/photonics_status",
            "기본적인 etch depth 220nm, sidewall angle 90도인 SOI waveguide의 mode profile 그려줘",
            "50대 50 directional coupler 기본 설계하고 GDS랑 시뮬레이션 파일 보내줘",
            "아주 간단한 FDTD 테스트 프로젝트 만들어줘",
            "",
            "일반 자연어 요청도 가능하지만, Lumerical 작업은 위 명령 형식이 가장 안정적입니다.",
        ]
    )


def looks_like_photonics_request(text: str) -> bool:
    lowered = text.lower()
    keywords = [
        "lumerical",
        "waveguide",
        "wg ",
        " wg",
        "soi",
        "mode profile",
        "neff",
        "sidewall",
        "etch",
        "slab",
        "rib",
        "strip",
        "sweep",
        "directional coupler",
        "coupler",
        "50:50",
        "50대 50",
        "coupling length",
        "gds",
        "fdtd",
        "fsp",
        "파장",
        "모드",
        "웨이브가이드",
        "도파로",
        "식각",
        "사이드월",
        "슬랩",
        "커플러",
        "커플링",
        "결합기",
        "실리콘 포토닉스",
        "광도파로",
    ]
    return any(keyword in lowered for keyword in keywords)


def resolve_codex_cmd(raw_value: str) -> str:
    configured = raw_value.strip()
    candidates: list[str] = []

    if configured:
        candidates.append(configured)
    candidates.append("codex")
    candidates.append(str(Path.home() / ".codex" / ".sandbox-bin" / "codex.exe"))

    vscode_extensions_dir = Path.home() / ".vscode" / "extensions"
    if vscode_extensions_dir.exists():
        for path in sorted(vscode_extensions_dir.glob("openai.chatgpt-*-win32-x64/bin/windows-x86_64/codex.exe"), reverse=True):
            candidates.append(str(path))

    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path.resolve())

    tried = ", ".join(dict.fromkeys(candidates))
    raise ConfigError(f"Could not find Codex executable. Tried: {tried}")


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}"

    def _call(self, method: str, payload: dict) -> dict:
        data = urlencode(payload).encode("utf-8")
        request = Request(f"{self.base_url}/{method}", data=data, method="POST")
        with urlopen(request, timeout=70) as response:
            body = response.read().decode("utf-8")
        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram API error for {method}: {parsed}")
        return parsed["result"]

    def _call_multipart(self, method: str, fields: dict[str, str], file_field: str, file_path: Path) -> dict:
        boundary = f"----CodexTelegram{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body = bytearray()

        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(file_path.read_bytes())
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        request = Request(
            f"{self.base_url}/{method}",
            data=bytes(body),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urlopen(request, timeout=300) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram API error for {method}: {parsed}")
        return parsed["result"]

    def get_updates(self, offset: int | None) -> list[dict]:
        payload = {"timeout": 60}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload)

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", {"drop_pending_updates": "false"})

    def send_message(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text})

    def send_document(self, chat_id: str, file_path: Path) -> None:
        self._call_multipart("sendDocument", {"chat_id": chat_id}, "document", file_path)

    def get_file(self, file_id: str) -> dict:
        return self._call("getFile", {"file_id": file_id})

    def download_file(self, file_id: str, destination: Path) -> Path:
        info = self.get_file(file_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(f"{self.file_base_url}/{info['file_path']}", timeout=300) as response:
            destination.write_bytes(response.read())
        return destination


class TaskRunner:
    def __init__(
        self,
        telegram: TelegramClient,
        allowed_chat_ids: set[str],
        codex_cmd: str,
        workdir: Path,
        sandbox_mode: str,
        approval_policy: str,
    ) -> None:
        self.telegram = telegram
        self.allowed_chat_ids = allowed_chat_ids
        self.codex_cmd = codex_cmd
        self.workdir = workdir
        self.sandbox_mode = sandbox_mode
        self.approval_policy = approval_policy
        self.python_cmd = sys.executable
        self.tasks: queue.Queue[dict] = queue.Queue()
        self.pending_confirmations: dict[str, dict] = {}
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)

    def start(self) -> None:
        self.worker.start()

    def enqueue_message(self, message: dict) -> None:
        chat_id = str(message["chat"]["id"])
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            self.telegram.send_message(chat_id, "이 대화에서는 아직 작업을 받을 수 없어요.")
            return

        text = (message.get("text") or message.get("caption") or "").strip()
        attachments = self._collect_attachments(message)
        if not text and not attachments:
            self.telegram.send_message(chat_id, "텍스트나 첨부 파일을 함께 보내주시면 처리할게요.")
            return

        if self._handle_pending_confirmation(chat_id, text):
            return

        if text == "/start":
            self.telegram.send_message(
                chat_id,
                "반갑습니다. 이제부터는 보내주신 내용을 보고 작업하거나 답변드릴게요. 편하게 말씀해 주세요.",
            )
            return

        if text == "/status":
            pending = self.tasks.qsize()
            self.telegram.send_message(chat_id, f"지금 실행 중이에요. 대기 중인 작업은 {pending}개예요.")
            return

        if text in {"/help", "/photonics_help"}:
            self.telegram.send_message(chat_id, bridge_help_text())
            return

        try:
            route, cli_args = self._resolve_route(text)
        except ConfigError as exc:
            self.telegram.send_message(chat_id, str(exc))
            return

        task_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        task = {
            "task_id": task_id,
            "chat_id": chat_id,
            "user": message.get("from", {}),
            "text": text or "첨부 파일을 확인해 주세요.",
            "attachments": attachments,
            "received_at": utc_now(),
            "route": route,
            "cli_args": cli_args,
        }

        confirmation = self._confirmation_for_task(task)
        if confirmation is not None:
            self.pending_confirmations[chat_id] = {
                "task": task,
                "created_at": time.time(),
                "message": confirmation,
            }
            self.telegram.send_message(chat_id, confirmation)
            return

        self._persist_and_queue(task)

    def _persist_and_queue(self, task: dict) -> None:
        task_file = TASKS_DIR / f"{task['task_id']}.json"
        task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        self.tasks.put(task)

    def _handle_pending_confirmation(self, chat_id: str, text: str) -> bool:
        normalized = text.strip().lower()
        if not normalized:
            return False

        yes_values = {"yes", "y", "ok", "okay", "go", "run", "execute", "네", "예", "응", "ㅇ", "ㅇㅇ", "좋아", "진행", "실행", "해", "해줘"}
        no_values = {"no", "n", "cancel", "stop", "아니", "아니오", "ㄴ", "취소", "중단", "멈춰"}

        pending = self.pending_confirmations.get(chat_id)
        if normalized not in yes_values and normalized not in no_values:
            return False

        if pending is None:
            self.telegram.send_message(chat_id, "대기 중인 확인 요청이 없습니다.")
            return True

        self.pending_confirmations.pop(chat_id, None)
        if normalized in no_values:
            self.telegram.send_message(chat_id, "취소했습니다. 조건을 바꿔서 다시 보내주시면 됩니다.")
            return True

        task = pending["task"]
        self._persist_and_queue(task)
        self.telegram.send_message(chat_id, "확인했습니다. 이제 실행하겠습니다.")
        return True

    def _confirmation_for_task(self, task: dict) -> str | None:
        reasons: list[str] = []
        text = task.get("text", "")
        route = task.get("route", "")
        cli_args = task.get("cli_args", [])

        if self._is_fdtd_task(route, cli_args, text):
            reasons.append(
                "FDTD 계열 작업은 상대적으로 무겁습니다. 이 테스트는 time-domain run 없이 project 생성/저장만 수행하고, ETA는 보통 1~2분입니다."
            )

        assumption_lines = self._assumption_lines_for_task(task)
        if assumption_lines:
            reasons.append("요청에 빠진 값이 있어 아래 기본값/추론을 적용하려고 합니다.\n" + "\n".join(f"- {line}" for line in assumption_lines))

        if not reasons:
            return None

        return "\n\n".join(
            [
                *reasons,
                "이 조건으로 실행할까요? 실행하려면 `yes` 또는 `ㅇㅇ`, 취소하려면 `no`라고 보내주세요.",
            ]
        )

    def _is_fdtd_task(self, route: str, cli_args: list[str], text: str) -> bool:
        if route == "photonics_fdtd_test":
            return "--script-only" not in cli_args
        lowered = text.lower()
        return route == "photonics_nl" and ("fdtd" in lowered or "fsp" in lowered)

    def _assumption_lines_for_task(self, task: dict) -> list[str]:
        route = task.get("route", "")
        text = task.get("text", "")
        if route == "photonics_nl":
            try:
                from photonics_agent import parse_natural_language_request

                parsed = parse_natural_language_request(text)
            except Exception:
                return []
            return [str(item) for item in parsed.assumptions]

        if route == "photonics_dc":
            return self._structured_default_assumptions(
                text,
                {
                    "width": "waveguide width 500 nm",
                    "height": "SOI/device layer height 220 nm",
                    "gap": "directional coupler gap 200 nm",
                    "coupling_length": "initial coupling length 20 um",
                },
            )

        if route == "photonics_fdtd_test":
            return self._structured_default_assumptions(
                text,
                {
                    "width": "waveguide width 500 nm",
                    "height": "SOI/device layer height 220 nm",
                    "length": "FDTD test waveguide length 2 um",
                    "wavelength": "wavelength 1550 nm",
                },
            )

        return []

    def _structured_default_assumptions(self, text: str, defaults: dict[str, str]) -> list[str]:
        try:
            tokens = shlex.split(text)
        except ValueError:
            return []
        keys = {token.split("=", 1)[0].strip().lower() for token in tokens[1:] if "=" in token}
        aliases = {
            "width": {"width", "width_nm", "wg_width"},
            "height": {"height", "height_nm", "wg_thickness", "thickness"},
            "gap": {"gap", "gap_nm", "spacing", "separation"},
            "coupling_length": {"coupling_length", "coupling_length_um", "length", "lc"},
            "length": {"length", "length_um", "wg_length"},
            "wavelength": {"wavelength", "wavelength_nm", "lambda"},
        }
        lines: list[str] = []
        for key, description in defaults.items():
            if keys.isdisjoint(aliases.get(key, {key})):
                lines.append(description)
        return lines

    def _collect_attachments(self, message: dict) -> list[dict]:
        attachments: list[dict] = []

        document = message.get("document")
        if document:
            attachments.append(
                {
                    "kind": "document",
                    "file_id": document["file_id"],
                    "file_name": document.get("file_name") or f"{document['file_unique_id']}.bin",
                }
            )

        photo_sizes = message.get("photo") or []
        if photo_sizes:
            photo = photo_sizes[-1]
            attachments.append(
                {
                    "kind": "photo",
                    "file_id": photo["file_id"],
                    "file_name": f"{photo['file_unique_id']}.jpg",
                }
            )

        return attachments

    def _worker_loop(self) -> None:
        while True:
            task = self.tasks.get()
            try:
                self._run_task(task)
            except Exception as exc:  # noqa: BLE001
                self._safe_send(task["chat_id"], f"처리 중 문제가 생겼습니다.\n\n{exc}")
            finally:
                self.tasks.task_done()

    def _run_task(self, task: dict) -> None:
        route = task.get("route", "codex")
        if route.startswith("photonics_"):
            self._run_photonics_task(task)
            return
        self._run_codex_task(task)

    def _run_codex_task(self, task: dict) -> None:
        task_id = task["task_id"]
        chat_id = task["chat_id"]
        self._download_attachments(task)
        prompt = self._build_prompt(task)
        prompt_file = TASKS_DIR / f"{task_id}.prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")

        output_file = LOGS_DIR / f"{task_id}.last_message.txt"
        transcript_file = LOGS_DIR / f"{task_id}.stdout.log"
        env = os.environ.copy()

        command = [
            self.codex_cmd,
            "-a",
            self.approval_policy,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            self.sandbox_mode,
            "-C",
            str(self.workdir),
            "-o",
            str(output_file),
            "-",
        ]
        command_log = " ".join(f'"{part}"' if " " in part else part for part in command)

        completed = subprocess.run(
            command,
            cwd=self.workdir,
            env=env,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=None,
        )

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        transcript = [
            f"timestamp: {utc_now()}",
            f"exit_code: {completed.returncode}",
            f"command: {command_log}",
            "",
            "[stdout]",
            stdout,
            "",
            "[stderr]",
            stderr,
        ]
        transcript_file.write_text("\n".join(transcript), encoding="utf-8")

        if completed.returncode == 0:
            summary = output_file.read_text(encoding="utf-8").strip() if output_file.exists() else ""
            attachments_to_send, reply_text = self._parse_reply(summary)
            for attachment in attachments_to_send:
                attachment_path = Path(attachment)
                if not attachment_path.is_absolute():
                    attachment_path = (self.workdir / attachment_path).resolve()
                if attachment_path.exists() and attachment_path.is_file():
                    self._safe_send_document(chat_id, attachment_path)
            self._safe_send(chat_id, (reply_text or "작업이 끝났습니다.")[:3000])
            return

        tail = (stderr or stdout)[-3000:].strip()
        reply = "작업을 처리하던 중 문제가 생겼습니다."
        if tail:
            reply += f"\n\n{tail}"
        self._safe_send(chat_id, reply)

    def _run_photonics_task(self, task: dict) -> None:
        task_id = task["task_id"]
        chat_id = task["chat_id"]
        transcript_file = LOGS_DIR / f"{task_id}.stdout.log"

        command = [self.python_cmd, str(PHOTONICS_CLI), *task.get("cli_args", []), "--json"]
        command_log = " ".join(f'"{part}"' if " " in part else part for part in command)
        completed = subprocess.run(
            command,
            cwd=self.workdir,
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=None,
        )

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        transcript = [
            f"timestamp: {utc_now()}",
            f"exit_code: {completed.returncode}",
            f"command: {command_log}",
            "",
            "[stdout]",
            stdout,
            "",
            "[stderr]",
            stderr,
        ]
        transcript_file.write_text("\n".join(transcript), encoding="utf-8")

        if completed.returncode != 0:
            tail = (stderr or stdout)[-3000:].strip()
            reply = "포토닉 작업 실행 중 문제가 생겼습니다."
            if tail:
                reply += f"\n\n{tail}"
            self._safe_send(chat_id, reply)
            return

        payload = json.loads(stdout.strip() or "{}")
        self._safe_send(chat_id, str(payload.get("message", "포토닉 작업을 완료했습니다."))[:3000])
        for attachment in payload.get("telegram_attachments", payload.get("attachments", [])):
            attachment_path = Path(attachment)
            if not attachment_path.is_absolute():
                attachment_path = (self.workdir / attachment_path).resolve()
            if attachment_path.exists() and attachment_path.is_file():
                self._safe_send_document(chat_id, attachment_path)

    def _download_attachments(self, task: dict) -> list[Path]:
        downloaded: list[Path] = []
        task_upload_dir = UPLOADS_DIR / task["task_id"]
        for item in task.get("attachments", []):
            safe_name = Path(item["file_name"]).name
            destination = task_upload_dir / safe_name
            downloaded.append(self.telegram.download_file(item["file_id"], destination))
        return downloaded

    def _parse_reply(self, text: str) -> tuple[list[str], str]:
        attachments: list[str] = []
        message_lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("ATTACH:"):
                attach_path = line[len("ATTACH:") :].strip()
                if attach_path:
                    attachments.append(attach_path)
                continue
            message_lines.append(raw_line)
        return attachments, "\n".join(message_lines).strip()

    def _safe_send(self, chat_id: str, text: str) -> None:
        try:
            self.telegram.send_message(chat_id, text)
        except Exception:  # noqa: BLE001
            pass

    def _safe_send_document(self, chat_id: str, file_path: Path) -> None:
        try:
            self.telegram.send_document(chat_id, file_path)
        except Exception:  # noqa: BLE001
            pass

    def _resolve_route(self, text: str) -> tuple[str, list[str]]:
        if not text.startswith("/"):
            if looks_like_photonics_request(text):
                return "photonics_nl", ["nl", "--request", text]
            return "codex", []

        tokens = shlex.split(text)
        if not tokens:
            return "codex", []

        command = tokens[0].lower()
        params = self._parse_params(tokens[1:])

        if command == "/photonics_status":
            if params:
                raise ConfigError("/photonics_status 는 추가 인자 없이 사용해 주세요.")
            return "photonics_env", ["env"]
        if command == "/mode":
            return "photonics_mode", self._mode_cli_args(params)
        if command == "/sweep":
            return "photonics_sweep", self._sweep_cli_args(params)
        if command == "/dc":
            return "photonics_dc", self._dc_cli_args(params)
        if command == "/dc_sweep":
            return "photonics_dc_sweep", self._dc_sweep_cli_args(params)
        if command == "/fdtd_test":
            return "photonics_fdtd_test", self._fdtd_test_cli_args(params)

        raise ConfigError("지원하지 않는 명령입니다.\n\n" + bridge_help_text())

    def _parse_params(self, tokens: list[str]) -> dict[str, str]:
        params: dict[str, str] = {}
        for token in tokens:
            if "=" not in token:
                raise ConfigError("명령 인자는 key=value 형식으로 보내 주세요.\n\n" + bridge_help_text())
            key, value = token.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if not key or not value:
                raise ConfigError("빈 key 또는 value 는 사용할 수 없습니다.")
            params[key] = value
        return params

    def _take_param(self, params: dict[str, str], *names: str, default: str | None = None) -> str | None:
        for name in names:
            if name in params:
                return params.pop(name)
        return default

    def _required_param(self, params: dict[str, str], *names: str) -> str:
        value = self._take_param(params, *names)
        if value is None:
            joined = ", ".join(names)
            raise ConfigError(f"필수 인자가 빠졌습니다: {joined}\n\n" + bridge_help_text())
        return value

    def _reject_unknown_params(self, params: dict[str, str]) -> None:
        if params:
            unknown = ", ".join(sorted(params))
            raise ConfigError(f"알 수 없는 인자입니다: {unknown}\n\n" + bridge_help_text())

    def _mode_cli_args(self, params: dict[str, str]) -> list[str]:
        height_value = self._required_param(params, "height", "height_nm")
        args = [
            "mode",
            "--width-nm",
            self._required_param(params, "width", "width_nm"),
            "--height-nm",
            height_value,
            "--wavelength-nm",
            self._take_param(params, "wavelength", "wavelength_nm", default="1550"),
        ]

        slab = self._take_param(params, "slab", "slab_nm")
        etch = self._take_param(params, "etch", "etch_depth", "etch_depth_nm")
        if slab is None and etch is not None:
            try:
                slab_value = float(height_value) - float(etch)
            except ValueError as exc:
                raise ConfigError(f"etch/slab 값을 숫자로 해석하지 못했습니다: {exc}") from exc
            slab = str(slab_value)
        if slab is not None:
            args.extend(["--slab-nm", slab])
        sidewall = self._take_param(params, "sidewall", "sidewall_angle", "sidewall_angle_deg", "angle")
        if sidewall is not None:
            args.extend(["--sidewall-angle-deg", sidewall])
        core_material = self._take_param(params, "core_material", "core")
        if core_material is not None:
            args.extend(["--core-material", core_material])
        clad_material = self._take_param(params, "clad_material", "clad")
        if clad_material is not None:
            args.extend(["--clad-material", clad_material])
        trial_modes = self._take_param(params, "trial_modes")
        if trial_modes is not None:
            args.extend(["--trial-modes", trial_modes])
        mesh_accuracy = self._take_param(params, "mesh_accuracy")
        if mesh_accuracy is not None:
            args.extend(["--mesh-accuracy", mesh_accuracy])
        timeout_s = self._take_param(params, "timeout", "timeout_s")
        if timeout_s is not None:
            args.extend(["--timeout-s", timeout_s])
        lumapi_dir = self._take_param(params, "lumapi_dir")
        if lumapi_dir is not None:
            args.extend(["--lumapi-dir", lumapi_dir])
        if parse_bool(self._take_param(params, "show_gui", default="false")):
            args.append("--show-gui")
        if not parse_bool(self._take_param(params, "live", default="true")):
            args.append("--script-only")

        self._reject_unknown_params(params)
        return args

    def _sweep_cli_args(self, params: dict[str, str]) -> list[str]:
        height_value = self._required_param(params, "height", "height_nm")
        args = [
            "sweep",
            "--width-start-nm",
            self._required_param(params, "start", "width_start", "width_start_nm"),
            "--width-stop-nm",
            self._required_param(params, "stop", "width_stop", "width_stop_nm"),
            "--width-step-nm",
            self._required_param(params, "step", "width_step", "width_step_nm"),
            "--height-nm",
            height_value,
            "--wavelength-nm",
            self._take_param(params, "wavelength", "wavelength_nm", default="1550"),
        ]

        slab = self._take_param(params, "slab", "slab_nm")
        etch = self._take_param(params, "etch", "etch_depth", "etch_depth_nm")
        if slab is None and etch is not None:
            try:
                slab_value = float(height_value) - float(etch)
            except ValueError as exc:
                raise ConfigError(f"etch/slab 값을 숫자로 해석하지 못했습니다: {exc}") from exc
            slab = str(slab_value)
        if slab is not None:
            args.extend(["--slab-nm", slab])
        sidewall = self._take_param(params, "sidewall", "sidewall_angle", "sidewall_angle_deg", "angle")
        if sidewall is not None:
            args.extend(["--sidewall-angle-deg", sidewall])
        core_material = self._take_param(params, "core_material", "core")
        if core_material is not None:
            args.extend(["--core-material", core_material])
        clad_material = self._take_param(params, "clad_material", "clad")
        if clad_material is not None:
            args.extend(["--clad-material", clad_material])
        trial_modes = self._take_param(params, "trial_modes")
        if trial_modes is not None:
            args.extend(["--trial-modes", trial_modes])
        mesh_accuracy = self._take_param(params, "mesh_accuracy")
        if mesh_accuracy is not None:
            args.extend(["--mesh-accuracy", mesh_accuracy])
        timeout_s = self._take_param(params, "timeout", "timeout_s")
        if timeout_s is not None:
            args.extend(["--timeout-s", timeout_s])
        lumapi_dir = self._take_param(params, "lumapi_dir")
        if lumapi_dir is not None:
            args.extend(["--lumapi-dir", lumapi_dir])
        if parse_bool(self._take_param(params, "show_gui", default="false")):
            args.append("--show-gui")
        if not parse_bool(self._take_param(params, "live", default="true")):
            args.append("--script-only")

        self._reject_unknown_params(params)
        return args

    def _dc_base_cli_args(self, params: dict[str, str]) -> list[str]:
        height_value = self._take_param(params, "height", "height_nm", "wg_thickness", "thickness", default="220")
        args = [
            "--width-nm",
            self._take_param(params, "width", "width_nm", "wg_width", default="500"),
            "--height-nm",
            height_value,
            "--wavelength-nm",
            self._take_param(params, "wavelength", "wavelength_nm", "lambda", default="1550"),
            "--gap-nm",
            self._take_param(params, "gap", "gap_nm", "spacing", "separation", default="200"),
            "--coupling-length-um",
            self._take_param(params, "coupling_length", "coupling_length_um", "length", "lc", default="20"),
        ]

        input_length = self._take_param(params, "input_length", "input_length_um")
        if input_length is not None:
            args.extend(["--input-length-um", input_length])
        output_length = self._take_param(params, "output_length", "output_length_um")
        if output_length is not None:
            args.extend(["--output-length-um", output_length])
        target_split = self._take_param(params, "target_split", "target_split_ratio", "split")
        if target_split is not None:
            if ":" in target_split:
                first, second = target_split.split(":", 1)
                try:
                    ratio = float(second) / (float(first) + float(second))
                except ValueError as exc:
                    raise ConfigError(f"split 값을 해석하지 못했습니다: {exc}") from exc
                args.extend(["--target-split-ratio", str(ratio)])
            else:
                args.extend(["--target-split-ratio", target_split])

        slab = self._take_param(params, "slab", "slab_nm")
        etch = self._take_param(params, "etch", "etch_depth", "etch_depth_nm")
        if slab is None and etch is not None:
            try:
                slab_value = float(height_value) - float(etch)
            except ValueError as exc:
                raise ConfigError(f"etch/slab 값을 숫자로 해석하지 못했습니다: {exc}") from exc
            slab = str(slab_value)
        if slab is not None:
            args.extend(["--slab-nm", slab])
        sidewall = self._take_param(params, "sidewall", "sidewall_angle", "sidewall_angle_deg", "angle")
        if sidewall is not None:
            args.extend(["--sidewall-angle-deg", sidewall])
        core_material = self._take_param(params, "core_material", "core")
        if core_material is not None:
            args.extend(["--core-material", core_material])
        clad_material = self._take_param(params, "clad_material", "clad")
        if clad_material is not None:
            args.extend(["--clad-material", clad_material])
        trial_modes = self._take_param(params, "trial_modes")
        if trial_modes is not None:
            args.extend(["--trial-modes", trial_modes])
        mesh_accuracy = self._take_param(params, "mesh_accuracy")
        if mesh_accuracy is not None:
            args.extend(["--mesh-accuracy", mesh_accuracy])
        lumapi_dir = self._take_param(params, "lumapi_dir")
        if lumapi_dir is not None:
            args.extend(["--lumapi-dir", lumapi_dir])
        if parse_bool(self._take_param(params, "show_gui", default="false")):
            args.append("--show-gui")
        return args

    def _dc_cli_args(self, params: dict[str, str]) -> list[str]:
        args = ["dc", *self._dc_base_cli_args(params)]
        timeout_s = self._take_param(params, "timeout", "timeout_s")
        if timeout_s is not None:
            args.extend(["--timeout-s", timeout_s])
        if not parse_bool(self._take_param(params, "live", default="true")):
            args.append("--script-only")
        self._reject_unknown_params(params)
        return args

    def _dc_sweep_cli_args(self, params: dict[str, str]) -> list[str]:
        parameter = self._take_param(params, "parameter", "param", "sweep")
        if parameter is None:
            raise ConfigError("/dc_sweep에는 parameter=gap 또는 parameter=length가 필요합니다.")
        normalized = parameter.strip().lower()
        if normalized in {"gap", "gap_nm", "spacing", "separation"}:
            parameter = "gap_nm"
        elif normalized in {"length", "coupling_length", "coupling_length_um", "lc"}:
            parameter = "coupling_length_um"
        else:
            raise ConfigError("parameter는 gap 또는 length 중 하나여야 합니다.")

        args = [
            "dc_sweep",
            *self._dc_base_cli_args(params),
            "--parameter",
            parameter,
            "--start",
            self._required_param(params, "start", "from"),
            "--stop",
            self._required_param(params, "stop", "to"),
            "--step",
            self._required_param(params, "step"),
        ]
        timeout_s = self._take_param(params, "timeout", "timeout_s")
        if timeout_s is not None:
            args.extend(["--timeout-s", timeout_s])
        if not parse_bool(self._take_param(params, "live", default="true")):
            args.append("--script-only")
        self._reject_unknown_params(params)
        return args

    def _fdtd_test_cli_args(self, params: dict[str, str]) -> list[str]:
        args = [
            "fdtd_test",
            "--width-nm",
            self._take_param(params, "width", "width_nm", "wg_width", default="500"),
            "--height-nm",
            self._take_param(params, "height", "height_nm", "wg_thickness", "thickness", default="220"),
            "--length-um",
            self._take_param(params, "length", "length_um", "wg_length", default="2"),
            "--wavelength-nm",
            self._take_param(params, "wavelength", "wavelength_nm", "lambda", default="1550"),
        ]
        mesh_accuracy = self._take_param(params, "mesh_accuracy")
        if mesh_accuracy is not None:
            args.extend(["--mesh-accuracy", mesh_accuracy])
        simulation_time = self._take_param(params, "simulation_time", "simulation_time_fs")
        if simulation_time is not None:
            args.extend(["--simulation-time-fs", simulation_time])
        core_material = self._take_param(params, "core_material", "core")
        if core_material is not None:
            args.extend(["--core-material", core_material])
        clad_material = self._take_param(params, "clad_material", "clad")
        if clad_material is not None:
            args.extend(["--clad-material", clad_material])
        timeout_s = self._take_param(params, "timeout", "timeout_s")
        if timeout_s is not None:
            args.extend(["--timeout-s", timeout_s])
        lumapi_dir = self._take_param(params, "lumapi_dir")
        if lumapi_dir is not None:
            args.extend(["--lumapi-dir", lumapi_dir])
        if parse_bool(self._take_param(params, "show_gui", default="false")):
            args.append("--show-gui")
        if not parse_bool(self._take_param(params, "live", default="true")):
            args.append("--script-only")
        self._reject_unknown_params(params)
        return args

    def _build_prompt(self, task: dict) -> str:
        attachment_paths = self._task_attachment_paths(task)
        attachment_lines = [f"- {path}" for path in attachment_paths] or ["- none"]

        return "\n".join(
            [
                "You are Junhyung's private photonic design assistant running on his computer.",
                "Specialize in Lumerical API workflows, silicon photonics, waveguides, MMIs, rings, gratings, and parameter sweeps.",
                "Prefer using the local helper CLI `python photonics_agent.py ...` for MODE-based tasks before writing raw lumapi code.",
                "Reply in natural, concise Korean.",
                "Use polite Korean consistently.",
                "Do not mention task IDs, internal logs, workspace paths, or system details unless absolutely necessary.",
                "If the message is casual conversation, answer naturally and do not modify files.",
                "If the message asks for computer work, perform it directly in the workspace when feasible.",
                "If the request is about photonic simulation, keep units explicit and consistent.",
                "If a live Lumerical solve fails, still leave behind reproducible Python or LSF files and explain what is ready.",
                f"Current workspace: {self.workdir}",
                "If you create or modify files, do so in this workspace.",
                "Useful local helper commands include `python photonics_agent.py env`, `python photonics_agent.py mode`, `python photonics_agent.py sweep`, `python photonics_agent.py dc`, `python photonics_agent.py dc_sweep`, and `python photonics_agent.py fdtd_test`.",
                "After finishing, reply like a compact engineering assistant.",
                "Keep the reply short and natural.",
                "Do not use sections or bullet lists unless truly needed.",
                "When files were changed, mention them naturally in one or two sentences.",
                "When nothing was changed, just say so naturally.",
                "If you want to send files back to Telegram, put one line per file in the exact format: ATTACH: <path>",
                "Only use ATTACH for files that already exist when you finish.",
                "",
                "Attached local files:",
                *attachment_lines,
                "",
                "Telegram request:",
                task["text"],
            ]
        )

    def _task_attachment_paths(self, task: dict) -> list[Path]:
        task_upload_dir = UPLOADS_DIR / task["task_id"]
        return [task_upload_dir / Path(item["file_name"]).name for item in task.get("attachments", [])]


def main() -> None:
    load_env_file(BASE_DIR / ".env")
    ensure_dirs()

    token = require_env("TELEGRAM_BOT_TOKEN")
    workdir = Path(os.environ.get("BRIDGE_WORKDIR", BASE_DIR)).resolve()
    codex_cmd = resolve_codex_cmd(os.environ.get("CODEX_CMD", "codex"))
    sandbox_mode = os.environ.get("CODEX_SANDBOX", "workspace-write").strip() or "workspace-write"
    approval_policy = os.environ.get("CODEX_APPROVAL_POLICY", "never").strip() or "never"
    allowed_chat_ids = parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))

    telegram = TelegramClient(token)
    telegram.delete_webhook()
    runner = TaskRunner(
        telegram=telegram,
        allowed_chat_ids=allowed_chat_ids,
        codex_cmd=codex_cmd,
        workdir=workdir,
        sandbox_mode=sandbox_mode,
        approval_policy=approval_policy,
    )
    runner.start()

    offset = None
    while True:
        try:
            updates = telegram.get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    runner.enqueue_message(message)
        except KeyboardInterrupt:
            raise
        except HTTPError as exc:
            error_file = LOGS_DIR / "bridge_errors.log"
            with error_file.open("a", encoding="utf-8") as handle:
                handle.write(f"[{utc_now()}] HTTP Error {exc.code}: {exc.reason}\n")
            if exc.code == 409:
                time.sleep(10)
                continue
            time.sleep(5)
        except Exception as exc:  # noqa: BLE001
            error_file = LOGS_DIR / "bridge_errors.log"
            with error_file.open("a", encoding="utf-8") as handle:
                handle.write(f"[{utc_now()}] {exc}\n")
            time.sleep(5)


if __name__ == "__main__":
    main()
