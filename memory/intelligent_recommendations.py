#!/usr/bin/env python3
"""
Intelligent Recommendation System - Netflix-Style Config Matching

Matches users to optimal configurations based on:
1. Hardware profile (CPU, RAM, GPU)
2. Success rate on similar hardware  
3. Performance metrics (tok/s, latency, memory)
4. Optimization score (using hardware to full potential)
5. Community signals (upvotes, verified status)

Shows ALL working options (ranked), not just one best match.

Example output:
    "Users with your hardware (M3 Max, 64GB) prefer:"
    
    #1: Qwen2.5-Coder-14B @ temp=0.7
        ✅ 98% success rate (127 users)
        ⚡ 13.5 tok/s average
        🎯 95% optimization score
        👍 47 upvotes
        
    #2: Qwen2.5-Coder-32B @ temp=0.6
        ✅ 92% success rate (43 users)
        ⚡ 7.2 tok/s average
        🎯 78% optimization score
        👍 23 upvotes
        (Slower but more intelligent)
    
    #3: Qwen3-8B @ temp=0.7
        ✅ 94% success rate (89 users)
        ⚡ 22 tok/s average
        🎯 88% optimization score
        👍 31 upvotes
        (Faster but less intelligent)

Usage:
    from memory.intelligent_recommendations import get_personalized_recommendations
    
    # Get recommendations for current hardware
    recommendations = get_personalized_recommendations()
    
    for rec in recommendations:
        print(f"#{rec.rank}: {rec.model_name}")
        print(f"  Success rate: {rec.success_rate:.0%}")
        print(f"  Performance: {rec.avg_tokens_per_sec} tok/s")
        print(f"  Optimization: {rec.optimization_score:.0%}")
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).parent


@dataclass
class PerformanceMetrics:
    """Measured performance of an LLM config on specific hardware."""
    config_id: str
    hardware_hash: str      # Profile hash (not unique device ID)
    
    # Performance
    avg_tokens_per_sec: float
    avg_first_token_ms: int
    avg_total_latency_ms: int
    avg_memory_mb: int
    
    # Reliability
    total_executions: int
    successful_executions: int
    failed_executions: int
    success_rate: float
    
    # Optimization
    optimization_score: float  # 0.0-1.0 (how well it uses hardware)
    
    # Metadata
    measured_at: str
    sample_size: int


@dataclass
class PersonalizedRecommendation:
    """A recommended config matched to user's hardware."""
    rank: int
    
    # Config details
    config_id: str
    model_name: str
    backend: str            # "vllm-mlx", "ollama", "vllm", "lmstudio"
    model_params: Dict[str, Any]
    
    # Quality metrics
    success_rate: float     # 0.0-1.0
    optimization_score: float  # 0.0-1.0
    match_score: float      # 0.0-1.0 (how well it matches user's hardware)
    
    # Performance (expected)
    avg_tokens_per_sec: float
    avg_first_token_ms: int
    avg_memory_mb: int
    
    # Community
    upvotes: int
    total_users: int        # How many users tested this
    is_verified: bool
    
    # Explanation
    why_recommended: str    # "Optimal for M3 Max with 64GB - best balance of speed/intelligence"
    tradeoffs: str          # "Faster than #2 but slightly less intelligent"


class IntelligentRecommendations:
    """Smart recommendation engine for LLM configs."""
    
    def __init__(self):
        from memory.hardware_fingerprint import get_hardware_profile
        from memory.community_templates import CommunityTemplates
        
        self.user_profile = get_hardware_profile()
        self.templates = CommunityTemplates()
    
    def get_personalized_recommendations(
        self,
        limit: int = 5,
        include_alternatives: bool = True
    ) -> List[PersonalizedRecommendation]:
        """
        Get personalized LLM config recommendations for user's hardware.
        
        Args:
            limit: Max recommendations to return
            include_alternatives: Include slower/faster alternatives
        
        Returns:
            Ranked list of recommendations (best first)
        """
        # Get all LLM config templates
        all_configs = self._get_all_llm_configs()
        
        # Filter by hardware compatibility
        compatible = [
            cfg for cfg in all_configs
            if cfg['min_ram_gb'] <= self.user_profile.ram_gb
        ]
        
        if not compatible:
            return []
        
        # Score each config
        scored = []
        for cfg in compatible:
            # Get performance metrics for similar hardware
            metrics = self._get_metrics_for_hardware(
                cfg['config_id'],
                self.user_profile.profile_hash
            )
            
            # Calculate match score
            match_score = self._calculate_match_score(cfg, metrics)
            
            # Build recommendation
            rec = PersonalizedRecommendation(
                rank=0,  # Will be set after sorting
                config_id=cfg['config_id'],
                model_name=cfg['model_name'],
                backend=cfg['backend'],
                model_params=cfg['model_params'],
                success_rate=metrics.get('success_rate', cfg.get('success_rate', 0.5)),
                optimization_score=metrics.get('optimization_score', cfg.get('optimization_score', 0.5)),
                match_score=match_score,
                avg_tokens_per_sec=metrics.get('avg_tokens_per_sec', 0),
                avg_first_token_ms=metrics.get('avg_first_token_ms', 0),
                avg_memory_mb=metrics.get('avg_memory_mb', 0),
                upvotes=cfg.get('upvotes', 0),
                total_users=metrics.get('total_users', 1),
                is_verified=cfg.get('is_verified', False),
                why_recommended=self._generate_explanation(cfg, metrics),
                tradeoffs=self._generate_tradeoffs(cfg, all_configs)
            )
            
            scored.append(rec)
        
        # Sort by match score
        scored.sort(key=lambda x: x.match_score, reverse=True)
        
        # Assign ranks
        for i, rec in enumerate(scored[:limit], 1):
            rec.rank = i
        
        return scored[:limit]
    
    def _get_all_llm_configs(self) -> List[Dict[str, Any]]:
        """Get all LLM configs from community templates."""
        # TODO: Query community_templates.db for LLM configs
        # For now, return known configs
        
        return [
            {
                'config_id': 'qwen25-14b-mlx',
                'model_name': 'Qwen2.5-Coder-14B-Instruct-4bit',
                'backend': 'vllm-mlx',
                'model_params': {
                    'max_tokens': 4096,
                    'temperature': 0.7,
                    'enable_auto_tool_choice': True,
                    'tool_call_parser': 'hermes'
                },
                'min_ram_gb': 16,
                'min_vram_gb': None,
                'recommended_ram_gb': 32,
                'hardware_type': 'apple_silicon',
                'success_rate': 0.98,
                'optimization_score': 0.95,
                'upvotes': 47,
                'is_verified': True
            },
            {
                'config_id': 'qwen25-7b-mlx',
                'model_name': 'Qwen2.5-Coder-7B-Instruct-4bit',
                'backend': 'vllm-mlx',
                'model_params': {
                    'max_tokens': 2048,
                    'temperature': 0.7
                },
                'min_ram_gb': 8,
                'recommended_ram_gb': 16,
                'hardware_type': 'apple_silicon',
                'success_rate': 0.94,
                'optimization_score': 0.90,
                'upvotes': 31,
                'is_verified': True
            }
        ]
    
    def _get_metrics_for_hardware(
        self,
        config_id: str,
        hardware_hash: str
    ) -> Dict[str, Any]:
        """Get performance metrics for this config on similar hardware."""
        # TODO: Query performance_metrics table
        # For now, return estimated metrics
        return {
            'success_rate': 0.95,
            'optimization_score': 0.92,
            'avg_tokens_per_sec': 13.5,
            'avg_first_token_ms': 1000,
            'avg_memory_mb': 950,
            'total_users': 27
        }
    
    def _calculate_match_score(
        self,
        config: Dict[str, Any],
        metrics: Dict[str, Any]
    ) -> float:
        """Calculate how well this config matches user's hardware."""
        score = 0.0
        
        # Factor 1: Hardware compatibility (30%)
        if config.get('hardware_type') == self.user_profile.cpu_type:
            score += 0.30
        elif config.get('hardware_type') == 'any':
            score += 0.15
        
        # Factor 2: Success rate (30%)
        score += metrics.get('success_rate', 0.5) * 0.30
        
        # Factor 3: Optimization score (25%)
        score += metrics.get('optimization_score', 0.5) * 0.25
        
        # Factor 4: Community signals (10%)
        upvotes = config.get('upvotes', 0)
        community_score = min(upvotes / 50.0, 1.0)  # Normalize to 1.0
        score += community_score * 0.10
        
        # Factor 5: Verification bonus (5%)
        if config.get('is_verified'):
            score += 0.05
        
        return round(score, 3)
    
    def _generate_explanation(
        self,
        config: Dict[str, Any],
        metrics: Dict[str, Any]
    ) -> str:
        """Generate human-readable explanation of why this is recommended."""
        tier = self.user_profile.performance_tier
        ram = self.user_profile.ram_gb
        cpu = self.user_profile.cpu_type
        
        # Build explanation
        parts = []
        
        # Hardware match
        parts.append(f"Optimal for {cpu} with {ram}GB RAM")
        
        # Performance highlight
        tok_s = metrics.get('avg_tokens_per_sec', 0)
        if tok_s > 15:
            parts.append("very fast")
        elif tok_s > 10:
            parts.append("fast")
        elif tok_s > 5:
            parts.append("moderate speed")
        
        # Quality
        opt_score = metrics.get('optimization_score', 0)
        if opt_score > 0.9:
            parts.append("highly optimized")
        elif opt_score > 0.75:
            parts.append("well optimized")
        
        # Community validation
        total_users = metrics.get('total_users', 0)
        if total_users > 50:
            parts.append(f"proven by {total_users} users")
        elif total_users > 10:
            parts.append(f"tested by {total_users} users")
        
        return " - ".join(parts)
    
    def _generate_tradeoffs(
        self,
        config: Dict[str, Any],
        all_configs: List[Dict[str, Any]]
    ) -> str:
        """Generate tradeoff explanation vs other options."""
        # Find faster alternative
        # Find more intelligent alternative
        # Compare
        
        return "Best balance of speed and intelligence for your hardware"


def get_personalized_recommendations(limit: int = 5) -> List[PersonalizedRecommendation]:
    """Convenience function - get recommendations for current hardware."""
    engine = IntelligentRecommendations()
    return engine.get_personalized_recommendations(limit=limit)


if __name__ == "__main__":
    print("🎯 Intelligent LLM Recommendations (Personalized)\n")
    
    recommendations = get_personalized_recommendations(limit=3)
    
    if not recommendations:
        print("❌ No compatible configurations found for your hardware")
    else:
        print(f"Based on your hardware profile, here are the top {len(recommendations)} configs:\n")
        
        for rec in recommendations:
            print(f"{'='*70}")
            print(f"#{rec.rank}: {rec.model_name}")
            print(f"{'='*70}")
            print(f"Backend: {rec.backend}")
            print(f"\n📊 Quality Metrics:")
            print(f"  ✅ Success rate: {rec.success_rate:.0%} ({rec.total_users} users)")
            print(f"  🎯 Optimization: {rec.optimization_score:.0%}")
            print(f"  🏆 Match score: {rec.match_score:.0%}")
            
            if rec.is_verified:
                print(f"  ✓ Verified by maintainers")
            
            if rec.upvotes > 0:
                print(f"  👍 {rec.upvotes} community upvotes")
            
            print(f"\n⚡ Expected Performance:")
            print(f"  • Speed: {rec.avg_tokens_per_sec:.1f} tokens/second")
            print(f"  • Latency: {rec.avg_first_token_ms}ms first token")
            print(f"  • Memory: {rec.avg_memory_mb}MB")
            
            print(f"\n💡 Why recommended:")
            print(f"  {rec.why_recommended}")
            
            if rec.tradeoffs:
                print(f"\n⚖️  Tradeoffs:")
                print(f"  {rec.tradeoffs}")
            
            print()
