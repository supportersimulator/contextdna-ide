#!/usr/bin/env python3
"""
Quick Wins Prioritizer - Impact × Speed Matrix

Identifies and prioritizes changes that:
1. Make biggest difference for ENTIRE ecosystem
2. Can be implemented quickly
3. Have been tested and verified

NOT todo lists - TESTED quick wins with proven impact.

Impact Factors:
- Affects all users (not just one IDE)
- Improves multiple systems (not just one feature)
- Unlocks other capabilities (enabler, not endpoint)
- Measurable improvement (can verify it worked)

Speed Factors:
- Implementation time <30 minutes
- Testing time <10 minutes
- Low risk (easy rollback)
- Existing infrastructure (build on what works)

Usage:
    from memory.quick_wins_prioritizer import get_quick_wins
    
    wins = get_quick_wins(limit=5)
    
    for win in wins:
        print(f"{win.title}: {win.impact_score} impact, {win.time_estimate}min")
"""

from dataclasses import dataclass
from typing import List


@dataclass
class QuickWin:
    """A high-impact, low-effort improvement."""
    title: str
    impact_score: float       # 0.0-1.0 (ecosystem-wide impact)
    time_estimate: int        # Minutes to implement + test
    affects: List[str]        # Which systems benefit
    unlocks: List[str]        # What this enables
    测试_status: str         # "untested", "tested", "verified"
    risk_level: str           # "low", "medium", "high"


def get_current_quick_wins() -> List[QuickWin]:
    """
    Get current quick wins (identified, not yet implemented).
    
    MUST BE TESTED before claiming complete!
    """
    return [
        QuickWin(
            title="Test & verify logging fix end-to-end",
            impact_score=0.95,
            time_estimate=10,
            affects=["webhook_quality", "section_2", "section_8", "all_users"],
            unlocks=["LLM_content", "synaptic_voice", "professor_wisdom"],
            测试_status="partial",  # Fix done, full e2e test needed
            risk_level="low"
        ),
        QuickWin(
            title="Integrate anticipatory butler into webhook generation",
            impact_score=0.85,
            time_estimate=20,
            affects=["webhook_speed", "context_relevance", "all_users"],
            unlocks=["predictive_context", "redis_pre_loading"],
            测试_status="untested",
            risk_level="low"
        ),
        QuickWin(
            title="Add big picture reminder to Section 4 (when helpful)",
            impact_score=0.70,
            time_estimate=15,
            affects=["strategic_alignment", "all_users"],
            unlocks=["roadmap_awareness", "prevents_drift"],
            测试_status="untested",
            risk_level="low"
        ),
        QuickWin(
            title="Fix PostgreSQL schema (add learning_type column)",
            impact_score=0.80,
            time_estimate=5,
            affects=["foundation_sops", "search_relevance", "all_users"],
            unlocks=["better_sop_matching", "50%→80%_relevance"],
            测试_status="untested",
            risk_level="medium"
        ),
        QuickWin(
            title="Test Cursor hooks in live session",
            impact_score=0.90,
            time_estimate=5,
            affects=["cursor_users", "hook_reliability"],
            unlocks=["cursor_production_ready", "multi_ide_verified"],
            测试_status="untested",
            risk_level="low"
        ),
        QuickWin(
            title="Start anticipatory butler as background service",
            impact_score=0.75,
            time_estimate=10,
            affects=["context_speed", "redis_utilization"],
            unlocks=["real_time_anticipation", "predictive_pre_loading"],
            测试_status="untested",
            risk_level="low"
        )
    ]


def rank_by_quick_win_score(wins: List[QuickWin]) -> List[QuickWin]:
    """
    Rank wins by: (impact / time) × (1 if tested else 0.5)
    
    Prioritizes:
    - High impact
    - Low time
    - Already tested (proven)
    """
    def score(win):
        base_score = win.impact_score / (win.time_estimate / 60)  # Impact per hour
        
        # Tested items get priority
        test_multiplier = {
            "verified": 1.5,
            "tested": 1.0,
            "partial": 0.7,
            "untested": 0.5
        }.get(win.测试_status, 0.5)
        
        # Low risk gets priority
        risk_multiplier = {
            "low": 1.0,
            "medium": 0.8,
            "high": 0.6
        }.get(win.risk_level, 0.8)
        
        return base_score * test_multiplier * risk_multiplier
    
    return sorted(wins, key=score, reverse=True)


if __name__ == "__main__":
    print("⚡ Quick Wins Prioritizer - Ecosystem Impact\n")
    
    wins = get_current_quick_wins()
    ranked = rank_by_quick_win_score(wins)
    
    print("Ranked by: (Impact ÷ Time) × Testing × Risk")
    print("=" * 70)
    print()
    
    for i, win in enumerate(ranked, 1):
        test_icon = {"verified": "✅", "tested": "🧪", "partial": "⚠️", "untested": "❓"}.get(win.测试_status, "❓")
        
        print(f"#{i}: {win.title}")
        print(f"    Impact: {win.impact_score:.0%} | Time: {win.time_estimate}min | {test_icon} {win.测试_status}")
        print(f"    Affects: {', '.join(win.affects)}")
        print(f"    Unlocks: {', '.join(win.unlocks)}")
        print()
