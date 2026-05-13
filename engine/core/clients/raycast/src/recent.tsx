/**
 * Recent Learnings Command for Raycast
 */

import {
  Action,
  ActionPanel,
  Detail,
  List,
  showToast,
  Toast,
} from "@raycast/api";
import { useEffect, useState } from "react";
import { getRecent, Learning } from "./api";

export default function Command() {
  const [learnings, setLearnings] = useState<Learning[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    async function fetchRecent() {
      try {
        const response = await getRecent(20);
        setLearnings(response.recent);
      } catch (error) {
        showToast({
          style: Toast.Style.Failure,
          title: "Failed to load recent learnings",
          message: "Is the Context DNA server running?",
        });
      } finally {
        setIsLoading(false);
      }
    }

    fetchRecent();
  }, []);

  const getIcon = (type: string) => {
    switch (type) {
      case "win":
        return "🏆";
      case "fix":
        return "🔧";
      case "pattern":
        return "🔄";
      default:
        return "📝";
    }
  };

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return "";
    const date = new Date(dateStr);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return "just now";
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
  };

  return (
    <List isLoading={isLoading}>
      {learnings.length === 0 && !isLoading ? (
        <List.EmptyView
          title="No Learnings Yet"
          description="Record your first win with: context-dna win 'Title' 'Details'"
          icon="🌱"
        />
      ) : (
        learnings.map((learning) => (
          <List.Item
            key={learning.id}
            icon={getIcon(learning.type)}
            title={learning.title}
            subtitle={learning.content?.slice(0, 40) || ""}
            accessories={[
              { tag: learning.type },
              { text: formatDate(learning.created_at) },
            ]}
            actions={
              <ActionPanel>
                <Action.Push
                  title="View Details"
                  target={<LearningDetail learning={learning} />}
                />
                <Action.CopyToClipboard
                  title="Copy Content"
                  content={`${learning.title}\n\n${learning.content || ""}`}
                />
              </ActionPanel>
            }
          />
        ))
      )}
    </List>
  );
}

function LearningDetail({ learning }: { learning: Learning }) {
  const markdown = `# ${learning.title}

**Type:** ${learning.type}
**Tags:** ${learning.tags?.join(", ") || "none"}
${learning.created_at ? `**Created:** ${new Date(learning.created_at).toLocaleString()}` : ""}

---

${learning.content || "*No additional details*"}
`;

  return (
    <Detail
      markdown={markdown}
      actions={
        <ActionPanel>
          <Action.CopyToClipboard
            title="Copy Content"
            content={`${learning.title}\n\n${learning.content || ""}`}
          />
        </ActionPanel>
      }
    />
  );
}
