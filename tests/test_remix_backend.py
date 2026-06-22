import sqlite3
import unittest

from remix_backend import (
    RemixBackendError,
    create_backend,
    delete_backend,
    initialize_remix_schema,
    list_backends,
    normalize_backend_url,
    recover_interrupted_jobs,
    reorder_backends,
    update_backend,
)


class RemixBackendSchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.conn.close()

    def test_schema_seeds_existing_single_backend_configuration(self):
        initialize_remix_schema(self.conn, "http://127.0.0.1:8188/")
        backend = self.conn.execute(
            "SELECT name, base_url, priority, enabled FROM remix_backends"
        ).fetchone()
        self.assertEqual(
            backend,
            ("Default ComfyUI", "http://127.0.0.1:8188", 0, 1),
        )

    def test_schema_initialization_is_idempotent(self):
        initialize_remix_schema(self.conn, "http://first:8188")
        initialize_remix_schema(self.conn, "http://second:8188")
        count = self.conn.execute("SELECT COUNT(*) FROM remix_backends").fetchone()[0]
        self.assertEqual(count, 1)

    def test_create_update_and_reorder_backends(self):
        initialize_remix_schema(self.conn, "http://first:8188")
        first_id = self.conn.execute("SELECT id FROM remix_backends").fetchone()[0]
        second_id = create_backend(
            self.conn, "Fast server", "http://fastbox.local:8188/"
        )

        update_backend(
            self.conn,
            second_id,
            name="Fastest server",
            base_url="http://fastbox.local:8188",
            enabled=False,
        )
        reorder_backends(self.conn, [second_id, first_id])

        backends = list_backends(self.conn)
        self.assertEqual(
            [(row[0], row[1], row[3], row[4]) for row in backends],
            [
                (second_id, "Fastest server", 0, 0),
                (first_id, "Default ComfyUI", 1, 1),
            ],
        )

    def test_reorder_requires_every_backend_once(self):
        initialize_remix_schema(self.conn, "http://first:8188")
        first_id = self.conn.execute("SELECT id FROM remix_backends").fetchone()[0]
        create_backend(self.conn, "Second", "http://second:8188")
        with self.assertRaises(RemixBackendError):
            reorder_backends(self.conn, [first_id])

    def test_delete_refuses_backend_with_active_job(self):
        initialize_remix_schema(self.conn, "http://first:8188")
        backend_id = self.conn.execute("SELECT id FROM remix_backends").fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO remix_jobs
                (id, backend_id, workflow_json, status, created_at, updated_at)
            VALUES ('job-1', ?, '{}', 'running', 1, 1)
            """,
            (backend_id,),
        )
        with self.assertRaises(RemixBackendError):
            delete_backend(self.conn, backend_id)

    def test_url_validation_rejects_credentials_and_non_http(self):
        for value in ("file:///tmp/comfy", "http://user:pass@comfy:8188"):
            with self.subTest(value=value):
                with self.assertRaises(RemixBackendError):
                    normalize_backend_url(value)

    def test_restart_recovery_preserves_remote_prompt_tracking(self):
        initialize_remix_schema(self.conn, "http://first:8188")
        backend_id = self.conn.execute("SELECT id FROM remix_backends").fetchone()[0]
        self.conn.executemany(
            """
            INSERT INTO remix_jobs
                (id, backend_id, comfy_prompt_id, workflow_json, status,
                 created_at, updated_at)
            VALUES (?, ?, ?, '{}', ?, 1, 1)
            """,
            [
                ("unsubmitted", backend_id, None, "dispatching"),
                ("remote-running", backend_id, "prompt-1", "running"),
                ("downloading", backend_id, "prompt-2", "downloading"),
            ],
        )
        recover_interrupted_jobs(self.conn)
        statuses = dict(
            self.conn.execute("SELECT id, status FROM remix_jobs").fetchall()
        )
        self.assertEqual(statuses["unsubmitted"], "pending")
        self.assertEqual(statuses["remote-running"], "submitted")
        self.assertEqual(statuses["downloading"], "submitted")


if __name__ == "__main__":
    unittest.main()
