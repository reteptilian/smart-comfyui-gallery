import unittest

from comfyui_client import ComfyUIClient


class ComfyUIClientTests(unittest.TestCase):
    def test_rejects_non_http_url(self):
        with self.assertRaises(ValueError):
            ComfyUIClient("file:///tmp/comfy")

    def test_busy_only_counts_running_work(self):
        client = ComfyUIClient("http://comfy.test")
        client.queue = lambda: {"queue_running": [], "queue_pending": [["queued"]]}
        self.assertFalse(client.is_busy())

        client.queue = lambda: {"queue_running": [["running"]], "queue_pending": []}
        self.assertTrue(client.is_busy())

    def test_output_files_include_final_images_and_videos_only(self):
        history = {
            "outputs": {
                "10": {
                    "images": [
                        {
                            "filename": "image.png",
                            "subfolder": "",
                            "type": "output",
                        },
                        {
                            "filename": "preview.png",
                            "subfolder": "",
                            "type": "temp",
                        },
                    ]
                },
                "20": {
                    "videos": [
                        {
                            "filename": "clip.mp4",
                            "subfolder": "video",
                            "type": "output",
                        }
                    ]
                },
            }
        }

        self.assertEqual(
            ComfyUIClient.output_files(history),
            [
                {
                    "filename": "image.png",
                    "subfolder": "",
                    "type": "output",
                    "node_id": "10",
                },
                {
                    "filename": "clip.mp4",
                    "subfolder": "video",
                    "type": "output",
                    "node_id": "20",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
