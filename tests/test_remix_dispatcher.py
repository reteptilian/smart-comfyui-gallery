import os
import sqlite3
import tempfile
import unittest

from remix_backend import create_backend, create_remix_job, initialize_remix_schema
from remix_dispatcher import RemixDispatcher


class FakeComfyClient:
    states = {}

    def __init__(self, base_url):
        self.base_url = base_url
        self.state = self.states[base_url]

    def queue(self):
        return self.state.get(
            "queue", {"queue_running": [], "queue_pending": []}
        )

    def system_stats(self):
        return {"ok": True}

    def upload_image(self, path, remote_name=None, overwrite=False):
        self.state.setdefault("uploads", []).append((path, remote_name))
        return {"name": remote_name}

    def submit_prompt(self, workflow, client_id=None):
        self.state["submitted_workflow"] = workflow
        return self.state.get("prompt_id", "prompt-1")

    def history(self, prompt_id):
        return self.state.get("history")

    def output_files(self, history):
        return history["descriptors"]

    def download_output(self, descriptor, destination):
        with open(destination, "xb") as output:
            output.write(descriptor.get("content", b"output"))


class RemixDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "gallery.sqlite")
        self.output_dir = os.path.join(self.tempdir.name, "output", "Remix")
        os.makedirs(self.output_dir)
        with self.connect() as conn:
            initialize_remix_schema(conn, "")
            self.slow_id = create_backend(conn, "Slow", "http://slow:8188")
            self.fast_id = create_backend(conn, "Fast", "http://fast:8188")
            # Fast should be tried first.
            from remix_backend import reorder_backends

            reorder_backends(conn, [self.fast_id, self.slow_id])
            conn.commit()
        FakeComfyClient.states = {
            "http://fast:8188": {
                "queue": {"queue_running": [], "queue_pending": []},
                "prompt_id": "fast-prompt",
            },
            "http://slow:8188": {
                "queue": {"queue_running": [], "queue_pending": []},
                "prompt_id": "slow-prompt",
            },
        }
        self.indexed = []
        self.dispatcher = RemixDispatcher(
            self.connect,
            self.output_dir,
            client_factory=FakeComfyClient,
            index_outputs=lambda paths: self.indexed.extend(paths),
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def create_job(self, input_files=None):
        with self.connect() as conn:
            job_id = create_remix_job(
                conn,
                {
                    "1": {
                        "class_type": "LoadImage",
                        "inputs": {"image": "old.png"},
                    }
                },
                input_files=input_files,
            )
            conn.commit()
        return job_id

    def test_dispatches_to_highest_priority_available_backend(self):
        job_id = self.create_job()
        self.dispatcher.run_once()
        with self.connect() as conn:
            job = conn.execute(
                "SELECT * FROM remix_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        self.assertEqual(job["backend_id"], self.fast_id)
        self.assertEqual(job["status"], "submitted")
        self.assertEqual(job["comfy_prompt_id"], "fast-prompt")

    def test_skips_backend_with_remote_queue(self):
        FakeComfyClient.states["http://fast:8188"]["queue"] = {
            "queue_running": [],
            "queue_pending": [["someone-else"]],
        }
        job_id = self.create_job()
        self.dispatcher.run_once()
        with self.connect() as conn:
            job = conn.execute(
                "SELECT backend_id FROM remix_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        self.assertEqual(job["backend_id"], self.slow_id)

    def test_uploads_input_rewrites_workflow_and_downloads_result(self):
        input_path = os.path.join(self.tempdir.name, "input.png")
        with open(input_path, "wb") as image:
            image.write(b"input")
        job_id = self.create_job(
            [
                {
                    "local_path": input_path,
                    "remote_name": "remote-input.png",
                    "node_id": "1",
                    "key": "image",
                }
            ]
        )

        self.dispatcher.run_once()
        submitted = FakeComfyClient.states["http://fast:8188"]["submitted_workflow"]
        self.assertEqual(submitted["1"]["inputs"]["image"], "remote-input.png")

        FakeComfyClient.states["http://fast:8188"]["history"] = {
            "status": {"completed": True},
            "descriptors": [
                {
                    "filename": "result.png",
                    "subfolder": "",
                    "type": "output",
                    "node_id": "9",
                    "content": b"generated",
                }
            ],
        }
        self.dispatcher.run_once()

        with self.connect() as conn:
            job = conn.execute(
                "SELECT status, output_files_json FROM remix_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(self.indexed), 1)
        self.assertTrue(os.path.exists(self.indexed[0]))
        self.assertFalse(os.path.exists(input_path))

        # Simulate a restart after the final file was published but before the
        # completion state was durably retained. The deterministic name is reused.
        with self.connect() as conn:
            conn.execute(
                "UPDATE remix_jobs SET status = 'submitted' WHERE id = ?",
                (job_id,),
            )
            conn.commit()
        self.dispatcher.run_once()
        visible_files = [
            name for name in os.listdir(self.output_dir) if not name.startswith(".")
        ]
        self.assertEqual(len(visible_files), 1)


if __name__ == "__main__":
    unittest.main()
