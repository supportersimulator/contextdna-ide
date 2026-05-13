"""
FormatterAgent - Semantic block formatter for multi-modal delivery.

Transforms raw agent output into structured semantic blocks that can be
projected to different delivery modes (VOICE for phone, DEV for Claude Code).

Architecture:
- Agents emit SemanticBlocks with type hints
- FormatterAgent projects blocks based on DeliveryMode
- Voice mode: terse, spoken-friendly
- Dev mode: rich, detailed, includes code/diffs
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Dict, Any


class DeliveryMode(Enum):
    """Target output modality."""
    VOICE = auto()  # Phone/spoken interface - terse, no code
    DEV = auto()    # Claude Code/IDE - rich, detailed


class BlockType(Enum):
    """Semantic content categories."""
    SUMMARY = auto()      # High-level overview (both modes)
    STATUS = auto()       # Current state/progress (both modes)
    RISK = auto()         # Warnings/concerns (both modes, voice-condensed)
    NEXT = auto()         # Recommended actions (both modes)
    CONTEXT = auto()      # Background info (dev only usually)
    EXPLANATION = auto()  # Detailed reasoning (dev only)
    DATA = auto()         # Structured data/metrics (dev only)
    DIFF = auto()         # Code changes (dev only)
    CODE = auto()         # Code snippets (dev only)


@dataclass
class SemanticBlock:
    """
    A typed content block with metadata for formatting decisions.

    Attributes:
        type: BlockType indicating content category
        content: The actual content (string, dict, or list)
        priority: 0-10, higher = more important (affects voice truncation)
        voice_summary: Optional pre-condensed version for voice mode
        metadata: Additional context (file paths, line numbers, etc.)
    """
    type: BlockType
    content: Any
    priority: int = 5
    voice_summary: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FormatterOutput:
    """
    Formatted output ready for delivery.

    Attributes:
        mode: The DeliveryMode this was formatted for
        blocks: Ordered list of formatted content strings
        raw_blocks: Original SemanticBlocks (for debugging/logging)
        token_estimate: Approximate token count
        truncated: Whether content was truncated for mode constraints
    """
    mode: DeliveryMode
    blocks: List[str]
    raw_blocks: List[SemanticBlock]
    token_estimate: int = 0
    truncated: bool = False

    def as_string(self, separator: str = "\n\n") -> str:
        """Combine blocks into single output string."""
        return separator.join(self.blocks)


class FormatterAgent:
    """
    Projects semantic blocks to delivery-appropriate format.

    Design principles:
    - VOICE: Max ~200 words, no code, spoken-friendly
    - DEV: Full detail, includes diffs/code, markdown formatting

    Usage:
        formatter = FormatterAgent()
        blocks = [
            SemanticBlock(BlockType.SUMMARY, "Fixed async bug in LLM service"),
            SemanticBlock(BlockType.CODE, "def foo(): ...", priority=3),
        ]
        voice_out = formatter.project(blocks, DeliveryMode.VOICE)
        dev_out = formatter.project(blocks, DeliveryMode.DEV)
    """

    # Voice mode constraints
    VOICE_MAX_WORDS = 200
    VOICE_ALLOWED_TYPES = {
        BlockType.SUMMARY,
        BlockType.STATUS,
        BlockType.RISK,
        BlockType.NEXT,
    }

    def __init__(self, voice_max_words: int = 200):
        """
        Initialize formatter with optional constraints.

        Args:
            voice_max_words: Maximum words for voice output
        """
        self.voice_max_words = voice_max_words

    def project(
        self,
        blocks: List[SemanticBlock],
        mode: DeliveryMode
    ) -> FormatterOutput:
        """
        Project semantic blocks to target delivery mode.

        Args:
            blocks: List of SemanticBlocks to format
            mode: Target DeliveryMode

        Returns:
            FormatterOutput ready for delivery
        """
        if mode == DeliveryMode.VOICE:
            return self._project_voice(blocks)
        else:
            return self._project_dev(blocks)

    def _project_voice(self, blocks: List[SemanticBlock]) -> FormatterOutput:
        """
        Project blocks to voice-friendly format.

        Logos Voice Rules:
        - Max 4 blocks total
        - SUMMARY block always first (mandatory if present)
        - Max ~200 words (~12 seconds spoken at 150wpm)
        - No code, no markdown, no symbols
        - Each block should be a complete spoken sentence

        Priority order: SUMMARY > RISK > STATUS > NEXT
        """
        formatted_blocks = []
        truncated = False
        total_words = 0

        # Filter to voice-allowed types
        voice_blocks = [
            b for b in blocks
            if b.type in self.VOICE_ALLOWED_TYPES
        ]

        # Separate SUMMARY blocks (must come first)
        summary_blocks = [b for b in voice_blocks if b.type == BlockType.SUMMARY]
        other_blocks = [b for b in voice_blocks if b.type != BlockType.SUMMARY]

        # Sort other blocks by priority (highest first)
        other_blocks.sort(key=lambda b: b.priority, reverse=True)

        # Combine: SUMMARY first, then others
        ordered_blocks = summary_blocks + other_blocks

        # Process blocks with word limit and max 4 blocks
        for block in ordered_blocks:
            if len(formatted_blocks) >= 4:
                truncated = True
                break

            # Get content (prefer voice_summary if available)
            content = block.voice_summary if block.voice_summary else str(block.content)

            # Sanitize for TTS
            content = self._sanitize_for_voice(content)

            # Count words
            word_count = len(content.split())

            # Check word limit
            if total_words + word_count > self.voice_max_words:
                # Try to fit partial content
                remaining_words = self.voice_max_words - total_words
                if remaining_words > 10:  # Only include if meaningful
                    words = content.split()[:remaining_words]
                    content = " ".join(words)
                    if not content.endswith((".", "!", "?")):
                        content = content.rstrip(",;:") + "."
                    formatted_blocks.append(content)
                    total_words += remaining_words
                truncated = True
                break

            formatted_blocks.append(content)
            total_words += word_count

        return FormatterOutput(
            mode=DeliveryMode.VOICE,
            blocks=formatted_blocks,
            raw_blocks=blocks,
            token_estimate=self._estimate_tokens(formatted_blocks),
            truncated=truncated,
        )

    def _sanitize_for_voice(self, text: str) -> str:
        """
        Clean text for TTS synthesis.

        Removes markdown, code blocks, special chars that TTS vocalizes.
        """
        import re

        # Remove code blocks (triple backticks)
        text = re.sub(r'```[\s\S]*?```', '', text)

        # Remove inline code (single backticks) - keep content
        text = re.sub(r'`([^`]+)`', r'\1', text)

        # Remove bold/italic markdown - keep content
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'__([^_]+)__', r'\1', text)
        text = re.sub(r'_([^_]+)_', r'\1', text)

        # Remove markdown headers
        text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)

        # Remove bullet points - keep text
        text = re.sub(r'^[\s]*[-*+]\s+', '', text, flags=re.MULTILINE)

        # Remove numbered lists - keep text
        text = re.sub(r'^[\s]*\d+\.\s+', '', text, flags=re.MULTILINE)

        # Remove HTML/XML tags
        text = re.sub(r'<[^>]+>', '', text)

        # Replace URLs with "link"
        text = re.sub(r'https?://\S+', 'link', text)

        # Remove brackets and braces
        text = re.sub(r'[\[\]{}()]', '', text)

        # Replace underscores in identifiers with spaces
        text = re.sub(r'(\w)_(\w)', r'\1 \2', text)

        # Normalize whitespace
        text = re.sub(r'\s+', ' ', text)

        # Ensure ends with proper punctuation
        text = text.strip()
        if text and not text[-1] in '.!?':
            text += '.'

        return text

    def _project_dev(self, blocks: List[SemanticBlock]) -> FormatterOutput:
        """
        Project blocks to developer-friendly format.

        Dev mode features:
        - All block types included
        - Full markdown formatting
        - Code blocks with syntax highlighting
        - ALSO generates voice_narrator summary (first 2 sentences of SUMMARY)

        Output structure:
        - formatted_blocks: Full dev content
        - metadata.voice_narrator: Spoken summary for audio relay
        """
        formatted_blocks = []

        # All blocks allowed in dev mode
        # Sort by type for consistent ordering
        type_order = [
            BlockType.SUMMARY,
            BlockType.STATUS,
            BlockType.RISK,
            BlockType.NEXT,
            BlockType.CONTEXT,
            BlockType.EXPLANATION,
            BlockType.DATA,
            BlockType.DIFF,
            BlockType.CODE,
        ]

        sorted_blocks = sorted(
            blocks,
            key=lambda b: type_order.index(b.type) if b.type in type_order else 99
        )

        # Format all blocks for dev display
        for block in sorted_blocks:
            formatted_blocks.append(self._format_dev_block(block))

        # Extract voice narrator (first ~50 words from SUMMARY for audio relay)
        voice_narrator = self._extract_voice_narrator(blocks)

        output = FormatterOutput(
            mode=DeliveryMode.DEV,
            blocks=formatted_blocks,
            raw_blocks=blocks,
            token_estimate=self._estimate_tokens(formatted_blocks),
            truncated=False,
        )

        # Attach voice narrator for audio relay in dev mode
        output.voice_narrator = voice_narrator

        return output

    def _extract_voice_narrator(self, blocks: List[SemanticBlock]) -> str:
        """
        Extract brief spoken summary for dev mode audio relay.

        Takes first ~50 words from SUMMARY block, sanitized for TTS.
        Used as "narrator" voice while dev sees full visual output.
        """
        # Find summary blocks
        summary_blocks = [b for b in blocks if b.type == BlockType.SUMMARY]
        if not summary_blocks:
            return ""

        # Get first summary content
        content = summary_blocks[0].voice_summary or str(summary_blocks[0].content)

        # Sanitize for voice
        content = self._sanitize_for_voice(content)

        # Limit to ~50 words (about 20 seconds)
        words = content.split()
        if len(words) > 50:
            content = " ".join(words[:50])
            if not content.endswith((".", "!", "?")):
                content = content.rstrip(",;:") + "."

        return content

    def _format_dev_block(self, block: SemanticBlock) -> str:
        """
        Format a single block for dev mode.

        TODO: Implement per-type formatting
        """
        # Placeholder formatting
        type_headers = {
            BlockType.SUMMARY: "## Summary",
            BlockType.STATUS: "## Status",
            BlockType.RISK: "## Risks",
            BlockType.NEXT: "## Next Steps",
            BlockType.CONTEXT: "## Context",
            BlockType.EXPLANATION: "## Explanation",
            BlockType.DATA: "## Data",
            BlockType.DIFF: "## Changes",
            BlockType.CODE: "## Code",
        }

        header = type_headers.get(block.type, f"## {block.type.name}")
        content = str(block.content)

        # Code blocks get special formatting
        if block.type == BlockType.CODE:
            lang = block.metadata.get("language", "")
            content = f"```{lang}\n{content}\n```"
        elif block.type == BlockType.DIFF:
            content = f"```diff\n{content}\n```"

        return f"{header}\n\n{content}"

    def _estimate_tokens(self, blocks: List[str]) -> int:
        """
        Rough token estimate (words * 1.3).

        TODO: Use tiktoken for accuracy if needed
        """
        total_words = sum(len(b.split()) for b in blocks)
        return int(total_words * 1.3)


# Convenience constructors for common block patterns
def summary_block(content: str, priority: int = 8) -> SemanticBlock:
    """Create a summary block (high priority by default)."""
    return SemanticBlock(
        type=BlockType.SUMMARY,
        content=content,
        priority=priority,
    )


def status_block(content: str, priority: int = 6) -> SemanticBlock:
    """Create a status block."""
    return SemanticBlock(
        type=BlockType.STATUS,
        content=content,
        priority=priority,
    )


def risk_block(
    content: str,
    voice_summary: Optional[str] = None,
    priority: int = 7
) -> SemanticBlock:
    """Create a risk/warning block (high priority for voice)."""
    return SemanticBlock(
        type=BlockType.RISK,
        content=content,
        priority=priority,
        voice_summary=voice_summary,
    )


def next_block(content: str, priority: int = 6) -> SemanticBlock:
    """Create a next-steps block."""
    return SemanticBlock(
        type=BlockType.NEXT,
        content=content,
        priority=priority,
    )


def code_block(
    content: str,
    language: str = "",
    file_path: Optional[str] = None,
    priority: int = 3
) -> SemanticBlock:
    """Create a code block (dev-only, low priority for voice filtering)."""
    metadata = {"language": language}
    if file_path:
        metadata["file_path"] = file_path
    return SemanticBlock(
        type=BlockType.CODE,
        content=content,
        priority=priority,
        metadata=metadata,
    )


def diff_block(
    content: str,
    file_path: Optional[str] = None,
    priority: int = 4
) -> SemanticBlock:
    """Create a diff block (dev-only)."""
    metadata = {}
    if file_path:
        metadata["file_path"] = file_path
    return SemanticBlock(
        type=BlockType.DIFF,
        content=content,
        priority=priority,
        metadata=metadata,
    )


def context_block(content: str, priority: int = 4) -> SemanticBlock:
    """Create a context/background block (dev-only usually)."""
    return SemanticBlock(
        type=BlockType.CONTEXT,
        content=content,
        priority=priority,
    )


def explanation_block(content: str, priority: int = 3) -> SemanticBlock:
    """Create an explanation block (dev-only)."""
    return SemanticBlock(
        type=BlockType.EXPLANATION,
        content=content,
        priority=priority,
    )


def data_block(
    content: Any,
    label: Optional[str] = None,
    priority: int = 4
) -> SemanticBlock:
    """Create a data/metrics block (dev-only)."""
    metadata = {}
    if label:
        metadata["label"] = label
    return SemanticBlock(
        type=BlockType.DATA,
        content=content,
        priority=priority,
        metadata=metadata,
    )
