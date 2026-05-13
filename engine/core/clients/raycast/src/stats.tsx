/**
 * Stats Command for Raycast
 */

import { Detail, showToast, Toast } from "@raycast/api";
import { useEffect, useState } from "react";
import { getStats, Stats } from "./api";

export default function Command() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchStats() {
      try {
        const data = await getStats();
        setStats(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to fetch stats");
        showToast({
          style: Toast.Style.Failure,
          title: "Failed to load stats",
          message: "Is the Context DNA server running?",
        });
      } finally {
        setIsLoading(false);
      }
    }

    fetchStats();
  }, []);

  if (error) {
    return (
      <Detail
        markdown={`# ⚠️ Server Not Running

The Context DNA server is not responding.

Start it with:
\`\`\`bash
context-dna serve
\`\`\`

Then try again.`}
      />
    );
  }

  if (!stats) {
    return <Detail isLoading={isLoading} markdown="Loading stats..." />;
  }

  const markdown = `# 🧬 Context DNA Stats

## Overview

| Metric | Value |
|--------|-------|
| **Total Learnings** | ${stats.total} |
| **Wins** | 🏆 ${stats.wins} |
| **Fixes** | 🔧 ${stats.fixes} |
| **Patterns** | 🔄 ${stats.patterns} |

## Activity

| Metric | Value |
|--------|-------|
| **Today** | ${stats.today} learnings |
| **Streak** | 🔥 ${stats.streak} days |

---

*Last updated: ${new Date(stats.last_updated).toLocaleString()}*
`;

  return <Detail isLoading={isLoading} markdown={markdown} />;
}
