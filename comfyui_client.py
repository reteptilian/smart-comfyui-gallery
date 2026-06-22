"""Small, dependency-free client for the ComfyUI HTTP API."""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional


class ComfyUIError(RuntimeError):
    """Raised when a ComfyUI request fails or returns invalid data."""


class ComfyUIClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        normalized = (base_url or "").strip().rstrip("/")
        parsed = urllib.parse.urlparse(normalized)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("ComfyUI URL must be an absolute http or https URL.")
        self.base_url = normalized
        self.timeout = timeout

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ComfyUIError(f"ComfyUI returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ComfyUIError(f"Could not reach ComfyUI at {self.base_url}: {exc}") from exc

        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyUIError("ComfyUI returned invalid JSON.") from exc
        if not isinstance(result, dict):
            raise ComfyUIError("ComfyUI returned an unexpected JSON response.")
        return result

    def system_stats(self) -> Dict[str, Any]:
        return self._request_json("/system_stats")

    def object_info(self) -> Dict[str, Any]:
        return self._request_json("/object_info")

    def queue(self) -> Dict[str, Any]:
        return self._request_json("/queue")

    def is_busy(self) -> bool:
        """Return True only while ComfyUI is actively executing a prompt."""
        queue = self.queue()
        return bool(queue.get("queue_running"))

    def submit_prompt(
        self,
        workflow: Dict[str, Any],
        *,
        client_id: Optional[str] = None,
    ) -> str:
        result = self._request_json(
            "/prompt",
            method="POST",
            payload={
                "prompt": workflow,
                "client_id": client_id or str(uuid.uuid4()),
            },
        )
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise ComfyUIError("ComfyUI accepted the request but returned no prompt_id.")
        return str(prompt_id)

    def history(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        result = self._request_json(
            f"/history/{urllib.parse.quote(prompt_id, safe='')}"
        )
        entry = result.get(prompt_id)
        return entry if isinstance(entry, dict) else None

    @staticmethod
    def output_files(history_entry: Dict[str, Any]) -> List[Dict[str, str]]:
        """Collect final output descriptors from all output nodes.

        ComfyUI and custom nodes commonly use keys such as images, gifs, and
        videos. Only descriptors explicitly marked as output files are kept;
        temporary and input artifacts are intentionally excluded.
        """
        found: List[Dict[str, str]] = []
        seen = set()
        outputs = history_entry.get("outputs", {})
        if not isinstance(outputs, dict):
            return found

        for node_id, node_output in outputs.items():
            if not isinstance(node_output, dict):
                continue
            for value in node_output.values():
                if not isinstance(value, list):
                    continue
                for descriptor in value:
                    if not isinstance(descriptor, dict):
                        continue
                    filename = descriptor.get("filename")
                    file_type = descriptor.get("type")
                    if not filename or file_type != "output":
                        continue
                    item = {
                        "filename": str(filename),
                        "subfolder": str(descriptor.get("subfolder") or ""),
                        "type": "output",
                        "node_id": str(node_id),
                    }
                    key = (item["filename"], item["subfolder"], item["type"])
                    if key not in seen:
                        seen.add(key)
                        found.append(item)
        return found

    def download_output(self, descriptor: Dict[str, str], destination: str) -> None:
        query = urllib.parse.urlencode(
            {
                "filename": descriptor["filename"],
                "subfolder": descriptor.get("subfolder", ""),
                "type": descriptor.get("type", "output"),
            }
        )
        request = urllib.request.Request(f"{self.base_url}/view?{query}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                with open(destination, "xb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
        except FileExistsError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ComfyUIError(f"Failed to download {descriptor['filename']}: {exc}") from exc

    def upload_image(
        self,
        path: str,
        *,
        remote_name: Optional[str] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Upload an image to a remote ComfyUI input directory."""
        filename = remote_name or os.path.basename(path)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        boundary = f"----SmartGallery{uuid.uuid4().hex}"
        with open(path, "rb") as source:
            content = source.read()
        fields: Iterable[bytes] = (
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="image"; '
                f'filename="{filename}"\r\n'
            ).encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            content,
            f"\r\n--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="overwrite"\r\n\r\n',
            str(overwrite).lower().encode(),
            f"\r\n--{boundary}--\r\n".encode(),
        )
        request = urllib.request.Request(
            f"{self.base_url}/upload/image",
            data=b"".join(fields),
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ComfyUIError(f"Failed to upload {filename}: {exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyUIError("ComfyUI returned invalid JSON after upload.") from exc
        if not isinstance(result, dict) or not result.get("name"):
            raise ComfyUIError("ComfyUI returned an unexpected upload response.")
        return result
