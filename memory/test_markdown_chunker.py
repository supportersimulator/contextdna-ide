"""Tests for markdown_chunker — heading-based section splitting."""

import unittest
from memory.markdown_chunker import chunk, slugify, Section


class TestSlugify(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_special_chars(self):
        self.assertEqual(slugify("G1: Event Bridge (kernel↔Cap)"), "g1-event-bridge-kernelcap")

    def test_empty(self):
        self.assertEqual(slugify(""), "untitled")

    def test_hyphens_collapsed(self):
        self.assertEqual(slugify("one -- two --- three"), "one-two-three")

    def test_numbers_preserved(self):
        self.assertEqual(slugify("Section 15.3"), "section-153")


class TestChunkEmpty(unittest.TestCase):
    def test_empty_string(self):
        sections = chunk("")
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].heading, "Introduction")
        self.assertEqual(sections[0].heading_level, 0)

    def test_whitespace_only(self):
        sections = chunk("   \n  \n  ")
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].heading, "Introduction")


class TestChunkNoHeadings(unittest.TestCase):
    def test_plain_text(self):
        content = "This is just plain text.\nNo headings here.\nThree lines."
        sections = chunk(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].heading, "Introduction")
        self.assertEqual(sections[0].heading_level, 0)
        self.assertIn("plain text", sections[0].content)
        self.assertEqual(sections[0].start_line, 1)
        self.assertEqual(sections[0].end_line, 3)


class TestChunkSingleHeading(unittest.TestCase):
    def test_h1_only(self):
        content = "# Title\nSome content here."
        sections = chunk(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].heading, "Title")
        self.assertEqual(sections[0].heading_level, 1)
        self.assertIn("Some content", sections[0].content)

    def test_h2_only(self):
        content = "## Subtitle\nBody text."
        sections = chunk(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].heading, "Subtitle")
        self.assertEqual(sections[0].heading_level, 2)


class TestChunkMultipleHeadings(unittest.TestCase):
    def test_two_h1s(self):
        content = "# First\nContent A\n# Second\nContent B"
        sections = chunk(content)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0].heading, "First")
        self.assertEqual(sections[1].heading, "Second")
        self.assertIn("Content A", sections[0].content)
        self.assertIn("Content B", sections[1].content)

    def test_mixed_levels(self):
        content = "# H1\nIntro\n## H2\nDetail\n### H3\nDeep detail\n## Another H2\nMore"
        sections = chunk(content)
        self.assertEqual(len(sections), 4)
        self.assertEqual(sections[0].heading_level, 1)
        self.assertEqual(sections[1].heading_level, 2)
        self.assertEqual(sections[2].heading_level, 3)
        self.assertEqual(sections[3].heading_level, 2)

    def test_h6_detected(self):
        content = "###### Deep Heading\nContent"
        sections = chunk(content)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].heading_level, 6)
        self.assertEqual(sections[0].heading, "Deep Heading")


class TestChunkIntroduction(unittest.TestCase):
    def test_content_before_first_heading(self):
        content = "Preamble text\nMore preamble\n# Actual Section\nContent"
        sections = chunk(content)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0].heading, "Introduction")
        self.assertEqual(sections[0].heading_level, 0)
        self.assertIn("Preamble", sections[0].content)
        self.assertEqual(sections[1].heading, "Actual Section")

    def test_empty_lines_before_heading(self):
        content = "\n\n# Title\nContent"
        sections = chunk(content)
        # Empty whitespace before heading — no intro section
        self.assertEqual(sections[0].heading, "Title")


class TestChunkLineNumbers(unittest.TestCase):
    def test_line_numbers_accurate(self):
        content = "# First\nLine 2\nLine 3\n# Second\nLine 5\nLine 6"
        sections = chunk(content)
        self.assertEqual(sections[0].start_line, 1)
        self.assertEqual(sections[0].end_line, 3)  # end_line = next heading 0-based index
        self.assertEqual(sections[1].start_line, 4)  # 1-indexed: line 4 = "# Second"
        self.assertEqual(sections[1].end_line, 6)

    def test_last_section_extends_to_eof(self):
        content = "# Only\nLine 2\nLine 3\nLine 4"
        sections = chunk(content)
        self.assertEqual(sections[0].end_line, 4)


class TestChunkEdgeCases(unittest.TestCase):
    def test_heading_no_content(self):
        content = "# Empty\n# Also Empty"
        sections = chunk(content)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0].heading, "Empty")
        self.assertEqual(sections[1].heading, "Also Empty")

    def test_hash_in_content_not_heading(self):
        content = "# Real Heading\nSome text with #hashtag and ## not heading"
        sections = chunk(content)
        self.assertEqual(len(sections), 1)  # ## not at line start

    def test_code_fence_hashes_ignored(self):
        # Hashes at line start inside non-fenced context are headings
        # but "###" without space is not a heading
        content = "# Title\n###no space\nNormal line"
        sections = chunk(content)
        self.assertEqual(len(sections), 1)  # ###no space is NOT a heading (no space after #)


if __name__ == "__main__":
    unittest.main()
