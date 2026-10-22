import unittest

from weitblick.agent import ocr

TSV = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
1\t1\t0\t0\t0\t0\t0\t0\t2560\t1600\t-1\t
2\t1\t1\t0\t0\t0\t100\t40\t300\t30\t-1\t
5\t1\t2\t1\t1\t1\t400\t200\t80\t24\t91.5\tAbbrechen
5\t1\t1\t1\t1\t2\t180\t40\t120\t30\t90\tspeichern
5\t1\t1\t1\t1\t1\t100\t40\t70\t30\t96\tNicht
5\t1\t1\t1\t1\t3\t310\t40\t10\t30\t-1\t \t
"""


class ParseTSVTests(unittest.TestCase):
    def test_words_become_lines_in_desktop_pixels(self):
        lines = ocr.parse_tsv(TSV, offset=(10, 20), scale=2)
        self.assertEqual([line.text for line in lines], ["Nicht speichern", "Abbrechen"])
        first = lines[0]
        # (100, 40) .. (300, 70) in the 2× image = (50, 20) .. (150, 35) + offset.
        self.assertEqual(first.box, (60, 40, 100, 15))
        self.assertAlmostEqual(first.confidence, 0.93)
        self.assertEqual([(w.text, w.start, w.end) for w in first.words], [("Nicht", 0, 5), ("speichern", 6, 15)])

    def test_box_of_a_range(self):
        line = ocr.parse_tsv(TSV)[0]
        self.assertEqual(line.box_of_range(6, 15), (180, 40, 120, 30))
        self.assertEqual(line.box_of_range(0, 15), line.box)
        # Half a word: cut proportionally.
        left, top, width, height = line.box_of_range(6, 10)
        self.assertAlmostEqual(left, 180)
        self.assertAlmostEqual(width, 120 * 4 / 9)
        self.assertIsNone(line.box_of_range(5, 6))  # the space between words

    def test_garbage_is_skipped(self):
        self.assertEqual(ocr.parse_tsv(""), [])
        self.assertEqual(ocr.parse_tsv("header\n5\t1\tbroken"), [])

    def test_upscale_factor(self):
        self.assertEqual(ocr.upscale_factor(1280, 800), 2)
        self.assertEqual(ocr.upscale_factor(2560, 1600), 1)

    @unittest.skipIf(ocr.tesseract_path() is not None, "Tesseract is installed")
    def test_missing_tesseract_explains_how_to_install_it(self):
        with self.assertRaises(ocr.OCRUnavailable) as raised:
            ocr.recognize(b"")
        self.assertIn("apt install tesseract-ocr", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
