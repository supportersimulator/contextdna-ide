#!/usr/bin/env python3
"""
Deduplication Detector for Context DNA Learnings

Smart duplicate detection that:
1. Finds exact matches (same content)
2. Finds semantic duplicates (same meaning, different words)
3. Detects overlapping regex patterns that catch the same inputs
4. Analyzes if patterns offer unique value vs redundancy
5. Smart merge that keeps the BEST from both patterns

Usage:
    python dedup_detector.py              # Interactive menu
    python dedup_detector.py scan         # Quick scan for duplicates
    python dedup_detector.py report       # Full dedup report
    python dedup_detector.py analyze <id1> <id2>  # Deep compare two patterns
    python dedup_detector.py smart-merge <id1> <id2>  # Intelligent merge
"""

import sqlite3
import json
import re
import sys
from pathlib import Path
from difflib import SequenceMatcher
from collections import defaultdict
from datetime import datetime
from typing import Optional, Tuple

DB_PATH = Path(__file__).parent / '.pattern_evolution.db'
REGISTRY_PATH = Path(__file__).parent / '.pattern_registry.db'

# Common synonyms/variations for semantic matching
SEMANTIC_EQUIVALENTS = {
    'excellent': ['great', 'best', 'top', 'premium', 'exceptional', 'outstanding'],
    'critical': ['crucial', 'vital', 'essential', 'important', 'key'],
    'fast': ['quick', 'rapid', 'speedy', 'swift'],
    'careful': ['thorough', 'meticulous', 'detailed', 'precise'],
    'fix': ['repair', 'solve', 'resolve', 'address', 'handle'],
    'create': ['make', 'build', 'generate', 'produce', 'write'],
    'error': ['bug', 'issue', 'problem', 'mistake', 'fault'],
    'check': ['verify', 'validate', 'confirm', 'ensure', 'test'],
}


class DuplicateDetector:
    """Detects and manages duplicate learnings in Context DNA."""

    def __init__(self):
        from memory.db_utils import connect_wal
        self.evolution_db = connect_wal(str(DB_PATH))

        self.registry_db = None
        if REGISTRY_PATH.exists():
            self.registry_db = connect_wal(str(REGISTRY_PATH))

    def close(self):
        self.evolution_db.close()
        if self.registry_db:
            self.registry_db.close()

    def similarity_ratio(self, s1: str, s2: str) -> float:
        """Calculate similarity ratio between two strings."""
        if not s1 or not s2:
            return 0.0
        return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()

    def semantic_similarity(self, s1: str, s2: str) -> float:
        """
        Calculate semantic similarity - do the strings mean the same thing?
        Goes beyond string matching to detect synonyms and equivalent phrases.
        """
        if not s1 or not s2:
            return 0.0

        s1_lower = s1.lower()
        s2_lower = s2.lower()

        # Direct match
        if s1_lower == s2_lower:
            return 1.0

        # Tokenize
        words1 = set(re.findall(r'\b\w+\b', s1_lower))
        words2 = set(re.findall(r'\b\w+\b', s2_lower))

        # Check for synonym overlap
        expanded_words1 = set(words1)
        expanded_words2 = set(words2)

        for word in words1:
            for key, synonyms in SEMANTIC_EQUIVALENTS.items():
                if word == key or word in synonyms:
                    expanded_words1.update(synonyms)
                    expanded_words1.add(key)

        for word in words2:
            for key, synonyms in SEMANTIC_EQUIVALENTS.items():
                if word == key or word in synonyms:
                    expanded_words2.update(synonyms)
                    expanded_words2.add(key)

        # Calculate Jaccard with expanded vocabulary
        if not expanded_words1 or not expanded_words2:
            return self.similarity_ratio(s1, s2)

        intersection = len(expanded_words1 & expanded_words2)
        union = len(expanded_words1 | expanded_words2)

        return intersection / union if union > 0 else 0.0

    def regex_patterns_catch_same_input(self, regex1: str, regex2: str) -> Tuple[float, list]:
        """
        Test if two regex pattern sets would catch the same inputs.
        Returns (overlap_score, example_overlapping_phrases).
        """
        try:
            patterns1 = json.loads(regex1) if regex1 else []
            patterns2 = json.loads(regex2) if regex2 else []
        except json.JSONDecodeError:
            return 0.0, []

        if not patterns1 or not patterns2:
            return 0.0, []

        # Generate test phrases that might match each pattern
        test_phrases = [
            "give your best work",
            "this is critical",
            "make it exceptional",
            "ensure quality",
            "be thorough",
            "check carefully",
            "as if your life depends on it",
            "high stakes",
            "production system",
            "cannot afford errors",
            "make this the best",
            "world class quality",
            "step by step",
            "explain clearly",
            "pretend you are an expert",
        ]

        matched_by_both = []

        for phrase in test_phrases:
            match1 = any(re.search(p, phrase, re.IGNORECASE) for p in patterns1)
            match2 = any(re.search(p, phrase, re.IGNORECASE) for p in patterns2)
            if match1 and match2:
                matched_by_both.append(phrase)

        # Score based on how many test phrases both catch
        if len(test_phrases) == 0:
            return 0.0, []

        overlap_score = len(matched_by_both) / len(test_phrases)
        return overlap_score, matched_by_both

    def find_similar_patterns(self, threshold: float = 0.7) -> list:
        """Find prompt patterns with similar names or descriptions."""
        cursor = self.evolution_db.cursor()
        cursor.execute('''
            SELECT id, pattern_id, name, description, category, regex_patterns
            FROM prompt_patterns
            WHERE is_active = 1
        ''')
        patterns = cursor.fetchall()

        duplicates = []

        for i, p1 in enumerate(patterns):
            for p2 in patterns[i+1:]:
                # Check name similarity
                name_sim = self.similarity_ratio(p1['name'], p2['name'])

                # Check description similarity
                desc_sim = self.similarity_ratio(
                    p1['description'] or '',
                    p2['description'] or ''
                )

                # Check if in same category
                same_category = p1['category'] == p2['category']

                # Check regex overlap
                regex_overlap = self._check_regex_overlap(
                    p1['regex_patterns'],
                    p2['regex_patterns']
                )

                # Calculate overall similarity
                overall_sim = (
                    name_sim * 0.3 +
                    desc_sim * 0.3 +
                    (0.2 if same_category else 0) +
                    regex_overlap * 0.2
                )

                if overall_sim >= threshold:
                    duplicates.append({
                        'pattern1': {
                            'id': p1['id'],
                            'pattern_id': p1['pattern_id'],
                            'name': p1['name'],
                            'category': p1['category']
                        },
                        'pattern2': {
                            'id': p2['id'],
                            'pattern_id': p2['pattern_id'],
                            'name': p2['name'],
                            'category': p2['category']
                        },
                        'similarity': round(overall_sim, 3),
                        'details': {
                            'name_similarity': round(name_sim, 3),
                            'description_similarity': round(desc_sim, 3),
                            'same_category': same_category,
                            'regex_overlap': round(regex_overlap, 3)
                        }
                    })

        return sorted(duplicates, key=lambda x: x['similarity'], reverse=True)

    def _check_regex_overlap(self, regex1: str, regex2: str) -> float:
        """Check if two regex pattern sets have overlapping patterns."""
        try:
            patterns1 = json.loads(regex1) if regex1 else []
            patterns2 = json.loads(regex2) if regex2 else []
        except json.JSONDecodeError:
            return 0.0

        if not patterns1 or not patterns2:
            return 0.0

        # Check for exact matches
        overlap = len(set(patterns1) & set(patterns2))
        total = len(set(patterns1) | set(patterns2))

        return overlap / total if total > 0 else 0.0

    def find_exact_duplicates(self) -> list:
        """Find patterns with exactly matching content."""
        cursor = self.evolution_db.cursor()
        cursor.execute('''
            SELECT
                p1.id as id1, p1.name as name1, p1.pattern_id as pid1,
                p2.id as id2, p2.name as name2, p2.pattern_id as pid2
            FROM prompt_patterns p1
            JOIN prompt_patterns p2 ON p1.id < p2.id
            WHERE p1.regex_patterns = p2.regex_patterns
            AND p1.is_active = 1 AND p2.is_active = 1
        ''')

        return [
            {
                'pattern1': {'id': r['id1'], 'name': r['name1'], 'pattern_id': r['pid1']},
                'pattern2': {'id': r['id2'], 'name': r['name2'], 'pattern_id': r['pid2']},
                'type': 'exact_regex_match'
            }
            for r in cursor.fetchall()
        ]

    def find_category_imbalances(self) -> dict:
        """Find categories with too many or too few patterns."""
        cursor = self.evolution_db.cursor()
        cursor.execute('''
            SELECT category, COUNT(*) as count
            FROM prompt_patterns
            WHERE is_active = 1
            GROUP BY category
            ORDER BY count DESC
        ''')

        results = {r['category']: r['count'] for r in cursor.fetchall()}

        imbalances = {
            'overcrowded': {k: v for k, v in results.items() if v > 5},
            'sparse': {k: v for k, v in results.items() if v == 1},
            'distribution': results
        }

        return imbalances

    def find_unused_patterns(self, min_outcomes: int = 0) -> list:
        """Find patterns that have never been triggered."""
        cursor = self.evolution_db.cursor()
        cursor.execute('''
            SELECT pp.id, pp.pattern_id, pp.name, pp.category,
                   COUNT(ppo.id) as outcome_count
            FROM prompt_patterns pp
            LEFT JOIN prompt_pattern_outcomes ppo ON pp.id = ppo.pattern_id
            WHERE pp.is_active = 1
            GROUP BY pp.id
            HAVING outcome_count <= ?
            ORDER BY outcome_count ASC
        ''', (min_outcomes,))

        return [
            {
                'id': r['id'],
                'pattern_id': r['pattern_id'],
                'name': r['name'],
                'category': r['category'],
                'outcome_count': r['outcome_count']
            }
            for r in cursor.fetchall()
        ]

    def get_pattern_by_id(self, pattern_id: int) -> Optional[dict]:
        """Fetch full pattern data by ID."""
        cursor = self.evolution_db.cursor()
        cursor.execute('''
            SELECT id, pattern_id, name, description, category,
                   regex_patterns, example_phrases, injection_template,
                   is_active, is_protected, created_at
            FROM prompt_patterns
            WHERE id = ?
        ''', (pattern_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            'id': row['id'],
            'pattern_id': row['pattern_id'],
            'name': row['name'],
            'description': row['description'],
            'category': row['category'],
            'regex_patterns': row['regex_patterns'],
            'example_phrases': row['example_phrases'],
            'injection_template': row['injection_template'],
            'is_active': row['is_active'],
            'is_protected': row['is_protected'],
            'created_at': row['created_at']
        }

    def analyze_patterns(self, id1: int, id2: int) -> dict:
        """
        Deep analysis of two patterns to determine if they're duplicates
        and which has unique value.
        """
        p1 = self.get_pattern_by_id(id1)
        p2 = self.get_pattern_by_id(id2)

        if not p1 or not p2:
            return {'error': 'Pattern not found'}

        # Calculate various similarity metrics
        name_sim = self.similarity_ratio(p1['name'], p2['name'])
        desc_sim = self.semantic_similarity(p1['description'] or '', p2['description'] or '')

        # Check regex overlap
        regex_overlap, overlapping_phrases = self.regex_patterns_catch_same_input(
            p1['regex_patterns'], p2['regex_patterns']
        )

        # Check injection template similarity
        template_sim = self.semantic_similarity(
            p1['injection_template'] or '',
            p2['injection_template'] or ''
        )

        # Analyze unique regex patterns
        try:
            regex1 = set(json.loads(p1['regex_patterns']) if p1['regex_patterns'] else [])
            regex2 = set(json.loads(p2['regex_patterns']) if p2['regex_patterns'] else [])
            unique_to_p1 = regex1 - regex2
            unique_to_p2 = regex2 - regex1
            shared_regex = regex1 & regex2
        except json.JSONDecodeError:
            unique_to_p1 = set()
            unique_to_p2 = set()
            shared_regex = set()

        # Overall duplicate score
        overall_duplicate_score = (
            name_sim * 0.15 +
            desc_sim * 0.25 +
            regex_overlap * 0.35 +
            template_sim * 0.25
        )

        # Determine if they offer unique value
        has_unique_value = len(unique_to_p1) > 0 or len(unique_to_p2) > 0

        # Recommendation
        if overall_duplicate_score > 0.85 and not has_unique_value:
            recommendation = 'MERGE: Nearly identical, safe to combine'
        elif overall_duplicate_score > 0.7 and not has_unique_value:
            recommendation = 'LIKELY DUPLICATE: Consider merging'
        elif overall_duplicate_score > 0.5:
            recommendation = 'PARTIAL OVERLAP: Smart merge to keep best of both'
        else:
            recommendation = 'KEEP BOTH: Different purposes'

        return {
            'pattern1': {
                'id': p1['id'],
                'name': p1['name'],
                'category': p1['category'],
                'unique_regex_count': len(unique_to_p1),
                'unique_regex': list(unique_to_p1)[:3]  # First 3
            },
            'pattern2': {
                'id': p2['id'],
                'name': p2['name'],
                'category': p2['category'],
                'unique_regex_count': len(unique_to_p2),
                'unique_regex': list(unique_to_p2)[:3]
            },
            'similarity': {
                'name': round(name_sim, 3),
                'description': round(desc_sim, 3),
                'regex_overlap': round(regex_overlap, 3),
                'template': round(template_sim, 3),
                'overall': round(overall_duplicate_score, 3)
            },
            'shared_regex_count': len(shared_regex),
            'overlapping_test_phrases': overlapping_phrases,
            'has_unique_value': has_unique_value,
            'recommendation': recommendation
        }

    def smart_merge(self, id1: int, id2: int) -> dict:
        """
        Intelligently merge two patterns, keeping the BEST from both:
        - Combined regex patterns (union)
        - Better/longer description
        - More comprehensive injection template
        - Best example phrases from both
        """
        p1 = self.get_pattern_by_id(id1)
        p2 = self.get_pattern_by_id(id2)

        if not p1 or not p2:
            return {'success': False, 'error': 'Pattern not found'}

        cursor = self.evolution_db.cursor()

        try:
            # Combine regex patterns (union - keep all unique patterns)
            regex1 = json.loads(p1['regex_patterns']) if p1['regex_patterns'] else []
            regex2 = json.loads(p2['regex_patterns']) if p2['regex_patterns'] else []
            combined_regex = list(set(regex1) | set(regex2))

            # Keep better description (longer = more detailed)
            desc1 = p1['description'] or ''
            desc2 = p2['description'] or ''
            best_description = desc1 if len(desc1) >= len(desc2) else desc2

            # Keep better injection template (longer = more comprehensive)
            template1 = p1['injection_template'] or ''
            template2 = p2['injection_template'] or ''
            best_template = template1 if len(template1) >= len(template2) else template2

            # Combine example phrases
            examples1 = json.loads(p1['example_phrases']) if p1['example_phrases'] else []
            examples2 = json.loads(p2['example_phrases']) if p2['example_phrases'] else []
            combined_examples = list(set(examples1) | set(examples2))[:6]  # Max 6

            # Decide which pattern to keep as base (more outcomes = more tested)
            cursor.execute('''
                SELECT pattern_id, COUNT(*) as cnt FROM prompt_pattern_outcomes
                WHERE pattern_id IN (?, ?)
                GROUP BY pattern_id
            ''', (id1, id2))
            outcome_counts = {r['pattern_id']: r['cnt'] for r in cursor.fetchall()}

            # Keep the one with more outcomes as base, or first if equal
            keep_id = id1 if outcome_counts.get(id1, 0) >= outcome_counts.get(id2, 0) else id2
            remove_id = id2 if keep_id == id1 else id1

            # Update the kept pattern with merged content
            cursor.execute('''
                UPDATE prompt_patterns
                SET regex_patterns = ?,
                    description = ?,
                    injection_template = ?,
                    example_phrases = ?
                WHERE id = ?
            ''', (
                json.dumps(combined_regex),
                best_description,
                best_template,
                json.dumps(combined_examples),
                keep_id
            ))

            # Move outcomes from removed pattern
            cursor.execute('''
                UPDATE prompt_pattern_outcomes
                SET pattern_id = ?
                WHERE pattern_id = ?
            ''', (keep_id, remove_id))

            # Deactivate removed pattern
            cursor.execute('''
                UPDATE prompt_patterns
                SET is_active = 0
                WHERE id = ?
            ''', (remove_id,))

            # Log the smart merge with details
            merge_details = {
                'merged_from': remove_id,
                'merged_to': keep_id,
                'merge_type': 'smart_merge',
                'combined_regex_count': len(combined_regex),
                'original_regex_counts': [len(regex1), len(regex2)],
                'kept_description_from': 'pattern1' if best_description == desc1 else 'pattern2',
                'kept_template_from': 'pattern1' if best_template == template1 else 'pattern2'
            }

            cursor.execute('''
                INSERT INTO prompt_pattern_evolution_log
                (event_type, pattern_id, details, timestamp)
                VALUES ('smart_merge', ?, ?, ?)
            ''', (
                str(keep_id),
                json.dumps(merge_details),
                datetime.now().isoformat()
            ))

            self.evolution_db.commit()

            return {
                'success': True,
                'kept_pattern_id': keep_id,
                'removed_pattern_id': remove_id,
                'merged_content': {
                    'regex_patterns': len(combined_regex),
                    'examples': len(combined_examples),
                    'description_source': 'pattern1' if best_description == desc1 else 'pattern2',
                    'template_source': 'pattern1' if best_template == template1 else 'pattern2'
                }
            }

        except Exception as e:
            self.evolution_db.rollback()
            return {'success': False, 'error': str(e)}

    def merge_patterns(self, keep_id: int, remove_id: int) -> bool:
        """Simple merge - keeps one pattern, deactivates other. Use smart_merge for better results."""
        cursor = self.evolution_db.cursor()

        try:
            # Move outcomes to kept pattern
            cursor.execute('''
                UPDATE prompt_pattern_outcomes
                SET pattern_id = ?
                WHERE pattern_id = ?
            ''', (keep_id, remove_id))

            # Deactivate removed pattern
            cursor.execute('''
                UPDATE prompt_patterns
                SET is_active = 0
                WHERE id = ?
            ''', (remove_id,))

            # Log the merge
            cursor.execute('''
                INSERT INTO prompt_pattern_evolution_log
                (event_type, pattern_id, details, timestamp)
                VALUES ('pattern_merged', ?, ?, ?)
            ''', (
                str(keep_id),
                json.dumps({'merged_from': remove_id, 'merged_to': keep_id}),
                datetime.now().isoformat()
            ))

            self.evolution_db.commit()
            return True
        except Exception as e:
            self.evolution_db.rollback()
            print(f"Error merging patterns: {e}")
            return False

    def generate_report(self) -> dict:
        """Generate a comprehensive deduplication report."""
        report = {
            'generated_at': datetime.now().isoformat(),
            'exact_duplicates': self.find_exact_duplicates(),
            'similar_patterns': self.find_similar_patterns(threshold=0.6),
            'category_imbalances': self.find_category_imbalances(),
            'unused_patterns': self.find_unused_patterns(),
            'summary': {}
        }

        # Calculate summary
        report['summary'] = {
            'exact_duplicate_pairs': len(report['exact_duplicates']),
            'similar_pairs_found': len(report['similar_patterns']),
            'overcrowded_categories': len(report['category_imbalances']['overcrowded']),
            'unused_patterns': len(report['unused_patterns']),
            'action_needed': (
                len(report['exact_duplicates']) > 0 or
                len([s for s in report['similar_patterns'] if s['similarity'] > 0.8]) > 0
            )
        }

        return report

    def print_report(self):
        """Print a human-readable deduplication report."""
        report = self.generate_report()

        print("\n" + "=" * 60)
        print("  CONTEXT DNA DEDUPLICATION REPORT")
        print("=" * 60)
        print(f"  Generated: {report['generated_at']}")
        print()

        # Summary
        s = report['summary']
        print("📊 SUMMARY")
        print("-" * 40)
        print(f"  Exact duplicates:      {s['exact_duplicate_pairs']}")
        print(f"  Similar patterns:      {s['similar_pairs_found']}")
        print(f"  Overcrowded categories: {s['overcrowded_categories']}")
        print(f"  Unused patterns:       {s['unused_patterns']}")

        if s['action_needed']:
            print("\n  ⚠️  ACTION NEEDED: Duplicates found!")
        else:
            print("\n  ✅ No immediate action needed")
        print()

        # Exact duplicates
        if report['exact_duplicates']:
            print("🔴 EXACT DUPLICATES (same regex patterns)")
            print("-" * 40)
            for dup in report['exact_duplicates']:
                print(f"  • {dup['pattern1']['name']}")
                print(f"    ↔ {dup['pattern2']['name']}")
                print()

        # Similar patterns
        high_similarity = [s for s in report['similar_patterns'] if s['similarity'] > 0.7]
        if high_similarity:
            print("🟡 HIGHLY SIMILAR PATTERNS (>70% match)")
            print("-" * 40)
            for sim in high_similarity[:10]:  # Top 10
                print(f"  • {sim['pattern1']['name']}")
                print(f"    ↔ {sim['pattern2']['name']}")
                print(f"    Similarity: {sim['similarity']:.0%}")
                print()

        # Category distribution
        print("📁 CATEGORY DISTRIBUTION")
        print("-" * 40)
        for cat, count in report['category_imbalances']['distribution'].items():
            bar = "█" * count
            status = " ⚠️" if count > 5 else ""
            print(f"  {cat:20} {bar} ({count}){status}")
        print()

        # Unused patterns
        if report['unused_patterns']:
            print("⚪ UNUSED PATTERNS (never triggered)")
            print("-" * 40)
            for pat in report['unused_patterns'][:5]:
                print(f"  • {pat['name']} ({pat['category']})")
            if len(report['unused_patterns']) > 5:
                print(f"  ... and {len(report['unused_patterns']) - 5} more")
        print()

        print("=" * 60)
        print("  Use 'python dedup_detector.py merge <id1> <id2>' to merge duplicates")
        print("=" * 60)


def interactive_menu():
    """Interactive deduplication menu."""
    detector = DuplicateDetector()

    while True:
        print("\n" + "=" * 60)
        print("  SMART DEDUPLICATION DETECTOR")
        print("=" * 60)
        print()
        print("  DETECTION")
        print("  1. Quick scan for duplicates")
        print("  2. Full deduplication report")
        print("  3. Find similar patterns (custom threshold)")
        print()
        print("  ANALYSIS")
        print("  4. Deep analyze two patterns")
        print("  5. Check category balance")
        print("  6. Find unused patterns")
        print()
        print("  ACTIONS")
        print("  7. Smart merge (keeps best from both)")
        print("  8. Simple merge (keeps one, removes other)")
        print()
        print("  q. Quit")
        print()

        choice = input("  Select option: ").strip().lower()

        if choice == 'q':
            break
        elif choice == '1':
            exact = detector.find_exact_duplicates()
            similar = detector.find_similar_patterns(threshold=0.8)
            print(f"\n  Found {len(exact)} exact duplicates")
            print(f"  Found {len(similar)} highly similar patterns (>80%)")
            if similar:
                print("\n  Top matches:")
                for s in similar[:3]:
                    print(f"    • {s['pattern1']['name']} ↔ {s['pattern2']['name']}")
                    print(f"      Similarity: {s['similarity']:.0%}")
        elif choice == '2':
            detector.print_report()
        elif choice == '3':
            threshold = input("  Enter similarity threshold (0.0-1.0, default 0.7): ").strip()
            threshold = float(threshold) if threshold else 0.7
            similar = detector.find_similar_patterns(threshold)
            print(f"\n  Found {len(similar)} patterns with >{threshold:.0%} similarity")
            for s in similar[:5]:
                print(f"    • {s['pattern1']['name']} ↔ {s['pattern2']['name']} ({s['similarity']:.0%})")
        elif choice == '4':
            # Deep analyze
            id1 = input("  Enter first pattern ID: ").strip()
            id2 = input("  Enter second pattern ID: ").strip()
            if id1 and id2:
                analysis = detector.analyze_patterns(int(id1), int(id2))
                if 'error' in analysis:
                    print(f"  ❌ {analysis['error']}")
                else:
                    print(f"\n  ANALYSIS: {analysis['pattern1']['name']} vs {analysis['pattern2']['name']}")
                    print("-" * 50)
                    print(f"  Overall similarity: {analysis['similarity']['overall']:.0%}")
                    print(f"    - Name similarity: {analysis['similarity']['name']:.0%}")
                    print(f"    - Description (semantic): {analysis['similarity']['description']:.0%}")
                    print(f"    - Regex overlap: {analysis['similarity']['regex_overlap']:.0%}")
                    print(f"    - Template similarity: {analysis['similarity']['template']:.0%}")
                    print()
                    print(f"  Pattern 1 unique regex: {analysis['pattern1']['unique_regex_count']}")
                    print(f"  Pattern 2 unique regex: {analysis['pattern2']['unique_regex_count']}")
                    print(f"  Shared regex: {analysis['shared_regex_count']}")
                    print()
                    if analysis['overlapping_test_phrases']:
                        print(f"  Both catch these phrases:")
                        for phrase in analysis['overlapping_test_phrases'][:3]:
                            print(f"    • \"{phrase}\"")
                    print()
                    print(f"  📋 RECOMMENDATION: {analysis['recommendation']}")
        elif choice == '5':
            imbalances = detector.find_category_imbalances()
            print("\n  Category Distribution:")
            for cat, count in imbalances['distribution'].items():
                bar = "█" * count
                print(f"    {cat:20} {bar} ({count})")
        elif choice == '6':
            unused = detector.find_unused_patterns()
            print(f"\n  Found {len(unused)} unused patterns:")
            for u in unused[:10]:
                print(f"    [{u['id']}] {u['name']} ({u['category']})")
        elif choice == '7':
            # Smart merge
            print("\n  SMART MERGE: Combines best aspects from both patterns")
            id1 = input("  Enter first pattern ID: ").strip()
            id2 = input("  Enter second pattern ID: ").strip()
            if id1 and id2:
                # First show analysis
                analysis = detector.analyze_patterns(int(id1), int(id2))
                if 'error' not in analysis:
                    print(f"\n  Merging: {analysis['pattern1']['name']} + {analysis['pattern2']['name']}")
                    print(f"  Recommendation: {analysis['recommendation']}")
                    confirm = input("\n  Proceed with smart merge? (y/n): ").strip().lower()
                    if confirm == 'y':
                        result = detector.smart_merge(int(id1), int(id2))
                        if result['success']:
                            print(f"\n  ✅ Smart merge complete!")
                            print(f"     Kept pattern ID: {result['kept_pattern_id']}")
                            print(f"     Combined {result['merged_content']['regex_patterns']} regex patterns")
                            print(f"     Combined {result['merged_content']['examples']} examples")
                        else:
                            print(f"  ❌ Merge failed: {result.get('error', 'Unknown error')}")
        elif choice == '8':
            # Simple merge
            keep = input("  Enter ID of pattern to KEEP: ").strip()
            remove = input("  Enter ID of pattern to REMOVE: ").strip()
            if keep and remove:
                if detector.merge_patterns(int(keep), int(remove)):
                    print("  ✅ Patterns merged successfully")
                else:
                    print("  ❌ Merge failed")

    detector.close()


def main():
    if len(sys.argv) < 2:
        interactive_menu()
        return

    cmd = sys.argv[1]
    detector = DuplicateDetector()

    try:
        if cmd == 'scan':
            exact = detector.find_exact_duplicates()
            similar = detector.find_similar_patterns(threshold=0.8)
            print(json.dumps({
                'exact_duplicates': len(exact),
                'high_similarity': len(similar),
                'action_needed': len(exact) > 0 or len(similar) > 0
            }, indent=2))

        elif cmd == 'report':
            detector.print_report()

        elif cmd == 'analyze' and len(sys.argv) >= 4:
            id1 = int(sys.argv[2])
            id2 = int(sys.argv[3])
            analysis = detector.analyze_patterns(id1, id2)
            print(json.dumps(analysis, indent=2))

        elif cmd == 'smart-merge' and len(sys.argv) >= 4:
            id1 = int(sys.argv[2])
            id2 = int(sys.argv[3])
            result = detector.smart_merge(id1, id2)
            if result['success']:
                print(f"✅ Smart merge successful!")
                print(f"   Kept pattern: {result['kept_pattern_id']}")
                print(f"   Removed pattern: {result['removed_pattern_id']}")
                print(f"   Combined {result['merged_content']['regex_patterns']} regex patterns")
            else:
                print(f"❌ Smart merge failed: {result.get('error', 'Unknown error')}")

        elif cmd == 'merge' and len(sys.argv) >= 4:
            keep_id = int(sys.argv[2])
            remove_id = int(sys.argv[3])
            if detector.merge_patterns(keep_id, remove_id):
                print("✅ Merge successful")
            else:
                print("❌ Merge failed")

        elif cmd == '--json':
            print(json.dumps(detector.generate_report(), indent=2))

        else:
            print(f"Unknown command: {cmd}")
            print("Usage:")
            print("  python dedup_detector.py              # Interactive menu")
            print("  python dedup_detector.py scan         # Quick scan")
            print("  python dedup_detector.py report       # Full report")
            print("  python dedup_detector.py analyze <id1> <id2>     # Deep compare")
            print("  python dedup_detector.py smart-merge <id1> <id2> # Intelligent merge")
            print("  python dedup_detector.py merge <id1> <id2>       # Simple merge")
            print("  python dedup_detector.py --json       # JSON report")

    finally:
        detector.close()


if __name__ == "__main__":
    main()
