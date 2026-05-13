/**
 * Search Command for Raycast
 */

import {
  Action,
  ActionPanel,
  Detail,
  List,
  showToast,
  Toast,
} from "@raycast/api";
import { useState } from "react";
import { search, Learning } from "./api";

export default function Command() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Learning[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);

  async function handleSearch(text: string) {
    setQuery(text);

    if (!text.trim()) {
      setResults([]);
      setHasSearched(false);
      return;
    }

    setIsLoading(true);
    setHasSearched(true);

    try {
      const response = await search(text.trim());
      setResults(response.results);
    } catch (error) {
      showToast({
        style: Toast.Style.Failure,
        title: "Search failed",
        message: error instanceof Error ? error.message : "Unknown error",
      });
      setResults([]);
    } finally {
      setIsLoading(false);
    }
  }

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

  return (
    <List
      isLoading={isLoading}
      searchBarPlaceholder="Search learnings..."
      onSearchTextChange={handleSearch}
      throttle
    >
      {!hasSearched ? (
        <List.EmptyView
          title="Search Your Learnings"
          description="Type to search through your learning history"
          icon="🔍"
        />
      ) : results.length === 0 ? (
        <List.EmptyView
          title="No Results"
          description={`No learnings found for "${query}"`}
          icon="📭"
        />
      ) : (
        results.map((learning) => (
          <List.Item
            key={learning.id}
            icon={getIcon(learning.type)}
            title={learning.title}
            subtitle={learning.content?.slice(0, 50) || ""}
            accessories={[
              { tag: learning.type },
              ...(learning.score !== undefined
                ? [{ text: `${Math.round(learning.score * 100)}%` }]
                : []),
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
