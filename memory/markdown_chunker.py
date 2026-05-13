"""
Markdown Chunker — Heading-based section splitting for the Markdown Memory Layer.

Splits markdown content at heading boundaries (# through ######).
Each section includes its heading + all content until the next heading of same or higher level.
Content before the first heading becomes an "Introduction" section (level 0).
"""

import re
from dataclasses import dataclass
from typing import List


@dataclass
class Section:
    heading: str
    heading_level: int
    content: str
    start_line: int
    end_line: int


# Match lines starting with 1-6 # characters followed by a space
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def slugify(text: str) -> str:
    """Convert heading text to a URL-friendly slug.

    Lowercase, spaces to hyphens, strip non-alphanumeric (except hyphens).
    """
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


def chunk(content: str) -> List[Section]:
    """Split markdown content into sections at heading boundaries.

    Returns a list of Section objects. Content before the first heading
    becomes a section with heading="Introduction" and level=0.
    """
    if not content or not content.strip():
        return [Section(
            heading="Introduction",
            heading_level=0,
            content="",
            start_line=1,
            end_line=1,
        )]

    lines = content.split("\n")

    # Find all heading positions
    headings: List[tuple] = []  # (line_index, level, heading_text)
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            headings.append((i, level, text))

    # No headings — entire content is one "Introduction" section
    if not headings:
        return [Section(
            heading="Introduction",
            heading_level=0,
            content=content,
            start_line=1,
            end_line=len(lines),
        )]

    sections: List[Section] = []

    # Content before first heading → Introduction section
    first_heading_idx = headings[0][0]
    if first_heading_idx > 0:
        intro_lines = lines[:first_heading_idx]
        intro_content = "\n".join(intro_lines)
        if intro_content.strip():
            sections.append(Section(
                heading="Introduction",
                heading_level=0,
                content=intro_content,
                start_line=1,
                end_line=first_heading_idx,
            ))

    # Build sections from headings
    for idx, (line_idx, level, text) in enumerate(headings):
        # Section content runs from this heading to the next heading of same or higher level
        # (or to the next heading at all — simple split at every heading boundary)
        if idx + 1 < len(headings):
            next_line_idx = headings[idx + 1][0]
        else:
            next_line_idx = len(lines)

        section_lines = lines[line_idx:next_line_idx]
        section_content = "\n".join(section_lines)

        sections.append(Section(
            heading=text,
            heading_level=level,
            content=section_content,
            start_line=line_idx + 1,  # 1-indexed
            end_line=next_line_idx,   # 1-indexed (exclusive becomes inclusive of last content line)
        ))

    return sections
