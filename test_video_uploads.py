import unittest

import bot


class TestTelegramVideoUploadHelpers(unittest.TestCase):
    def test_detects_video_document_by_mime_type(self):
        self.assertTrue(bot.is_telegram_video_document({
            "file_name": "clip.bin",
            "mime_type": "video/mp4",
        }))

    def test_detects_video_document_by_extension(self):
        self.assertTrue(bot.is_telegram_video_document({
            "file_name": "screen-recording.MOV",
            "mime_type": "application/octet-stream",
        }))

    def test_rejects_non_video_document(self):
        self.assertFalse(bot.is_telegram_video_document({
            "file_name": "notes.pdf",
            "mime_type": "application/pdf",
        }))

    def test_video_filename_adds_extension_from_mime(self):
        self.assertEqual(
            "video.mp4",
            bot.telegram_video_filename({"mime_type": "video/mp4"}),
        )

    def test_video_prompt_includes_extracted_frames_and_caption(self):
        prompt = bot.build_video_analysis_prompt(
            "/tmp/uploaded.mp4",
            "What is happening here?",
            frame_paths=["/tmp/frame_01.jpg", "/tmp/frame_02.jpg"],
        )

        self.assertIn("[User uploaded a video: /tmp/uploaded.mp4]", prompt)
        self.assertIn("/tmp/frame_01.jpg", prompt)
        self.assertIn("/tmp/frame_02.jpg", prompt)
        self.assertTrue(prompt.endswith("What is happening here?"))

    def test_video_prompt_falls_back_to_video_instructions(self):
        prompt = bot.build_video_analysis_prompt(
            "/tmp/uploaded.mp4",
            "",
            frame_paths=[],
        )

        self.assertIn("Please analyze this video.", prompt)
        self.assertIn("ffmpeg/ffprobe", prompt)


if __name__ == "__main__":
    unittest.main()
