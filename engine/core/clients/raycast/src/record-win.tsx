/**
 * Record Win Command for Raycast
 */

import { Action, ActionPanel, Form, showToast, Toast, popToRoot } from "@raycast/api";
import { useState } from "react";
import { recordWin } from "./api";

export default function Command() {
  const [isLoading, setIsLoading] = useState(false);

  async function handleSubmit(values: { title: string; content: string; tags: string }) {
    if (!values.title.trim()) {
      showToast({ style: Toast.Style.Failure, title: "Title is required" });
      return;
    }

    setIsLoading(true);

    try {
      const tags = values.tags
        .split(",")
        .map((t) => t.trim())
        .filter((t) => t);

      await recordWin(values.title.trim(), values.content.trim(), tags);

      showToast({
        style: Toast.Style.Success,
        title: "🏆 Win Recorded",
        message: values.title,
      });

      popToRoot();
    } catch (error) {
      showToast({
        style: Toast.Style.Failure,
        title: "Failed to record win",
        message: error instanceof Error ? error.message : "Unknown error",
      });
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <Form
      isLoading={isLoading}
      actions={
        <ActionPanel>
          <Action.SubmitForm title="Record Win" onSubmit={handleSubmit} />
        </ActionPanel>
      }
    >
      <Form.TextField
        id="title"
        title="Title"
        placeholder="What worked?"
        autoFocus
      />
      <Form.TextArea
        id="content"
        title="Details"
        placeholder="How did you do it? (optional)"
      />
      <Form.TextField
        id="tags"
        title="Tags"
        placeholder="comma, separated, tags"
      />
    </Form>
  );
}
