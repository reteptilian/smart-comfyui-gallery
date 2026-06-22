"""Durable strict-priority dispatcher for remote ComfyUI backends."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from comfyui_client import ComfyUIClient, ComfyUIError
from remix_backend import recover_interrupted_jobs


class RemixDispatcher:
    def __init__(
        self,
        connection_factory,
        output_directory,
        *,
        client_factory=ComfyUIClient,
        index_outputs=None,
        poll_interval=2.0,
    ):
        self.connection_factory = connection_factory
        self.output_directory = output_directory
        self.client_factory = client_factory
        self.index_outputs = index_outputs or (lambda paths: None)
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        os.makedirs(self.output_directory, exist_ok=True)
        with self.connection_factory() as conn:
            recover_interrupted_jobs(conn)
            conn.commit()
        self._thread = threading.Thread(
            target=self._run,
            name="smartgallery-remix-dispatcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout=5):
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self):
        self._wake_event.set()

    def run_once(self):
        self._monitor_submitted_jobs()
        self._dispatch_one_pending_job()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                print(f"ERROR: Remix dispatcher loop failed: {exc}")
            self._wake_event.wait(self.poll_interval)
            self._wake_event.clear()

    def _monitor_submitted_jobs(self):
        with self.connection_factory() as conn:
            jobs = conn.execute(
                """
                SELECT j.*, b.name AS backend_name, b.base_url
                FROM remix_jobs j
                JOIN remix_backends b ON b.id = j.backend_id
                WHERE j.status IN ('submitted', 'running', 'downloading')
                  AND j.comfy_prompt_id IS NOT NULL
                ORDER BY j.created_at
                """
            ).fetchall()

        for job in jobs:
            try:
                client = self.client_factory(job["base_url"])
                history = client.history(job["comfy_prompt_id"])
                if history is None:
                    self._mark_running_if_active(job, client)
                    continue
                status = history.get("status", {})
                if isinstance(status, dict) and status.get("status_str") == "error":
                    messages = status.get("messages") or []
                    self._fail_job(job["id"], f"ComfyUI execution failed: {messages}")
                    continue
                completed = not isinstance(status, dict) or status.get("completed", True)
                if not completed:
                    self._set_job_status(job["id"], "running")
                    continue
                descriptors = client.output_files(history)
                if not descriptors:
                    self._fail_job(job["id"], "ComfyUI completed without final output files.")
                    continue
                self._download_outputs(job, client, descriptors)
            except ComfyUIError as exc:
                # Transient monitoring failures are retained for restart recovery.
                self._record_job_error(job["id"], str(exc))
                self._update_backend_health(job["backend_id"], "offline", str(exc))
            except Exception as exc:
                self._fail_job(job["id"], str(exc))

    def _mark_running_if_active(self, job, client):
        try:
            queue = client.queue()
        except ComfyUIError:
            raise
        prompt_id = job["comfy_prompt_id"]
        running = queue.get("queue_running") or []
        pending = queue.get("queue_pending") or []
        serialized = json.dumps([running, pending])
        if prompt_id in serialized:
            self._set_job_status(job["id"], "running")

    def _dispatch_one_pending_job(self):
        with self.connection_factory() as conn:
            job = conn.execute(
                """
                SELECT * FROM remix_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if not job:
                return
            backends = conn.execute(
                """
                SELECT * FROM remix_backends
                WHERE enabled = 1
                ORDER BY priority ASC, created_at ASC
                """
            ).fetchall()

        for backend in backends:
            if self._backend_has_active_job(backend["id"]):
                continue
            client = self.client_factory(backend["base_url"])
            try:
                queue = client.queue()
                if queue.get("queue_running") or queue.get("queue_pending"):
                    self._update_backend_health(backend["id"], "busy", None)
                    continue
                client.system_stats()
                self._update_backend_health(backend["id"], "idle", None)
            except (ComfyUIError, ValueError) as exc:
                self._update_backend_health(backend["id"], "offline", str(exc))
                continue

            if not self._claim_job(job["id"], backend["id"]):
                return
            self._submit_claimed_job(job["id"], backend, client)
            return

    def _backend_has_active_job(self, backend_id):
        with self.connection_factory() as conn:
            count = conn.execute(
                """
                SELECT COUNT(*) FROM remix_jobs
                WHERE backend_id = ?
                  AND status IN ('dispatching', 'submitted', 'running', 'downloading')
                """,
                (backend_id,),
            ).fetchone()[0]
        return count > 0

    def _claim_job(self, job_id, backend_id):
        with self.connection_factory() as conn:
            now = time.time()
            cursor = conn.execute(
                """
                UPDATE remix_jobs
                SET status = 'dispatching', backend_id = ?, attempts = attempts + 1,
                    error = NULL, started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (backend_id, now, now, job_id),
            )
            conn.commit()
            return cursor.rowcount == 1

    def _submit_claimed_job(self, job_id, backend, client):
        with self.connection_factory() as conn:
            job = conn.execute(
                "SELECT * FROM remix_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        workflow = copy.deepcopy(json.loads(job["workflow_json"]))
        input_files = json.loads(job["input_files_json"] or "[]")
        try:
            for item in input_files:
                uploaded = client.upload_image(
                    item["local_path"],
                    remote_name=item["remote_name"],
                    overwrite=False,
                )
                node = workflow.get(str(item["node_id"]))
                if not isinstance(node, dict):
                    raise RuntimeError(f"Input node {item['node_id']} no longer exists.")
                node.setdefault("inputs", {})[item["key"]] = uploaded["name"]

            prompt_id = client.submit_prompt(workflow, client_id=f"smartgallery-{job_id}")
            with self.connection_factory() as conn:
                now = time.time()
                conn.execute(
                    """
                    UPDATE remix_jobs
                    SET status = 'submitted', comfy_prompt_id = ?,
                        workflow_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (prompt_id, json.dumps(workflow), now, job_id),
                )
                conn.commit()
            self._update_backend_health(backend["id"], "busy", None)
        except Exception as exc:
            # A submit timeout is ambiguous, so never automatically duplicate it elsewhere.
            self._fail_job(job_id, f"Failed to submit to {backend['name']}: {exc}")

    def _download_outputs(self, job, client, descriptors):
        self._set_job_status(job["id"], "downloading")
        saved_paths = []
        try:
            for index, descriptor in enumerate(descriptors, start=1):
                original = os.path.basename(descriptor["filename"])
                extension = os.path.splitext(original)[1]
                backend_slug = self._slug(job["backend_name"]) or "comfyui"
                timestamp = datetime.fromtimestamp(
                    job["created_at"], timezone.utc
                ).strftime("%Y%m%dT%H%M%SZ")
                stem = (
                    f"{backend_slug}_{timestamp}_{job['id'][:8]}_"
                    f"node{descriptor.get('node_id', 'x')}_{index:02d}"
                )
                final_path = os.path.join(
                    self.output_directory, f"{stem}{extension}"
                )
                if os.path.exists(final_path):
                    saved_paths.append(final_path)
                    continue
                part_path = os.path.join(
                    self.output_directory, f".{stem}.{uuid.uuid4().hex}.part"
                )
                try:
                    client.download_output(descriptor, part_path)
                    final_path = self._publish_output(part_path, final_path)
                finally:
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass
                saved_paths.append(final_path)
            self.index_outputs(saved_paths)
            with self.connection_factory() as conn:
                now = time.time()
                conn.execute(
                    """
                    UPDATE remix_jobs
                    SET status = 'completed', output_files_json = ?, error = NULL,
                        updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(saved_paths), now, now, job["id"]),
                )
                conn.commit()
            self._cleanup_inputs(job)
            self._update_backend_health(job["backend_id"], "idle", None)
        except Exception:
            for path in saved_paths:
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise

    @staticmethod
    def _publish_output(part_path, final_path):
        try:
            os.link(part_path, final_path)
        except FileExistsError:
            # The deterministic job filename was already published before a
            # restart or by another worker; reuse it instead of duplicating it.
            pass
        os.unlink(part_path)
        return final_path

    def _cleanup_inputs(self, job):
        try:
            input_files = json.loads(job["input_files_json"] or "[]")
        except json.JSONDecodeError:
            return
        for item in input_files:
            try:
                os.remove(item["local_path"])
            except OSError:
                pass

    @staticmethod
    def _slug(value):
        return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")

    def _set_job_status(self, job_id, status):
        with self.connection_factory() as conn:
            conn.execute(
                "UPDATE remix_jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), job_id),
            )
            conn.commit()

    def _record_job_error(self, job_id, error):
        with self.connection_factory() as conn:
            conn.execute(
                "UPDATE remix_jobs SET error = ?, updated_at = ? WHERE id = ?",
                (error, time.time(), job_id),
            )
            conn.commit()

    def _fail_job(self, job_id, error):
        with self.connection_factory() as conn:
            now = time.time()
            conn.execute(
                """
                UPDATE remix_jobs
                SET status = 'failed', error = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (error, now, now, job_id),
            )
            conn.commit()

    def _update_backend_health(self, backend_id, status, error):
        with self.connection_factory() as conn:
            now = time.time()
            conn.execute(
                """
                UPDATE remix_backends
                SET health_status = ?, last_checked = ?, last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, now, error, now, backend_id),
            )
            conn.commit()
