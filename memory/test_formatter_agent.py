#!/usr/bin/env python3
"""
Test suite for FormatterAgent dual-projection system.

Tests:
- Voice projection (max 4 blocks, SUMMARY first, word limits)
- Dev projection (full output + voice narrator)
- TTS sanitization (markdown removal, symbol cleaning)
- Delivery mode switching

Run: python -m pytest memory/test_formatter_agent.py -v
"""

import pytest
from memory.formatter_agent import (
    FormatterAgent,
    DeliveryMode,
    BlockType,
    SemanticBlock,
    FormatterOutput,
    summary_block,
    status_block,
    risk_block,
    next_block,
    code_block,
    diff_block,
    context_block,
    explanation_block,
    data_block,
)


class TestFormatterAgentVoiceProjection:
    """Tests for voice mode projection."""

    def setup_method(self):
        self.formatter = FormatterAgent()

    def test_voice_filters_to_allowed_types(self):
        """Voice mode should only include SUMMARY, STATUS, RISK, NEXT blocks."""
        blocks = [
            summary_block("This is a summary"),
            code_block("def foo(): pass", language="python"),
            status_block("Status update"),
            diff_block("+ added line"),
        ]

        output = self.formatter.project(blocks, DeliveryMode.VOICE)

        # Should have 2 blocks (summary + status), not 4
        assert len(output.blocks) == 2
        assert "summary" in output.blocks[0].lower() or "This is a summary" in output.blocks[0]
        assert output.mode == DeliveryMode.VOICE

    def test_voice_summary_first(self):
        """SUMMARY blocks should always come first in voice output."""
        blocks = [
            status_block("Status first in list", priority=9),
            risk_block("High priority risk", priority=10),
            summary_block("Summary should be first", priority=1),  # Low priority but still first
        ]

        output = self.formatter.project(blocks, DeliveryMode.VOICE)

        # Summary should be first regardless of priority
        assert "Summary should be first" in output.blocks[0]

    def test_voice_max_4_blocks(self):
        """Voice mode should limit to maximum 4 blocks."""
        blocks = [
            summary_block("Summary 1"),
            status_block("Status 1"),
            risk_block("Risk 1"),
            next_block("Next 1"),
            status_block("Status 2"),  # 5th - should be truncated
            risk_block("Risk 2"),      # 6th - should be truncated
        ]

        output = self.formatter.project(blocks, DeliveryMode.VOICE)

        assert len(output.blocks) <= 4
        assert output.truncated == True

    def test_voice_word_limit(self):
        """Voice mode should truncate at word limit."""
        long_text = " ".join(["word"] * 300)  # 300 words, over 200 limit
        blocks = [summary_block(long_text)]

        output = self.formatter.project(blocks, DeliveryMode.VOICE)

        total_words = sum(len(b.split()) for b in output.blocks)
        assert total_words <= 200
        assert output.truncated == True

    def test_voice_uses_voice_summary(self):
        """Voice mode should prefer voice_summary when available."""
        blocks = [
            risk_block(
                content="This is a very long detailed risk explanation with lots of technical details.",
                voice_summary="Brief risk warning.",
                priority=8
            )
        ]

        output = self.formatter.project(blocks, DeliveryMode.VOICE)

        assert "Brief risk warning" in output.blocks[0]
        assert "very long detailed" not in output.blocks[0]


class TestFormatterAgentDevProjection:
    """Tests for dev mode projection."""

    def setup_method(self):
        self.formatter = FormatterAgent()

    def test_dev_includes_all_block_types(self):
        """Dev mode should include all block types including CODE and DIFF."""
        blocks = [
            summary_block("Summary"),
            code_block("def foo(): pass", language="python"),
            diff_block("+ added line"),
            explanation_block("Explanation details"),
        ]

        output = self.formatter.project(blocks, DeliveryMode.DEV)

        assert len(output.blocks) == 4
        assert output.mode == DeliveryMode.DEV
        assert output.truncated == False

    def test_dev_consistent_ordering(self):
        """Dev mode should order blocks consistently by type."""
        blocks = [
            code_block("code"),
            summary_block("summary"),
            diff_block("diff"),
            status_block("status"),
        ]

        output = self.formatter.project(blocks, DeliveryMode.DEV)

        # Summary should come before code, status before diff
        output_text = "\n".join(output.blocks)
        summary_pos = output_text.find("summary")
        code_pos = output_text.find("code")
        assert summary_pos < code_pos

    def test_dev_includes_voice_narrator(self):
        """Dev mode should generate voice_narrator for audio relay."""
        blocks = [
            summary_block("This is the main summary for voice narration."),
            code_block("def foo(): pass"),
        ]

        output = self.formatter.project(blocks, DeliveryMode.DEV)

        assert hasattr(output, 'voice_narrator')
        assert "main summary" in output.voice_narrator.lower()

    def test_dev_code_formatting(self):
        """Dev mode should format code blocks with syntax highlighting."""
        blocks = [code_block("def hello():\n    print('world')", language="python")]

        output = self.formatter.project(blocks, DeliveryMode.DEV)

        assert "```python" in output.blocks[0]
        assert "```" in output.blocks[0]


class TestTTSSanitization:
    """Tests for TTS text sanitization."""

    def setup_method(self):
        self.formatter = FormatterAgent()

    def test_removes_code_blocks(self):
        """Should remove triple-backtick code blocks."""
        text = "Here's code:\n```python\ndef foo():\n    pass\n```\nThat's it."

        result = self.formatter._sanitize_for_voice(text)

        assert "```" not in result
        assert "def foo" not in result
        assert "That's it" in result

    def test_removes_inline_code_keeps_content(self):
        """Should remove backticks but keep the code content."""
        text = "The function `get_user_id()` returns the ID."

        result = self.formatter._sanitize_for_voice(text)

        assert "`" not in result
        # Content preserved (underscores may be normalized)
        assert "getuserid" in result.lower() or "get user id" in result.lower()

    def test_removes_bold_markdown(self):
        """Should remove ** but keep content."""
        text = "This is **very important** text."

        result = self.formatter._sanitize_for_voice(text)

        assert "**" not in result
        assert "very important" in result

    def test_removes_italic_markdown(self):
        """Should remove * but keep content."""
        text = "This is *emphasized* text."

        result = self.formatter._sanitize_for_voice(text)

        assert "emphasized" in result
        # No dangling asterisks
        assert result.count("*") == 0

    def test_removes_headers(self):
        """Should remove markdown headers (# symbols)."""
        text = "# Main Title\n\nSome content\n\n## Subtitle"

        result = self.formatter._sanitize_for_voice(text)

        assert "#" not in result
        assert "Main Title" in result

    def test_removes_bullet_points(self):
        """Should remove bullet point markers."""
        text = "Items:\n- First item\n- Second item"

        result = self.formatter._sanitize_for_voice(text)

        # Should not start lines with dashes
        assert not any(line.strip().startswith("-") for line in result.split("\n") if line.strip())
        assert "First item" in result

    def test_removes_html_tags(self):
        """Should remove HTML/XML tags."""
        text = "<b>Bold</b> and <code>code</code> text."

        result = self.formatter._sanitize_for_voice(text)

        assert "<" not in result
        assert ">" not in result
        assert "Bold" in result

    def test_replaces_urls(self):
        """Should replace URLs with 'link'."""
        text = "Visit https://example.com/path for more info."

        result = self.formatter._sanitize_for_voice(text)

        assert "https://" not in result
        assert "link" in result.lower()

    def test_removes_brackets(self):
        """Should remove brackets and braces."""
        text = "Status: {task_id: 123} and [important] note."

        result = self.formatter._sanitize_for_voice(text)

        assert "{" not in result
        assert "}" not in result
        assert "[" not in result
        assert "]" not in result

    def test_normalizes_whitespace(self):
        """Should normalize multiple spaces to single space."""
        text = "Too    many     spaces   here."

        result = self.formatter._sanitize_for_voice(text)

        assert "    " not in result
        assert "  " not in result

    def test_ensures_ending_punctuation(self):
        """Should add period if no ending punctuation."""
        text = "This sentence has no period"

        result = self.formatter._sanitize_for_voice(text)

        assert result.endswith(".")

    def test_replaces_underscores_in_identifiers(self):
        """Should replace underscores with spaces in identifiers."""
        text = "The function get_user_id_from_db works."

        result = self.formatter._sanitize_for_voice(text)

        # Should have spaces instead of underscores
        assert "get user" in result.lower() or "get_user" not in result.lower()


class TestConvenienceConstructors:
    """Tests for convenience block constructors."""

    def test_summary_block_defaults(self):
        """Summary block should have high priority by default."""
        block = summary_block("Test summary")

        assert block.type == BlockType.SUMMARY
        assert block.priority == 8
        assert block.content == "Test summary"

    def test_risk_block_with_voice_summary(self):
        """Risk block should accept voice_summary."""
        block = risk_block(
            "Long detailed risk explanation",
            voice_summary="Brief risk"
        )

        assert block.voice_summary == "Brief risk"
        assert block.type == BlockType.RISK

    def test_code_block_metadata(self):
        """Code block should store language and file_path in metadata."""
        block = code_block(
            "print('hello')",
            language="python",
            file_path="/path/to/file.py"
        )

        assert block.metadata["language"] == "python"
        assert block.metadata["file_path"] == "/path/to/file.py"
        assert block.type == BlockType.CODE


class TestFormatterOutput:
    """Tests for FormatterOutput dataclass."""

    def test_as_string(self):
        """as_string should join blocks with separator."""
        output = FormatterOutput(
            mode=DeliveryMode.VOICE,
            blocks=["Block 1", "Block 2", "Block 3"],
            raw_blocks=[],
        )

        result = output.as_string()

        assert "Block 1\n\nBlock 2\n\nBlock 3" == result

    def test_as_string_custom_separator(self):
        """as_string should support custom separator."""
        output = FormatterOutput(
            mode=DeliveryMode.DEV,
            blocks=["A", "B", "C"],
            raw_blocks=[],
        )

        result = output.as_string(separator=" | ")

        assert "A | B | C" == result


class TestDeliveryModeSwitch:
    """Tests for switching between delivery modes."""

    def setup_method(self):
        self.formatter = FormatterAgent()
        self.test_blocks = [
            summary_block("Summary of changes"),
            status_block("Completed successfully"),
            code_block("def main(): pass", language="python"),
            risk_block("Watch for edge cases"),
        ]

    def test_same_blocks_different_output(self):
        """Same blocks should produce different output for different modes."""
        voice_output = self.formatter.project(self.test_blocks, DeliveryMode.VOICE)
        dev_output = self.formatter.project(self.test_blocks, DeliveryMode.DEV)

        # Dev should have more content
        assert len(dev_output.blocks) > len(voice_output.blocks)

        # Voice should not have code
        voice_text = voice_output.as_string()
        assert "def main" not in voice_text

        # Dev should have code
        dev_text = dev_output.as_string()
        assert "def main" in dev_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
